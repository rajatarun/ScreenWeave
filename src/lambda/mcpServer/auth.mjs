/**
 * Who is calling /mcp: CipherWeave access tokens, the same rules the four
 * Python Weaves run through cipherweave_verify.
 *
 * Until this file /mcp had no authentication. Anyone with the URL could start
 * crawler instances. Two callers reach it today with no token at all:
 * TeamWeave's mcp_client and its AgentCore gateway target. So this ships in
 * `audit` mode, the way ContextWeave's tool scopes did:
 *
 *   audit   (default) a request without a token still runs, and is logged as
 *           `mcp auth method=none`. A token that is presented is verified, and
 *           a bad one is refused (401) in every mode: a client sending a token
 *           expects it to count, and a wrong-audience token silently run as
 *           anonymous would hide the mistake until enforce broke it.
 *   enforce every request needs a valid token, and each tool its scope.
 *
 * Flip AuthMode to enforce once no `method=none` line appears in the logs.
 *
 * Token rules, mirrored from cipherweave_verify.verify (CipherWeave): compact
 * JWS, alg exactly ES256 (none and HS256 are refused before a key is looked
 * up), kid in the JWKS, a 64-byte r||s signature with r and s in range, and
 * only then the claims: iss, aud exactly "screenweave" as a single string,
 * exp/nbf/iat with 60 s leeway, lifetime at most an hour, sub/jti present, a
 * well-formed act chain, and cnf.jkt either absent or a DPoP binding that the
 * request's proof satisfies (RFC 9449). A key-bound token sent as Bearer is
 * dpop_required. DPoP replay protection needs a ledger this service does not
 * have: a captured proof is good for one URL for at most 60 s.
 */
import { createHash, createPublicKey, verify as verifySignature } from 'node:crypto';

export const AUDIENCE = 'screenweave';
export const DEFAULT_ISSUER = 'urn:cipherweave:prod';
export const WELL_KNOWN_PATH = '/.well-known/oauth-protected-resource';
const ALG = 'ES256';
const LEEWAY_S = 60;
const MAX_LIFETIME_S = 3600;
const MAX_TOKEN_LENGTH = 8192;
const MAX_PROOF_LENGTH = 4096;
const PROOF_MAX_AGE_S = 60;
const PROOF_LEEWAY_S = 5;
const MAX_ACTOR_DEPTH = 4;
const JWKS_TTL_MS = 600_000;
const JWKS_REFRESH_MIN_MS = 30_000;
const P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551n;
const ALLOWED_TYP = new Set(['jwt', 'at+jwt']);

// Launching an instance or writing into another Weave's bucket is `execute`;
// reading back what a crawl produced is `read`. Same scope names as the
// other Weaves (cipherweave_verify.authz).
export const TOOL_SCOPES = {
  crawl_url: 'execute',
  generate_apple_video: 'execute',
  export_session: 'execute',
  get_session_status: 'read',
  get_screenshots: 'read',
  get_metrics: 'read',
  get_full_session: 'read',
};

export class AuthError extends Error {
  constructor(reason, status = 401) {
    super(reason);
    this.reason = reason;
    this.status = status;
  }
}

/** `audit` only when asked for by name; anything else enforces. */
export function authMode(env = process.env) {
  return (env.AUTH_MODE || 'audit').trim().toLowerCase() === 'audit' ? 'audit' : 'enforce';
}

function b64urlDecode(segment) {
  if (typeof segment !== 'string' || !/^[A-Za-z0-9_-]*$/.test(segment)) {
    throw new AuthError('malformed_token');
  }
  return Buffer.from(segment, 'base64url');
}

