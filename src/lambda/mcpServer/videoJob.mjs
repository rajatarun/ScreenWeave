/**
 * The showcase-video engine's job lifecycle.
 *
 * ScreenWeave was a QA service; this is the second engine. It deliberately
 * reuses the crawl side's shape rather than inventing a parallel one: a job is
 * a row in the same sessions table under the same `SESSION#` key, so
 * `get_session_status` polls a render exactly as it polls a crawl, and no
 * client learns a second protocol.
 *
 * What is NOT here is the renderer. Capturing frames and driving Remotion is a
 * separate worker; this module starts the job and records it. When no worker
 * is configured the job is recorded as NOT_CONFIGURED with a reason — it never
 * invents an artifact URL, because a fabricated S3 link is indistinguishable
 * from a real one until someone tries to play it.
 */
export const VIDEO_KIND = 'video';
export const MIN_DURATION_S = 5;
export const MAX_DURATION_S = 30;
export const ALLOWED_SCALES = [1, 2, 3];
export const DEFAULT_DURATION_S = 20;
export const DEFAULT_SCALE = 3;

const URL_RE = /^https?:\/\/[^\s]+$/i;

/**
 * Normalise and check a generate_apple_video request.
 * Throws with a message the caller can act on; never returns partial input.
 */
export function validateVideoRequest({ url, session_id, duration_s, scale } = {}) {
  const hasUrl = typeof url === 'string' && url.trim() !== '';
  const hasSession = typeof session_id === 'string' && session_id.trim() !== '';

  // Exactly one source. Both would leave it ambiguous which page is the
  // subject; neither leaves nothing to render.
  if (hasUrl && hasSession)
    throw new Error('pass either url or session_id, not both');
  if (!hasUrl && !hasSession)
    throw new Error('one of url or session_id is required');
  if (hasUrl && !URL_RE.test(url.trim()))
    throw new Error('url must be a valid http(s) URL');

  const duration = duration_s === undefined ? DEFAULT_DURATION_S : duration_s;
  if (!Number.isInteger(duration) || duration < MIN_DURATION_S || duration > MAX_DURATION_S)
    throw new Error(`duration_s must be an integer ${MIN_DURATION_S}–${MAX_DURATION_S}`);

  const resolvedScale = scale === undefined ? DEFAULT_SCALE : scale;
  if (!ALLOWED_SCALES.includes(resolvedScale))
    throw new Error(`scale must be one of ${ALLOWED_SCALES.join(', ')}`);

  return {
    targetUrl: hasUrl ? url.trim() : null,
    sourceSessionId: hasSession ? session_id.trim() : null,
    durationS: duration,
    scale: resolvedScale,
  };
}

/** The row a video job occupies in the sessions table. */
export function buildVideoJobItem({ jobId, request, now, ttl, status, reason = null }) {
  return {
    session_id: `SESSION#${jobId}`,
    kind: VIDEO_KIND,
    status,
    target_url: request.targetUrl,
    source_session_id: request.sourceSessionId,
    duration_s: request.durationS,
    scale: request.scale,
    created_at: now,
    updated_at: now,
    ttl,
    ...(reason ? { status_reason: reason } : {}),
  };
}

/**
 * The completion envelope, shared with the QA engine so TeamWeave parses one
 * shape. `artifacts` is a list rather than named fields so a future engine
 * adds a `kind` instead of a schema version, and a FAILED job still carries
 * whatever it produced before failing.
 */
export function buildCompletionEvent({ jobId, mode, status, artifacts = [], error = null, startedAt, finishedAt, sessionId = null }) {
  return {
    event: 'screenweave.job.completed',
    job_id: jobId,
    session_id: sessionId,
    mode,
    status,
    artifacts,
    error,
    started_at: startedAt,
    finished_at: finishedAt,
  };
}

/**
 * Start a render. Writes the job row, then hands it to the worker if one is
 * configured.
 *
 * The job is recorded before the worker is invoked: an invoke that fails after
 * a write leaves a job someone can see and retry, while a write that fails
 * after an invoke leaves a render nothing is tracking.
 */
export async function startVideoJob({ request, jobId, now, ttl, workerFunction, putItem, invokeWorker, log = () => {} }) {
  const configured = Boolean(workerFunction);
  const status = configured ? 'PENDING' : 'NOT_CONFIGURED';
  const reason = configured
    ? null
    : 'No render worker is configured (VIDEO_WORKER_FUNCTION unset), so no video will be produced.';

  const item = buildVideoJobItem({ jobId, request, now, ttl, status, reason });
  await putItem(item);
  log('INFO', 'Recorded video job', { job_id: jobId, status });

  if (!configured) return { job_id: jobId, status, reason, poll_with: 'get_session_status' };

  await invokeWorker({
    function_name: workerFunction,
    payload: {
      job_id: jobId,
      target_url: request.targetUrl,
      source_session_id: request.sourceSessionId,
      duration_s: request.durationS,
      scale: request.scale,
    },
  });
  log('INFO', 'Dispatched video job to worker', { job_id: jobId, worker: workerFunction });

  return { job_id: jobId, status, poll_with: 'get_session_status' };
}
