#!/usr/bin/env node
/**
 * ScreenWeave MCP Server
 * ======================
 * Exposes Playwright crawl, screenshot retrieval, and page metrics as MCP tools
 * so that any AI agent (Claude, Cursor, etc.) can invoke them.
 *
 * Transport: stdio  (add to Claude Desktop / Cursor via config — see README)
 *
 * Required environment variables:
 *   SCREENWEAVE_API_URL   Base URL of the deployed API Gateway stage
 *                         e.g. https://abc123.execute-api.us-east-1.amazonaws.com/prod
 *   SCREENWEAVE_API_KEY   Value of the API key (x-api-key header)
 *
 * Tools exposed:
 *   crawl_url           – Start a new Playwright crawl for a given URL
 *   get_session_status  – Check whether a crawl has completed
 *   get_screenshots     – Get signed screenshot URLs for every captured state
 *   get_metrics         – Get computed page metrics (states, transitions, coverage)
 *   get_full_session    – Get the full artifact bundle (screenshots + states + transitions)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

// ── Config ────────────────────────────────────────────────────────────────────
const API_URL = (process.env.SCREENWEAVE_API_URL || '').replace(/\/$/, '');
const API_KEY = process.env.SCREENWEAVE_API_KEY || '';

if (!API_URL || !API_KEY) {
  process.stderr.write(
    '[screenweave-mcp] ERROR: SCREENWEAVE_API_URL and SCREENWEAVE_API_KEY must be set.\n'
  );
  process.exit(1);
}

// ── API helpers ───────────────────────────────────────────────────────────────

/** POST JSON to the API Gateway. */
async function apiPost(path, body) {
  const res = await fetch(`${API_URL}${path}`, {
    method: 'POST',
    headers: { 'x-api-key': API_KEY, 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

/** GET from the API Gateway with optional query params. */
async function apiGet(path, params = {}) {
  const qs = new URLSearchParams(params).toString();
  const url = `${API_URL}${path}${qs ? `?${qs}` : ''}`;
  const res = await fetch(url, { headers: { 'x-api-key': API_KEY } });
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

/**
 * Fetch states.json from the signed S3 URL returned by the retrieval API.
 * Used by get_metrics to compute derived metrics without needing a dedicated endpoint.
 */
async function fetchStatesJson(signedUrl) {
  if (!signedUrl) throw new Error('states_json URL is not available for this session');
  const res = await fetch(signedUrl);
  if (!res.ok) throw new Error(`Failed to fetch states.json: HTTP ${res.status}`);
  return res.json();
}

/** Build a uniform tool error response. */
function toolError(err) {
  return {
    content: [{ type: 'text', text: `Error: ${err.message}` }],
    isError: true,
  };
}

/** Build a uniform tool success response. */
function toolOk(data) {
  return {
    content: [{ type: 'text', text: typeof data === 'string' ? data : JSON.stringify(data, null, 2) }],
  };
}

// ── MCP server ────────────────────────────────────────────────────────────────
const server = new McpServer({
  name: 'screenweave',
  version: '1.0.0',
});

// ─────────────────────────────────────────────────────────────────────────────
// Tool 1: crawl_url
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'crawl_url',
  `Start a Playwright crawl for the given URL. The crawler visits the page, scrolls to
reveal lazy-loaded content, clicks every interactive element (tabs, accordions, buttons),
and recursively follows internal links up to max_depth. For each distinct visual state
it captures a full-page screenshot and structured metadata (headings, links, visible text).

Returns a session_id immediately. The crawl runs asynchronously on a cloud worker
(EC2/Fargate). Use get_session_status to poll for completion, then get_screenshots or
get_metrics to retrieve results.

Typical completion time: 3–10 minutes depending on site complexity.`,
  {
    url: z.string().url().describe('The URL to crawl (must be https://)'),
    max_depth: z
      .number()
      .int()
      .min(0)
      .max(3)
      .default(2)
      .describe('How many link hops to follow from the root URL (0 = root page only)'),
    max_links: z
      .number()
      .int()
      .min(1)
      .max(30)
      .default(12)
      .describe('Max child links to follow per page'),
  },
  async ({ url, max_depth, max_links }) => {
    try {
      const result = await apiPost('/session', { url, max_depth, max_links });
      return toolOk({
        session_id: result.session_id,
        status: result.status,
        message: `Crawl started. Poll get_session_status(session_id="${result.session_id}") until status is COMPLETED, then call get_screenshots or get_metrics.`,
        target_url: url,
      });
    } catch (err) {
      return toolError(err);
    }
  }
);

// ─────────────────────────────────────────────────────────────────────────────
// Tool 2: get_session_status
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'get_session_status',
  `Check the status of a crawl session. Returns RUNNING, COMPLETED, or FAILED.
Poll every 30–60 seconds after calling crawl_url. Once COMPLETED, call
get_screenshots or get_metrics.`,
  {
    session_id: z.string().describe('Session ID returned by crawl_url'),
  },
  async ({ session_id }) => {
    try {
      // Retrieve only the lightweight metadata (no S3 listing needed)
      const result = await apiGet(`/session/${session_id}`, { include: 'none' });
      return toolOk({
        session_id: result.session_id,
        status: result.status,
        created_at: result.metadata?.created_at,
        updated_at: result.metadata?.updated_at,
        summary_stats: result.metadata?.summary_stats ?? null,
      });
    } catch (err) {
      return toolError(err);
    }
  }
);

// ─────────────────────────────────────────────────────────────────────────────
// Tool 3: get_screenshots
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'get_screenshots',
  `Get pre-signed HTTPS URLs for every screenshot captured during a crawl session.
Each entry maps a state_id to the URL and page context that triggered it.
URLs expire after 1 hour. Call this after get_session_status returns COMPLETED.`,
  {
    session_id: z.string().describe('Session ID returned by crawl_url'),
  },
  async ({ session_id }) => {
    try {
      const result = await apiGet(`/session/${session_id}`, { include: 'screenshots' });

      if (!result.artifacts?.screenshots?.length) {
        return toolOk({ message: 'No screenshots found. Check get_session_status first.', session_id });
      }

      // Enrich with state metadata from indexed_states
      const stateIndex = Object.fromEntries(
        (result.indexed_states || []).map((s) => [s.state_id, s])
      );

      const screenshots = result.artifacts.screenshots.map((shot) => {
        const meta = stateIndex[shot.state_id] || {};
        return {
          state_id: shot.state_id,
          https_url: shot.https_url,
          page_url: meta.url || '',
          timestamp: meta.timestamp || '',
          s3_uri: shot.s3_url,
        };
      });

      return toolOk({
        session_id,
        status: result.status,
        total_screenshots: screenshots.length,
        screenshots,
      });
    } catch (err) {
      return toolError(err);
    }
  }
);

// ─────────────────────────────────────────────────────────────────────────────
// Tool 4: get_metrics
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'get_metrics',
  `Get computed page metrics for a completed crawl session. Fetches states.json from S3,
then derives:
  - Session summary (total states, unique pages, duration)
  - State coverage by action type (navigation / scroll / click)
  - Content analysis (heading distribution, links found, interactive elements)
  - Per-state summary list for quick overview

Use this when you need to understand the site structure or validate QA coverage.`,
  {
    session_id: z.string().describe('Session ID returned by crawl_url'),
  },
  async ({ session_id }) => {
    try {
      // Get the states_json signed URL from the retrieval API
      const apiResult = await apiGet(`/session/${session_id}`, { include: 'states' });

      if (apiResult.status !== 'COMPLETED') {
        return toolOk({
          session_id,
          status: apiResult.status,
          message: `Crawl is ${apiResult.status}. Metrics are only available after COMPLETED.`,
        });
      }

      if (!apiResult.artifacts?.states_json) {
        return toolOk({ session_id, message: 'states.json not available for this session.' });
      }

      // Fetch and parse states.json from S3
      const statesData = await fetchStatesJson(apiResult.artifacts.states_json);
      const states = statesData.states || [];

      if (!states.length) {
        return toolOk({ session_id, message: 'No states recorded in this session.' });
      }

      // ── Compute metrics ────────────────────────────────────────────────────
      const timestamps = states.map((s) => s.timestamp).filter(Boolean).sort();
      const firstTs = timestamps[0] || null;
      const lastTs  = timestamps[timestamps.length - 1] || null;
      const durationSec = firstTs && lastTs
        ? Math.round((new Date(lastTs) - new Date(firstTs)) / 1000)
        : null;

      const uniqueUrls = [...new Set(states.map((s) => s.url))];

      // States by action type
      const byAction = {};
      for (const s of states) {
        const a = s.trigger_action || 'unknown';
        byAction[a] = (byAction[a] || 0) + 1;
      }

      // Heading distribution
      const headingCounts = { h1: 0, h2: 0, h3: 0 };
      for (const s of states) {
        for (const h of s.headings || []) {
          if (h.tag in headingCounts) headingCounts[h.tag]++;
        }
      }

      // Unique interactive element labels
      const allInteractive = new Set();
      for (const s of states) {
        for (const el of s.interactive_elements || []) {
          if (el) allInteractive.add(el.trim());
        }
      }

      // Total unique links found across all states
      const allLinks = new Set();
      for (const s of states) {
        for (const l of s.links_found || []) allLinks.add(l);
      }

      // Average document height
      const heights = states.map((s) => s.document_height || 0).filter((h) => h > 0);
      const avgHeight = heights.length
        ? Math.round(heights.reduce((a, b) => a + b, 0) / heights.length)
        : 0;

      // Per-state summary (compact, for agent overview)
      const statesSummary = states.map((s) => ({
        state_id:       s.state_id,
        url:            s.url,
        title:          s.title,
        trigger_action: s.trigger_action,
        trigger_label:  s.trigger_label,
        timestamp:      s.timestamp,
        headings_count: (s.headings || []).length,
        links_count:    (s.links_found || []).length,
      }));

      return toolOk({
        session_id,
        status:   apiResult.status,
        base_url: statesData.base_url,
        summary: {
          total_states:             states.length,
          unique_pages_visited:     uniqueUrls.length,
          session_duration_seconds: durationSec,
          first_captured_at:        firstTs,
          last_captured_at:         lastTs,
        },
        coverage: {
          unique_urls:     uniqueUrls,
          states_by_action: byAction,
        },
        content: {
          heading_distribution:          headingCounts,
          total_unique_links_found:      allLinks.size,
          total_unique_interactive_elements: allInteractive.size,
          interactive_element_labels:    [...allInteractive].slice(0, 30),
          avg_document_height_px:        avgHeight,
        },
        states_summary: statesSummary,
      });
    } catch (err) {
      return toolError(err);
    }
  }
);

// ─────────────────────────────────────────────────────────────────────────────
// Tool 5: get_full_session
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'get_full_session',
  `Get the complete artifact bundle for a crawl session: screenshots, states metadata,
transitions graph, and trace file. Use the include parameter to request only what you
need and reduce response size.

All URLs are pre-signed S3 links that expire in 1 hour.`,
  {
    session_id: z.string().describe('Session ID returned by crawl_url'),
    include: z
      .array(z.enum(['screenshots', 'states', 'transitions', 'trace']))
      .default(['screenshots', 'states', 'transitions', 'trace'])
      .describe('Which artifact types to include in the response'),
  },
  async ({ session_id, include }) => {
    try {
      const result = await apiGet(`/session/${session_id}`, {
        include: include.join(','),
      });
      return toolOk(result);
    } catch (err) {
      return toolError(err);
    }
  }
);

// ── Start server ──────────────────────────────────────────────────────────────
const transport = new StdioServerTransport();
await server.connect(transport);
process.stderr.write('[screenweave-mcp] Server running (stdio)\n');
