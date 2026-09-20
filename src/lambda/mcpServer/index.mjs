/**
 * ScreenWeave – MCP Server Lambda
 *
 * Implements the Model Context Protocol (MCP) over HTTP using JSON-RPC 2.0.
 * All tool logic is inline — no Lambda-to-Lambda calls.
 *
 * Transport: API Gateway v2 (HTTP API)
 *   POST /mcp  →  JSON-RPC dispatcher  →  SSE-formatted response
 *   GET  /mcp  →  empty SSE stream (protocol negotiation / health)
 *
 * MCP methods handled:
 *   initialize              → server capabilities
 *   notifications/initialized → (notification, no response)
 *   ping                    → {}
 *   tools/list              → tool definitions with JSON Schema
 *   tools/call              → invoke tool, return MCP content envelope
 *
 * Tools:
 *   crawl_url             – validate URL, write RUNNING to DynamoDB, launch EC2
 *   get_session_status    – DynamoDB GetItem only (no S3 work)
 *   get_screenshots       – DynamoDB + S3 list + pre-signed URLs
 *   get_metrics           – DynamoDB + states.json fetch + computed metrics
 *   get_full_session      – DynamoDB + S3 list + pre-signed URLs (full bundle)
 *   export_session        – copy text/metadata artifacts to ContextWeave's raw/ prefix
 *
 * Every tool call is wrapped by observatory.mjs, which records an invocation
 * span (tool name, args hash, duration, outcome) to the shared
 * OBSERVATORY_METRICS_TABLE when configured, and is a no-op otherwise.
 *
 * MCP client config (no AWS credentials required by the caller):
 *   { "mcpServers": { "screenweave": { "url": "<McpEndpoint output>" } } }
 *
 * Lambda env vars (injected by CloudFormation):
 *   SESSIONS_TABLE               DynamoDB table name
 *   ARTIFACTS_BUCKET             S3 bucket for Playwright artifacts
 *   BUCKET_PREFIX                S3 key prefix (default: screenweave)
 *   CRAWLER_AMI_ID               AL2023 AMI for EC2 crawlers
 *   CRAWLER_SG_ID                Egress-only security group ID
 *   CRAWLER_INSTANCE_PROFILE_ARN IAM instance profile ARN for crawlers
 *   CRAWLER_INSTANCE_TYPE        EC2 instance type (default: t3.medium)
 *   CRAWLER_CODE_BUCKET          Dedicated S3 bucket for crawl.py (key: crawler/crawl.py)
 *   SIGNED_URL_EXPIRES_SECONDS   Pre-signed URL TTL (default: 3600)
 *   OBSERVATORY_METRICS_TABLE    Shared cross-project telemetry table (optional)
 *   CONTEXTWEAVE_RAW_BUCKET      ContextWeave raw ingestion bucket for export_session (optional)
 */

import { DynamoDBClient, GetItemCommand, PutItemCommand } from '@aws-sdk/client-dynamodb';
import { EC2Client, RunInstancesCommand } from '@aws-sdk/client-ec2';
import { S3Client, GetObjectCommand, ListObjectsV2Command } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { marshall, unmarshall } from '@aws-sdk/util-dynamodb';
import { randomUUID } from 'crypto';
import { validateVideoRequest, startVideoJob } from './videoJob.mjs';
import { withObservability } from './observatory.mjs';
import { uploadSessionExport } from './exportSession.mjs';

// ── AWS SDK clients (reused across warm invocations) ──────────────────────────
const dynamo = new DynamoDBClient({});
const ec2    = new EC2Client({});
const s3     = new S3Client({});

// ── Config ────────────────────────────────────────────────────────────────────
const TABLE_NAME           = process.env.SESSIONS_TABLE;
const VIDEO_WORKER_FUNCTION = process.env.VIDEO_WORKER_FUNCTION || '';
const BUCKET_NAME          = process.env.ARTIFACTS_BUCKET;
const BUCKET_PREFIX        = process.env.BUCKET_PREFIX        || 'screenweave';
const AMI_ID               = process.env.CRAWLER_AMI_ID;
const SG_ID                = process.env.CRAWLER_SG_ID;
const INSTANCE_PROFILE_ARN = process.env.CRAWLER_INSTANCE_PROFILE_ARN;
const INSTANCE_TYPE        = process.env.CRAWLER_INSTANCE_TYPE || 't3.medium';
const REGION               = process.env.AWS_REGION            || 'us-east-1';
const CODE_BUCKET          = process.env.CRAWLER_CODE_BUCKET;
const SIGNED_URL_EXPIRES   = parseInt(process.env.SIGNED_URL_EXPIRES_SECONDS || '3600', 10);
const LOG_LEVEL            = (process.env.LOG_LEVEL || 'INFO').toUpperCase();
const CONTEXTWEAVE_RAW_BUCKET = process.env.CONTEXTWEAVE_RAW_BUCKET || '';

