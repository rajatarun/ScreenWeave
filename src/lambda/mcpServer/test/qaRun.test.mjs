import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  ACTION_VERBS, GENERIC_WORDS, MAX_MESSAGES, QA_TOOL_DEFINITIONS, QA_TOOL_NAMES, TEST_VERBS,
  artifactKey, attemptKey, classifyRequest, continueQaRun, getQaRun, messageKind, qaAgentEnabled,
  rerunQa, runKey, startQaRun, validateAdjustments, validateStartRequest,
} from '../qaRun.mjs';
import { TOOL_SCOPES } from '../auth.mjs';

const CONTRACT = JSON.parse(readFileSync(new URL('../../../../contracts/qa_request_classification.json', import.meta.url)));

// ── An in-memory sessions table + bucket with the same conditional semantics ──
function fakeDeps({ sessions = {}, capacity = true, launchFails = false } = {}) {
  const table = new Map(Object.entries(sessions).map(([id, v]) => [`SESSION#${id}`, v]));
  const objects = new Map();
  const launches = [];
  const writes = [];
  let clock = Date.parse('2026-11-10T15:00:00.000Z');
  let ids = 0;
  const deps = {
    prefix: 'screenweave',
    now: () => new Date(clock += 1000).toISOString(),
    newId: () => `run-${String(++ids).padStart(4, '0')}`,
    assertCapacity: async () => { if (!capacity) throw new Error('3 crawls are already running (limit 3)'); },
    fetchSession: async (id) => {
      const s = table.get(`SESSION#${id}`);
      if (!s) throw new Error(`Session not found: ${id}`);
      return s;
    },
    getItem: async (pk) => (table.has(pk) ? structuredClone(table.get(pk)) : null),
    putItem: async (item, { mustNotExist, expectVersion } = {}) => {
      const cur = table.get(item.session_id);
      const fail = () => Object.assign(new Error('conditional'), { name: 'ConditionalCheckFailedException' });
      if (mustNotExist && cur) throw fail();
      if (expectVersion !== undefined && cur?.version !== expectVersion) throw fail();
      writes.push(item.session_id);
      table.set(item.session_id, structuredClone(item));
    },
    getJson: async (key) => (objects.has(key) ? structuredClone(objects.get(key)) : null),
    launchAttempt: async (a) => {
      if (launchFails) throw new Error('InsufficientInstanceCapacity');
      launches.push({ ...a, writesBefore: [...writes] });
      return { instance_id: `i-${launches.length}` };
    },
  };
  return { deps, table, objects, launches, writes, advance: (ms) => { clock += ms; } };
}

// ── Flag, catalogue, scopes ──────────────────────────────────────────────────

test('the QA tools are off unless QA_AGENT_ENABLED is exactly true', () => {
  assert.equal(qaAgentEnabled({}), false);
  assert.equal(qaAgentEnabled({ QA_AGENT_ENABLED: 'false' }), false);
  assert.equal(qaAgentEnabled({ QA_AGENT_ENABLED: '1' }), false);
  assert.equal(qaAgentEnabled({ QA_AGENT_ENABLED: 'TRUE ' }), true);
});

test('every QA tool has a definition and a scope', () => {
  assert.deepEqual(QA_TOOL_DEFINITIONS.map((t) => t.name), QA_TOOL_NAMES);
  assert.deepEqual(QA_TOOL_NAMES.map((n) => TOOL_SCOPES[n]), ['execute', 'execute', 'read', 'execute']);
});

// ── Shared classification rule ───────────────────────────────────────────────

test('word lists match the contract', () => {
  assert.deepEqual([...GENERIC_WORDS].sort(), [...CONTRACT.generic_words].sort());
  assert.deepEqual([...TEST_VERBS].sort(), [...CONTRACT.test_verbs].sort());
  assert.deepEqual([...ACTION_VERBS].sort(), [...CONTRACT.action_verbs].sort());
});

