'use strict';

/**
 * ScreenWeave – Artifact Retrieval Lambda
 *
 * GET /session/{session_id}[?include=screenshots,states,transitions,trace]
 *
 * Fetches session metadata from DynamoDB, then generates pre-signed S3 URLs
 * for all requested artifact types and returns a structured bundle.
 *
 * Environment variables (required):
 *   SESSIONS_TABLE              – DynamoDB table name
 *   ARTIFACTS_BUCKET            – S3 bucket holding artifacts
 *   BUCKET_PREFIX               – S3 key root prefix (default: "screenweave")
 *   SIGNED_URL_EXPIRES_SECONDS  – Pre-signed URL TTL (default: 3600)
 *   NODE_ENV                    – Runtime environment label
 */

const { DynamoDBClient, GetItemCommand } = require('@aws-sdk/client-dynamodb');
const { S3Client, GetObjectCommand, ListObjectsV2Command } = require('@aws-sdk/client-s3');
const { getSignedUrl } = require('@aws-sdk/s3-request-presigner');
const { unmarshall } = require('@aws-sdk/util-dynamodb');

// ─── Clients (reused across warm invocations) ────────────────────────────────
const dynamo = new DynamoDBClient({});
const s3 = new S3Client({});

// ─── Config ──────────────────────────────────────────────────────────────────
const TABLE_NAME = process.env.SESSIONS_TABLE;
const BUCKET_NAME = process.env.ARTIFACTS_BUCKET;
const BUCKET_PREFIX = process.env.BUCKET_PREFIX || 'screenweave';
const SIGNED_URL_EXPIRES = parseInt(process.env.SIGNED_URL_EXPIRES_SECONDS || '3600', 10);

// session_id: 4–64 chars, alphanumeric + hyphens + underscores only
const SESSION_ID_REGEX = /^[a-zA-Z0-9_-]{4,64}$/;

// Max concurrent S3 signing operations per batch (avoids SDK exhaustion)
const SIGN_BATCH_SIZE = 25;

