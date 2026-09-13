// Runs in its own process. Conformance test for the shared OBSERVATORY_METRICS
// table contract (contracts/observatory_metrics_item.json, canonical home
// mcp-observatory).
//
// The shared table has several writers in two languages and several dashboard
// readers, none of which can see each other's code. The item shape is therefore
// a cross-repository interface, and the only thing keeping it honest is that
// each repository asserts its own writer against the vendored contract. This
// file does that for ScreenWeave: it drives the REAL persistSpan/export path
// through `withObservability` with the DynamoDB client mocked, captures the
// actual PutItemCommand Item, and checks it with the same I1-I4 checks the
// Python siblings run (contracts/conformance.mjs is a port of conformance.py).
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
const { checkItem, readersFor, loadContract } = await import('../../../../contracts/conformance.mjs');

/** Drive the real telemetry path once and hand back the Item it actually wrote. */
async function captureEmittedItem(toolName = 'crawl_url', args = { url: 'https://example.com' }) {
  puts.length = 0;
  await withObservability(toolName, args, async () => ({ ok: true }));
  assert.equal(puts.length, 1, 'expected exactly one PutItemCommand');
  return puts[0].input.Item;
}

test('the span ScreenWeave actually writes satisfies contract invariants I1-I4', async () => {
  const item = await captureEmittedItem();

  // checkItem unwraps the low-level AttributeValue shape ({ S: "..." }) that
  // this writer emits, so the same function checks Python and Node writers.
  const problems = checkItem(item);
  assert.deepEqual(problems, [], `contract violations: ${problems.join(' | ')}`);
});

test('I1: key attributes are the lower-case pk/sk the table declares', async () => {
  const item = await captureEmittedItem();

  assert.ok(item.pk?.S, "pk must be present and a string attribute");
  assert.ok(item.sk?.S, "sk must be present and a string attribute");
  // A writer that spells these PK/SK gets a ValidationException from PutItem —
  // which observatory.mjs deliberately swallows, so it would look like success
  // while writing nothing at all. That is exactly why this is asserted here.
  assert.ok(!('PK' in item) && !('SK' in item), 'key attributes must not be upper case');
});

test('I3: sk is {iso8601}#{trace_id} so readers can range-query on time', async () => {
  const item = await captureEmittedItem();

  const [timestamp, traceId] = item.sk.S.split('#');
  assert.ok(traceId, 'sk must carry a trace id after the separator');
  assert.equal(new Date(timestamp).toISOString(), timestamp, 'sk must lead with a UTC ISO 8601 instant');
});

test('I4: ttl is present and in the future so rows expire from the shared table', async () => {
  const item = await captureEmittedItem();

  assert.ok(item.ttl?.N, 'ttl must be present as a numeric attribute');
  assert.ok(Number(item.ttl.N) > Math.floor(Date.now() / 1000), 'ttl must be a future Unix epoch');
});

// ---------------------------------------------------------------------------
// Finding 2 — pinned as current truth, NOT as desired behaviour.
// ---------------------------------------------------------------------------

test('KNOWN GAP: ScreenWeave writes to a pk that no dashboard reader enumerates', async () => {
  // observatory.mjs writes `pk: OBSERVATORY#${toolName}`. The OBSERVATORY
  // namespace IS registered (so I2 above passes and nothing errors anywhere),
  // but the contract declares its discriminator to be an OPERATION name, and
  // both dashboard readers enumerate a fixed list of them:
  //   invoke_agent, invoke_model, classify_question, synthesize_answer
  // ScreenWeave puts a TOOL name there instead, so its rows land in partitions
  // like OBSERVATORY#crawl_url that no reader ever queries. The rows are
  // durable, billable and permanently invisible to every dashboard, and the
  // failure is completely silent — the PutItem succeeds.
  //
  // Which naming scheme should win across the portfolio (teach the readers tool
  // names, or make writers emit operation names) is an open PLATFORM decision,
  // not something this repository can settle alone. This test therefore pins
  // the current behaviour so the gap is a checked fact rather than folklore:
  // if someone fixes the pk, or a reader is taught to enumerate tool names,
  // this test fails and forces the change to be acknowledged here.
  const item = await captureEmittedItem('crawl_url');
  const pk = item.pk.S;

  assert.equal(pk, 'OBSERVATORY#crawl_url', 'writer still keys spans by tool name');

  const contract = loadContract();
  const registry = contract.namespace_registry.OBSERVATORY;
  assert.equal(registry.discriminator, 'operation', 'contract expects an operation discriminator');
  assert.ok(
    !registry.discriminator_values.includes('crawl_url'),
    'crawl_url is not one of the operation names readers enumerate',
  );

  // The machine-readable form of "nothing will ever read this row".
  assert.deepEqual(
    readersFor(pk),
    [],
    'if this now returns readers, the Finding 2 gap has been closed — update this test',
  );

  // Contrast: a span keyed by a registered OPERATION name does have readers.
  assert.ok(
    readersFor('OBSERVATORY#invoke_model').length > 0,
    'sanity check — the same namespace IS read when the discriminator is an operation name',
  );
});

test('every MCP tool ScreenWeave exposes lands in an unread partition', async () => {
  // Not one unlucky tool name — the whole surface is affected.
  for (const toolName of ['crawl_url', 'get_session_status', 'get_screenshots',
                          'get_metrics', 'get_full_session', 'export_session']) {
    const item = await captureEmittedItem(toolName, { session_id: 'x' });
    assert.deepEqual(checkItem(item), [], `${toolName} span must still satisfy I1-I4`);
    assert.deepEqual(readersFor(item.pk.S), [], `${toolName} span has no reader`);
  }
});