test('every contract case classifies the same as in Python', () => {
  for (const c of CONTRACT.cases) {
    const got = classifyRequest(c.text);
    assert.deepEqual([got.kind, got.rule], [c.kind, c.rule], c.text);
  }
});

test('message kind is inferred or validated', () => {
  assert.equal(messageKind('no, skip the newsletter'), 'correction');
  assert.equal(messageKind('Actually test the pricing page'), 'correction');
  assert.equal(messageKind('also check the footer'), 'followup');
  assert.equal(messageKind('anything', 'correction'), 'correction');
  assert.throws(() => messageKind('x', 'command'), /followup or correction/);
});

// ── Validation ───────────────────────────────────────────────────────────────

test('start needs exactly one of url or session_id and bounded limits', () => {
  assert.throws(() => validateStartRequest({}), /one of url or session_id/);
  assert.throws(() => validateStartRequest({ url: 'https://a.com', session_id: 'abcd' }), /not both/);
  assert.throws(() => validateStartRequest({ url: 'http://169.254.169.254/' }));
  assert.throws(() => validateStartRequest({ url: 'https://a.com', max_cases: 21 }), /max_cases/);
  assert.throws(() => validateStartRequest({ url: 'https://a.com', viewport: { width: 10, height: 800 } }), /viewport.width/);
  assert.throws(() => validateStartRequest({ url: 'https://a.com', instruction: 'x'.repeat(2001) }), /longer/);
  const r = validateStartRequest({ url: 'https://a.com', max_steps: 30 });
  assert.deepEqual(r.limits, { max_cases: 8, max_steps: 30 });
});

test('adjustments are a closed set', () => {
  assert.deepEqual(validateAdjustments(undefined), {});
  assert.throws(() => validateAdjustments({ shell: 'rm' }), /unknown adjustment/);
  assert.throws(() => validateAdjustments({ case_ids: ['TC-1; rm'] }), /case_ids/);
  assert.throws(() => validateAdjustments({ replan: 'yes' }), /replan/);
  assert.deepEqual(validateAdjustments({ case_ids: ['TC-2'], replan: false, viewport: { width: 390, height: 844 } }),
    { case_ids: ['TC-2'], replan: false, viewport: { width: 390, height: 844 } });
});

// ── start_qa_run ─────────────────────────────────────────────────────────────

test('start: a new crawl session id becomes the run id, persisted before launch', async () => {
  const f = fakeDeps();
  const out = await startQaRun({ url: 'https://shop.example.com/', instruction: 'test checkout with an expired card' }, f.deps);
  assert.equal(out.run_id, 'run-0001');
  assert.equal(out.attempt, 1);
  assert.equal(out.classification.kind, 'specific');
  assert.match(out.next, /runs it directly/);
  const run = f.table.get(runKey('run-0001'));
  assert.equal(run.instruction, 'test checkout with an expired card');
  assert.deepEqual(run.attempts.map((a) => a.n), [1]);
  assert.equal(f.launches.length, 1);
  assert.ok(f.launches[0].writesBefore.includes(runKey('run-0001')), 'run record written before the instance');
  assert.equal(f.launches[0].newSession, true);
  assert.equal(f.table.get(attemptKey('run-0001', 1)).instance_id, 'i-1');
});

test('start without an instruction says it will plan first', async () => {
  const f = fakeDeps();
  const out = await startQaRun({ url: 'https://shop.example.com/' }, f.deps);
  assert.equal(out.classification.rule, 'empty');
  assert.match(out.next, /first generates and saves a test plan/);
});

