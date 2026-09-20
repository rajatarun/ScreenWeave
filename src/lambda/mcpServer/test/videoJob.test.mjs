import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  validateVideoRequest,
  buildVideoJobItem,
  buildCompletionEvent,
  startVideoJob,
  DEFAULT_DURATION_S,
  DEFAULT_SCALE,
} from '../videoJob.mjs';

// ── input ────────────────────────────────────────────────────────────────────

test('a url alone is accepted and defaults are applied', () => {
  const r = validateVideoRequest({ url: 'https://example.com' });
  assert.equal(r.targetUrl, 'https://example.com');
  assert.equal(r.sourceSessionId, null);
  assert.equal(r.durationS, DEFAULT_DURATION_S);
  assert.equal(r.scale, DEFAULT_SCALE);
});

test('a session_id alone is accepted', () => {
  const r = validateVideoRequest({ session_id: 'sess-1' });
  assert.equal(r.sourceSessionId, 'sess-1');
  assert.equal(r.targetUrl, null);
});

test('both url and session_id is rejected as ambiguous', () => {
  // Which page is the subject would be undefined.
  assert.throws(() => validateVideoRequest({ url: 'https://a.com', session_id: 's' }), /not both/);
});

test('neither url nor session_id is rejected', () => {
  assert.throws(() => validateVideoRequest({}), /one of url or session_id/);
});

test('a non-http url is rejected', () => {
  for (const url of ['ftp://x.com', 'javascript:alert(1)', 'example.com', '  '])
    assert.throws(() => validateVideoRequest({ url }), /required|valid http/);
});

test('duration is bounded and must be a whole number of seconds', () => {
  for (const duration_s of [4, 31, 20.5, '20', null])
    assert.throws(() => validateVideoRequest({ url: 'https://a.com', duration_s }), /duration_s/);
  assert.equal(validateVideoRequest({ url: 'https://a.com', duration_s: 5 }).durationS, 5);
  assert.equal(validateVideoRequest({ url: 'https://a.com', duration_s: 30 }).durationS, 30);
});

test('scale is restricted to the capture factors the engine supports', () => {
  for (const scale of [0, 4, 2.5, '3'])
    assert.throws(() => validateVideoRequest({ url: 'https://a.com', scale }), /scale/);
  assert.equal(validateVideoRequest({ url: 'https://a.com', scale: 1 }).scale, 1);
});

test('whitespace around inputs is trimmed rather than stored', () => {
  const r = validateVideoRequest({ url: '  https://example.com  ' });
  assert.equal(r.targetUrl, 'https://example.com');
});

// ── the job row ──────────────────────────────────────────────────────────────

test('a job is keyed like a crawl so get_session_status polls it unchanged', () => {
  const item = buildVideoJobItem({
    jobId: 'job-1',
    request: validateVideoRequest({ url: 'https://a.com' }),
    now: 'NOW', ttl: 1, status: 'PENDING',
  });
  assert.equal(item.session_id, 'SESSION#job-1');
  assert.equal(item.kind, 'video');
  assert.equal(item.status, 'PENDING');
});

test('a reason is recorded only when there is one', () => {
  const base = { jobId: 'j', request: validateVideoRequest({ url: 'https://a.com' }), now: 'N', ttl: 1 };
  assert.equal('status_reason' in buildVideoJobItem({ ...base, status: 'PENDING' }), false);
  assert.match(buildVideoJobItem({ ...base, status: 'NOT_CONFIGURED', reason: 'no worker' }).status_reason, /no worker/);
});

// ── starting a job ───────────────────────────────────────────────────────────

function harness({ workerFunction = '' } = {}) {
  const puts = [], invokes = [];
  return {
    puts, invokes,
    run: (overrides = {}) => startVideoJob({
      request: validateVideoRequest({ url: 'https://a.com' }),
      jobId: 'job-1', now: 'NOW', ttl: 1,
      workerFunction,
      putItem: async (i) => { puts.push(i); },
      invokeWorker: async (i) => { invokes.push(i); },
      ...overrides,
    }),
  };
}

