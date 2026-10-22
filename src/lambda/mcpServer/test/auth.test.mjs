// CipherWeave tokens on /mcp: the rules cipherweave_verify applies in the
// Python Weaves, and the audit/enforce switch that lets this ship while
// TeamWeave still calls with no token.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';

import {
  AUDIENCE, AuthError, Verifier, authenticate, jwkThumbprint, protectedResourceMetadata,
  requestUrl, toolAllowed,
} from '../auth.mjs';

const ISSUER = 'urn:cipherweave:prod';
const NOW = 1_800_000_000;

function keypair() {
  const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
  const jwk = publicKey.export({ format: 'jwk' });
  return { privateKey, jwk: { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y } };
}

const b64 = (obj) => Buffer.from(JSON.stringify(obj)).toString('base64url');

function jws(header, claims, privateKey) {
  const input = `${b64(header)}.${b64(claims)}`;
  const sig = sign('sha256', Buffer.from(input), { key: privateKey, dsaEncoding: 'ieee-p1363' });
  return `${input}.${sig.toString('base64url')}`;
}

const ISSUER_KEY = keypair();
const JWKS = { keys: [{ ...ISSUER_KEY.jwk, kid: 'k1', use: 'sig', alg: 'ES256' }] };
const JWKS_B64 = Buffer.from(JSON.stringify(JWKS)).toString('base64');

function claims(extra = {}) {
  return { iss: ISSUER, aud: AUDIENCE, sub: 'client-teamweave', jti: 'j-1', scope: 'read execute',
    iat: NOW - 10, exp: NOW + 890, ...extra };
}

function token(extra = {}, header = {}, key = ISSUER_KEY.privateKey) {
  return jws({ alg: 'ES256', typ: 'at+jwt', kid: 'k1', ...header }, claims(extra), key);
}

function verifier(opts = {}) {
  return new Verifier({ staticJwks: JWKS_B64, issuer: ISSUER, clock: () => NOW, ...opts });
}

const URL_ = 'https://abc.execute-api.us-east-1.amazonaws.com/mcp';

function proof(clientKey, accessToken, extra = {}) {
  const ath = createHash('sha256').update(accessToken).digest('base64url');
  return jws({ typ: 'dpop+jwt', alg: 'ES256', jwk: clientKey.jwk },
    { jti: 'p-1', htm: 'POST', htu: URL_, iat: NOW, ath, ...extra }, clientKey.privateKey);
}

async function reason(promise) {
  try {
    await promise;
  } catch (err) {
    assert.ok(err instanceof AuthError, `AuthError, got ${err}`);
    return err.reason;
  }
  assert.fail('accepted');
}

test('a valid token is accepted with its scopes', async () => {
  const p = await verifier().verify(token());
  assert.equal(p.subject, 'client-teamweave');
  assert.deepEqual([...p.scopes].sort(), ['execute', 'read']);
  assert.equal(p.dpopBound, false);
});

test('alg none and HS256 are refused before any key is used', async () => {
  const [h, c] = token().split('.');
  const none = `${b64({ alg: 'none', kid: 'k1' })}.${c}.${'A'.repeat(86)}`;
  const hs = `${b64({ alg: 'HS256', kid: 'k1' })}.${c}.${'A'.repeat(43)}`;
  assert.equal(await reason(verifier().verify(none)), 'unsupported_algorithm');
  assert.equal(await reason(verifier().verify(hs)), 'unsupported_algorithm');
  assert.ok(h);
});

test('claims are refused only after the signature holds', async () => {
  const forged = token({ aud: 'deviceweave' }, {}, keypair().privateKey);
  assert.equal(await reason(verifier().verify(forged)), 'invalid_signature');
});

for (const [name, extra, why] of [
  ['another Weave', { aud: 'contextweave' }, 'invalid_audience'],
  ['an audience list', { aud: [AUDIENCE] }, 'invalid_claims'],
  ['another issuer', { iss: 'urn:evil' }, 'invalid_issuer'],
  ['expired', { exp: NOW - 61, iat: NOW - 900 }, 'expired_token'],
  ['issued in the future', { iat: NOW + 120, exp: NOW + 900 }, 'not_yet_valid'],
  ['not yet valid', { nbf: NOW + 120 }, 'not_yet_valid'],
  ['a day long', { iat: NOW - 10, exp: NOW + 86400 }, 'invalid_claims'],
  ['no jti', { jti: undefined }, 'invalid_claims'],
  ['a boolean iat', { iat: true }, 'invalid_claims'],
  ['a malformed act chain', { act: { act: { sub: 'b' } } }, 'invalid_claims'],
  ['an unsupported cnf', { cnf: { 'x5t#S256': 'abc' } }, 'invalid_claims'],
]) {
  test(`a token for ${name} is refused`, async () => {
    assert.equal(await reason(verifier().verify(token(extra))), why);
  });
}