test('start on an existing crawl session reuses its id', async () => {
  const f = fakeDeps({ sessions: { 'sess-42': { status: 'COMPLETED', target_url: 'https://shop.example.com/' } } });
  const out = await startQaRun({ session_id: 'sess-42', viewport: { width: 390, height: 844 } }, f.deps);
  assert.equal(out.run_id, 'sess-42');
  assert.equal(f.launches[0].newSession, false);
  assert.deepEqual(f.launches[0].viewport, { width: 390, height: 844 });
  await assert.rejects(startQaRun({ session_id: 'sess-42' }, f.deps), /already exists/);
  await assert.rejects(startQaRun({ session_id: 'nope-1' }, f.deps), /Session not found/);
});

test('start refuses at capacity before writing anything, and records a failed launch', async () => {
  const busy = fakeDeps({ capacity: false });
  await assert.rejects(startQaRun({ url: 'https://a.com/' }, busy.deps), /already running/);
  assert.equal(busy.writes.length, 0);
  const broken = fakeDeps({ launchFails: true });
  await assert.rejects(startQaRun({ url: 'https://a.com/' }, broken.deps), /InsufficientInstanceCapacity/);
  assert.equal(broken.table.get(attemptKey('run-0001', 1)).status, 'FAILED');
});

// ── continue_qa_run ──────────────────────────────────────────────────────────

test('continue stores follow-ups and corrections in the session', async () => {
  const f = fakeDeps();
  const { run_id } = await startQaRun({ url: 'https://shop.example.com/' }, f.deps);
  const a = await continueQaRun({ run_id, message: 'no, skip the newsletter' }, f.deps);
  assert.equal(a.kind, 'correction');
  assert.match(a.effect, /exclusion/);
  const b = await continueQaRun({ run_id, message: 'click "Yearly" then verify "Save 20%"' }, f.deps);
  assert.equal(b.kind, 'followup');
  assert.match(b.effect, /specific instruction/);
  const run = f.table.get(runKey(run_id));
  assert.deepEqual(run.messages.map((m) => [m.kind, m.text]), [
    ['correction', 'no, skip the newsletter'], ['followup', 'click "Yearly" then verify "Save 20%"']]);
  assert.equal(run.version, 3);
  assert.equal(f.launches.length, 1, 'talking does not launch anything');
});

test('continue validates and caps', async () => {
  const f = fakeDeps();
  await assert.rejects(continueQaRun({ run_id: '../x', message: 'hi' }, f.deps), /Invalid run_id/);
  await assert.rejects(continueQaRun({ run_id: 'run-9999', message: 'hi' }, f.deps), /QA run not found/);
  const { run_id } = await startQaRun({ url: 'https://shop.example.com/' }, f.deps);
  await assert.rejects(continueQaRun({ run_id, message: '   ' }, f.deps), /message is required/);
  const run = f.table.get(runKey(run_id));
  run.messages = Array.from({ length: MAX_MESSAGES }, (_, i) => ({ kind: 'followup', text: `m${i}` }));
  await assert.rejects(continueQaRun({ run_id, message: 'one more' }, f.deps), /start a new run/);
});

test('a concurrent write is retried once on the fresh record', async () => {
  const f = fakeDeps();
  const { run_id } = await startQaRun({ url: 'https://shop.example.com/' }, f.deps);
  const realGet = f.deps.getItem;
  let bumped = false;
  f.deps.getItem = async (pk) => {
    const item = await realGet(pk);
    if (!bumped && pk === runKey(run_id)) {            // someone else writes in between
      bumped = true;
      const cur = f.table.get(pk);
      f.table.set(pk, { ...cur, version: cur.version + 1, messages: [{ kind: 'followup', text: 'theirs' }] });
    }
    return item;
  };
  await continueQaRun({ run_id, message: 'mine' }, f.deps);
  assert.deepEqual(f.table.get(runKey(run_id)).messages.map((m) => m.text), ['theirs', 'mine']);
});

// ── rerun_qa ─────────────────────────────────────────────────────────────────

