// Google ID token verification (RS256 against Google's JWKS) and the HMAC session cookie.
import { HttpError, b64urlDecode, b64urlEncode, safeEqual, utf8 } from './util.js';

export const GOOGLE_JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs';
const GOOGLE_ISS = new Set(['accounts.google.com', 'https://accounts.google.com']);
const CLOCK_SKEW_S = 60;
const DEFAULT_JWKS_TTL_MS = 60 * 60 * 1000;
const MIN_REFETCH_MS = 30 * 1000;

// Module-level cache: lives as long as the isolate.
let jwksCache = { keys: new Map(), expires: 0, fetchedAt: 0 };

export function _resetJwksCache() {
  jwksCache = { keys: new Map(), expires: 0, fetchedAt: 0 };
}

async function fetchJwks(now) {
  // globalThis.fetch is read at call time so tests can stub it.
  const res = await globalThis.fetch(GOOGLE_JWKS_URL, { cf: { cacheTtl: 3600 } });
  if (!res.ok) throw new HttpError(503, 'jwks_unavailable', 'could not fetch Google keys');
  const body = await res.json();
  const keys = new Map();
  for (const jwk of body.keys || []) {
    if (jwk.kty !== 'RSA' || !jwk.kid) continue;
    const key = await crypto.subtle.importKey(
      'jwk',
      { kty: 'RSA', n: jwk.n, e: jwk.e, alg: 'RS256', ext: true },
      { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
      false,
      ['verify'],
    );
    keys.set(jwk.kid, key);
  }
  let ttl = DEFAULT_JWKS_TTL_MS;
  const m = /max-age=(\d+)/.exec(res.headers.get('Cache-Control') || '');
  if (m) ttl = Math.min(Number(m[1]) * 1000, 24 * 3600 * 1000);
  jwksCache = { keys, expires: now + ttl, fetchedAt: now };
}

async function getKey(kid, now) {
  if (now >= jwksCache.expires) await fetchJwks(now);
  let key = jwksCache.keys.get(kid);
  // Unknown kid: Google may have rotated keys; refetch once (throttled).
  if (!key && now - jwksCache.fetchedAt >= MIN_REFETCH_MS) {
    await fetchJwks(now);
    key = jwksCache.keys.get(kid);
  }
  return key || null;
}

const bad = (msg) => new HttpError(401, 'invalid_token', msg);

export async function verifyGoogleIdToken(token, clientId, now = Date.now()) {
  if (!clientId) throw new HttpError(500, 'misconfigured', 'GOOGLE_CLIENT_ID not set');
  if (typeof token !== 'string' || token.length > 8192) throw bad('credential missing');
  const parts = token.split('.');
  if (parts.length !== 3) throw bad('malformed token');
  let header, payload, sig;
  try {
    header = JSON.parse(new TextDecoder().decode(b64urlDecode(parts[0])));
    payload = JSON.parse(new TextDecoder().decode(b64urlDecode(parts[1])));
    sig = b64urlDecode(parts[2]);
  } catch {
    throw bad('malformed token');
  }
  if (header.alg !== 'RS256' || !header.kid) throw bad('unsupported token algorithm');
  const key = await getKey(header.kid, now);
  if (!key) throw bad('unknown signing key');
  const ok = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, sig, utf8(`${parts[0]}.${parts[1]}`));
  if (!ok) throw bad('bad signature');
  const nowS = Math.floor(now / 1000);
  if (!GOOGLE_ISS.has(payload.iss)) throw bad('bad issuer');
  const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!aud.includes(clientId)) throw bad('bad audience');
  if (typeof payload.exp !== 'number' || payload.exp + CLOCK_SKEW_S < nowS) throw bad('token expired');
  if (typeof payload.iat === 'number' && payload.iat - CLOCK_SKEW_S > nowS) throw bad('token issued in the future');
  if (typeof payload.nbf === 'number' && payload.nbf - CLOCK_SKEW_S > nowS) throw bad('token not yet valid');
  if (typeof payload.sub !== 'string' || !payload.sub) throw bad('no subject');
  return payload;
}

// ---- Session cookie: apb_s = <user_id>|<expiry_s>|<hmac> (base64url HMAC-SHA256 of "user_id|expiry_s").

export const COOKIE = 'apb_s';
export const SESSION_TTL_S = 30 * 24 * 3600;

async function hmacKey(secret) {
  if (!secret || secret.length < 16) throw new HttpError(500, 'misconfigured', 'SESSION_SECRET not set');
  return crypto.subtle.importKey('raw', utf8(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
}

async function sign(secret, data) {
  const key = await hmacKey(secret);
  return b64urlEncode(await crypto.subtle.sign('HMAC', key, utf8(data)));
}

export async function makeSessionCookie(secret, userId, now = Date.now()) {
  const exp = Math.floor(now / 1000) + SESSION_TTL_S;
  const data = `${userId}|${exp}`;
  const value = `${data}|${await sign(secret, data)}`;
  return `${COOKIE}=${encodeURIComponent(value)}; Path=/; Max-Age=${SESSION_TTL_S}; HttpOnly; Secure; SameSite=None`;
}

export function clearSessionCookie() {
  return `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=None`;
}

function readCookie(request, name) {
  const header = request.headers.get('Cookie') || '';
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    if (part.slice(0, i).trim() === name) {
      try {
        return decodeURIComponent(part.slice(i + 1).trim());
      } catch {
        return null;
      }
    }
  }
  return null;
}

// Returns user_id from a valid, unexpired cookie, else null.
export async function sessionUserId(request, secret, now = Date.now()) {
  const raw = readCookie(request, COOKIE);
  if (!raw) return null;
  const parts = raw.split('|');
  if (parts.length !== 3) return null;
  const [userId, expS, mac] = parts;
  if (!/^\d+$/.test(expS) || Number(expS) * 1000 < now) return null;
  const expect = await sign(secret, `${userId}|${expS}`);
  return safeEqual(mac, expect) ? userId : null;
}
