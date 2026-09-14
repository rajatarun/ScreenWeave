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
// actual PutItemCommand Item, and checks it with the same I1-I8 checks the
// Python siblings run (contracts/conformance.mjs is a port of conformance.py).
//
// v2.0.0: reads now go through the SpanTimelineIndex GSI (span_date +
// timestamp) instead of the partition key, so a writer's pk is no longer what
// determines whether its rows are visible. See the "v2" tests below.
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

test('the span ScreenWeave actually writes satisfies contract invariants I1-I8', async () => {
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
// Finding 2 — HISTORICAL. Pre-v2, ScreenWeave's rows were durable, billable
// and permanently invisible to every dashboard because readers queried the
// partition key directly and enumerated OPERATION names, while ScreenWeave's
// pk carried a TOOL name (`OBSERVATORY#crawl_url`) that no reader enumerated.
// `readersFor`/pk-reachability is now a question about that pre-migration
// shape, not about whether a row is visible today — see the v2 tests below,
// which assert the opposite property using the actual reading path.
// ---------------------------------------------------------------------------

test('HISTORICAL: under the pre-v2 pk-reachability model, ScreenWeave\'s pk had no reader', async () => {
  // observatory.mjs still writes `pk: OBSERVATORY#${toolName}` — v2 makes the
  // base-table pk the writer's own business, so this is unchanged and is not
  // itself a defect any more. This test only pins what `readersFor` (the
  // superseded, partition-key-based reachability model kept for archaeology)
  // says about that pk, so a future reader that resurrects direct pk queries
  // does not silently regain a false sense of visibility here.
  const item = await captureEmittedItem('crawl_url');
  const pk = item.pk.S;

  assert.equal(pk, 'OBSERVATORY#crawl_url', 'writer still keys its base-table pk by tool name');

  const contract = loadContract();
  const registry = contract.namespace_registry.OBSERVATORY;
  assert.equal(registry.status, 'legacy-informational', 'namespace_registry is demoted in v2');
  assert.deepEqual(
    readersFor(pk),
    [],
    'readersFor is the historical pk-reachability model and still reports none for this pk',
  );
});

test('v2: every MCP tool ScreenWeave exposes is in the SpanTimelineIndex regardless of pk', async () => {
  // This is the fix: span_date + timestamp put every span in the GSI that
  // readers now query, independent of whatever the pk says. Not one lucky
  // tool name — the whole surface is affected.
  const contract = loadContract();
  const { partition_key: gsiPk, sort_key: gsiSk } = contract.gsi;

  for (const toolName of ['crawl_url', 'get_session_status', 'get_screenshots',
                          'get_metrics', 'get_full_session', 'export_session']) {
    const item = await captureEmittedItem(toolName, { session_id: 'x' });
    assert.deepEqual(checkItem(item), [], `${toolName} span must satisfy I1-I8`);

    const gsiPartitionValue = item[gsiPk]?.S;
    const gsiSortValue = item[gsiSk]?.S;
    assert.ok(gsiPartitionValue, `${toolName} span must carry the GSI partition key '${gsiPk}'`);
    assert.ok(gsiSortValue, `${toolName} span must carry the GSI sort key '${gsiSk}'`);
    assert.equal(
      gsiSortValue.slice(0, 10),
      gsiPartitionValue,
      `${toolName} span's ${gsiSk} and ${gsiPk} must agree (I7) or it indexes under the wrong day`,
    );

    // The old pk-based reachability model still finds nothing here (unchanged
    // by design — see the HISTORICAL test) — that is exactly why the GSI,
    // not the pk, is now what makes this row visible.
    assert.deepEqual(readersFor(item.pk.S), [], `${toolName} pk is still unread under the old model`);
  }
});