test('with no worker configured the job is recorded but nothing is dispatched', async () => {
  const h = harness();
  const out = await h.run();
  assert.equal(out.status, 'NOT_CONFIGURED');
  assert.match(out.reason, /No render worker is configured/);
  assert.equal(h.puts.length, 1);
  assert.equal(h.invokes.length, 0);
});

test('an unconfigured job never invents an artifact url', async () => {
  // A fabricated S3 link is indistinguishable from a real one until someone
  // tries to play it.
  const out = await harness().run();
  assert.equal(JSON.stringify(out).includes('s3://'), false);
  assert.equal('artifacts' in out, false);
});

test('with a worker configured the job is dispatched to it', async () => {
  const h = harness({ workerFunction: 'render-fn' });
  const out = await h.run();
  assert.equal(out.status, 'PENDING');
  assert.equal(h.invokes.length, 1);
  assert.equal(h.invokes[0].function_name, 'render-fn');
  assert.equal(h.invokes[0].payload.job_id, 'job-1');
  assert.equal(h.invokes[0].payload.duration_s, DEFAULT_DURATION_S);
});

test('the job is written before the worker is invoked', async () => {
  // An invoke that fails after a write leaves a job someone can retry; a
  // write that fails after an invoke leaves a render nothing is tracking.
  const order = [];
  await startVideoJob({
    request: validateVideoRequest({ url: 'https://a.com' }),
    jobId: 'j', now: 'N', ttl: 1, workerFunction: 'fn',
    putItem: async () => { order.push('put'); },
    invokeWorker: async () => { order.push('invoke'); },
  });
  assert.deepEqual(order, ['put', 'invoke']);
});

test('a failed write stops the job rather than dispatching anyway', async () => {
  const invokes = [];
  await assert.rejects(() => startVideoJob({
    request: validateVideoRequest({ url: 'https://a.com' }),
    jobId: 'j', now: 'N', ttl: 1, workerFunction: 'fn',
    putItem: async () => { throw new Error('ddb down'); },
    invokeWorker: async () => { invokes.push(1); },
  }), /ddb down/);
  assert.equal(invokes.length, 0);
});

test('the caller is told how to poll', async () => {
  assert.equal((await harness().run()).poll_with, 'get_session_status');
});

// ── the completion envelope ──────────────────────────────────────────────────

test('completion uses one envelope for both engines', () => {
  const e = buildCompletionEvent({
    jobId: 'j', mode: 'generate_apple_video', status: 'COMPLETED',
    artifacts: [{ kind: 'showcase_video', s3_uri: 's3://b/v.mp4', content_type: 'video/mp4' }],
    startedAt: 'A', finishedAt: 'B',
  });
  assert.equal(e.event, 'screenweave.job.completed');
  assert.equal(e.mode, 'generate_apple_video');
  assert.equal(e.artifacts[0].kind, 'showcase_video');
  assert.equal(e.error, null);
});

test('a failed job still carries whatever it produced', () => {
  // A partial QA report is more useful than nothing.
  const e = buildCompletionEvent({
    jobId: 'j', mode: 'qa_audit', status: 'FAILED',
    artifacts: [{ kind: 'qa_report', s3_uri: 's3://b/r.json' }],
    error: 'render timed out', startedAt: 'A', finishedAt: 'B',
  });
  assert.equal(e.status, 'FAILED');
  assert.equal(e.artifacts.length, 1);
  assert.equal(e.error, 'render timed out');
});

test('artifacts is a list so a new kind needs no schema version', () => {
  const e = buildCompletionEvent({ jobId: 'j', mode: 'qa_audit', status: 'COMPLETED', startedAt: 'A', finishedAt: 'B' });
  assert.ok(Array.isArray(e.artifacts));
  assert.equal(e.artifacts.length, 0);
});
