#!/usr/bin/env node
/**
 * ScreenWeave MCP Server
 * ======================
 * Exposes Playwright crawl, screenshot retrieval, and page metrics as MCP tools.
 * Communicates with AWS Lambda DIRECTLY via the AWS SDK (IAM auth) — API Gateway
 * is NOT in the data path. API Gateway is only the MCP protocol endpoint for
 * agents that cannot run this server locally.
 *
 * Transport: stdio
 *
 * Required environment variables:
 *   AWS_REGION               AWS region where Lambdas are deployed
 *   START_CRAWL_FUNCTION     Lambda function name for startCrawl
 *   GET_SESSION_FUNCTION     Lambda function name for getSession
 *
 * AWS credentials are resolved via the standard SDK chain:
 *   1. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars
 *   2. ~/.aws/credentials profile
 *   3. EC2/ECS/Lambda IAM role (if running on AWS)
 *
 * The IAM principal running this server needs:
 *   lambda:InvokeFunction on START_CRAWL_FUNCTION and GET_SESSION_FUNCTION
 *
 * MCP client config (claude_desktop_config.json / .cursor/mcp.json):
 * {
 *   "mcpServers": {
 *     "screenweave": {
 *       "command": "node",
 *       "args": ["/path/to/src/mcp-server/index.js"],
 *       "env": {
 *         "AWS_REGION": "us-east-1",
 *         "START_CRAWL_FUNCTION": "screenweave-start-crawl-prod",
 *         "GET_SESSION_FUNCTION": "screenweave-get-session-prod"
 *       }
 *     }
 *   }
 * }
 */

import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

// ── Config ────────────────────────────────────────────────────────────────────
const REGION               = process.env.AWS_REGION || 'us-east-1';
const START_CRAWL_FUNCTION = process.env.START_CRAWL_FUNCTION;
const GET_SESSION_FUNCTION = process.env.GET_SESSION_FUNCTION;

if (!START_CRAWL_FUNCTION || !GET_SESSION_FUNCTION) {
  process.stderr.write(
    '[screenweave-mcp] ERROR: START_CRAWL_FUNCTION and GET_SESSION_FUNCTION must be set.\n'
  );
  process.exit(1);
}

// ── Lambda client (reused across tool calls) ──────────────────────────────────
const lambda = new LambdaClient({ region: REGION });

// ── Lambda invocation helpers ─────────────────────────────────────────────────

/**
 * Invoke a Lambda function synchronously and return its parsed payload.
 * The Lambdas are dual-mode: when called directly (not via API Gateway) they
 * accept a plain JSON payload and return plain JSON (not the APIGateway proxy
 * envelope). See each Lambda's handler for the dual-mode detection logic.
 *
 * Throws if the function returns a FunctionError (unhandled exception in Lambda).
 */
async function invokeLambda(functionName, payload) {
  const result = await lambda.send(
    new InvokeCommand({
      FunctionName:   functionName,
      InvocationType: 'RequestResponse',
      Payload:        JSON.stringify(payload),
    })
  );

  const responseText = new TextDecoder().decode(result.Payload);
  const response     = JSON.parse(responseText);

  if (result.FunctionError) {
    // Lambda threw an unhandled exception
    const msg = response.errorMessage || response.message || 'Lambda invocation failed';
    throw new Error(`${functionName}: ${msg}`);
  }

  // Lambdas signal application-level errors via { error: "..." }
  if (response && response.error) {
    throw new Error(response.error);
  }

  return response;
}

/**
 * Fetch states.json from a pre-signed S3 URL returned by getSession.
 * This is an S3 call, NOT an API Gateway call — the signed URL bypasses
 * API Gateway entirely.
 */
async function fetchStatesJson(signedUrl) {
  if (!signedUrl) throw new Error('states_json signed URL is not available for this session');
  const res = await fetch(signedUrl);
  if (!res.ok) throw new Error(`Failed to fetch states.json: HTTP ${res.status}`);
  return res.json();
}

// ── Uniform tool response helpers ─────────────────────────────────────────────
function toolOk(data) {
  return {
    content: [{ type: 'text', text: typeof data === 'string' ? data : JSON.stringify(data, null, 2) }],
  };
}

function toolError(err) {
  return {
    content: [{ type: 'text', text: `Error: ${err.message}` }],
    isError: true,
  };
}