test('tampering, unknown kids and DER signatures are refused', async () => {
  const t = token();
  const [h, , s] = t.split('.');
  assert.equal(await reason(verifier().verify(`${h}.${b64(claims({ scope: 'read execute destructive' }))}.${s}`)), 'invalid_signature');
  assert.equal(await reason(verifier().verify(token({}, { kid: 'k9' }))), 'unknown_key');
  const der = Buffer.alloc(70, 1).toString('base64url');
  assert.equal(await reason(verifier().verify(`${h}.${t.split('.')[1]}.${der}`)), 'invalid_signature');
  assert.equal(await reason(verifier().verify(`${h}.${t.split('.')[1]}.${Buffer.alloc(64).toString('base64url')}`)), 'invalid_signature');
});

test('a JWKS entry carrying a private key is never used', async () => {
  const leaked = { keys: [{ ...ISSUER_KEY.jwk, kid: 'k1', d: 'AAAA' }] };
  const v = new Verifier({ staticJwks: JSON.stringify(leaked), issuer: ISSUER, clock: () => NOW });
  assert.equal(await reason(v.verify(token())), 'unknown_key');
});

test('an unparseable deploy-time JWKS fails closed', () => {
  assert.throws(() => new Verifier({ staticJwks: 'not-base64-json' }), /verifier_not_configured/);
  assert.throws(() => new Verifier({}), /verifier_not_configured/);
});

test('a delegated token carries its actors', async () => {
  const p = await verifier().verify(token({ act: { sub: 'agent-a', act: { sub: 'agent-b' } } }));
  assert.deepEqual(p.actors, ['agent-a', 'agent-b']);
  const deep = { sub: 'a1', act: { sub: 'a2', act: { sub: 'a3', act: { sub: 'a4', act: { sub: 'a5' } } } } };
  assert.equal(await reason(verifier().verify(token({ act: deep }))), 'invalid_claims');
});

test('a key-bound token needs a proof by that key for this request', async () => {
  const client = keypair();
  const bound = token({ cnf: { jkt: jwkThumbprint(client.jwk) } });
  const ok = { scheme: 'dpop', htm: 'POST', htu: URL_ };

  const p = await verifier().verify(bound, { ...ok, dpopProof: proof(client, bound) });
  assert.equal(p.dpopBound, true);

  assert.equal(await reason(verifier().verify(bound, { scheme: 'bearer', dpopProof: proof(client, bound), htm: 'POST', htu: URL_ })), 'dpop_required');
  assert.equal(await reason(verifier().verify(bound, ok)), 'dpop_required');
  assert.equal(await reason(verifier().verify(bound, { ...ok, dpopProof: proof(keypair(), bound) })), 'invalid_dpop_proof');
  assert.equal(await reason(verifier().verify(bound, { ...ok, dpopProof: proof(client, bound, { htu: 'https://other.example.com/mcp' }) })), 'invalid_dpop_proof');
  assert.equal(await reason(verifier().verify(bound, { ...ok, dpopProof: proof(client, bound, { htm: 'GET' }) })), 'invalid_dpop_proof');
  assert.equal(await reason(verifier().verify(bound, { ...ok, dpopProof: proof(client, bound, { iat: NOW - 120 }) })), 'invalid_dpop_proof');
  assert.equal(await reason(verifier().verify(bound, { ...ok, dpopProof: proof(client, token()) })), 'invalid_dpop_proof');
  const p2 = proof(client, bound);
  assert.equal(await reason(verifier().verify(bound, { ...ok, dpopProof: `${p2},${p2}` })), 'invalid_dpop_proof');
});

test('the DPoP scheme with an unbound token is refused', async () => {
  assert.equal(await reason(verifier().verify(token(), { scheme: 'dpop', htm: 'POST', htu: URL_ })), 'token_not_dpop_bound');
});

test('an unknown kid refreshes the fetched JWKS, at most every 30 s', async () => {
  let fetches = 0;
  let t = 0;
  const rotated = keypair();
  const v = new Verifier({ jwksUrl: 'https://cw.example.com/.well-known/jwks.json', issuer: ISSUER,
    clock: () => NOW, monotonic: () => t,
    fetchJson: async () => {
      fetches += 1;
      return fetches === 1 ? JWKS : { keys: [...JWKS.keys, { ...rotated.jwk, kid: 'k2' }] };
    } });
  await v.verify(token());
  assert.equal(fetches, 1);
  const next = token({}, { kid: 'k2' }, rotated.privateKey);
  assert.equal(await reason(v.verify(next)), 'unknown_key', 'within 30 s of the last fetch');
  assert.equal(fetches, 1);
  t = 31_000;
  assert.equal((await v.verify(next)).subject, 'client-teamweave');
  assert.equal(fetches, 2);
});

const ENV = { CIPHERWEAVE_JWKS: JWKS_B64, CIPHERWEAVE_ISSUER: ISSUER,
  CIPHERWEAVE_JWKS_URL: 'https://issuer.example.com/.well-known/jwks.json' };

function event(authorization, extraHeaders = {}) {
  return {
    rawPath: '/mcp',
    headers: { ...(authorization ? { Authorization: authorization } : {}), ...extraHeaders },
    requestContext: { domainName: 'abc.execute-api.us-east-1.amazonaws.com', stage: '$default', http: { method: 'POST' } },
  };
}

