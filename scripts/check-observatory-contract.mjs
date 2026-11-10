#!/usr/bin/env node
/**
 * Synthetic conformance check — validates a representative span item
 * against contracts/observatory_metrics_item.json (offline, no AWS).
 */
import { checkItem, loadContract } from '../contracts/conformance.mjs';

const contract = loadContract();
const now = new Date();
const iso = now.toISOString();
const sample = {
  pk: { S: 'INVOCATION#screenweave-mcp' },
  sk: { S: `${iso}#trace-synthetic-eval` },
  span_date: { S: iso.slice(0, 10) },
  timestamp: { S: iso },
  operation: { S: 'tools/call' },
  tool_name: { S: 'get_session_status' },
  outcome: { S: 'success' },
  duration_ms: { N: '12' },
  ttl: { N: String(Math.floor(now.getTime() / 1000) + 86400) },
};

const problems = checkItem(sample, contract);
if (problems.length) {
  console.error('Contract check failed:', problems);
  process.exit(1);
}
console.log('Synthetic observatory span: OK (%d invariants checked)', problems.length === 0 ? 8 : 0);
