/**
 * Shared-gate telemetry for ScreenWeave MCP tool invocations.
 *
 * Mirrors the @weaveaijs/mcp-observatory usage pattern from RoutineWeave
 * (src/engine/GeminiClient.ts, NovaStructurer.ts): lazily import the
 * ESM-only InvocationWrapper, call wrapper.invoke({ source, model, prompt,
 * call }), then persist an invocation span to the shared metrics table.
 *
 * ScreenWeave's invocations are MCP tool calls rather than model calls, so
 * `source: 'agent'` is used, `model` carries the tool name, and `prompt`
 * carries a hash of its arguments (never the raw arguments, which may
 * contain URLs or session identifiers).
 *
 * UPSTREAM BUG, fixed in 0.3.1/0.4.0 (mcp-observatory@0.3.0, dist/core/wrapper.js):
 * when the wrapped `call` rejects, InvocationWrapper's own `finally` block did
 * `hashText(JSON.stringify(output))` where `output` is still `undefined`
 * (the assignment never ran) — `JSON.stringify(undefined)` is `undefined`,
 * and Node's `crypto.hash.update(undefined)` throws a TypeError. That
 * TypeError, thrown from a `finally`, REPLACED the tool's real error before
 * it ever reached us. This package is now pinned to ^0.4.0, where that is
 * fixed, but the guard below is retained deliberately: it is cheap, and this
 * module's contract is that telemetry can never alter a tool's outcome — which
 * should not depend on the behaviour of whichever wrapper version resolves.
 * So `call` is still never allowed to reject through the wrapper: failures are
 * caught locally, the wrapper is fed a harmless placeholder, and the real error
 * is rethrown afterwards from the outcome we captured ourselves.
 *
 * This module must never change tool behaviour or cause a call to fail:
 *   - OBSERVATORY_METRICS_TABLE unset       → call() runs unwrapped.
 *   - @weaveaijs/mcp-observatory import fails → call() runs unwrapped.
 *   - InvocationWrapper misbehaves           → the real call() outcome (see
 *                                               above) still wins.
 *   - DynamoDB write fails                   → swallowed after logging.
 */
import { DynamoDBClient, PutItemCommand } from '@aws-sdk/client-dynamodb';
import { createHash, randomUUID } from 'crypto';

const OBSERVATORY_METRICS_TABLE = process.env.OBSERVATORY_METRICS_TABLE || '';
const TTL_SECONDS = 7_776_000; // 90 days, matching the shared table's convention

let dynamo;
function dynamoClient() {
  if (!dynamo) dynamo = new DynamoDBClient({});
  return dynamo;
}

let wrapperModulePromise;
function loadInvocationWrapper() {
  if (!wrapperModulePromise) {
    wrapperModulePromise = import('@weaveaijs/mcp-observatory')
      .then((mod) => mod.InvocationWrapper)
      .catch(() => null);
  }
  return wrapperModulePromise;
}

function hashArgs(args) {
  try {
    return createHash('sha256').update(JSON.stringify(args ?? {})).digest('hex').slice(0, 16);
  } catch {
    return 'unhashable';
  }
}

async function persistSpan({ toolName, argsHash, durationMs, outcome, errorMessage }) {
  if (!OBSERVATORY_METRICS_TABLE) return;
  // One clock reading, reused for sk/timestamp AND span_date. Two separate
  // `new Date()` calls can straddle midnight UTC and produce a row whose
  // span_date disagrees with its timestamp -- invariant I7 -- indexing it
  // under a day it did not happen on.
  const now = new Date().toISOString();
  const spanDate = now.slice(0, 10);
  try {
    await dynamoClient().send(new PutItemCommand({
      TableName: OBSERVATORY_METRICS_TABLE,
      Item: {
        pk:          { S: `OBSERVATORY#${toolName}` },
        sk:          { S: `${now}#${randomUUID()}` },
        service:     { S: 'screenweave-mcp' },
        operation:   { S: toolName },
        tool_name:   { S: toolName },
        args_hash:   { S: argsHash },
        duration_ms: { N: String(durationMs) },
        outcome:     { S: outcome },
        timestamp:   { S: now },
        span_date:   { S: spanDate },
        ttl:         { N: String(Math.floor(Date.now() / 1000) + TTL_SECONDS) },
        ...(errorMessage ? { error: { S: errorMessage.slice(0, 500) } } : {}),
      },
    }));
  } catch {
    // Telemetry must never break the tool call it is observing.
  }
}

/**
 * Run `call()` (an MCP tool handler) inside an Observatory invocation span
 * when the shared gate is configured, recording tool name, args hash,
 * duration, and outcome. Falls through to a bare `call()` when the gate
 * isn't configured or the package can't be loaded. Return value and thrown
 * errors from `call()` are always preserved unchanged.
 */
export async function withObservability(toolName, args, call) {
  if (!OBSERVATORY_METRICS_TABLE) return call();

  const InvocationWrapper = await loadInvocationWrapper();
  if (!InvocationWrapper) return call();

  let wrapper;
  try {
    wrapper = new InvocationWrapper(`screenweave-${toolName}`);
  } catch {
    return call();
  }

  const argsHash = hashArgs(args);
  const startedAt = Date.now();

  let calledThrough = false;
  let caughtError;
  let realOutput;
  const guardedCall = async () => {
    calledThrough = true;
    try {
      realOutput = await call();
      return realOutput;
    } catch (err) {
      caughtError = err;
      return null; // never let `call`'s rejection reach the buggy wrapper
    }
  };

  let decisionAction;
  try {
    const result = await wrapper.invoke({
      source: 'agent',
      model: toolName,
      prompt: argsHash,
      call: guardedCall,
    });
    decisionAction = result?.decision?.action;
  } catch {
    // The wrapper itself failed. If it never even invoked guardedCall, fall
    // through below to run the real tool directly — telemetry must never
    // prevent a tool from running.
  }

  if (!calledThrough) {
    try {
      realOutput = await call();
    } catch (err) {
      caughtError = err;
    }
  }

  await persistSpan({
    toolName,
    argsHash,
    durationMs: Date.now() - startedAt,
    outcome: caughtError ? 'error' : (decisionAction || 'success'),
    errorMessage: caughtError ? (caughtError instanceof Error ? caughtError.message : String(caughtError)) : undefined,
  });

  if (caughtError) throw caughtError;
  return realOutput;
}