test('audit mode lets an anonymous request through and still refuses a bad token', async () => {
  const env = { ...ENV, AUTH_MODE: 'audit' };
  const v = verifier();
  assert.deepEqual(await authenticate(event(), { env, verifier: v }), { ok: true, principal: null, mode: 'audit' });
  const bad = await authenticate(event(`Bearer ${token({ aud: 'toolweave' })}`), { env, verifier: v });
  assert.equal(bad.ok, false);
  assert.equal(bad.reason, 'invalid_audience');
  assert.equal((await authenticate(event('Basic abc'), { env, verifier: v })).ok, false);
});

test('enforce mode, and any mode name it does not know, needs a token', async () => {
  for (const mode of ['enforce', 'Audit-ish', '']) {
    const res = await authenticate(event(), { env: { ...ENV, AUTH_MODE: mode || 'enforce' }, verifier: verifier() });
    assert.equal(res.ok, false, mode);
    assert.equal(res.reason, 'missing');
  }
  const res = await authenticate(event(`Bearer ${token()}`), { env: { ...ENV, AUTH_MODE: 'enforce' }, verifier: verifier() });
  assert.equal(res.ok, true);
});

test('a token with no verifier configured is refused, not ignored', async () => {
  const res = await authenticate(event(`Bearer ${token()}`), { env: { AUTH_MODE: 'audit' } });
  assert.deepEqual([res.ok, res.reason], [false, 'verifier_not_configured']);
});

test('tool scopes', () => {
  const read = { scopes: new Set(['read']) };
  assert.ok(toolAllowed(read, 'get_metrics'));
  assert.ok(!toolAllowed(read, 'crawl_url'));
  assert.ok(!toolAllowed(read, 'generate_apple_video'));
  assert.ok(!toolAllowed(read, 'export_session'));
  assert.ok(!toolAllowed(read, 'some_new_tool'), 'an unmapped tool needs execute');
});

test('the request URL comes from API Gateway, not the Host header', () => {
  const e = event(null, { host: 'evil.example.com' });
  assert.equal(requestUrl(e).url, URL_);
  assert.equal(requestUrl({ ...e, requestContext: { ...e.requestContext, stage: 'v1' } }).base,
    'https://abc.execute-api.us-east-1.amazonaws.com/v1');
});

test('resource metadata names CipherWeave, derived from the JWKS URL', () => {
  const doc = protectedResourceMetadata(ENV);
  assert.deepEqual(doc.authorization_servers, ['https://issuer.example.com']);
  assert.equal(doc.resource, AUDIENCE);
  assert.deepEqual(doc.scopes_supported, ['execute', 'read']);
  assert.equal(protectedResourceMetadata({}), null);
});

// ── Through the handler ──────────────────────────────────────────────────────

function rpc(method, params, authorization) {
  return {
    ...event(authorization, { 'content-type': 'application/json' }),
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
  };
}

test('the handler: 401 with a challenge, 403 for a missing scope, filtered tools/list', async () => {
  Object.assign(process.env, ENV, { AUTH_MODE: 'enforce' });
  // A fixed clock is not reachable through the handler, so mint for now.
  const now = Math.floor(Date.now() / 1000);
  const readOnly = jws({ alg: 'ES256', kid: 'k1' },
    claims({ scope: 'read', iat: now - 5, exp: now + 600 }), ISSUER_KEY.privateKey);
  const { handler } = await import('../index.mjs');

  const anon = await handler(rpc('tools/list', {}));
  assert.equal(anon.statusCode, 401);
  assert.match(anon.headers['WWW-Authenticate'],
    /^Bearer error="invalid_token", resource_metadata="https:\/\/abc\.execute-api\.us-east-1\.amazonaws\.com\/\.well-known\/oauth-protected-resource"$/);

  const crawl = await handler(rpc('tools/call', { name: 'crawl_url', arguments: { url: 'https://example.com/' } }, `Bearer ${readOnly}`));
  assert.equal(crawl.statusCode, 403);
  assert.match(crawl.headers['WWW-Authenticate'], /error="insufficient_scope", scope="execute"/);

  const list = await handler(rpc('tools/list', {}, `Bearer ${readOnly}`));
  assert.equal(list.statusCode, 200);
  const names = JSON.parse(list.body.split('data: ')[1]).result.tools.map((t) => t.name);
  assert.ok(names.includes('get_metrics'));
  assert.ok(!names.includes('crawl_url') && !names.includes('export_session'));

  const meta = await handler({ rawPath: '/.well-known/oauth-protected-resource',
    requestContext: { http: { method: 'GET' } } });
  assert.equal(meta.statusCode, 200);
  assert.equal(JSON.parse(meta.body).resource, 'screenweave');

  process.env.AUTH_MODE = 'audit';
  const audited = await handler(rpc('tools/list', {}));
  assert.equal(audited.statusCode, 200, 'audit serves an anonymous caller');
  const auditedScope = await handler(rpc('tools/list', {}, `Bearer ${readOnly}`));
  assert.equal(JSON.parse(auditedScope.body.split('data: ')[1]).result.tools.length, 7, 'audit does not filter');
});