// ── Validation constants ──────────────────────────────────────────────────────
const URL_REGEX        = /^https?:\/\/[a-zA-Z0-9._-]+(?::\d+)?(?:\/[^\s]*)?$/;
const SESSION_ID_REGEX = /^[a-zA-Z0-9_-]{4,64}$/;
const MAX_DEPTH_LIMIT  = 3;
const MAX_LINKS_LIMIT  = 30;
const SIGN_BATCH_SIZE  = 25;

// ── MCP protocol ──────────────────────────────────────────────────────────────
const MCP_PROTOCOL_VERSION = '2024-11-05';
const JSONRPC_VERSION      = '2.0';

const LOG_LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARN: 30, ERROR: 40 };

function shouldLog(level) {
  const configured = LOG_LEVEL_ORDER[LOG_LEVEL] ?? LOG_LEVEL_ORDER.INFO;
  const requested = LOG_LEVEL_ORDER[level] ?? LOG_LEVEL_ORDER.INFO;
  return requested >= configured;
}

function log(level, message, fields = {}) {
  if (!shouldLog(level)) return;
  console.log(JSON.stringify({
    level,
    message,
    service: 'screenweave-mcp',
    ...fields,
  }));
}

// ── Tool definitions (returned by tools/list) ─────────────────────────────────
const TOOL_DEFINITIONS = [
  {
    name: 'crawl_url',
    description:
      'Start a Playwright crawl for the given URL. The crawler visits the page, ' +
      'scrolls to reveal lazy-loaded content, clicks interactive elements (tabs, ' +
      'accordions, buttons), and follows internal links up to max_depth. Captures ' +
      'a full-page screenshot and structured metadata for each distinct visual state.\n\n' +
      'Returns session_id immediately; the crawl runs asynchronously on EC2. ' +
      'Poll get_session_status until COMPLETED, then call get_screenshots or get_metrics.',
    inputSchema: {
      type: 'object',
      properties: {
        url: {
          type: 'string',
          description: 'The URL to crawl (must be http or https)',
        },
        max_depth: {
          type: 'integer',
          minimum: 0,
          maximum: MAX_DEPTH_LIMIT,
          default: 2,
          description: 'Link recursion depth (0 = root page only)',
        },
        max_links: {
          type: 'integer',
          minimum: 1,
          maximum: MAX_LINKS_LIMIT,
          default: 12,
          description: 'Max internal links to follow per page',
        },
      },
      required: ['url'],
    },
  },
  {
    name: 'get_session_status',
    description:
      'Check the status of a crawl session: RUNNING, COMPLETED, or FAILED.\n' +
      'Poll every 30–60 seconds after crawl_url. ' +
      'Once COMPLETED, call get_screenshots or get_metrics.',
    inputSchema: {
      type: 'object',
      properties: {
        session_id: { type: 'string', description: 'Session ID returned by crawl_url' },
      },
      required: ['session_id'],
    },
  },
  {
    name: 'get_screenshots',
    description:
      'Get pre-signed HTTPS URLs for every screenshot captured during a crawl session. ' +
      'Each entry maps a state_id to its signed URL and page context (URL, timestamp). ' +
      'URLs expire after 1 hour. Call after get_session_status returns COMPLETED.',
    inputSchema: {
      type: 'object',
      properties: {
        session_id: { type: 'string', description: 'Session ID returned by crawl_url' },
      },
      required: ['session_id'],
    },
  },
  {
    name: 'get_metrics',
    description:
      'Get computed page metrics for a completed crawl session. ' +
      'Fetches states.json from S3 and computes:\n' +
      '  - Session summary: total states, unique pages, duration\n' +
      '  - Coverage: states by action type (navigation / scroll / click)\n' +
      '  - Content: heading distribution, unique links, interactive elements\n' +
      '  - Per-state table for QA review',
    inputSchema: {
      type: 'object',
      properties: {
        session_id: { type: 'string', description: 'Session ID returned by crawl_url' },
      },
      required: ['session_id'],
    },
  },
  {
    name: 'get_full_session',
    description:
      'Get the complete artifact bundle for a crawl session. ' +
      'Use the include parameter to request only what you need. ' +
      'Returns pre-signed S3 URLs for all requested artifact types.',
    inputSchema: {
      type: 'object',
      properties: {
        session_id: { type: 'string', description: 'Session ID returned by crawl_url' },
        include: {
          type: 'array',
          items: {
            type: 'string',
            enum: ['screenshots', 'states', 'transitions', 'trace'],
          },
          default: ['screenshots', 'states', 'transitions', 'trace'],
          description: 'Artifact types to include in the response',
        },
      },
      required: ['session_id'],
    },
  },
  {
    name: 'generate_apple_video',
    description:
      'Produce a short Apple-style kinetic showcase video of a page.\n\n' +
      'Pass either url (the page is captured fresh) or session_id (reuse a ' +
      'completed crawl, which avoids capturing the same site twice and keeps ' +
      'the QA report and the video describing the same page).\n\n' +
      'Returns a job_id immediately; the render runs asynchronously. Poll ' +
      'get_session_status with that job_id until COMPLETED or FAILED.',
    inputSchema: {
      type: 'object',
      properties: {
        url: {
          type: 'string',
          description: 'Page to showcase (http or https). Mutually exclusive with session_id.',
        },
        session_id: {
          type: 'string',
          description: 'Reuse this completed crawl instead of capturing again.',
        },
        duration_s: {
          type: 'integer',
          minimum: 5,
          maximum: 30,
          default: 20,
          description: 'Video length in seconds',
        },
        scale: {
          type: 'integer',
          enum: [1, 2, 3],
          default: 3,
          description: 'Capture device scale factor; 3 is Retina',
        },
      },
    },
  },
  {
    name: 'export_session',
    description:
      'Export a completed crawl session\'s extracted text/metadata artifacts to ContextWeave ' +
      '(s3://<CONTEXTWEAVE_RAW_BUCKET>/raw/screenweave/<session_id>/) so its preprocessor can ' +
      'ingest them: states.json and transitions.json verbatim, plus a synthesized summary.md. ' +
      'Raw screenshots and the Playwright trace are not exported. ' +
      'Requires CONTEXTWEAVE_RAW_BUCKET to be configured and the session to be COMPLETED.',
    inputSchema: {
      type: 'object',
      properties: {
        session_id: { type: 'string', description: 'Session ID returned by crawl_url' },
      },
      required: ['session_id'],
    },
  },
];