// ── MCP server ────────────────────────────────────────────────────────────────
const server = new McpServer({ name: 'screenweave', version: '1.0.0' });

// ─────────────────────────────────────────────────────────────────────────────
// Tool 1: crawl_url
// Directly invokes the startCrawl Lambda.
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'crawl_url',
  `Start a Playwright crawl for the given URL. The crawler visits the page, scrolls to
reveal lazy-loaded content, clicks every interactive element (tabs, accordions, buttons),
and follows internal links up to max_depth. For each distinct visual state it captures a
full-page screenshot and structured metadata (headings, links, visible text).

Invokes the startCrawl Lambda directly via IAM — no API Gateway in the path.
Returns a session_id immediately; crawl runs asynchronously on EC2.
Poll get_session_status until COMPLETED, then call get_screenshots or get_metrics.`,
  {
    url: z.string().url().describe('The URL to crawl (must be http/https)'),
    max_depth: z.number().int().min(0).max(3).default(2)
      .describe('Link recursion depth (0 = root page only)'),
    max_links: z.number().int().min(1).max(30).default(12)
      .describe('Max child links to follow per page'),
  },
  async ({ url, max_depth, max_links }) => {
    try {
      // Direct Lambda invocation — plain payload, no API Gateway envelope
      const result = await invokeLambda(START_CRAWL_FUNCTION, { url, max_depth, max_links });
      return toolOk({
        session_id: result.session_id,
        status:     result.status,
        target_url: url,
        message:    `Crawl started. Poll get_session_status(session_id="${result.session_id}") until COMPLETED.`,
      });
    } catch (err) {
      return toolError(err);
    }
  }
);

// ─────────────────────────────────────────────────────────────────────────────
// Tool 2: get_session_status
// Directly invokes the getSession Lambda (metadata only, no S3 listing).
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'get_session_status',
  `Check the status of a crawl session (RUNNING, COMPLETED, FAILED).
Invokes the getSession Lambda directly via IAM.
Poll every 30–60 seconds after crawl_url. Once COMPLETED, call get_screenshots or get_metrics.`,
  {
    session_id: z.string().describe('Session ID returned by crawl_url'),
  },
  async ({ session_id }) => {
    try {
      // include=none → DynamoDB lookup only, no S3 listing
      const result = await invokeLambda(GET_SESSION_FUNCTION, {
        session_id,
        include: 'none',
      });
      return toolOk({
        session_id:    result.session_id,
        status:        result.status,
        created_at:    result.metadata?.created_at  ?? null,
        updated_at:    result.metadata?.updated_at  ?? null,
        summary_stats: result.metadata?.summary_stats ?? null,
      });
    } catch (err) {
      return toolError(err);
    }
  }
);