// ─── Handler ─────────────────────────────────────────────────────────────────
exports.handler = async (event) => {
  try {
    // ── Step 1: Input validation ──────────────────────────────────────────
    const sessionId = event.pathParameters && event.pathParameters.session_id;

    if (!sessionId) {
      return buildResponse(400, { error: 'Missing required path parameter: session_id' });
    }

    if (!SESSION_ID_REGEX.test(sessionId)) {
      return buildResponse(400, {
        error: 'Invalid session_id. Must be 4–64 characters: alphanumeric, hyphens, or underscores.',
      });
    }

    // Parse ?include= query param; default to all artifact types
    const includeParam = event.queryStringParameters && event.queryStringParameters.include;
    const includeSet = includeParam
      ? new Set(includeParam.split(',').map((s) => s.trim().toLowerCase()))
      : new Set(['screenshots', 'states', 'transitions', 'trace']);

    // ── Step 2: Fetch session metadata from DynamoDB ──────────────────────
    const dbResult = await dynamo.send(
      new GetItemCommand({
        TableName: TABLE_NAME,
        Key: { session_id: { S: `SESSION#${sessionId}` } },
      })
    );

    if (!dbResult.Item) {
      return buildResponse(404, { error: `Session not found: ${sessionId}` });
    }

    const session = unmarshall(dbResult.Item);

    // ── Step 3 & 4: Build S3 keys and generate signed URLs ────────────────
    const prefix = `${BUCKET_PREFIX}/${sessionId}`;
    const artifacts = {};

    // Kick off non-screenshot URL signing concurrently
    const [states_json, transitions_json, trace_file] = await Promise.all([
      includeSet.has('states')
        ? signKey(`${prefix}/states.json`)
        : Promise.resolve(null),
      includeSet.has('transitions')
        ? signKey(`${prefix}/transitions.json`)
        : Promise.resolve(null),
      includeSet.has('trace')
        ? signKey(`${prefix}/trace.zip`)
        : Promise.resolve(null),
    ]);

    if (states_json !== null) artifacts.states_json = states_json;
    if (transitions_json !== null) artifacts.transitions_json = transitions_json;
    if (trace_file !== null) artifacts.trace_file = trace_file;

    // Screenshots: list existing objects, then sign in batches
    let screenshots = [];
    if (includeSet.has('screenshots')) {
      const screenshotKeys = await listS3Objects(`${prefix}/screenshots/`);
      const signedUrls = await signKeysBatched(screenshotKeys);

      screenshots = screenshotKeys.map((key, i) => {
        const filename = key.split('/').pop();
        const stateId = filename.replace(/\.(png|jpg|jpeg|webp)$/i, '');
        return {
          state_id: stateId,
          s3_url: `s3://${BUCKET_NAME}/${key}`,
          https_url: signedUrls[i],
        };
      });
    }

    artifacts.screenshots = screenshots;

    // ── Step 5: Build indexed_states ─────────────────────────────────────
    // Populated from artifact_manifest stored in DynamoDB (written by the
    // crawler). Falls back to screenshot listing if manifest is absent.
    const manifest = session.artifact_manifest || {};
    let indexedStates;

    if (Object.keys(manifest).length > 0) {
      indexedStates = Object.entries(manifest).map(([stateId, stateData]) => ({
        state_id: stateId,
        url: stateData.url || '',
        timestamp: stateData.timestamp || '',
        s3_screenshot: `s3://${BUCKET_NAME}/${prefix}/screenshots/${stateId}.png`,
      }));
    } else {
      // Derive from screenshot list when no manifest is present
      indexedStates = screenshots.map((s) => ({
        state_id: s.state_id,
        url: '',
        timestamp: '',
        s3_screenshot: s.s3_url,
      }));
    }

    // Sort by timestamp, then numerically by embedded state number
    indexedStates.sort((a, b) => {
      if (a.timestamp && b.timestamp) return a.timestamp.localeCompare(b.timestamp);
      const aNum = parseInt((a.state_id.match(/(\d+)/) || [])[1] || '0', 10);
      const bNum = parseInt((b.state_id.match(/(\d+)/) || [])[1] || '0', 10);
      return aNum - bNum || a.state_id.localeCompare(b.state_id);
    });

    // ── Construct final response ──────────────────────────────────────────
    return buildResponse(200, {
      session_id: sessionId,
      status: session.status,
      artifacts,
      indexed_states: indexedStates,
      metadata: {
        created_at: session.created_at || null,
        updated_at: session.updated_at || null,
        summary_stats: session.summary_stats || null,
      },
    });
  } catch (err) {
    // Log structured error; never leak internal details to caller
    console.error(
      JSON.stringify({
        level: 'ERROR',
        message: err.message,
        code: err.code || err.name,
        stack: err.stack,
      })
    );
    return buildResponse(500, { error: 'Internal server error' });
  }
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Generate a single pre-signed GET URL.
 * Returns null on error so callers can omit missing artifacts gracefully.
 */
async function signKey(key) {
  try {
    return await getSignedUrl(
      s3,
      new GetObjectCommand({ Bucket: BUCKET_NAME, Key: key }),
      { expiresIn: SIGNED_URL_EXPIRES }
    );
  } catch (err) {
    console.warn(JSON.stringify({ level: 'WARN', message: `Failed to sign key: ${key}`, error: err.message }));
    return null;
  }
}

/**
 * Sign an array of keys in fixed-size batches to avoid flooding the SDK.
 */
async function signKeysBatched(keys) {
  const results = [];
  for (let i = 0; i < keys.length; i += SIGN_BATCH_SIZE) {
    const batch = keys.slice(i, i + SIGN_BATCH_SIZE);
    const batchResults = await Promise.all(batch.map(signKey));
    results.push(...batchResults);
  }
  return results;
}

/**
 * Paginate through all S3 objects under the given prefix.
 * Filters to image files only and sorts numerically by embedded state number.
 */
async function listS3Objects(prefix) {
  const keys = [];
  let continuationToken;

  do {
    const result = await s3.send(
      new ListObjectsV2Command({
        Bucket: BUCKET_NAME,
        Prefix: prefix,
        ContinuationToken: continuationToken,
      })
    );

    if (result.Contents) {
      for (const obj of result.Contents) {
        // Skip "directory" markers and non-image entries
        if (obj.Key !== prefix && /\.(png|jpg|jpeg|webp)$/i.test(obj.Key)) {
          keys.push(obj.Key);
        }
      }
    }

    continuationToken = result.NextContinuationToken;
  } while (continuationToken);

  // Sort numerically by the first integer found in the filename
  return keys.sort((a, b) => {
    const aFile = a.split('/').pop();
    const bFile = b.split('/').pop();
    const aNum = parseInt((aFile.match(/(\d+)/) || [])[1] || '0', 10);
    const bNum = parseInt((bFile.match(/(\d+)/) || [])[1] || '0', 10);
    return aNum - bNum || aFile.localeCompare(bFile);
  });
}

/**
 * Build an API Gateway Lambda Proxy integration response.
 */
function buildResponse(statusCode, body) {
  return {
    statusCode,
    headers: {
      'Content-Type': 'application/json',
      // Pre-signed URLs must not be cached by intermediaries
      'Cache-Control': statusCode === 200 ? 'no-store, max-age=0' : 'no-cache',
      'X-Content-Type-Options': 'nosniff',
    },
    body: JSON.stringify(body),
  };
}
