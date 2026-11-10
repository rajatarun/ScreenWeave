/**
 * QA agent sessions: run IDs, conversation, attempts.
 *
 * A QA run reuses the crawl's id. start_qa_run either starts a crawl (its
 * session_id becomes the run_id) or attaches to an existing crawl session;
 * no second id scheme exists. The run is a row in the same sessions table:
 *
 *   SESSION#<run_id>                  the crawl row, unchanged (get_session_status works)
 *   QARUN#<run_id>                    this module: instruction, follow-ups and
 *                                     corrections, environment, limits, attempts
 *   QARUN#<run_id>#ATTEMPT#<n>        status of attempt n (written REQUESTED here,
 *                                     then by the crawler's qa_agent.py)
 *   s3://<artifacts>/<prefix>/<run_id>/qa/attempt_<nnn>/{plan,results,signoff}.json
 *
 * Each attempt is one crawler instance: crawl, then qa_agent.py plans (the plan
 * is persisted before anything runs), executes and signs off. rerun_qa adds an
 * attempt to the same run, so the agent sees everything said so far.
 *
 * Opt-in: QA_AGENT_ENABLED=true. Off, the tools are not listed and refused.
 *
 * AWS access is injected (deps) so the logic tests without AWS, like
 * videoJob.mjs and exportSession.mjs.
 */
import { validateTargetUrl } from './crawlTarget.mjs';

export const QA_TOOL_NAMES = ['start_qa_run', 'continue_qa_run', 'get_qa_run', 'rerun_qa'];
export const RUN_ID_REGEX = /^[a-zA-Z0-9_-]{4,64}$/;
export const MAX_MESSAGES = 50;
export const MAX_MESSAGE_CHARS = 2000;
export const MAX_ATTEMPTS = 20;
export const DEFAULT_LIMITS = { max_cases: 8, max_steps: 60 };
export const LIMIT_BOUNDS = { max_cases: [1, 20], max_steps: [1, 200] };
const ACTIVE = new Set(['REQUESTED', 'PLANNING', 'PLANNED', 'RUNNING']);
const STALE_MS = 60 * 60 * 1000;   // an attempt silent this long no longer blocks a rerun
const RUN_TTL_S = 30 * 24 * 60 * 60;

export function qaAgentEnabled(env = process.env) {
  return (env.QA_AGENT_ENABLED || '').trim().toLowerCase() === 'true';
}

// ── Shared classification rule (contracts/qa_request_classification.json) ──
// Same lists and steps as src/crawler/qa_agent.py; both suites check the contract.
export const GENERIC_WORDS = new Set(`a about all an and app application around at audit basic bug bugs
can check could do entire everything explore find for full general go i issues it look me my of on our
page pages please problems product qa quick regressions review run site smoke some test testing tests the
this through to ui url us website whole would you`.split(/\s+/));
export const TEST_VERBS = new Set('check test try verify validate confirm ensure'.split(' '));
export const ACTION_VERBS = new Set(`add buy cancel checkout choose click download enter fill log login logout
navigate open pay press register remove search select sign signup submit tap type upload`.split(/\s+/));