// ── Lambda entry point ────────────────────────────────────────────────────────
export const handler = async (event) => {
  const httpMethod = (event.requestContext?.http?.method || 'POST').toUpperCase();
  log('INFO', 'Incoming MCP request', { http_method: httpMethod });

  // GET /mcp — return empty SSE stream for protocol negotiation / health check
  if (httpMethod === 'GET') {
    return {
      statusCode: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: '',
    };
  }

  if (httpMethod !== 'POST') {
    return {
      statusCode: 405,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ error: 'Method Not Allowed' }),
    };
  }

  // Parse JSON-RPC body
  let parsed;
  try {
    const raw = event.isBase64Encoded
      ? Buffer.from(event.body || '', 'base64').toString('utf8')
      : (event.body || '{}');
    parsed = JSON.parse(raw);
  } catch (err) {
    log('ERROR', 'Invalid JSON-RPC payload', { error: err.message });
    return sseResponse(jsonRpcError(null, -32700, 'Parse error'));
  }

  // Support JSON-RPC batches (array of requests)
  const requests = Array.isArray(parsed) ? parsed : [parsed];
  log('DEBUG', 'Parsed JSON-RPC payload', { request_count: requests.length, is_batch: Array.isArray(parsed) });
  const responses = await Promise.all(requests.map(dispatchRpc));
  const valid = responses.filter(Boolean); // drop nulls from notifications

  if (valid.length === 0) {
    // All messages were notifications — acknowledge with 202
    return { statusCode: 202, headers: { 'Content-Type': 'application/json' }, body: '' };
  }

  return sseResponse(valid.length === 1 ? valid[0] : valid);
};