test('rerun refuses while the last attempt is active, then adds an attempt to the same run', async () => {
  const f = fakeDeps();
  const { run_id } = await startQaRun({ url: 'https://shop.example.com/' }, f.deps);
  await assert.rejects(rerunQa({ run_id }, f.deps), /still REQUESTED/);
  f.table.get(attemptKey(run_id, 1)).status = 'COMPLETED';
  const out = await rerunQa({ run_id, adjustments: { instruction: 'click Yearly', viewport: { width: 390, height: 844 } } }, f.deps);
  assert.equal(out.attempt, 2);
  assert.match(out.next, /re-plans/);
  const run = f.table.get(runKey(run_id));
  assert.deepEqual(run.attempts.map((a) => a.n), [1, 2]);
  assert.deepEqual(run.attempts[1].adjustments.viewport, { width: 390, height: 844 });
  assert.equal(run.messages.at(-1).kind, 'adjustment');
  assert.deepEqual(f.launches.map((l) => [l.runId, l.attempt, l.newSession]), [[run_id, 1, true], [run_id, 2, false]]);
  assert.deepEqual(f.launches[1].viewport, { width: 390, height: 844 });
});

test('a plain rerun says it reuses the stored plan; a stale attempt no longer blocks', async () => {
  const f = fakeDeps();
  const { run_id } = await startQaRun({ url: 'https://shop.example.com/' }, f.deps);
  f.advance(2 * 60 * 60 * 1000);
  const out = await rerunQa({ run_id }, f.deps);
  assert.match(out.next, /reuses the stored plan/);
});

// ── get_qa_run ───────────────────────────────────────────────────────────────

test('get returns the session, attempts and the latest plan, results and sign-off', async () => {
  const f = fakeDeps();
  const { run_id } = await startQaRun({ url: 'https://shop.example.com/', instruction: 'QA this site' }, f.deps);
  await continueQaRun({ run_id, message: 'skip the footer' }, f.deps);
  f.table.set(attemptKey(run_id, 1), { ...f.table.get(attemptKey(run_id, 1)), status: 'COMPLETED',
    mode: 'planned', cases: 1, release_state: 'pending', summary: { passed: 0, failed: 1 } });
  f.objects.set(artifactKey('screenweave', run_id, 1, 'plan.json'), { mode: 'planned', request: 'QA this site', notes: [],
    cases: [{ id: 'TC-1', title: 'Page loads: /', priority: 'P1', source: 'page-content', status: 'ready', expected: 'ok', steps: [{}, {}] }] });
  f.objects.set(artifactKey('screenweave', run_id, 1, 'results.json'), { summary: { passed: 0, failed: 1 },
    cases: [{ case_id: 'TC-1', status: 'failed' }] });
  f.objects.set(artifactKey('screenweave', run_id, 1, 'signoff.json'), { release_state: 'pending', signoffs: {},
    findings: [{ id: 'f_1', title: 'TC-1 failed', severity: 'major', status: 'escalated', needs_human: true }], human_queue: ['f_1'] });
  const out = await getQaRun({ run_id }, f.deps);
  assert.equal(out.instruction, 'QA this site');
  assert.equal(out.messages[0].text, 'skip the footer');
  assert.equal(out.attempts[0].status, 'COMPLETED');
  assert.deepEqual(out.latest_attempt.plan.cases[0], { id: 'TC-1', title: 'Page loads: /', priority: 'P1',
    source: 'page-content', status: 'ready', expected: 'ok', steps: 2 });
  assert.equal(out.latest_attempt.results.cases[0].status, 'failed');
  assert.deepEqual(out.latest_attempt.signoff.human_queue, ['f_1']);
});

test('get before the agent has written anything shows the request only', async () => {
  const f = fakeDeps();
  const { run_id } = await startQaRun({ url: 'https://shop.example.com/' }, f.deps);
  const out = await getQaRun({ run_id }, f.deps);
  assert.equal(out.attempts[0].status, 'REQUESTED');
  assert.equal(out.latest_attempt.plan, null);
  assert.equal(out.latest_attempt.signoff, null);
});
