// AgentpitBench API Worker: entry point, routing, CORS and request guards.
// Contract: docs/backend-api.md (v1).
import { HttpError, errorResponse, safeEqual } from './util.js';
import { sessionUserId } from './auth.js';
import * as h from './handlers.js';

const LOCALHOST_RE = /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?$/;

export function isAllowedOrigin(origin, env) {
  if (!origin) return false;
  if (origin === (env.ALLOWED_ORIGIN || 'https://agentpitbench.org')) return true;
  return env.ALLOW_LOCALHOST === '1' && LOCALHOST_RE.test(origin);
}

function withCors(response, request, env) {
  const origin = request.headers.get('Origin');
  const headers = new Headers(response.headers);
  headers.append('Vary', 'Origin');
  if (isAllowedOrigin(origin, env)) {
    headers.set('Access-Control-Allow-Origin', origin);
    headers.set('Access-Control-Allow-Credentials', 'true');
  }
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

function preflight(request, env) {
  if (!isAllowedOrigin(request.headers.get('Origin'), env)) {
    return new Response(null, { status: 403, headers: { Vary: 'Origin' } });
  }
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': request.headers.get('Origin'),
      'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, X-Bench-Key',
      'Access-Control-Max-Age': '86400',
      Vary: 'Origin',
    },
  });
}

// Request context passed to handlers.
class Ctx {
  constructor(request, env, url) {
    this.request = request;
    this.env = env;
    this.db = env.DB;
    this.url = url;
    this.now = Date.now();
    this._uid = undefined;
  }

  async userId() {
    if (this._uid === undefined) {
      this._uid = env_secret(this.env) ? await sessionUserId(this.request, this.env.SESSION_SECRET, this.now) : null;
    }
    return this._uid;
  }

  // Signed-in user row or null (a valid cookie for a deleted user counts as signed out).
  async user() {
    if (this._user !== undefined) return this._user;
    const uid = await this.userId();
    this._user = uid ? await this.db.prepare('SELECT id, name FROM users WHERE id = ?').bind(uid).first() : null;
    return this._user;
  }

  async requireUser() {
    const u = await this.user();
    if (!u) throw new HttpError(401, 'signed_out', 'sign in first');
    return u;
  }

  requireBenchKey() {
    const expected = this.env.BENCH_INGEST_KEY;
    if (!expected) throw new HttpError(503, 'misconfigured', 'BENCH_INGEST_KEY not set');
    if (!safeEqual(this.request.headers.get('X-Bench-Key') || '', expected)) {
      throw new HttpError(401, 'bad_key', 'X-Bench-Key missing or wrong');
    }
  }
}

function env_secret(env) {
  return typeof env.SESSION_SECRET === 'string' && env.SESSION_SECRET.length >= 16;
}

// [method, pattern, handler, kind]; kind: 'user' (cookie-authenticated browser call) or 'bench' (X-Bench-Key).
const ROUTES = [
  ['GET', '/v1/health', h.health, 'user'],
  ['POST', '/v1/auth/google', h.authGoogle, 'user'],
  ['POST', '/v1/auth/logout', h.logout, 'user'],
  ['GET', '/v1/me', h.me, 'user'],
  ['POST', '/v1/me/name', h.rename, 'user'],
  ['POST', '/v1/me/delete', h.deleteAccount, 'user'],
  ['GET', '/v1/users/:id', h.publicUser, 'user'],
  ['POST', '/v1/picks', h.postPick, 'user'],
  ['GET', '/v1/markets/:id', h.getMarket, 'user'],
  ['GET', '/v1/scoreboard', h.scoreboard, 'user'],
  ['GET', '/v1/comments/:round', h.listComments, 'user'],
  ['POST', '/v1/comments/:round', h.postComment, 'user'],
  ['POST', '/v1/comments/:id/like', h.likeComment, 'user'],
  ['POST', '/v1/comments/:id/report', h.reportComment, 'user'],
  ['POST', '/v1/ingest/markets', h.ingestMarkets, 'bench'],
  ['POST', '/v1/ingest/results', h.ingestResults, 'bench'],
  ['GET', '/v1/admin/review', h.adminReview, 'bench'],
  ['POST', '/v1/admin/comments/:id', h.adminComment, 'bench'],
];

function match(pattern, path) {
  const ps = pattern.split('/');
  const xs = path.split('/');
  if (ps.length !== xs.length) return null;
  const params = {};
  for (let i = 0; i < ps.length; i++) {
    if (ps[i].startsWith(':')) {
      if (!xs[i]) return null;
      try {
        params[ps[i].slice(1)] = decodeURIComponent(xs[i]);
      } catch {
        return null;
      }
    } else if (ps[i] !== xs[i]) return null;
  }
  return params;
}

async function route(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, '') || '/';
  let methodMismatch = false;
  for (const [method, pattern, handler, kind] of ROUTES) {
    const params = match(pattern, path);
    if (!params) continue;
    if (method !== request.method) {
      methodMismatch = true;
      continue;
    }
    const ctx = new Ctx(request, env, url);
    if (kind === 'bench') {
      ctx.requireBenchKey();
    } else if (method === 'POST') {
      // CSRF guard for cookie-authenticated writes: the cookie is SameSite=None, so a foreign
      // origin must be refused and a JSON content type required (forces a CORS preflight).
      const origin = request.headers.get('Origin');
      if (origin && !isAllowedOrigin(origin, env)) throw new HttpError(403, 'bad_origin', 'origin not allowed');
      const ct = (request.headers.get('Content-Type') || '').toLowerCase();
      if (handler !== h.logout && !ct.startsWith('application/json')) {
        throw new HttpError(415, 'json_required', 'Content-Type must be application/json');
      }
    }
    return handler(ctx, params);
  }
  if (methodMismatch) throw new HttpError(405, 'method_not_allowed', 'method not allowed');
  throw new HttpError(404, 'not_found', 'no such endpoint');
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') return preflight(request, env);
    let response;
    try {
      response = await route(request, env);
    } catch (err) {
      response = errorResponse(err);
    }
    return withCors(response, request, env);
  },
};