function jsonSegment(segment, reason = 'malformed_token') {
  let value;
  try {
    value = JSON.parse(b64urlDecode(segment).toString('utf8'));
  } catch {
    throw new AuthError(reason);
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new AuthError(reason);
  return value;
}

/** A P-256 public key from a JWK, or null when the entry is not one. */
export function publicKeyFromJwk(jwk) {
  if (!jwk || typeof jwk !== 'object') return null;
  if (jwk.kty !== 'EC' || jwk.crv !== 'P-256') return null;
  if ((jwk.use ?? 'sig') !== 'sig' || (jwk.alg ?? ALG) !== ALG) return null;
  // A private key has no business in a published set or a proof header.
  if ('d' in jwk) return null;
  if (typeof jwk.x !== 'string' || typeof jwk.y !== 'string') return null;
  try {
    return createPublicKey({ key: { kty: 'EC', crv: 'P-256', x: jwk.x, y: jwk.y }, format: 'jwk' });
  } catch {
    return null;
  }
}

/** A JWKS given at deploy time: JSON text or base64 of it. Unparseable fails closed. */
export function parseStaticJwks(value) {
  const text = (value || '').trim();
  if (!text) return null;
  let doc;
  try {
    doc = JSON.parse(text.startsWith('{') ? text : Buffer.from(text, 'base64').toString('utf8'));
  } catch {
    throw new AuthError('verifier_not_configured', 500);
  }
  if (!doc || !Array.isArray(doc.keys)) throw new AuthError('verifier_not_configured', 500);
  return doc;
}

function keysOf(doc) {
  const keys = new Map();
  for (const jwk of doc?.keys || []) {
    const key = typeof jwk?.kid === 'string' && jwk.kid ? publicKeyFromJwk(jwk) : null;
    if (key) keys.set(jwk.kid, key);
  }
  return keys;
}

function es256Verifies(key, signingInput, signatureB64) {
  const sig = b64urlDecode(signatureB64);
  if (sig.length !== 64) return false;
  const r = BigInt('0x' + sig.subarray(0, 32).toString('hex'));
  const s = BigInt('0x' + sig.subarray(32).toString('hex'));
  if (!(r > 0n && r < P256_ORDER && s > 0n && s < P256_ORDER)) return false;
  try {
    return verifySignature('sha256', Buffer.from(signingInput, 'ascii'),
      { key, dsaEncoding: 'ieee-p1363' }, sig);
  } catch {
    return false;
  }
}

function sha256b64url(text) {
  return createHash('sha256').update(text, 'ascii').digest('base64url');
}

/** RFC 7638 thumbprint of an EC public JWK. */
export function jwkThumbprint(jwk) {
  return sha256b64url(JSON.stringify({ crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y }));
}

/** Scheme and host lower-cased, default port dropped, query and fragment removed. */
export function normalizeHtu(raw) {
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new AuthError('invalid_dpop_proof');
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') throw new AuthError('invalid_dpop_proof');
  // URL already drops a default port and lower-cases scheme and host.
  return `${url.protocol}//${url.host}${url.pathname || '/'}`;
}

function intClaim(claims, name, required) {
  const v = claims[name];
  if (v === undefined || v === null) {
    if (required) throw new AuthError('invalid_claims');
    return null;
  }
  if (typeof v !== 'number' || !Number.isFinite(v)) throw new AuthError('invalid_claims');
  return Math.trunc(v);
}

function strClaim(claims, name, allowEmpty = false) {
  const v = claims[name];
  if (typeof v !== 'string' || (!v && !allowEmpty)) throw new AuthError('invalid_claims');
  return v;
}

export function actorChain(claims) {
  const chain = [];
  let act = claims.act;
  while (act !== undefined && act !== null) {
    if (typeof act !== 'object' || Array.isArray(act)) throw new AuthError('invalid_claims');
    if (typeof act.sub !== 'string' || !act.sub) throw new AuthError('invalid_claims');
    chain.push(act.sub);
    if (chain.length > MAX_ACTOR_DEPTH) throw new AuthError('invalid_claims');
    act = act.act;
  }
  return chain;
}

function confirmationJkt(claims) {
  const cnf = claims.cnf;
  if (cnf === undefined || cnf === null) return null;
  if (typeof cnf !== 'object' || Array.isArray(cnf)) throw new AuthError('invalid_claims');
  const extra = Object.keys(cnf).filter((k) => k !== 'jkt');
  // A binding this verifier cannot check must not read as no binding at all.
  if (typeof cnf.jkt !== 'string' || !cnf.jkt || extra.length) throw new AuthError('invalid_claims');
  return cnf.jkt;
}

/** Verify one DPoP proof for one request (RFC 9449 §4.3). */
export function verifyDpopProof(proof, { htm, htu, accessToken, expectedJkt, now }) {
  if (typeof proof !== 'string' || !proof || proof.length > MAX_PROOF_LENGTH || proof.includes(',')) {
    throw new AuthError('invalid_dpop_proof');
  }
  const parts = proof.split('.');
  if (parts.length !== 3 || parts.some((p) => !p)) throw new AuthError('invalid_dpop_proof');
  const [h, p, s] = parts;
  const header = jsonSegment(h, 'invalid_dpop_proof');
  if (header.typ !== 'dpop+jwt' || header.alg !== ALG || 'crit' in header) {
    throw new AuthError('invalid_dpop_proof');
  }
  const jwk = header.jwk;
  const key = jwk && typeof jwk === 'object' && !('d' in jwk)
    ? publicKeyFromJwk({ kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y })
    : null;
  if (!key) throw new AuthError('invalid_dpop_proof');
  if (!es256Verifies(key, `${h}.${p}`, s)) throw new AuthError('invalid_dpop_proof');

  const claims = jsonSegment(p, 'invalid_dpop_proof');
  if (typeof claims.jti !== 'string' || !claims.jti || claims.jti.length > 256) throw new AuthError('invalid_dpop_proof');
  if (claims.htm !== String(htm || '').toUpperCase()) throw new AuthError('invalid_dpop_proof');
  if (typeof claims.htu !== 'string' || normalizeHtu(claims.htu) !== normalizeHtu(htu)) {
    throw new AuthError('invalid_dpop_proof');
  }
  if (typeof claims.iat !== 'number' || !Number.isFinite(claims.iat)) throw new AuthError('invalid_dpop_proof');
  if (claims.iat > now + PROOF_LEEWAY_S || now - claims.iat > PROOF_MAX_AGE_S) {
    throw new AuthError('invalid_dpop_proof');
  }
  if (claims.ath !== sha256b64url(accessToken)) throw new AuthError('invalid_dpop_proof');
  if (jwkThumbprint(jwk) !== expectedJkt) throw new AuthError('invalid_dpop_proof');
}

/**
 * Keys from CIPHERWEAVE_JWKS (taken on the deploy runner, no network) and
 * CIPHERWEAVE_JWKS_URL (fetched, cached ten minutes, refreshed on an unknown
 * kid at most every 30 s so junk kids cannot turn into a fetch per request).
 */
export class Verifier {
  constructor({ staticJwks = '', jwksUrl = '', issuer = DEFAULT_ISSUER,
    fetchJson = defaultFetchJson, clock = () => Date.now() / 1000, monotonic = () => Date.now() } = {}) {
    const doc = parseStaticJwks(staticJwks);
    if (!doc && !jwksUrl) throw new AuthError('verifier_not_configured', 500);
    this.issuer = issuer || DEFAULT_ISSUER;
    this.staticKeys = doc ? keysOf(doc) : new Map();
    this.jwksUrl = jwksUrl;
    this.fetchJson = fetchJson;
    this.clock = clock;
    this.monotonic = monotonic;
    this.fetched = new Map();
    this.fetchedAt = null;
    this.lastAttempt = null;
  }

  async keyFor(kid) {
    if (this.staticKeys.has(kid)) return this.staticKeys.get(kid);
    if (!this.jwksUrl) return null;
    const t = this.monotonic();
    const stale = this.fetchedAt === null || t - this.fetchedAt > JWKS_TTL_MS;
    const mayRefresh = this.lastAttempt === null || t - this.lastAttempt >= JWKS_REFRESH_MIN_MS;
    if ((stale || !this.fetched.has(kid)) && mayRefresh) {
      this.lastAttempt = t;
      try {
        this.fetched = keysOf(await this.fetchJson(this.jwksUrl));
        this.fetchedAt = t;
      } catch {
        // Keep the last good set; a stale set past its TTL is not used.
      }
    }
    if (this.fetchedAt === null || this.monotonic() - this.fetchedAt > 2 * JWKS_TTL_MS) return null;
    return this.fetched.get(kid) || null;
  }

  /** The verified token, or throws AuthError. */
  async verify(token, { scheme, dpopProof, htm, htu, audience = AUDIENCE } = {}) {
    if (typeof token !== 'string' || !token || token.length > MAX_TOKEN_LENGTH) throw new AuthError('malformed_token');
    const parts = token.split('.');
    if (parts.length !== 3 || parts.some((p) => !p)) throw new AuthError('malformed_token');
    const [h, p, s] = parts;
    const header = jsonSegment(h);
    if (header.alg !== ALG) throw new AuthError('unsupported_algorithm');
    if ('crit' in header) throw new AuthError('malformed_token');
    if (header.typ !== undefined && (typeof header.typ !== 'string' || !ALLOWED_TYP.has(header.typ.toLowerCase()))) {
      throw new AuthError('malformed_token');
    }
    if (typeof header.kid !== 'string' || !header.kid || header.kid.length > 256) throw new AuthError('malformed_token');
    const key = await this.keyFor(header.kid);
    if (!key) throw new AuthError('unknown_key');
    if (!es256Verifies(key, `${h}.${p}`, s)) throw new AuthError('invalid_signature');

    // Claims are read only after the signature holds.
    const claims = jsonSegment(p);
    const sub = strClaim(claims, 'sub');
    const jti = strClaim(claims, 'jti');
    const iss = strClaim(claims, 'iss');
    const scope = strClaim(claims, 'scope', true);
    const iat = intClaim(claims, 'iat', true);
    const exp = intClaim(claims, 'exp', true);
    const nbf = intClaim(claims, 'nbf', false);
    if (iss !== this.issuer) throw new AuthError('invalid_issuer');
    if (typeof claims.aud !== 'string' || !claims.aud) throw new AuthError('invalid_claims');
    if (claims.aud !== audience) throw new AuthError('invalid_audience');
    const now = this.clock();
    if (now >= exp + LEEWAY_S) throw new AuthError('expired_token');
    if (nbf !== null && now < nbf - LEEWAY_S) throw new AuthError('not_yet_valid');
    if (iat > now + LEEWAY_S) throw new AuthError('not_yet_valid');
    if (exp <= iat || exp - iat > MAX_LIFETIME_S) throw new AuthError('invalid_claims');
    const actors = actorChain(claims);
    const jkt = confirmationJkt(claims);

    if (jkt !== null) {
      if (scheme === 'bearer') throw new AuthError('dpop_required');
      if (!dpopProof) throw new AuthError('dpop_required');
      if (!htm || !htu) throw new AuthError('invalid_dpop_proof');
      verifyDpopProof(dpopProof, { htm, htu, accessToken: token, expectedJkt: jkt, now });
    } else if (scheme === 'dpop') {
      throw new AuthError('token_not_dpop_bound');
    }
    return { subject: sub, jti, scopes: new Set(scope.split(/\s+/).filter(Boolean)), actors, dpopBound: jkt !== null };
  }
}

async function defaultFetchJson(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
  if (!res.ok) throw new Error(`JWKS HTTP ${res.status}`);
  return res.json();
}

let cached = null;
let cachedConfig = null;

/** One verifier per process, so warm invocations keep the JWKS cache. Null when unconfigured. */
export function defaultVerifier(env = process.env) {
  const config = [env.CIPHERWEAVE_JWKS || '', env.CIPHERWEAVE_JWKS_URL || '', env.CIPHERWEAVE_ISSUER || ''].map((v) => v.trim());
  if (!config[0] && !config[1]) return null;
  if (!cached || cachedConfig !== config.join('\n')) {
    cached = new Verifier({ staticJwks: config[0], jwksUrl: config[1], issuer: config[2] || DEFAULT_ISSUER });
    cachedConfig = config.join('\n');
  }
  return cached;
}

/** `Authorization` → { scheme: 'bearer'|'dpop', token } or nulls. */
export function parseAuthorization(header) {
  if (typeof header !== 'string' || !header.trim()) return { scheme: null, token: null };
  const trimmed = header.trim();
  const space = trimmed.indexOf(' ');
  if (space < 0) return { scheme: null, token: null };
  const scheme = trimmed.slice(0, space).toLowerCase();
  const token = trimmed.slice(space + 1).trim();
  if ((scheme !== 'bearer' && scheme !== 'dpop') || !token || /\s/.test(token)) return { scheme: null, token: null };
  return { scheme, token };
}

/** (method, absolute URL, base URL) of an HTTP API (v2) event, from API Gateway's own context. */
export function requestUrl(event) {
  const ctx = event?.requestContext || {};
  const method = String(ctx.http?.method || 'GET').toUpperCase();
  const domain = ctx.domainName;
  const rawPath = event?.rawPath || ctx.http?.path;
  if (!domain || !rawPath) return { method, url: null, base: null };
  const prefix = ctx.stage && ctx.stage !== '$default' ? `/${ctx.stage}` : '';
  return { method, url: `https://${domain}${rawPath}`, base: `https://${domain}${prefix}` };
}

/** CipherWeave's base URL: explicit, else the JWKS URL without its well-known suffix. */
export function authorizationServer(env = process.env) {
  const explicit = (env.CIPHERWEAVE_AUTHORIZATION_SERVER || '').trim();
  if (explicit) return explicit.replace(/\/+$/, '');
  const jwksUrl = (env.CIPHERWEAVE_JWKS_URL || '').trim();
  const suffix = '/.well-known/jwks.json';
  return jwksUrl.startsWith('https://') && jwksUrl.endsWith(suffix) ? jwksUrl.slice(0, -suffix.length) : null;
}

/** The RFC 9728 document, or null when there is no authorization server to name. */
export function protectedResourceMetadata(env = process.env) {
  const server = authorizationServer(env);
  if (!server) return null;
  return {
    resource: AUDIENCE,
    resource_name: 'ScreenWeave',
    authorization_servers: [server],
    bearer_methods_supported: ['header'],
    scopes_supported: [...new Set(Object.values(TOOL_SCOPES))].sort(),
    resource_signing_alg_values_supported: [ALG],
    dpop_signing_alg_values_supported: [ALG],
    dpop_bound_access_tokens_required: false,
  };
}

const SAFE = /[^A-Za-z0-9 :/._~?#[\]@!$&'()*+,;=%-]/g;
const quote = (v) => String(v).replace(SAFE, '').replace(/"/g, '');

/** RFC 6750 / 9449 challenge pointing at the resource metadata (RFC 9728 §5.1). */
export function wwwAuthenticate({ error, scope, base, dpop = false }) {
  const params = [];
  if (error) params.push(`error="${quote(error)}"`);
  if (scope) params.push(`scope="${quote(scope)}"`);
  if (base && protectedResourceMetadata()) params.push(`resource_metadata="${quote(base + WELL_KNOWN_PATH)}"`);
  if (dpop) params.push('algs="ES256"');
  return (dpop ? 'DPoP' : 'Bearer') + (params.length ? ' ' + params.join(', ') : '');
}

/**
 * Decide on a request. Returns { ok: true, principal } or
 * { ok: false, status, reason }. principal is null for an anonymous request
 * that audit mode lets through.
 */
export async function authenticate(event, { env = process.env, verifier } = {}) {
  const headers = Object.fromEntries(Object.entries(event?.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
  const mode = authMode(env);
  const { scheme, token } = parseAuthorization(headers.authorization);
  if (!token) {
    if (headers.authorization) return { ok: false, status: 401, reason: 'invalid_request', mode };
    return mode === 'audit' ? { ok: true, principal: null, mode } : { ok: false, status: 401, reason: 'missing', mode };
  }
  const v = verifier === undefined ? defaultVerifier(env) : verifier;
  if (!v) return { ok: false, status: 401, reason: 'verifier_not_configured', mode };
  const { method, url } = requestUrl(event);
  try {
    const principal = await v.verify(token, { scheme, dpopProof: headers.dpop, htm: method, htu: url });
    return { ok: true, principal, mode };
  } catch (err) {
    if (err instanceof AuthError) return { ok: false, status: 401, reason: err.reason, mode };
    // A verifier bug must be a 401, not a 500 that reads as an outage.
    return { ok: false, status: 401, reason: 'verifier_error', mode };
  }
}

/** True when the principal may call the tool. An unknown tool needs `execute`. */
export function toolAllowed(principal, toolName) {
  const needed = TOOL_SCOPES[toolName] || 'execute';
  return Boolean(principal && principal.scopes.has(needed));
}

export function requiredScope(toolName) {
  return TOOL_SCOPES[toolName] || 'execute';
}
