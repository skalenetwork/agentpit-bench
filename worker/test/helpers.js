// Test helpers: schema setup, a mock Google JWKS (local RSA key), signed ID tokens, request helpers.
import { env } from 'cloudflare:test';
import { vi } from 'vitest';
import schema from '../schema.sql?raw';
import worker from '../src/index.js';
import { _resetJwksCache, GOOGLE_JWKS_URL } from '../src/auth.js';
import { b64urlEncode } from '../src/util.js';

export const ORIGIN = 'https://agentpitbench.org';
export const CLIENT_ID = 'test-client.apps.googleusercontent.com';
export const BENCH_KEY = 'test-bench-key';

export async function applySchema() {
  const stmts = schema
    .split('\n')
    .map((l) => l.replace(/--.*$/, ''))
    .join('\n')
    .split(';')
    .map((s) => s.trim())
    .filter(Boolean);
  await env.DB.batch(stmts.map((s) => env.DB.prepare(s)));
}

export async function clearTables() {
  for (const t of ['users', 'markets', 'picks', 'pick_vs', 'comments', 'likes', 'reports', 'rate']) {
    await env.DB.prepare(`DELETE FROM ${t}`).run();
  }
}

// ---- Google JWKS mock

export async function makeGoogleKey(kid) {
  const pair = await crypto.subtle.generateKey(
    { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    true,
    ['sign', 'verify'],
  );
  const jwk = await crypto.subtle.exportKey('jwk', pair.publicKey);
  return { kid, privateKey: pair.privateKey, jwk: { kty: 'RSA', n: jwk.n, e: jwk.e, kid, alg: 'RS256', use: 'sig' } };
}

// Installs a fetch stub serving the given keys (a function so tests can rotate keys). Returns the spy.
export function mockJwks(getKeys) {
  _resetJwksCache();
  const spy = vi.fn(async (input) => {
    const url = typeof input === 'string' ? input : input.url;
    if (url !== GOOGLE_JWKS_URL) throw new Error(`unexpected fetch ${url}`);
    return new Response(JSON.stringify({ keys: getKeys().map((k) => k.jwk) }), {
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=3600' },
    });
  });
  vi.stubGlobal('fetch', spy);
  return spy;
}

const enc = (o) => b64urlEncode(new TextEncoder().encode(JSON.stringify(o)));

export async function signIdToken(key, claims = {}, header = {}) {
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    iss: 'https://accounts.google.com',
    aud: CLIENT_ID,
    sub: 'sub-1',
    iat: now,
    exp: now + 3600,
    ...claims,
  };
  const h = { alg: 'RS256', kid: key.kid, typ: 'JWT', ...header };
  const data = `${enc(h)}.${enc(payload)}`;
  const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key.privateKey, new TextEncoder().encode(data));
  return `${data}.${b64urlEncode(sig)}`;
}

// ---- requests

export async function call(method, path, { body, cookie, origin = ORIGIN, headers = {}, raw } = {}) {
  const h = new Headers(headers);
  if (origin) h.set('Origin', origin);
  if (cookie) h.set('Cookie', cookie);
  let payload;
  if (raw !== undefined) payload = raw;
  else if (body !== undefined) {
    payload = JSON.stringify(body);
    if (!h.has('Content-Type')) h.set('Content-Type', 'application/json');
  } else if (method === 'POST' && !h.has('Content-Type')) h.set('Content-Type', 'application/json');
  const res = await worker.fetch(new Request(`https://api.agentpitbench.org${path}`, { method, headers: h, body: payload }), env);
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  return { status: res.status, headers: res.headers, data };
}

export const bench = (method, path, body) => call(method, path, { body, origin: null, headers: { 'X-Bench-Key': BENCH_KEY } });

// Signs in a Google subject and returns {cookie, user}.
export async function signIn(key, sub) {
  const credential = await signIdToken(key, { sub });
  const res = await call('POST', '/v1/auth/google', { body: { credential } });
  if (res.status !== 200) throw new Error(`sign-in failed: ${res.status} ${JSON.stringify(res.data)}`);
  const setCookie = res.headers.get('Set-Cookie');
  return { cookie: setCookie.split(';')[0], user: res.data.user, setCookie };
}

export function market(id, overrides = {}) {
  const now = Date.now();
  return {
    market_id: id,
    round_id: `r-${id}`,
    question: `Will ${id} happen?`,
    event_title: `Event ${id}`,
    outcomes: ['Yes', 'No'],
    opens_at: new Date(now - 3600_000).toISOString(),
    locks_at: new Date(now + 3600_000).toISOString(),
    url: `https://polymarket.com/event/${id}`,
    ...overrides,
  };
}
