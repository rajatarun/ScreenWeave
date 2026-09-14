/**
 * Validate one OBSERVATORY_METRICS item against the shared table contract.
 *
 * This is the Node port of mcp-observatory's `contracts/conformance.py`. The
 * shared table is written by services in two languages that cannot import each
 * other's code, so the only thing keeping their rows mutually legible is that
 * each repository checks itself against the same vendored contract file. The
 * Python module and this one therefore implement the SAME invariants (I1-I4)
 * over the SAME `observatory_metrics_item.json` sitting beside them — if you
 * change a check here, change it there too.
 *
 * Deliberately dependency-free (node:fs + node:path only) so it can be vendored
 * into any consumer repository and run under `node --test` with no install step.
 *
 * v2.0.0 adds I6-I8: span_date/timestamp/operation are the SpanTimelineIndex
 * GSI's key attributes plus the field readers group on. A GSI indexes only
 * items carrying both of its key attributes, so a writer that omits span_date
 * or timestamp is exactly as invisible as a v1 writer with the wrong pk
 * namespace -- these are now REQUIRED, not recommended.
 *
 * Usage:
 *
 *   import { loadContract, checkItem, readersFor } from '../../contracts/conformance.mjs';
 *   const problems = checkItem(emittedItem);
 *   assert.deepEqual(problems, []);
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const CONTRACT_FILENAME = 'observatory_metrics_item.json';

// "{iso8601}#{trace_id}" -- the timestamp must sort first, so it is anchored.
const SK_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?#.+$/;
const PK_RE = /^([A-Z_]+)#(.+)$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;

/** Read the contract JSON sitting beside this module unless told otherwise. */
export function loadContract(path) {
  const target = path
    ? resolve(path)
    : resolve(dirname(fileURLToPath(import.meta.url)), CONTRACT_FILENAME);
  return JSON.parse(readFileSync(target, 'utf-8'));
}

/**
 * Accept both the resource-level item shape and the low-level AttributeValue one.
 *
 * Python writers use `boto3.resource(...).Table.put_item` and emit plain values;
 * the Node writers here use the low-level DynamoDB client and emit `{ S: "..." }`.
 * Both must be checkable by the same function or the two languages drift.
 */
export function unwrap(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const keys = Object.keys(value);
    if (keys.length === 1 && ['S', 'N', 'BOOL', 'NULL'].includes(keys[0])) {
      return value[keys[0]];
    }
  }
  return value;
}

/**
 * Return a list of contract violations; empty means the item conforms.
 *
 * Returning problems rather than throwing lets a caller report every violation
 * in one go, which matters when a writer is wrong about several things at once
 * (a wrong key spelling usually travels with a wrong namespace).
 */
export function checkItem(item, contract) {
  const c = contract || loadContract();
  const problems = [];

  // I1 -- key attributes are spelled in lower case.
  for (const [wrong, right] of [['PK', 'pk'], ['SK', 'sk']]) {
    if (wrong in item && !(right in item)) {
      problems.push(
        `I1: item uses '${wrong}' but the table's key attribute is '${right}'; ` +
        'DynamoDB attribute names are case sensitive, so PutItem rejects this ' +
        'item with a ValidationException',
      );
    }
  }
  for (const required of [c.key_schema.partition_key, c.key_schema.sort_key]) {
    if (!(required in item)) {
      problems.push(`I1: required key attribute '${required}' is missing`);
    }
  }

  const pk = unwrap(item.pk);
  const sk = unwrap(item.sk);

  // I2 -- pk carries a registered namespace.
  if (typeof pk === 'string') {
    const match = PK_RE.exec(pk);
    if (!match) {
      problems.push(`I2: pk '${pk}' does not match '{namespace}#{discriminator}'`);
    } else if (!(match[1] in c.namespace_registry)) {
      problems.push(
        `I2: pk namespace '${match[1]}' is not in the contract's namespace_registry ` +
        `${JSON.stringify(Object.keys(c.namespace_registry).sort())}; a row in an ` +
        'unregistered partition is invisible to every reader',
      );
    }
  }

  // I3 -- sk sorts by time.
  if (typeof sk === 'string' && !SK_RE.test(sk)) {
    problems.push(
      `I3: sk '${sk}' does not match '{iso8601}#{trace_id}'; readers range-query ` +
      'sk lexicographically, so a non-ISO or non-leading timestamp breaks time filters',
    );
  }

  // I4 -- rows expire.
  if (!('ttl' in item)) {
    problems.push("I4: no 'ttl' attribute; rows would accumulate in a shared table forever");
  }

  // I6-I8 -- the SpanTimelineIndex key attributes. A GSI indexes only items
  // that carry both of its keys, so a writer omitting either is invisible to
  // every dashboard in exactly the way v1's pk-prefix mismatches were, except
  // now it is a test failure instead of a silence.
  const spanDate = unwrap(item.span_date);
  const timestamp = unwrap(item.timestamp);
  const operation = unwrap(item.operation);

  if (!spanDate) {
    problems.push(
      "I6: no 'span_date'; the SpanTimelineIndex partition key is missing, so this " +
      'row is not in the index and no dashboard will ever show it',
    );
  } else if (!DATE_RE.test(String(spanDate))) {
    problems.push(`I6: span_date '${spanDate}' is not YYYY-MM-DD`);
  }

  if (!timestamp) {
    problems.push(
      "I7: no 'timestamp'; the SpanTimelineIndex sort key is missing, so this row is " +
      'not in the index',
    );
  } else if (!TS_RE.test(String(timestamp))) {
    problems.push(`I7: timestamp '${timestamp}' is not ISO 8601`);
  } else if (spanDate && DATE_RE.test(String(spanDate)) && String(timestamp).slice(0, 10) !== String(spanDate)) {
    problems.push(
      `I7: timestamp '${timestamp}' and span_date '${spanDate}' disagree; the row ` +
      'would be indexed under a day it did not happen on',
    );
  }

  if (!operation) {
    problems.push("I8: no 'operation'; readers filter and group on it");
  }

  return problems;
}

/**
 * Which readers, if any, will ever see a row written at this pk.
 *
 * An empty array is the machine-readable form of "this telemetry is written,
 * billed, and never read by anything".
 */
export function readersFor(pk, contract) {
  const c = contract || loadContract();
  const match = PK_RE.exec(pk || '');
  if (!match) return [];
  const entry = c.namespace_registry[match[1]];
  if (!entry) return [];
  if (entry.discriminator === 'operation') {
    const allowed = entry.discriminator_values || [];
    if (allowed.length && !allowed.includes(match[2])) {
      // Registered namespace, but a discriminator no reader enumerates.
      return [];
    }
  }
  return [...(entry.readers || [])];
}