// ─────────────────────────────────────────────────────────────────────────────
// Tool 3: get_screenshots
// Directly invokes getSession Lambda with include=screenshots.
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'get_screenshots',
  `Get pre-signed HTTPS URLs for every screenshot captured during a crawl session.
Invokes the getSession Lambda directly via IAM — URLs come from S3, not API Gateway.
Each entry maps a state_id to its signed URL and the page context that triggered it.
URLs expire after 1 hour. Call after get_session_status returns COMPLETED.`,
  {
    session_id: z.string().describe('Session ID returned by crawl_url'),
  },
  async ({ session_id }) => {
    try {
      const result = await invokeLambda(GET_SESSION_FUNCTION, {
        session_id,
        include: 'screenshots',
      });

      if (!result.artifacts?.screenshots?.length) {
        return toolOk({ message: 'No screenshots yet. Check get_session_status first.', session_id });
      }

      // Enrich screenshot entries with state metadata from indexed_states
      const stateIndex = Object.fromEntries(
        (result.indexed_states || []).map((s) => [s.state_id, s])
      );

      const screenshots = result.artifacts.screenshots.map((shot) => {
        const meta = stateIndex[shot.state_id] || {};
        return {
          state_id:  shot.state_id,
          https_url: shot.https_url,
          page_url:  meta.url       || '',
          timestamp: meta.timestamp || '',
          s3_uri:    shot.s3_url,
        };
      });

      return toolOk({
        session_id,
        status:            result.status,
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
// Invokes getSession Lambda to get the states.json signed URL, then fetches
// states.json directly from S3 (signed URL) and computes metrics client-side.
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'get_metrics',
  `Get computed page metrics for a completed crawl session.
Invokes the getSession Lambda directly to get the states.json pre-signed S3 URL,
then fetches and parses states.json from S3 (direct, not via API Gateway).

Computed metrics:
  - Session summary: total states, unique pages visited, duration
  - Coverage: states by action type (navigation / scroll / click)
  - Content: heading distribution, unique links, interactive elements
  - Per-state table for QA review`,
  {
    session_id: z.string().describe('Session ID returned by crawl_url'),
  },
  async ({ session_id }) => {
    try {
      // Step 1: get the states_json signed URL via direct Lambda invocation
      const apiResult = await invokeLambda(GET_SESSION_FUNCTION, {
        session_id,
        include: 'states',
      });

      if (apiResult.status !== 'COMPLETED') {
        return toolOk({
          session_id,
          status:  apiResult.status,
          message: `Crawl is ${apiResult.status}. Metrics available after COMPLETED.`,
        });
      }

      if (!apiResult.artifacts?.states_json) {
        return toolOk({ session_id, message: 'states.json not available for this session.' });
      }

      // Step 2: fetch states.json directly from S3 via the pre-signed URL
      const statesData = await fetchStatesJson(apiResult.artifacts.states_json);
      const states = statesData.states || [];

      if (!states.length) {
        return toolOk({ session_id, message: 'No states recorded in this session.' });
      }

      // ── Compute metrics ────────────────────────────────────────────────────
      const timestamps = states.map((s) => s.timestamp).filter(Boolean).sort();
      const firstTs    = timestamps[0] || null;
      const lastTs     = timestamps[timestamps.length - 1] || null;
      const durationSec = firstTs && lastTs
        ? Math.round((new Date(lastTs) - new Date(firstTs)) / 1000)
        : null;

      const uniqueUrls = [...new Set(states.map((s) => s.url))];

      const byAction = {};
      for (const s of states) {
        const a = s.trigger_action || 'unknown';
        byAction[a] = (byAction[a] || 0) + 1;
      }

      const headingCounts = { h1: 0, h2: 0, h3: 0 };
      for (const s of states) {
        for (const h of s.headings || []) {
          if (h.tag in headingCounts) headingCounts[h.tag]++;
        }
      }

      const allInteractive = new Set();
      for (const s of states) {
        for (const el of s.interactive_elements || []) {
          if (el) allInteractive.add(el.trim());
        }
      }

      const allLinks = new Set();
      for (const s of states) {
        for (const l of s.links_found || []) allLinks.add(l);
      }

      const heights = states.map((s) => s.document_height || 0).filter((h) => h > 0);
      const avgHeight = heights.length
        ? Math.round(heights.reduce((a, b) => a + b, 0) / heights.length)
        : 0;

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
          unique_urls:      uniqueUrls,
          states_by_action: byAction,
        },
        content: {
          heading_distribution:               headingCounts,
          total_unique_links_found:           allLinks.size,
          total_unique_interactive_elements:  allInteractive.size,
          interactive_element_labels:         [...allInteractive].slice(0, 30),
          avg_document_height_px:             avgHeight,
        },
        states_summary: states.map((s) => ({
          state_id:       s.state_id,
          url:            s.url,
          title:          s.title,
          trigger_action: s.trigger_action,
          trigger_label:  s.trigger_label,
          timestamp:      s.timestamp,
          headings_count: (s.headings || []).length,
          links_count:    (s.links_found || []).length,
        })),
      });
    } catch (err) {
      return toolError(err);
    }
  }
);

// ─────────────────────────────────────────────────────────────────────────────
// Tool 5: get_full_session
// Directly invokes getSession Lambda with the requested include set.
// ─────────────────────────────────────────────────────────────────────────────
server.tool(
  'get_full_session',
  `Get the complete artifact bundle for a crawl session.
Invokes the getSession Lambda directly via IAM — all signed URLs come from S3.
Use the include parameter to request only what you need.`,
  {
    session_id: z.string().describe('Session ID returned by crawl_url'),
    include: z
      .array(z.enum(['screenshots', 'states', 'transitions', 'trace']))
      .default(['screenshots', 'states', 'transitions', 'trace'])
      .describe('Artifact types to include'),
  },
  async ({ session_id, include }) => {
    try {
      const result = await invokeLambda(GET_SESSION_FUNCTION, {
        session_id,
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
process.stderr.write('[screenweave-mcp] Server running (stdio) — Lambda direct invoke mode\n');
