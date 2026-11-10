/**
 * Through the Lambda handler: with QA_AGENT_ENABLED unset the catalogue is
 * exactly what it was (7 tools) and QA tool calls are refused before any AWS
 * call; with it set the four QA tools are listed.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

process.env.AUTH_MODE = 'audit';
const { handler } = await import('../index.mjs');

function rpc(method, params) {
  return { requestContext: { http: { method: 'POST' } }, headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) };
}
const body = (res) => JSON.parse(res.body.split('data: ')[1]);

test('default: QA tools are neither listed nor callable', async () => {
  delete process.env.QA_AGENT_ENABLED;
  const names = body(await handler(rpc('tools/list', {}))).result.tools.map((t) => t.name);
  assert.equal(names.length, 7);
  assert.ok(!names.includes('start_qa_run'));
  const res = body(await handler(rpc('tools/call', { name: 'start_qa_run', arguments: { url: 'https://a.com' } })));
  assert.match(res.error.message, /not enabled/);
});

test('enabled: the four QA tools are listed after the existing ones', async () => {
  process.env.QA_AGENT_ENABLED = 'true';
  try {
    const names = body(await handler(rpc('tools/list', {}))).result.tools.map((t) => t.name);
    assert.deepEqual(names.slice(7), ['start_qa_run', 'continue_qa_run', 'get_qa_run', 'rerun_qa']);
    const bad = body(await handler(rpc('tools/call', { name: 'get_qa_run', arguments: { run_id: '../etc' } })));
    assert.match(bad.error.message, /Invalid run_id/);
  } finally {
    delete process.env.QA_AGENT_ENABLED;
  }
});
