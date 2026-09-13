// Runs in its own process. Exercises withObservability with the shared
// gate "on" (OBSERVATORY_METRICS_TABLE set) and, when @weaveaijs/mcp-observatory
// is actually installed (as it will be after `npm install`, same as CI's
// `sam build` step), against the real InvocationWrapper — guarding against
// mcp-observatory@0.3.0's known bug where a rejected `call` gets its error
// replaced by an internal TypeError (see observatory.mjs for detail).
import { test } from 'node:test';
import assert from 'node:assert/strict';

process.env.OBSERVATORY_METRICS_TABLE = 'test-observatory-table';

const { DynamoDBClient } = await import('@aws-sdk/client-dynamodb');
const puts = [];
DynamoDBClient.prototype.send = async (command) => {
  puts.push(command);
  return {};
};

const { withObservability } = await import('../observatory.mjs');

let observatoryAvailable = true;
try {
  await import('@weaveaijs/mcp-observatory');
} catch {
  observatoryAvailable = false;
}

test('withObservability returns the real output on success and records a span', { skip: !observatoryAvailable && 'package not installed' }, async () => {
  puts.length = 0;
  const result = await withObservability('get_metrics', { session_id: 'abc123' }, async () => ({ total_states: 3 }));

  assert.deepEqual(result, { total_states: 3 });
  assert.equal(puts.length, 1, 'exactly one telemetry record should be written');
});

test('withObservability preserves the tool\'s real error message', { skip: !observatoryAvailable && 'package not installed' }, async () => {
  puts.length = 0;
  await assert.rejects(
    () => withObservability('crawl_url', { url: 'bad' }, async () => {
      throw new Error('url must be a valid http(s) URL');
    }),
    (err) => {
      assert.equal(err.message, 'url must be a valid http(s) URL');
      return true;
    },
  );
  assert.equal(puts.length, 1, 'a telemetry record should still be written for a failed call');
});