// ── JSON-RPC dispatcher ───────────────────────────────────────────────────────
async function dispatchRpc(req) {
  log('DEBUG', 'Dispatching JSON-RPC request', { method: req?.method || null, id: req?.id ?? null });
  if (!req || req.jsonrpc !== JSONRPC_VERSION) {
    return jsonRpcError(req?.id ?? null, -32600, 'Invalid Request');
  }

  const { method, params, id } = req;

  // Notifications have no id and need no response
  if (id === undefined && typeof method === 'string' && method.startsWith('notifications/')) {
    return null;
  }

  try {
    switch (method) {
      case 'initialize':
        return jsonRpcOk(id, {
          protocolVersion: MCP_PROTOCOL_VERSION,
          capabilities: { tools: {} },
          serverInfo: { name: 'screenweave', version: '1.0.0' },
        });

      case 'notifications/initialized':
        return null;

      case 'ping':
        return jsonRpcOk(id, {});

      case 'tools/list':
        return jsonRpcOk(id, { tools: TOOL_DEFINITIONS });

      case 'tools/call': {
        if (!params?.name) return jsonRpcError(id, -32602, 'Missing tool name in params');
        log('INFO', 'Invoking MCP tool', { tool_name: params.name, rpc_id: id ?? null });
        const result = await invokeTool(params.name, params.arguments || {});
        log('INFO', 'Completed MCP tool', { tool_name: params.name, rpc_id: id ?? null });
        return jsonRpcOk(id, result);
      }

      default:
        return jsonRpcError(id, -32601, `Method not found: ${method}`);
    }
  } catch (err) {
    log('ERROR', 'JSON-RPC dispatch failed', { method, error: err.message, stack: err.stack });
    // Application errors become JSON-RPC errors so the client sees them
    return jsonRpcError(id, -32603, err.message);
  }
}

// ── Tool router ───────────────────────────────────────────────────────────────
// Every dispatch is wrapped in an Observatory span (see observatory.mjs);
// wrapping here at the single dispatch point covers all tools identically
// and is a no-op when the shared gate isn't configured.
async function invokeTool(name, args) {
  return withObservability(name, args, () => dispatchTool(name, args));
}

async function dispatchTool(name, args) {
  switch (name) {
    case 'crawl_url':          return toolCrawlUrl(args);
    case 'get_session_status': return toolGetSessionStatus(args);
    case 'get_screenshots':    return toolGetScreenshots(args);
    case 'get_metrics':        return toolGetMetrics(args);
    case 'get_full_session':   return toolGetFullSession(args);
    case 'export_session':     return toolExportSession(args);
    case 'generate_apple_video': return toolGenerateAppleVideo(args);
    default: throw new Error(`Unknown tool: ${name}`);
  }
}

// ── Tool: crawl_url ───────────────────────────────────────────────────────────
async function toolCrawlUrl({ url, max_depth = 2, max_links = 12 }) {
  if (!url || typeof url !== 'string')
    throw new Error('url is required (string)');
  if (!URL_REGEX.test(url.trim()))
    throw new Error('url must be a valid http(s) URL');
  if (typeof max_depth !== 'number' || max_depth < 0 || max_depth > MAX_DEPTH_LIMIT)
    throw new Error(`max_depth must be 0–${MAX_DEPTH_LIMIT}`);
  if (typeof max_links !== 'number' || max_links < 1 || max_links > MAX_LINKS_LIMIT)
    throw new Error(`max_links must be 1–${MAX_LINKS_LIMIT}`);

  const targetUrl = url.trim();
  const sessionId = randomUUID();
  const now       = new Date().toISOString();
  const ttl       = Math.floor(Date.now() / 1000) + 7 * 24 * 60 * 60; // 7-day TTL
  log('INFO', 'Validated crawl_url input', { session_id: sessionId, target_url: targetUrl, max_depth, max_links });

  // Write RUNNING record to DynamoDB
  log('INFO', 'Writing RUNNING session to DynamoDB', { session_id: sessionId, table_name: TABLE_NAME });
  await dynamo.send(new PutItemCommand({
    TableName: TABLE_NAME,
    Item: marshall({
      session_id:  `SESSION#${sessionId}`,
      status:      'RUNNING',
      target_url:  targetUrl,
      max_depth,
      max_links,
      created_at:  now,
      updated_at:  now,
      ttl,
    }),
    ConditionExpression: 'attribute_not_exists(session_id)',
  }));

  // Launch EC2 crawler
  log('INFO', 'Building EC2 user-data and launching crawler instance', { session_id: sessionId, ami_id: AMI_ID, instance_type: INSTANCE_TYPE });
  const userDataBase64 = Buffer.from(
    buildUserData({ sessionId, targetUrl, region: REGION, table: TABLE_NAME, maxDepth: max_depth, maxLinks: max_links })
  ).toString('base64');

  const ec2Result = await ec2.send(new RunInstancesCommand({
    ImageId:      AMI_ID,
    InstanceType: INSTANCE_TYPE,
    MinCount: 1,
    MaxCount: 1,
    IamInstanceProfile: { Arn: INSTANCE_PROFILE_ARN },
    SecurityGroupIds: [SG_ID],
    UserData: userDataBase64,
    InstanceInitiatedShutdownBehavior: 'terminate',
    TagSpecifications: [{
      ResourceType: 'instance',
      Tags: [
        { Key: 'Name',      Value: `screenweave-crawler-${sessionId.slice(0, 8)}` },
        { Key: 'SessionId', Value: sessionId },
        { Key: 'Project',   Value: 'ScreenWeave' },
      ],
    }],
  }));

  const instanceId = ec2Result.Instances[0].InstanceId;
  log('INFO', 'Crawl started', { session_id: sessionId, instance_id: instanceId });

  return toolContent({
    session_id:  sessionId,
    status:      'RUNNING',
    target_url:  targetUrl,
    instance_id: instanceId,
    message:     `Crawl started. Poll get_session_status(session_id="${sessionId}") until COMPLETED.`,
  });
}