const URL_RE = /https?:\/\/\S+/g;
const QUOTED_RE = /"[^"]{2,}"|“[^”]{2,}”|`[^`]{2,}`/g;
const TOKEN_RE = /[a-z0-9]+(?:'[a-z]+)?/g;

export function classifyRequest(text) {
  if (typeof text !== 'string' || !text.trim()) return { kind: 'unspecified', rule: 'empty', ambiguous: false, focus: [] };
  const low = text.toLowerCase();
  const quoted = new RegExp(QUOTED_RE.source).test(low);
  const stripped = low.replace(URL_RE, ' ').replace(QUOTED_RE, ' ');
  const tokens = stripped.match(TOKEN_RE) || [];
  const leftover = tokens.filter((t) => !GENERIC_WORDS.has(t));
  if (!leftover.length && !quoted) return { kind: 'unspecified', rule: 'generic', ambiguous: false, focus: [] };
  if (quoted) return { kind: 'specific', rule: 'quoted', ambiguous: false, focus: [] };
  if (leftover.some((t) => ACTION_VERBS.has(t))) return { kind: 'specific', rule: 'action-verb', ambiguous: false, focus: [] };
  if (tokens.some((t) => TEST_VERBS.has(t)) && leftover.length)
    return { kind: 'specific', rule: 'test-verb-object', ambiguous: false, focus: [] };
  const focus = [...new Set(leftover.filter((t) => !TEST_VERBS.has(t)))];
  return { kind: 'unspecified', rule: 'ambiguous', ambiguous: true, focus };
}

const CORRECTION_RE = /^\s*(no\b|nope\b|not\b|don't|do not|stop\b|instead\b|actually\b|wrong\b|that's wrong|skip\b|ignore\b)/i;
const EXCLUDE_RE = /\b(skip|ignore|don't test|do not test|exclude|leave out)\b/i;

export function messageKind(text, explicit) {
  if (explicit !== undefined) {
    if (!['followup', 'correction'].includes(explicit)) throw new Error('kind must be followup or correction');
    return explicit;
  }
  return CORRECTION_RE.test(text) ? 'correction' : 'followup';
}

// ── Validation ───────────────────────────────────────────────────────────────

function optText(v, name, max = MAX_MESSAGE_CHARS) {
  if (v === undefined || v === null) return '';
  if (typeof v !== 'string') throw new Error(`${name} must be a string`);
  if (v.length > max) throw new Error(`${name} is longer than ${max} characters`);
  return v.trim();
}

export function validateRunId(runId) {
  if (typeof runId !== 'string' || !RUN_ID_REGEX.test(runId))
    throw new Error('Invalid run_id format (4–64 alphanumeric/hyphen/underscore)');
  return runId;
}

function boundedLimit(v, name) {
  const [lo, hi] = LIMIT_BOUNDS[name];
  if (!Number.isInteger(v) || v < lo || v > hi) throw new Error(`${name} must be an integer ${lo}–${hi}`);
  return v;
}

export function validateViewport(v) {
  if (v === undefined) return undefined;
  if (!v || typeof v !== 'object') throw new Error('viewport must be {width, height}');
  const { width, height } = v;
  if (!Number.isInteger(width) || width < 320 || width > 3840) throw new Error('viewport.width must be an integer 320–3840');
  if (!Number.isInteger(height) || height < 320 || height > 2160) throw new Error('viewport.height must be an integer 320–2160');
  return { width, height };
}

export function validateStartRequest(args = {}) {
  const hasUrl = typeof args.url === 'string' && args.url.trim() !== '';
  const hasSession = typeof args.session_id === 'string' && args.session_id.trim() !== '';
  if (hasUrl && hasSession) throw new Error('pass either url or session_id, not both');
  if (!hasUrl && !hasSession) throw new Error('one of url or session_id is required');
  const limits = { ...DEFAULT_LIMITS };
  for (const k of Object.keys(LIMIT_BOUNDS)) if (args[k] !== undefined) limits[k] = boundedLimit(args[k], k);
  return {
    targetUrl: hasUrl ? validateTargetUrl(args.url) : null,
    sessionId: hasSession ? validateRunId(args.session_id.trim()) : null,
    instruction: optText(args.instruction, 'instruction'),
    productDescription: optText(args.product_description, 'product_description', 4000),
    viewport: validateViewport(args.viewport),
    limits,
    maxDepth: args.max_depth,
    maxLinks: args.max_links,
  };
}

const ADJUSTMENT_KEYS = new Set(['instruction', 'viewport', 'case_ids', 'replan', 'max_cases', 'max_steps']);

export function validateAdjustments(adj) {
  if (adj === undefined || adj === null) return {};
  if (typeof adj !== 'object' || Array.isArray(adj)) throw new Error('adjustments must be an object');
  for (const k of Object.keys(adj)) if (!ADJUSTMENT_KEYS.has(k)) throw new Error(`unknown adjustment: ${k}`);
  const out = {};
  if (adj.instruction !== undefined) out.instruction = optText(adj.instruction, 'adjustments.instruction');
  if (adj.viewport !== undefined) out.viewport = validateViewport(adj.viewport);
  if (adj.case_ids !== undefined) {
    if (!Array.isArray(adj.case_ids) || adj.case_ids.length > 50 || !adj.case_ids.every((c) => typeof c === 'string' && /^TC-\d{1,3}$/.test(c)))
      throw new Error('adjustments.case_ids must be a list of up to 50 ids like "TC-3"');
    out.case_ids = adj.case_ids;
  }
  if (adj.replan !== undefined) {
    if (typeof adj.replan !== 'boolean') throw new Error('adjustments.replan must be true or false');
    out.replan = adj.replan;
  }
  for (const k of Object.keys(LIMIT_BOUNDS)) if (adj[k] !== undefined) out[k] = boundedLimit(adj[k], k);
  return out;
}

// ── Records ─────────────────────────────────────────────────────────────────

export const runKey = (runId) => `QARUN#${runId}`;
export const attemptKey = (runId, n) => `QARUN#${runId}#ATTEMPT#${n}`;
export const artifactKey = (prefix, runId, n, name) =>
  `${prefix}/${runId}/qa/attempt_${String(n).padStart(3, '0')}/${name}`;

export function newRunItem({ runId, req, targetUrl, now, nowS }) {
  const environment = { url: targetUrl, ...(req.viewport ? { viewport: req.viewport } : {}) };
  return {
    session_id: runKey(runId),
    kind: 'qa_run',
    run_id: runId,
    url: targetUrl,
    instruction: req.instruction,
    product_description: req.productDescription,
    environment,
    classification: classifyRequest(req.instruction),
    messages: [],
    attempts: [{ n: 1, adjustments: {}, requested_at: now }],
    limits: req.limits,
    crawl: { max_depth: req.maxDepth ?? 1, max_links: req.maxLinks ?? 8 },
    version: 1,
    created_at: now,
    updated_at: now,
    ttl: nowS + RUN_TTL_S,
  };
}

function attemptItem(runId, n, status, now, extra = {}) {
  return { session_id: attemptKey(runId, n), run_id: runId, attempt: n, status, updated_at: now, ...extra };
}

async function loadRun(deps, runId) {
  const run = await deps.getItem(runKey(runId));
  if (!run) throw new Error(`QA run not found: ${runId}`);
  return run;
}

/** Optimistic write: succeeds only if nobody changed the run since we read it. */
async function saveRun(deps, run, expectedVersion) {
  await deps.putItem({ ...run, version: expectedVersion + 1 }, { expectVersion: expectedVersion });
}

async function withRetry(fn) {
  try {
    return await fn();
  } catch (err) {
    if (err?.name !== 'ConditionalCheckFailedException') throw err;
    return fn();   // one re-read and retry; a second conflict surfaces to the caller
  }
}

// ── Tools ───────────────────────────────────────────────────────────────────

function nextStep(cls) {
  return cls.kind === 'specific'
    ? 'specific instruction: the agent runs it directly as the plan'
    : 'no specific instruction: the agent first generates and saves a test plan from the URL, crawl and context, then runs it';
}

export async function startQaRun(args, deps) {
  const req = validateStartRequest(args);
  let runId; let targetUrl; let newSession;
  if (req.sessionId) {
    const session = await deps.fetchSession(req.sessionId);
    if (!session.target_url) throw new Error('session has no target_url to test');
    runId = req.sessionId;
    targetUrl = validateTargetUrl(session.target_url);
    newSession = false;
  } else {
    runId = deps.newId();
    targetUrl = req.targetUrl;
    newSession = true;
  }
  await deps.assertCapacity();
  const now = deps.now();
  const nowS = Math.floor(Date.parse(now) / 1000);
  const run = newRunItem({ runId, req, targetUrl, now, nowS });
  // Persist the run (and attempt 1) before any instance exists.
  await deps.putItem(run, { mustNotExist: true }).catch((err) => {
    if (err?.name === 'ConditionalCheckFailedException')
      throw new Error(`QA run ${runId} already exists; use continue_qa_run or rerun_qa`);
    throw err;
  });
  await deps.putItem(attemptItem(runId, 1, 'REQUESTED', now, { requested_at: now }), {});
  const launched = await launch(deps, run, 1, newSession, now);
  return {
    run_id: runId,
    attempt: 1,
    status: launched.status,
    target_url: targetUrl,
    classification: run.classification,
    next: nextStep(run.classification),
    instance_id: launched.instance_id ?? null,
    message: `QA run started. Talk to it with continue_qa_run(run_id="${runId}", message=...), ` +
      `check it with get_qa_run, redo it with rerun_qa.`,
  };
}

async function launch(deps, run, n, newSession, now) {
  try {
    const { instance_id } = await deps.launchAttempt({
      runId: run.run_id, attempt: n, targetUrl: run.environment?.url || run.url,
      viewport: (run.attempts.find((a) => a.n === n)?.adjustments?.viewport) || run.environment?.viewport,
      maxDepth: run.crawl?.max_depth ?? 1, maxLinks: run.crawl?.max_links ?? 8, newSession,
    });
    await deps.putItem(attemptItem(run.run_id, n, 'REQUESTED', now, { requested_at: now, instance_id }), {});
    return { status: 'REQUESTED', instance_id };
  } catch (err) {
    await deps.putItem(attemptItem(run.run_id, n, 'FAILED', now, { reason: String(err.message).slice(0, 300) }), {});
    throw err;
  }
}

export async function continueQaRun(args = {}, deps) {
  const runId = validateRunId(args.run_id);
  const text = optText(args.message, 'message');
  if (!text) throw new Error('message is required');
  const kind = messageKind(text, args.kind);
  const cls = classifyRequest(text);
  const result = await withRetry(async () => {
    const run = await loadRun(deps, runId);
    if ((run.messages || []).length >= MAX_MESSAGES)
      throw new Error(`run ${runId} has ${MAX_MESSAGES} messages; start a new run`);
    const now = deps.now();
    const messages = [...(run.messages || []), { kind, text, at: now }];
    await saveRun(deps, { ...run, messages, updated_at: now }, run.version);
    return { count: messages.length, attempts: run.attempts.length };
  });
  let effect;
  if (EXCLUDE_RE.test(text)) effect = 'recorded as an exclusion: matching test cases are left out when the agent re-plans';
  else if (cls.kind === 'specific') effect = 'recorded as the latest specific instruction: rerun_qa runs it directly as the plan';
  else effect = 'recorded as context: rerun_qa re-plans with it (generate, save, then run)';
  return { run_id: runId, kind, message_count: result.count, classification: cls, effect,
    latest_attempt: result.attempts, next: `rerun_qa(run_id="${runId}") to apply` };
}

export async function rerunQa(args = {}, deps) {
  const runId = validateRunId(args.run_id);
  const adjustments = validateAdjustments(args.adjustments);
  await deps.assertCapacity();
  const { run, n, now } = await withRetry(async () => {
    const current = await loadRun(deps, runId);
    const last = current.attempts[current.attempts.length - 1];
    const lastRec = await deps.getItem(attemptKey(runId, last.n));
    const status = lastRec?.status || 'REQUESTED';
    const at = Date.parse(lastRec?.updated_at || last.requested_at || 0);
    if (ACTIVE.has(status) && Date.parse(deps.now()) - at < STALE_MS)
      throw new Error(`attempt ${last.n} is still ${status}; wait for it or check get_qa_run`);
    if (current.attempts.length >= MAX_ATTEMPTS) throw new Error(`run ${runId} reached ${MAX_ATTEMPTS} attempts; start a new run`);
    const ts = deps.now();
    const next = last.n + 1;
    const messages = [...(current.messages || [])];
    if (adjustments.instruction) {
      if (messages.length >= MAX_MESSAGES) throw new Error(`run ${runId} has ${MAX_MESSAGES} messages; start a new run`);
      messages.push({ kind: 'adjustment', text: adjustments.instruction, at: ts });
    }
    const updated = { ...current, messages, updated_at: ts,
      attempts: [...current.attempts, { n: next, adjustments, requested_at: ts }] };
    await saveRun(deps, updated, current.version);
    return { run: updated, n: next, now: ts };
  });
  await deps.putItem(attemptItem(runId, n, 'REQUESTED', now, { requested_at: now }), {});
  const launched = await launch(deps, run, n, false, now);
  const reuse = !adjustments.instruction && !adjustments.replan;
  return { run_id: runId, attempt: n, status: launched.status, adjustments,
    instance_id: launched.instance_id ?? null,
    next: reuse
      ? 'reuses the stored plan unless messages arrived since it was made; otherwise re-plans with them'
      : 're-plans with the new instruction and stored context, saves the plan, then runs it' };
}

export async function getQaRun(args = {}, deps) {
  const runId = validateRunId(args.run_id);
  const run = await loadRun(deps, runId);
  const recent = run.attempts.slice(-10);
  const attempts = await Promise.all(recent.map(async (a) => {
    const rec = (await deps.getItem(attemptKey(runId, a.n))) || {};
    return { n: a.n, adjustments: a.adjustments, requested_at: a.requested_at,
      status: rec.status || 'REQUESTED', reason: rec.reason || null, mode: rec.mode || null,
      cases: rec.cases ?? null, summary: rec.summary || null, release_state: rec.release_state || null };
  }));
  const latest = recent[recent.length - 1].n;
  const [plan, results, signoff] = await Promise.all(['plan.json', 'results.json', 'signoff.json']
    .map((name) => deps.getJson(artifactKey(deps.prefix, runId, latest, name))));
  return {
    run_id: runId,
    url: run.url,
    instruction: run.instruction,
    classification: run.classification,
    environment: run.environment,
    limits: run.limits,
    messages: run.messages,
    attempts,
    latest_attempt: {
      n: latest,
      plan: plan ? { mode: plan.mode, request: plan.request, notes: plan.notes,
        cases: plan.cases.map((c) => ({ id: c.id, title: c.title, priority: c.priority, source: c.source,
          status: c.status, expected: c.expected, steps: c.steps.length })) } : null,
      results: results ? { summary: results.summary, stopped: results.stopped || null,
        cases: results.cases.map((c) => ({ case_id: c.case_id, status: c.status, reason: c.reason || null })) } : null,
      signoff: signoff ? { release_state: signoff.release_state, signoffs: signoff.signoffs,
        findings: signoff.findings.map((f) => ({ id: f.id, title: f.title, severity: f.severity, status: f.status,
          needs_human: f.needs_human })), human_queue: signoff.human_queue } : null,
    },
  };
}

// ── Tool definitions (listed only when enabled) ──────────────────────────────

const viewportSchema = {
  type: 'object',
  properties: { width: { type: 'integer', minimum: 320, maximum: 3840 }, height: { type: 'integer', minimum: 320, maximum: 2160 } },
  required: ['width', 'height'],
};

export const QA_TOOL_DEFINITIONS = [
  {
    name: 'start_qa_run',
    description:
      'Start a QA run and get its run_id (the crawl session_id). With a specific instruction ' +
      '(e.g. "click Checkout then verify \\"Order placed\\"") the agent runs exactly that. Without one ' +
      'it first generates and saves a capped test plan from the URL, crawl, page content and product ' +
      'description, then runs it. Failed cases go through the L1 → L2 → human sign-off chain. ' +
      'Pass url to crawl fresh, or session_id to test an existing crawl session.',
    inputSchema: {
      type: 'object',
      properties: {
        url: { type: 'string', description: 'Site to test (http or https)' },
        session_id: { type: 'string', description: 'Existing crawl session to test; becomes the run_id' },
        instruction: { type: 'string', description: 'What to test. Leave empty to let the agent plan.' },
        product_description: { type: 'string', description: 'What the product is for; used when planning' },
        viewport: viewportSchema,
        max_cases: { type: 'integer', minimum: 1, maximum: 20, default: 8 },
        max_steps: { type: 'integer', minimum: 1, maximum: 200, default: 60 },
      },
    },
  },
  {
    name: 'continue_qa_run',
    description:
      'Tell a QA run something: a follow-up ("also check the pricing page"), a correction ' +
      '("no, skip the newsletter") or a new specific instruction. Stored in the run\'s session; ' +
      'applied on the next rerun_qa.',
    inputSchema: {
      type: 'object',
      properties: {
        run_id: { type: 'string' },
        message: { type: 'string' },
        kind: { type: 'string', enum: ['followup', 'correction'], description: 'Inferred when omitted' },
      },
      required: ['run_id', 'message'],
    },
  },
  {
    name: 'get_qa_run',
    description: 'Session of a QA run: instruction, messages, attempts, latest plan, results, findings and sign-off state.',
    inputSchema: { type: 'object', properties: { run_id: { type: 'string' } }, required: ['run_id'] },
  },
  {
    name: 'rerun_qa',
    description:
      'Run a QA run again with its stored context. No adjustments and no new messages: the same plan ' +
      'runs again. adjustments.instruction or replan: re-plan first. case_ids: redo only those cases. ' +
      'viewport, max_cases, max_steps override the run\'s settings for this attempt.',
    inputSchema: {
      type: 'object',
      properties: {
        run_id: { type: 'string' },
        adjustments: {
          type: 'object',
          properties: {
            instruction: { type: 'string' },
            viewport: viewportSchema,
            case_ids: { type: 'array', items: { type: 'string' } },
            replan: { type: 'boolean' },
            max_cases: { type: 'integer', minimum: 1, maximum: 20 },
            max_steps: { type: 'integer', minimum: 1, maximum: 200 },
          },
        },
      },
      required: ['run_id'],
    },
  },
];
