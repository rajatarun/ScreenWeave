// ERROR lines raise the ops alarm (infra/main-stack.yaml ErrorLinesAlarm), so
// only this service failing may log one. A caller's bad input is WARN:
// otherwise anyone who can reach /mcp could page the operator.
import { test } from 'node:test';
import assert from 'node:assert/strict';

const lines = [];
const original = console.log;

async function call(body) {
  const { handler } = await import('../index.mjs');
  lines.length = 0;
  console.log = (line) => lines.push(String(line));
  try {
    return await handler({ requestContext: { http: { method: 'POST' } }, headers: {}, body });
  } finally {
    console.log = original;
  }
}

const levels = () => lines.map((l) => JSON.parse(l).level);

test('a malformed body is WARN, not ERROR', async () => {
  await call('{not json');
  assert.ok(levels().includes('WARN'));
  assert.ok(!levels().includes('ERROR'));
});

for (const [name, args] of [
  ['crawl_url', { url: 'http://169.254.169.254/' }],
  ['get_session_status', {}],
  ['get_metrics', { session_id: 'bad id!' }],
  ['no_such_tool', {}],
]) {
  test(`a rejected ${name} call is WARN, not ERROR`, async () => {
    const res = await call(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }));
    assert.match(res.body, /"error"/);
    assert.ok(!levels().includes('ERROR'), lines.join('\n'));
  });
}
