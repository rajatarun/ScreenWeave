// MCP protocol version negotiation, driven through the real handler.
//
// `initialize` answered with a hardcoded '2024-11-05' whatever the client
// asked for. Bedrock AgentCore Gateway handshakes an MCP server while it
// *creates* the GatewayTarget, so that reply failed a CloudFormation deploy in
// another repository rather than one tool call here:
//
//   GatewayTarget ... failed to stabilize, status: FAILED, reason: Failed to
//   connect and fetch tools from the provided MCP target server.
//   Error - Unsupported protocol version
//
// These drive `handler` over a real API Gateway v2 event rather than calling a
// helper, because the bug was reachable only through the dispatcher and a
// helper test would have passed against the broken version.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { handler } from '../index.mjs';

const SUPPORTED = ['2024-11-05', '2025-03-26', '2025-06-18'];

async function initialize(protocolVersion) {
  const body = {
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      ...(protocolVersion === undefined ? {} : { protocolVersion }),
      capabilities: {},
      clientInfo: { name: 'test-client', version: '1.0.0' },
    },
  };
  const res = await handler({
    requestContext: { http: { method: 'POST' } },
    body: JSON.stringify(body),
  });
  assert.equal(res.statusCode, 200);
  // The transport is SSE: `event: message\ndata: <json>\n\n`
  const line = res.body.split('\n').find((l) => l.startsWith('data: '));
  assert.ok(line, `no data frame in response: ${JSON.stringify(res.body)}`);
  return JSON.parse(line.slice('data: '.length));
}

for (const version of SUPPORTED) {
  test(`initialize echoes the supported version the client asked for: ${version}`, async () => {
    const res = await initialize(version);
    assert.equal(res.error, undefined);
    assert.equal(
      res.result.protocolVersion,
      version,
      'a client that speaks this revision must not be told the server speaks another',
    );
  });
}

test('an unknown version is answered with the newest the server supports, not echoed', async () => {
  // Echoing would claim a revision nobody here has read. The fallback gives
  // the client a real version to accept or reject.
  const res = await initialize('2099-01-01');
  assert.equal(res.result.protocolVersion, SUPPORTED[SUPPORTED.length - 1]);
});

test('a client that sends no version still gets a usable answer', async () => {
  const res = await initialize(undefined);
  assert.ok(SUPPORTED.includes(res.result.protocolVersion));
});

test('the default is the newest revision, not the oldest', async () => {
  // The regression: a server whose default is the 2024 revision fails every
  // modern client's handshake even though its JSON-RPC surface is compatible.
  const res = await initialize('2099-01-01');
  assert.notEqual(res.result.protocolVersion, '2024-11-05');
});

test('initialize still reports the tools capability and server identity', async () => {
  const res = await initialize('2025-06-18');
  assert.deepEqual(res.result.capabilities, { tools: {} });
  assert.equal(res.result.serverInfo.name, 'screenweave');
});

test('tools/list answers with a non-empty catalogue', async () => {
  // A gateway fetches tools straight after initialize; an empty list is the
  // handshake succeeding and the target still being useless.
  const res = await handler({
    requestContext: { http: { method: 'POST' } },
    body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' }),
  });
  const line = res.body.split('\n').find((l) => l.startsWith('data: '));
  const parsed = JSON.parse(line.slice('data: '.length));
  assert.ok(Array.isArray(parsed.result.tools));
  assert.ok(parsed.result.tools.length > 0, 'no tools to fetch');
  for (const tool of parsed.result.tools) {
    assert.ok(tool.name, 'a tool with no name cannot be called');
    assert.ok(tool.inputSchema, `${tool.name} has no inputSchema`);
  }
});
