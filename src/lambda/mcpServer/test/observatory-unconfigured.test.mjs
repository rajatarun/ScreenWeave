// Runs in its own process (node's test runner spawns one process per file),
// so this is the one place we can rely on OBSERVATORY_METRICS_TABLE being
// unset before observatory.mjs is first imported.
import { test } from 'node:test';
import assert from 'node:assert/strict';

delete process.env.OBSERVATORY_METRICS_TABLE;

const { withObservability } = await import('../observatory.mjs');

test('withObservability is a no-op passthrough when OBSERVATORY_METRICS_TABLE is unset', async () => {
  let calls = 0;
  const result = await withObservability('crawl_url', { url: 'https://example.com' }, async () => {
    calls += 1;
    return { session_id: 'abc123', status: 'RUNNING' };
  });

  assert.equal(calls, 1, 'call() must run exactly once');
  assert.deepEqual(result, { session_id: 'abc123', status: 'RUNNING' });
});

test('withObservability propagates errors unchanged when unconfigured', async () => {
  await assert.rejects(
    () => withObservability('crawl_url', { url: 'not-a-url' }, async () => {
      throw new Error('url must be a valid http(s) URL');
    }),
    /url must be a valid http\(s\) URL/,
  );
});