// ── Tool: generate_apple_video ────────────────────────────────────────────────
// The second engine. A render is a row in the same sessions table under the
// same SESSION# key as a crawl, so get_session_status polls it unchanged and
// no client learns a second protocol.
async function toolGenerateAppleVideo(args) {
  const request = validateVideoRequest(args);
  const jobId   = randomUUID();
  const now     = new Date().toISOString();
  const ttl     = Math.floor(Date.now() / 1000) + 7 * 24 * 60 * 60;

  const result = await startVideoJob({
    request,
    jobId,
    now,
    ttl,
    workerFunction: VIDEO_WORKER_FUNCTION,
    log,
    putItem: (item) => dynamo.send(new PutItemCommand({
      TableName: TABLE_NAME,
      Item: marshall(item, { removeUndefinedValues: true }),
      ConditionExpression: 'attribute_not_exists(session_id)',
    })),
    invokeWorker: async () => {
      // Reached only when VIDEO_WORKER_FUNCTION is set. The worker itself is
      // the remaining piece of this engine; until it exists the job is
      // recorded as NOT_CONFIGURED rather than promised an artifact.
      throw new Error('VIDEO_WORKER_FUNCTION is set but no render worker is deployed yet');
    },
  });

  return toolContent(result);
}

// ── Tool: get_session_status ──────────────────────────────────────────────────
async function toolGetSessionStatus({ session_id }) {
  log('DEBUG', 'Fetching session status', { session_id });
  const session = await fetchSession(session_id);
  return toolContent({
    session_id,
    status:        session.status,
    target_url:    session.target_url    || null,
    created_at:    session.created_at    || null,
    updated_at:    session.updated_at    || null,
    summary_stats: session.summary_stats || null,
  });
}

// ── Tool: get_screenshots ─────────────────────────────────────────────────────
async function toolGetScreenshots({ session_id }) {
  log('DEBUG', 'Fetching screenshots for session', { session_id });
  const session        = await fetchSession(session_id);
  const prefix         = `${BUCKET_PREFIX}/${session_id}`;
  const screenshotKeys = await listS3Objects(`${prefix}/screenshots/`);

  if (!screenshotKeys.length) {
    return toolContent({
      session_id,
      status:  session.status,
      message: 'No screenshots found. Check get_session_status first.',
    });
  }

  const signedUrls = await signKeysBatched(screenshotKeys);
  const manifest   = session.artifact_manifest || {};

  const screenshots = screenshotKeys.map((key, i) => {
    const filename = key.split('/').pop();
    const stateId  = filename.replace(/\.(png|jpg|jpeg|webp)$/i, '');
    const meta     = manifest[stateId] || {};
    return {
      state_id:  stateId,
      https_url: signedUrls[i],
      page_url:  meta.url       || '',
      timestamp: meta.timestamp || '',
      s3_uri:    `s3://${BUCKET_NAME}/${key}`,
    };
  });

  return toolContent({
    session_id,
    status:            session.status,
    total_screenshots: screenshots.length,
    screenshots,
  });
}

