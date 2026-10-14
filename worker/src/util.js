// Shared helpers: JSON responses, errors, base64url, constant-time compare, time.

export class HttpError extends Error {
  constructor(status, code, message, extra) {
    super(message || code);
    this.status = status;
    this.code = code;
    this.extra = extra;
  }
}

export function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers },
  });
}

export function errorResponse(err) {
  if (err instanceof HttpError) {
    return json({ error: err.code, message: err.message, ...(err.extra || {}) }, err.status);
  }
  console.error(err && err.stack ? err.stack : err);
  return json({ error: 'internal', message: 'internal error' }, 500);
}

const enc = new TextEncoder();
export const utf8 = (s) => enc.encode(s);

export function b64urlEncode(bytes) {
  if (!(bytes instanceof Uint8Array)) bytes = new Uint8Array(bytes);
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function b64urlDecode(str) {
  if (typeof str !== 'string' || !/^[A-Za-z0-9_-]*$/.test(str)) throw new Error('bad base64url');
  let s = str.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// Constant-time string comparison (length leak only).
export function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const x = utf8(a);
  const y = utf8(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ (y[i] ?? 0);
  return diff === 0;
}

export function randomId(bytes = 12) {
  return b64urlEncode(crypto.getRandomValues(new Uint8Array(bytes)));
}

// Accept ISO 8601 strings or epoch numbers (seconds if < 1e12, else ms). Returns epoch ms or null.
export function parseTime(v) {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number' && Number.isFinite(v)) return v < 1e12 ? Math.round(v * 1000) : Math.round(v);
  if (typeof v === 'string') {
    if (/^\d+(\.\d+)?$/.test(v)) return parseTime(Number(v));
    const t = Date.parse(v);
    if (!Number.isNaN(t)) return t;
  }
  return undefined; // invalid
}

export const iso = (ms) => (ms === null || ms === undefined ? null : new Date(ms).toISOString());

// Monday 00:00 UTC of the ISO week containing `now`.
export function weekStart(now) {
  const d = new Date(now);
  const day = (d.getUTCDay() + 6) % 7; // 0 = Monday
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - day);
}

export function seasonStart(env) {
  const t = Date.parse(env.SEASON_START || '2026-10-01');
  return Number.isNaN(t) ? 0 : t;
}

export function periodStart(period, env, now) {
  if (period === 'week') return weekStart(now);
  if (period === 'season') return seasonStart(env);
  throw new HttpError(400, 'bad_period', 'period must be week or season');
}

export const MAX_BODY = 16 * 1024;
export const MAX_INGEST_BODY = 2 * 1024 * 1024;

export async function readJson(request, limit = MAX_BODY) {
  const len = Number(request.headers.get('Content-Length') || '0');
  if (len > limit) throw new HttpError(413, 'too_large', 'request body too large');
  const buf = await request.arrayBuffer();
  if (buf.byteLength > limit) throw new HttpError(413, 'too_large', 'request body too large');
  if (buf.byteLength === 0) return {};
  try {
    return JSON.parse(new TextDecoder().decode(buf));
  } catch {
    throw new HttpError(400, 'bad_json', 'body must be JSON');
  }
}