// ── Tool: get_metrics ─────────────────────────────────────────────────────────
async function toolGetMetrics({ session_id }) {
  log('DEBUG', 'Fetching metrics for session', { session_id });
  const session = await fetchSession(session_id);

  if (session.status !== 'COMPLETED') {
    return toolContent({
      session_id,
      status:  session.status,
      message: `Crawl is ${session.status}. Metrics are available after COMPLETED.`,
    });
  }

  const statesUrl = await signKey(`${BUCKET_PREFIX}/${session_id}/states.json`);
  if (!statesUrl) throw new Error('states.json not available for this session');

  const res = await fetch(statesUrl);
  if (!res.ok) throw new Error(`Failed to fetch states.json: HTTP ${res.status}`);
  const statesData = await res.json();
  const states     = statesData.states || [];

  if (!states.length) {
    return toolContent({ session_id, message: 'No states recorded in this session.' });
  }

  // ── Compute metrics ────────────────────────────────────────────────────────
  const timestamps  = states.map((s) => s.timestamp).filter(Boolean).sort();
  const firstTs     = timestamps[0] || null;
  const lastTs      = timestamps[timestamps.length - 1] || null;
  const durationSec = firstTs && lastTs
    ? Math.round((new Date(lastTs) - new Date(firstTs)) / 1000)
    : null;

  const uniqueUrls = [...new Set(states.map((s) => s.url))];
  const byAction   = {};
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

  const heights   = states.map((s) => s.document_height || 0).filter((h) => h > 0);
  const avgHeight = heights.length
    ? Math.round(heights.reduce((a, b) => a + b, 0) / heights.length)
    : 0;

  return toolContent({
    session_id,
    status:   session.status,
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
      heading_distribution:              headingCounts,
      total_unique_links_found:          allLinks.size,
      total_unique_interactive_elements: allInteractive.size,
      interactive_element_labels:        [...allInteractive].slice(0, 30),
      avg_document_height_px:            avgHeight,
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
}

// ── Tool: get_full_session ────────────────────────────────────────────────────
async function toolGetFullSession({ session_id, include = ['screenshots', 'states', 'transitions', 'trace'] }) {
  log('DEBUG', 'Fetching full session bundle', { session_id, include });
  const session    = await fetchSession(session_id);
  const prefix     = `${BUCKET_PREFIX}/${session_id}`;
  const includeSet = new Set(include.map((s) => s.toLowerCase()));
  const artifacts  = {};

  const [states_json, transitions_json, trace_file] = await Promise.all([
    includeSet.has('states')      ? signKey(`${prefix}/states.json`)      : Promise.resolve(null),
    includeSet.has('transitions') ? signKey(`${prefix}/transitions.json`) : Promise.resolve(null),
    includeSet.has('trace')       ? signKey(`${prefix}/trace.zip`)        : Promise.resolve(null),
  ]);

  if (states_json)      artifacts.states_json      = states_json;
  if (transitions_json) artifacts.transitions_json = transitions_json;
  if (trace_file)       artifacts.trace_file       = trace_file;

  if (includeSet.has('screenshots')) {
    const keys       = await listS3Objects(`${prefix}/screenshots/`);
    const signedUrls = await signKeysBatched(keys);
    artifacts.screenshots = keys.map((key, i) => ({
      state_id:  key.split('/').pop().replace(/\.(png|jpg|jpeg|webp)$/i, ''),
      s3_uri:    `s3://${BUCKET_NAME}/${key}`,
      https_url: signedUrls[i],
    }));
  }

  return toolContent({
    session_id,
    status: session.status,
    artifacts,
    metadata: {
      created_at:    session.created_at    || null,
      updated_at:    session.updated_at    || null,
      summary_stats: session.summary_stats || null,
    },
  });
}

// ── Tool: export_session ──────────────────────────────────────────────────────
async function toolExportSession({ session_id }) {
  log('DEBUG', 'Exporting session artifacts to ContextWeave', { session_id });

  if (!CONTEXTWEAVE_RAW_BUCKET) {
    throw new Error(
      'CONTEXTWEAVE_RAW_BUCKET is not configured. Set the environment variable ' +
      '(see the ContextWeaveRawBucketName SAM parameter) to enable export_session.'
    );
  }

  const session = await fetchSession(session_id);
  if (session.status !== 'COMPLETED') {
    return toolContent({
      session_id,
      status:  session.status,
      message: `Crawl is ${session.status}. export_session is available after COMPLETED.`,
    });
  }

  const prefix = `${BUCKET_PREFIX}/${session_id}`;
  const [statesText, transitionsText] = await Promise.all([
    getS3Text(`${prefix}/states.json`),
    getS3Text(`${prefix}/transitions.json`),
  ]);
  if (!statesText)      throw new Error('states.json not available for this session');
  if (!transitionsText) throw new Error('transitions.json not available for this session');

  const keys = await uploadSessionExport({
    s3Client:   s3,
    bucket:     CONTEXTWEAVE_RAW_BUCKET,
    sessionId:  session_id,
    statesText,
    transitionsText,
  });

  log('INFO', 'Exported session artifacts to ContextWeave', { session_id, bucket: CONTEXTWEAVE_RAW_BUCKET, keys });

  return toolContent({
    session_id,
    status:   session.status,
    bucket:   CONTEXTWEAVE_RAW_BUCKET,
    exported: Object.entries(keys).map(([artifact, key]) => ({
      artifact,
      key,
      s3_uri: `s3://${CONTEXTWEAVE_RAW_BUCKET}/${key}`,
    })),
    message: 'Artifacts exported. ContextWeave\'s preprocessor ingests them on its next raw/ scan.',
  });
}

// ── DynamoDB helpers ──────────────────────────────────────────────────────────
async function fetchSession(session_id) {
  if (!session_id) throw new Error('Missing required parameter: session_id');
  if (!SESSION_ID_REGEX.test(session_id)) throw new Error('Invalid session_id format (4–64 alphanumeric/hyphen/underscore)');

  log('DEBUG', 'Reading session from DynamoDB', { session_id, table_name: TABLE_NAME });
  const result = await dynamo.send(new GetItemCommand({
    TableName: TABLE_NAME,
    Key: { session_id: { S: `SESSION#${session_id}` } },
  }));

  if (!result.Item) throw new Error(`Session not found: ${session_id}`);
  return unmarshall(result.Item);
}

// ── S3 helpers ────────────────────────────────────────────────────────────────
async function signKey(key) {
  try {
    log('DEBUG', 'Signing S3 object key', { key });
    return await getSignedUrl(
      s3,
      new GetObjectCommand({ Bucket: BUCKET_NAME, Key: key }),
      { expiresIn: SIGNED_URL_EXPIRES }
    );
  } catch (err) {
    log('WARN', `Failed to sign: ${key}`, { error: err.message });
    return null;
  }
}

/** Read an S3 object's body as UTF-8 text, or null if it doesn't exist. */
async function getS3Text(key) {
  try {
    const res = await s3.send(new GetObjectCommand({ Bucket: BUCKET_NAME, Key: key }));
    return await res.Body.transformToString('utf-8');
  } catch (err) {
    if (err.name === 'NoSuchKey' || err.$metadata?.httpStatusCode === 404) return null;
    throw err;
  }
}

async function signKeysBatched(keys) {
  const results = [];
  for (let i = 0; i < keys.length; i += SIGN_BATCH_SIZE) {
    const batch = await Promise.all(keys.slice(i, i + SIGN_BATCH_SIZE).map(signKey));
    results.push(...batch);
  }
  return results;
}

async function listS3Objects(prefix) {
  log('DEBUG', 'Listing S3 objects', { bucket: BUCKET_NAME, prefix });
  const keys = [];
  let continuationToken;

  do {
    const result = await s3.send(new ListObjectsV2Command({
      Bucket: BUCKET_NAME,
      Prefix: prefix,
      ContinuationToken: continuationToken,
    }));

    if (result.Contents) {
      for (const obj of result.Contents) {
        if (obj.Key !== prefix && /\.(png|jpg|jpeg|webp)$/i.test(obj.Key)) {
          keys.push(obj.Key);
        }
      }
    }
    continuationToken = result.NextContinuationToken;
  } while (continuationToken);
  log('DEBUG', 'Completed S3 listing', { prefix, key_count: keys.length });

  // Sort numerically by the first integer in the filename (state_0001, state_0002 …)
  return keys.sort((a, b) => {
    const aFile = a.split('/').pop();
    const bFile = b.split('/').pop();
    const aNum  = parseInt((aFile.match(/(\d+)/) || [])[1] || '0', 10);
    const bNum  = parseInt((bFile.match(/(\d+)/) || [])[1] || '0', 10);
    return aNum - bNum || aFile.localeCompare(bFile);
  });
}

// ── EC2 UserData builder ──────────────────────────────────────────────────────
/**
 * Generates the bash UserData script for a single EC2 crawler run.
 * crawl.py is downloaded from CrawlerCodeBucket at boot time (uploaded by the CI workflow).
 * Key: crawler/crawl.py
 */
function buildUserData({ sessionId, targetUrl, region, table, maxDepth, maxLinks }) {
  return `#!/bin/bash
set -euxo pipefail
exec > /var/log/screenweave-crawler.log 2>&1

SESSION_ID="${sessionId}"
TARGET_URL="${targetUrl}"
S3_BUCKET="${BUCKET_NAME}"
S3_PREFIX="${BUCKET_PREFIX}"
DYNAMO_TABLE="${table}"
REGION="${region}"
MAX_DEPTH="${maxDepth}"
MAX_LINKS="${maxLinks}"
LOG_LEVEL="${LOG_LEVEL}"
OUT_DIR="/opt/output"

# IMDSv2 – fetch instance ID
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \\
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \\
  http://169.254.169.254/latest/meta-data/instance-id)

echo "SESSION  : $SESSION_ID"
echo "TARGET   : $TARGET_URL"
echo "INSTANCE : $INSTANCE_ID"
echo "LOGLEVEL : $LOG_LEVEL"

mkdir -p "$OUT_DIR/screenshots"

# ── 1. System packages ─────────────────────────────────────────────────────────
dnf update -y
dnf install -y python3-pip ImageMagick unzip tar xz
dnf install -y atk cups-libs gtk3 libXcomposite libXcursor libXdamage \\
  libXext libXi libXrandr libXScrnSaver libXtst pango alsa-lib \\
  at-spi2-atk at-spi2-core libdrm mesa-libgbm nss nspr \\
  libxkbcommon libgbm xdg-utils

# ── 2. Python dependencies + Playwright ───────────────────────────────────────
pip3 install playwright boto3
python3 -m playwright install chromium

# ── 3. Download crawler script ─────────────────────────────────────────────────
aws s3 cp "s3://${CODE_BUCKET}/crawler/crawl.py" /opt/crawl.py --region "$REGION"
echo "Crawler: $(wc -l /opt/crawl.py | awk '{print $1}') lines"

# ── 4. Run crawler ─────────────────────────────────────────────────────────────
export SCREENWEAVE_SESSION_ID="$SESSION_ID"
export SCREENWEAVE_MAX_DEPTH="$MAX_DEPTH"
export SCREENWEAVE_MAX_LINKS="$MAX_LINKS"
export SCREENWEAVE_DYNAMO_TABLE="$DYNAMO_TABLE"
export SCREENWEAVE_S3_BUCKET="$S3_BUCKET"
export SCREENWEAVE_S3_PREFIX="$S3_PREFIX"
export SCREENWEAVE_REGION="$REGION"
export SCREENWEAVE_LOG_LEVEL="$LOG_LEVEL"

python3 /opt/crawl.py "$TARGET_URL" || {
  aws dynamodb update-item \\
    --table-name "$DYNAMO_TABLE" \\
    --key '{"session_id":{"S":"SESSION#'"$SESSION_ID"'"}}' \\
    --update-expression "SET #s = :s, updated_at = :u" \\
    --expression-attribute-names '{"#s":"status"}' \\
    --expression-attribute-values '{":s":{"S":"FAILED"},":u":{"S":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}}' \\
    --region "$REGION"
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION"
  exit 1
}

# ── 5. Upload artifacts ────────────────────────────────────────────────────────
SESSION_S3="s3://$S3_BUCKET/$S3_PREFIX/$SESSION_ID"
aws s3 cp "$OUT_DIR/states.json"      "$SESSION_S3/states.json"      --region "$REGION"
aws s3 cp "$OUT_DIR/transitions.json" "$SESSION_S3/transitions.json" --region "$REGION"
aws s3 cp "$OUT_DIR/trace.zip"        "$SESSION_S3/trace.zip"        --region "$REGION"
aws s3 sync "$OUT_DIR/screenshots/"   "$SESSION_S3/screenshots/"     --region "$REGION"
aws s3 cp /var/log/screenweave-crawler.log "$SESSION_S3/crawler.log" --region "$REGION"

echo "Uploaded $(ls $OUT_DIR/screenshots/*.png 2>/dev/null | wc -l) screenshots to $SESSION_S3"

# ── 6. Self-terminate ──────────────────────────────────────────────────────────
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION"
`;
}

// ── Response helpers ──────────────────────────────────────────────────────────

/** Wrap data in an MCP tool result content envelope */
function toolContent(data) {
  return {
    content: [{
      type: 'text',
      text: typeof data === 'string' ? data : JSON.stringify(data, null, 2),
    }],
  };
}

/** JSON-RPC 2.0 success */
function jsonRpcOk(id, result) {
  return { jsonrpc: JSONRPC_VERSION, id, result };
}

/** JSON-RPC 2.0 error */
function jsonRpcError(id, code, message) {
  return { jsonrpc: JSONRPC_VERSION, id, error: { code, message } };
}

/**
 * Wrap a JSON-RPC response payload in an SSE envelope.
 * API Gateway v2 buffers the body before sending, so this is buffered SSE —
 * the client receives the complete event in one HTTP response.
 * Format: https://html.spec.whatwg.org/multipage/server-sent-events.html
 */
function sseResponse(payload) {
  const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
  return {
    statusCode: 200,
    headers: {
      'Content-Type':      'text/event-stream',
      'Cache-Control':     'no-cache',
      'X-Accel-Buffering': 'no',
    },
    body: `event: message\ndata: ${data}\n\n`,
  };
}
