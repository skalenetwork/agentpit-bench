import { beforeAll, beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { env } from 'cloudflare:test';
import {
  applySchema, makeGoogleKey, mockJwks, signIdToken, call, bench, signIn, market, ORIGIN, CLIENT_ID,
} from './helpers.js';
import { checkName, containsBlocked } from '../src/words.js';

let keyA;
let keyB;
let keys;
let jwksSpy;

beforeAll(async () => {
  await applySchema();
  keyA = await makeGoogleKey('kid-a');
  keyB = await makeGoogleKey('kid-b');
});

beforeEach(() => {
  keys = [keyA];
  jwksSpy = mockJwks(() => keys);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function freezeAt(ms) {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(ms);
}

// ------------------------------------------------------------------ auth

describe('auth', () => {
  it('signs in with a valid Google ID token, sets the session cookie, and /v1/me works', async () => {
    const credential = await signIdToken(keyA, { sub: 'g-123', email: 'x@example.com' });
    const res = await call('POST', '/v1/auth/google', { body: { credential } });
    expect(res.status).toBe(200);
    expect(res.data.user.name).toMatch(/^[A-Z][a-z]+ [A-Z][a-z]+ \d{4}$/);
    const sc = res.headers.get('Set-Cookie');
    expect(sc).toMatch(/^apb_s=/);
    for (const attr of ['HttpOnly', 'Secure', 'SameSite=None', 'Max-Age=2592000', 'Path=/']) expect(sc).toContain(attr);
    const cookie = sc.split(';')[0];
    const me = await call('GET', '/v1/me', { cookie });
    expect(me.data.user).toEqual(res.data.user);
    expect(me.data.record.vs.claude).toEqual({ w: 0, l: 0, t: 0 });
    // Same sub -> same user; email never stored.
    const again = await call('POST', '/v1/auth/google', { body: { credential: await signIdToken(keyA, { sub: 'g-123' }) } });
    expect(again.data.user.id).toBe(res.data.user.id);
    const row = await env.DB.prepare('SELECT * FROM users WHERE id = ?').bind(res.data.user.id).first();
    expect(JSON.stringify(row)).not.toContain('example.com');
  });

  it('signed out /v1/me returns user null', async () => {
    const me = await call('GET', '/v1/me');
    expect(me.status).toBe(200);
    expect(me.data).toEqual({ user: null, picks: [], record: null });
  });

  it.each([
    ['wrong audience', { aud: 'someone-else' }, {}],
    ['wrong issuer', { iss: 'https://evil.example.com' }, {}],
    ['expired', { exp: Math.floor(Date.now() / 1000) - 3600 }, {}],
    ['wrong alg', {}, { alg: 'HS256' }],
  ])('rejects a token with %s', async (_label, claims, header) => {
    const credential = await signIdToken(keyA, claims, header);
    const res = await call('POST', '/v1/auth/google', { body: { credential } });
    expect(res.status).toBe(401);
    expect(res.data.error).toBe('invalid_token');
  });

  it('rejects a bad signature and garbage', async () => {
    const good = await signIdToken(keyA, { sub: 'x' });
    const forged = await signIdToken(keyA, { sub: 'admin' });
    const tampered = `${forged.split('.').slice(0, 2).join('.')}.${good.split('.')[2]}`;
    expect((await call('POST', '/v1/auth/google', { body: { credential: tampered } })).status).toBe(401);
    expect((await call('POST', '/v1/auth/google', { body: { credential: 'abc' } })).status).toBe(401);
    expect((await call('POST', '/v1/auth/google', { body: {} })).status).toBe(401);
    // Signed by a key Google does not publish.
    const rogue = await signIdToken(await makeGoogleKey('kid-a'), { sub: 'x' });
    expect((await call('POST', '/v1/auth/google', { body: { credential: rogue } })).status).toBe(401);
  });

  it('caches the JWKS and refetches once for an unknown kid (key rotation)', async () => {
    const t0 = Date.now();
    freezeAt(t0);
    await signIn(keyA, 's1');
    await signIn(keyA, 's2');
    expect(jwksSpy).toHaveBeenCalledTimes(1);
    keys = [keyA, keyB];
    vi.setSystemTime(t0 + 60_000);
    await signIn(keyB, 's3');
    expect(jwksSpy).toHaveBeenCalledTimes(2);
    // Unknown kid that is still unknown after refetch: one more fetch at most, then 401.
    const ghost = await makeGoogleKey('kid-ghost');
    vi.setSystemTime(t0 + 120_000);
    const res = await call('POST', '/v1/auth/google', { body: { credential: await signIdToken(ghost) } });
    expect(res.status).toBe(401);
    expect(jwksSpy).toHaveBeenCalledTimes(3);
    // Throttled: an immediate retry with an unknown kid does not refetch.
    await call('POST', '/v1/auth/google', { body: { credential: await signIdToken(ghost) } });
    expect(jwksSpy).toHaveBeenCalledTimes(3);
  });

  it('rejects tampered, expired and foreign session cookies', async () => {
    const { cookie, user } = await signIn(keyA, 'sub-c');
    const [uid, exp, mac] = decodeURIComponent(cookie.slice('apb_s='.length)).split('|');
    const forged = `apb_s=${encodeURIComponent(`other-user|${exp}|${mac}`)}`;
    expect((await call('GET', '/v1/me', { cookie: forged })).data.user).toBeNull();
    const badExp = `apb_s=${encodeURIComponent(`${uid}|${Number(exp) + 10}|${mac}`)}`;
    expect((await call('GET', '/v1/me', { cookie: badExp })).data.user).toBeNull();
    expect((await call('GET', '/v1/me', { cookie })).data.user.id).toBe(user.id);
    freezeAt(Date.now() + 31 * 86400_000);
    expect((await call('GET', '/v1/me', { cookie })).data.user).toBeNull();
  });

  it('logout clears the cookie', async () => {
    const res = await call('POST', '/v1/auth/logout');
    expect(res.status).toBe(200);
    expect(res.headers.get('Set-Cookie')).toMatch(/^apb_s=;.*Max-Age=0/);
  });

  it('needs sign-in for writes', async () => {
    expect((await call('POST', '/v1/picks', { body: { market_id: 'm', outcome: 'Yes' } })).status).toBe(401);
    expect((await call('POST', '/v1/comments/r1', { body: { text: 'hi' } })).status).toBe(401);
    expect((await call('POST', '/v1/me/name', { body: { name: 'Hello' } })).status).toBe(401);
  });
});

// ------------------------------------------------------------------ names

describe('names', () => {
  it('word filter and name rules', () => {
    expect(checkName('Ok').error).toBeTruthy();
    expect(checkName('x'.repeat(25)).error).toBeTruthy();
    expect(checkName('  Market   Wizard ').name).toBe('Market Wizard');
    expect(checkName('sh1t head').error).toBeTruthy();
    expect(checkName('Claude Fan').error).toBe('name reserved');
    expect(checkName('<script>').error).toBeTruthy();
    expect(containsBlocked('Scunthorpe classic assessment')).toBe(false);
    expect(containsBlocked('what an a55')).toBe(true);
  });

  it('renames with case-insensitive uniqueness', async () => {
    const a = await signIn(keyA, 'n1');
    const b = await signIn(keyA, 'n2');
    const r1 = await call('POST', '/v1/me/name', { cookie: a.cookie, body: { name: 'Market Wizard' } });
    expect(r1.status).toBe(200);
    expect(r1.data.user.name).toBe('Market Wizard');
    const r2 = await call('POST', '/v1/me/name', { cookie: b.cookie, body: { name: 'market WIZARD' } });
    expect(r2.status).toBe(409);
    expect(r2.data.error).toBe('name_taken');
    const r3 = await call('POST', '/v1/me/name', { cookie: b.cookie, body: { name: 'fuckface' } });
    expect(r3.status).toBe(400);
    // Changing the case of your own name is fine.
    expect((await call('POST', '/v1/me/name', { cookie: a.cookie, body: { name: 'MARKET wizard' } })).status).toBe(200);
  });
});

// ------------------------------------------------------------------ CORS + guards

describe('CORS and request guards', () => {
  it('echoes the allowed origin with credentials', async () => {
    const res = await call('GET', '/v1/health');
    expect(res.headers.get('Access-Control-Allow-Origin')).toBe(ORIGIN);
    expect(res.headers.get('Access-Control-Allow-Credentials')).toBe('true');
    expect(res.headers.get('Vary')).toContain('Origin');
  });

  it('does not allow other origins', async () => {
    const res = await call('GET', '/v1/health', { origin: 'https://evil.example.com' });
    expect(res.headers.get('Access-Control-Allow-Origin')).toBeNull();
    const local = await call('GET', '/v1/health', { origin: 'http://localhost:8000' });
    expect(local.headers.get('Access-Control-Allow-Origin')).toBeNull();
  });

  it('allows localhost only when ALLOW_LOCALHOST=1', async () => {
    const { default: worker } = await import('../src/index.js');
    const devEnv = { ...env, ALLOW_LOCALHOST: '1' };
    const res = await worker.fetch(new Request('https://x/v1/health', { headers: { Origin: 'http://localhost:8000' } }), devEnv);
    expect(res.headers.get('Access-Control-Allow-Origin')).toBe('http://localhost:8000');
    const res2 = await worker.fetch(new Request('https://x/v1/health', { headers: { Origin: 'http://localhost.evil.com' } }), devEnv);
    expect(res2.headers.get('Access-Control-Allow-Origin')).toBeNull();
  });

  it('answers preflight for the allowed origin only', async () => {
    const { default: worker } = await import('../src/index.js');
    const ok = await worker.fetch(
      new Request('https://x/v1/picks', { method: 'OPTIONS', headers: { Origin: ORIGIN, 'Access-Control-Request-Method': 'POST' } }),
      env,
    );
    expect(ok.status).toBe(204);
    expect(ok.headers.get('Access-Control-Allow-Origin')).toBe(ORIGIN);
    expect(ok.headers.get('Access-Control-Allow-Credentials')).toBe('true');
    expect(ok.headers.get('Access-Control-Allow-Methods')).toContain('POST');
    expect(ok.headers.get('Access-Control-Allow-Headers')).toContain('Content-Type');
    const no = await worker.fetch(new Request('https://x/v1/picks', { method: 'OPTIONS', headers: { Origin: 'https://evil.example.com' } }), env);
    expect(no.status).toBe(403);
    expect(no.headers.get('Access-Control-Allow-Origin')).toBeNull();
  });

  it('refuses cookie writes from foreign origins and non-JSON bodies (CSRF)', async () => {
    const { cookie } = await signIn(keyA, 'csrf');
    const foreign = await call('POST', '/v1/me/name', { cookie, origin: 'https://evil.example.com', body: { name: 'Pwned Name' } });
    expect(foreign.status).toBe(403);
    const form = await call('POST', '/v1/me/name', {
      cookie,
      raw: 'name=Pwned',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    expect(form.status).toBe(415);
    const del = await call('POST', '/v1/me/delete', { cookie, origin: 'https://evil.example.com' });
    expect(del.status).toBe(403);
  });

  it('returns 404/405 JSON errors and caps body size', async () => {
    expect((await call('GET', '/v1/nope')).status).toBe(404);
    expect((await call('GET', '/v1/picks')).status).toBe(405);
    const { cookie } = await signIn(keyA, 'big');
    const res = await call('POST', '/v1/comments/r1', { cookie, body: { text: 'x'.repeat(20000) } });
    expect(res.status).toBe(413);
  });
});

// ------------------------------------------------------------------ ingest key

describe('ingest key', () => {
  it('requires the correct X-Bench-Key on ingest and admin endpoints', async () => {
    for (const [method, path] of [
      ['POST', '/v1/ingest/markets'],
      ['POST', '/v1/ingest/results'],
      ['GET', '/v1/admin/review'],
      ['POST', '/v1/admin/comments/1'],
    ]) {
      const none = await call(method, path, { body: method === 'POST' ? [] : undefined, origin: null });
      expect(none.status).toBe(401);
      const wrong = await call(method, path, { body: method === 'POST' ? [] : undefined, origin: null, headers: { 'X-Bench-Key': 'nope' } });
      expect(wrong.status).toBe(401);
    }
    expect((await bench('POST', '/v1/ingest/markets', [market('k1')])).status).toBe(200);
    // A signed-in user cannot ingest.
    const { cookie } = await signIn(keyA, 'u-ingest');
    expect((await call('POST', '/v1/ingest/markets', { cookie, body: [market('k2')] })).status).toBe(401);
  });

  it('validates markets', async () => {
    expect((await bench('POST', '/v1/ingest/markets', [{ market_id: 'x' }])).status).toBe(400);
    expect((await bench('POST', '/v1/ingest/markets', [market('x', { outcomes: ['Only'] })])).status).toBe(400);
    expect((await bench('POST', '/v1/ingest/markets', [market('x', { locks_at: 'not a date' })])).status).toBe(400);
    // Epoch seconds accepted.
    expect((await bench('POST', '/v1/ingest/markets', [market('x', { locks_at: Math.floor(Date.now() / 1000) + 60 })])).status).toBe(200);
  });
});

// ------------------------------------------------------------------ picks and lock times

describe('picks and lock times', () => {
  it('accepts and changes a pick before lock, refuses after (409); counts hidden until lock', async () => {
    const t0 = Date.parse('2026-10-14T12:00:00Z');
    freezeAt(t0);
    await bench('POST', '/v1/ingest/markets', [
      market('m1', { opens_at: '2026-10-14T00:00:00Z', locks_at: '2026-10-14T15:00:00Z' }),
    ]);
    const a = await signIn(keyA, 'pa');
    const b = await signIn(keyA, 'pb');
    let r = await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'm1', outcome: 'Yes' } });
    expect(r.status).toBe(200);
    expect(r.data.pick).toMatchObject({ market_id: 'm1', outcome: 'Yes' });
    r = await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'm1', outcome: 'No' } });
    expect(r.data.pick.outcome).toBe('No');
    await call('POST', '/v1/picks', { cookie: b.cookie, body: { market_id: 'm1', outcome: 'No' } });
    expect((await call('POST', '/v1/picks', { cookie: b.cookie, body: { market_id: 'm1', outcome: 'Maybe' } })).status).toBe(400);
    expect((await call('POST', '/v1/picks', { cookie: b.cookie, body: { market_id: 'nope', outcome: 'Yes' } })).status).toBe(404);

    let m = await call('GET', '/v1/markets/m1', { cookie: a.cookie });
    expect(m.status).toBe(200);
    expect(m.data.locked).toBe(false);
    expect(m.data.counts).toBeNull();
    expect(m.data.my_pick).toBe('No');
    expect(m.data.market.outcomes).toEqual(['Yes', 'No']);
    expect(m.data.market.locks_at).toBe('2026-10-14T15:00:00.000Z');

    vi.setSystemTime(Date.parse('2026-10-14T14:59:59Z'));
    expect((await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'm1', outcome: 'Yes' } })).status).toBe(200);
    vi.setSystemTime(Date.parse('2026-10-14T15:00:00Z'));
    r = await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'm1', outcome: 'No' } });
    expect(r.status).toBe(409);
    expect(r.data.error).toBe('locked');

    m = await call('GET', '/v1/markets/m1');
    expect(m.data.locked).toBe(true);
    expect(m.data.counts).toEqual({ Yes: 1, No: 1 });
    expect(m.data.my_pick).toBeNull();
  });

  it('refuses picks before opens_at and after resolution; lock moves with re-ingest', async () => {
    const t0 = Date.parse('2026-10-14T12:00:00Z');
    freezeAt(t0);
    await bench('POST', '/v1/ingest/markets', [market('m2', { opens_at: '2026-10-14T13:00:00Z', locks_at: '2026-10-14T18:00:00Z' })]);
    const a = await signIn(keyA, 'pc');
    const r = await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'm2', outcome: 'Yes' } });
    expect(r.status).toBe(409);
    expect(r.data.error).toBe('not_open');
    await bench('POST', '/v1/ingest/markets', [market('m2', { opens_at: null, locks_at: '2026-10-14T11:00:00Z' })]);
    expect((await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'm2', outcome: 'Yes' } })).status).toBe(409);
    await bench('POST', '/v1/ingest/markets', [market('m2', { opens_at: null, locks_at: '2026-10-15T11:00:00Z' })]);
    expect((await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'm2', outcome: 'Yes' } })).status).toBe(200);
    await bench('POST', '/v1/ingest/results', [{ market_id: 'm2', round_id: 'r-m2', winner: 'Yes', ai_picks: {} }]);
    expect((await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'm2', outcome: 'No' } })).status).toBe(409);
  });
});

// ------------------------------------------------------------------ scoring + scoreboard

describe('scoring', () => {
  async function setupResolved() {
    // Wednesday 2026-10-14; ISO week starts Monday 2026-10-12.
    freezeAt(Date.parse('2026-10-14T10:00:00Z'));
    const ids = ['s1', 's2', 's3', 's4', 's5', 's6'];
    await bench('POST', '/v1/ingest/markets', ids.map((id) => market(id, { opens_at: null, locks_at: '2026-10-14T12:00:00Z' })));
    const alice = await signIn(keyA, 'alice');
    const bob = await signIn(keyA, 'bob');
    // Alice picks Yes everywhere, Bob picks No everywhere.
    for (const id of ids) {
      await call('POST', '/v1/picks', { cookie: alice.cookie, body: { market_id: id, outcome: 'Yes' } });
      await call('POST', '/v1/picks', { cookie: bob.cookie, body: { market_id: id, outcome: 'No' } });
    }
    vi.setSystemTime(Date.parse('2026-10-14T20:00:00Z'));
    // Winner Yes in s1..s5, No in s6. Claude always Yes, Grok always No, Codex no pick.
    const res = await bench(
      'POST',
      '/v1/ingest/results',
      ids.map((id) => ({
        market_id: id,
        round_id: `r-${id}`,
        winner: id === 's6' ? 'No' : 'Yes',
        ai_picks: { claude: 'Yes', grok: 'No', codex: null, agy: 'Yes' },
      })),
    );
    expect(res.status).toBe(200);
    expect(res.data).toMatchObject({ scored: 6, voided: 0, unknown: [] });
    return { alice, bob };
  }

  it('scores each human pick vs the winner and vs each AI', async () => {
    const { alice, bob } = await setupResolved();
    const me = await call('GET', '/v1/me', { cookie: alice.cookie });
    expect(me.data.record.week).toEqual({ picks: 6, correct: 5, pending: 0 });
    expect(me.data.record.season).toEqual({ picks: 6, correct: 5, pending: 0 });
    // Alice == Claude everywhere -> 6 ties; vs Grok: Alice right 5 (Grok wrong) -> w, s6 Alice wrong Grok right -> l.
    expect(me.data.record.vs.claude).toEqual({ w: 0, l: 0, t: 6 });
    expect(me.data.record.vs.grok).toEqual({ w: 5, l: 1, t: 0 });
    expect(me.data.record.vs.codex).toEqual({ w: 0, l: 0, t: 0 });
    const s6 = me.data.picks.find((p) => p.market_id === 's6');
    expect(s6).toMatchObject({ outcome: 'Yes', correct: false, winner: 'No', resolved: true, locked: true });
    expect(s6.vs).toEqual({ claude: 't', grok: 'l', agy: 't' });
    const bobMe = await call('GET', '/v1/me', { cookie: bob.cookie });
    expect(bobMe.data.record.vs.claude).toEqual({ w: 1, l: 5, t: 0 });

    const sb = await call('GET', '/v1/scoreboard?period=week');
    expect(sb.status).toBe(200);
    expect(sb.data.humans).toEqual({ picks: 12, correct: 6 });
    expect(sb.data.by_ai.claude).toEqual({ human_wins: 1, ai_wins: 5, ties: 6 });
    expect(sb.data.by_ai.grok).toEqual({ human_wins: 5, ai_wins: 1, ties: 6 });
    expect(sb.data.top.map((t) => [t.name, t.correct, t.picks])).toEqual([
      [alice.user.name, 5, 6],
      [bob.user.name, 1, 6],
    ]);
    expect((await call('GET', '/v1/scoreboard?period=season')).data.humans).toEqual({ picks: 12, correct: 6 });
    expect((await call('GET', '/v1/scoreboard?period=year')).status).toBe(400);

    // Next week: the week scoreboard is empty, the season one is not.
    vi.setSystemTime(Date.parse('2026-10-19T01:00:00Z'));
    expect((await call('GET', '/v1/scoreboard?period=week')).data.humans).toEqual({ picks: 0, correct: 0 });
    expect((await call('GET', '/v1/scoreboard?period=season')).data.top).toHaveLength(2);
  });

  it('re-ingesting results rescored idempotently; void winner unscores', async () => {
    const { alice } = await setupResolved();
    await bench('POST', '/v1/ingest/results', [{ market_id: 's6', round_id: 'r-s6', winner: 'Yes', ai_picks: { claude: 'Yes' } }]);
    let me = await call('GET', '/v1/me', { cookie: alice.cookie });
    expect(me.data.record.week.correct).toBe(6);
    const res = await bench('POST', '/v1/ingest/results', [
      { market_id: 's6', round_id: 'r-s6', winner: null, ai_picks: {} },
      { market_id: 'ghost', round_id: 'r', winner: 'Yes', ai_picks: {} },
    ]);
    expect(res.data).toMatchObject({ scored: 0, voided: 1, unknown: ['ghost'] });
    me = await call('GET', '/v1/me', { cookie: alice.cookie });
    expect(me.data.record.week).toMatchObject({ picks: 5, correct: 5 });
  });

  it('top needs at least 5 scored picks; x_poll is aggregated', async () => {
    await setupResolved();
    freezeAt(Date.parse('2026-10-14T10:00:00Z'));
    const carol = await signIn(keyA, 'carol');
    await bench('POST', '/v1/ingest/markets', [market('t1', { opens_at: null, locks_at: '2026-10-14T12:00:00Z' })]);
    await call('POST', '/v1/picks', { cookie: carol.cookie, body: { market_id: 't1', outcome: 'Yes' } });
    vi.setSystemTime(Date.parse('2026-10-14T20:00:00Z'));
    await bench('POST', '/v1/ingest/results', [
      { market_id: 't1', round_id: 'r-t1', winner: 'Yes', ai_picks: {}, x_poll: { Yes: 30, No: 12 } },
    ]);
    const sb = await call('GET', '/v1/scoreboard?period=week');
    expect(sb.data.top.map((t) => t.name)).not.toContain(carol.user.name);
    expect(sb.data.x_poll).toEqual({ markets: 1, votes: 42, correct: 30 });
  });

  it('public user record by id', async () => {
    const { alice } = await setupResolved();
    const res = await call('GET', `/v1/users/${alice.user.id}`);
    expect(res.data.user.name).toBe(alice.user.name);
    expect(res.data.record.week.correct).toBe(5);
    expect((await call('GET', '/v1/users/nope')).status).toBe(404);
  });
});

// ------------------------------------------------------------------ comments

describe('comments', () => {
  it('posts, lists with picks, replies one level, plain text', async () => {
    freezeAt(Date.parse('2026-10-14T10:00:00Z'));
    await bench('POST', '/v1/ingest/markets', [market('c1', { round_id: 'round-1', opens_at: null })]);
    const a = await signIn(keyA, 'ca');
    const b = await signIn(keyA, 'cb');
    await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'c1', outcome: 'Yes' } });
    const c1 = await call('POST', '/v1/comments/round-1', { cookie: a.cookie, body: { text: '  <b>Yes</b> https://x.com/a  ' } });
    expect(c1.status).toBe(201);
    expect(c1.data).toMatchObject({ text: '<b>Yes</b> https://x.com/a', pick: 'Yes', likes: 0, parent_id: null, hidden: false });
    expect(c1.data.user).toEqual(a.user);
    vi.setSystemTime(Date.now() + 11_000);
    const reply = await call('POST', '/v1/comments/round-1', { cookie: b.cookie, body: { text: 'nah', parent_id: c1.data.id } });
    expect(reply.status).toBe(201);
    expect(reply.data.parent_id).toBe(c1.data.id);
    expect(reply.data.pick).toBeNull();
    vi.setSystemTime(Date.now() + 11_000);
    const nested = await call('POST', '/v1/comments/round-1', { cookie: a.cookie, body: { text: 'deep', parent_id: reply.data.id } });
    expect(nested.status).toBe(400);
    const other = await call('POST', '/v1/comments/round-2', { cookie: a.cookie, body: { text: 'x', parent_id: c1.data.id } });
    expect(other.status).toBe(400);

    const list = await call('GET', '/v1/comments/round-1?sort=new');
    expect(list.data.map((c) => c.id)).toEqual([reply.data.id, c1.data.id]);
    const pickers = await call('GET', '/v1/comments/round-1?pickers_only=1');
    expect(pickers.data.map((c) => c.id)).toEqual([c1.data.id]);
    expect((await call('GET', '/v1/comments/round-1?sort=old')).status).toBe(400);
  });

  it('enforces length and word filter', async () => {
    const a = await signIn(keyA, 'cl');
    expect((await call('POST', '/v1/comments/r', { cookie: a.cookie, body: { text: '   ' } })).status).toBe(400);
    expect((await call('POST', '/v1/comments/r', { cookie: a.cookie, body: { text: 'x'.repeat(501) } })).status).toBe(400);
    expect((await call('POST', '/v1/comments/r', { cookie: a.cookie, body: { text: 'é'.repeat(500) } })).status).toBe(201);
    freezeAt(Date.now() + 11_000);
    const bad = await call('POST', '/v1/comments/r', { cookie: a.cookie, body: { text: 'you are a f.u.c.k.i.n.g idiot' } });
    expect(bad.status).toBe(400);
    expect(bad.data.error).toBe('blocked_words');
  });

  it('rate limits: 1 per 10 s, 5 per minute, 50 per day', async () => {
    const t0 = Date.parse('2026-10-14T10:00:00Z');
    freezeAt(t0);
    const a = await signIn(keyA, 'rl');
    const post = () => call('POST', '/v1/comments/r', { cookie: a.cookie, body: { text: 'hello' } });
    expect((await post()).status).toBe(201);
    const fast = await post();
    expect(fast.status).toBe(429);
    expect(fast.data.retry_after).toBeGreaterThan(0);
    // 10.5 s apart: 6 within 60 s would exceed 5/min.
    let t = t0;
    for (let i = 0; i < 4; i++) {
      t += 10_500;
      vi.setSystemTime(t);
      expect((await post()).status).toBe(201);
    }
    t += 10_500;
    vi.setSystemTime(t);
    expect((await post()).status).toBe(429);
    // Daily cap: spread posts 61 s apart.
    let ok = 5;
    while (ok < 50) {
      t += 61_000;
      vi.setSystemTime(t);
      expect((await post()).status).toBe(201);
      ok++;
    }
    t += 61_000;
    vi.setSystemTime(t);
    const daily = await post();
    expect(daily.status).toBe(429);
    expect(daily.data.message).toContain('per day');
    vi.setSystemTime(t0 + 86_400_000 + 1000);
    expect((await post()).status).toBe(201);
  });

  it('likes toggle and sort=top', async () => {
    freezeAt(Date.parse('2026-10-14T10:00:00Z'));
    const a = await signIn(keyA, 'la');
    const b = await signIn(keyA, 'lb');
    const c1 = (await call('POST', '/v1/comments/rl', { cookie: a.cookie, body: { text: 'first' } })).data;
    vi.setSystemTime(Date.now() + 11_000);
    const c2 = (await call('POST', '/v1/comments/rl', { cookie: a.cookie, body: { text: 'second' } })).data;
    let r = await call('POST', `/v1/comments/${c1.id}/like`, { cookie: b.cookie });
    expect(r.data).toEqual({ id: c1.id, liked: true, likes: 1 });
    expect((await call('GET', '/v1/comments/rl?sort=top')).data.map((c) => c.id)).toEqual([c1.id, c2.id]);
    expect((await call('GET', '/v1/comments/rl', { cookie: b.cookie })).data.find((c) => c.id === c1.id).liked).toBe(true);
    r = await call('POST', `/v1/comments/${c1.id}/like`, { cookie: b.cookie });
    expect(r.data).toEqual({ id: c1.id, liked: false, likes: 0 });
    expect((await call('POST', '/v1/comments/99999/like', { cookie: b.cookie })).status).toBe(404);
  });

  it('3 distinct reports hide a comment; operator review/restore/hide/delete', async () => {
    freezeAt(Date.parse('2026-10-14T10:00:00Z'));
    const author = await signIn(keyA, 'author');
    const reporters = [];
    for (const s of ['r1', 'r2', 'r3', 'r4']) reporters.push(await signIn(keyA, s));
    const c = (await call('POST', '/v1/comments/rr', { cookie: author.cookie, body: { text: 'spicy take' } })).data;
    const reply = (await call('POST', '/v1/comments/rr', { cookie: reporters[3].cookie, body: { text: 'agree', parent_id: c.id } })).data;
    const report = (who) => call('POST', `/v1/comments/${c.id}/report`, { cookie: who.cookie, body: { reason: 'rude' } });
    expect((await call('POST', `/v1/comments/${c.id}/report`, { cookie: author.cookie, body: {} })).status).toBe(400);
    expect((await report(reporters[0])).data.hidden).toBe(false);
    expect((await report(reporters[0])).data.hidden).toBe(false); // same user twice counts once
    expect((await report(reporters[1])).data.hidden).toBe(false);
    expect((await call('GET', '/v1/comments/rr')).data).toHaveLength(2);
    expect((await report(reporters[2])).data.hidden).toBe(true);
    // Hidden comment and its replies are omitted.
    expect((await call('GET', '/v1/comments/rr')).data).toHaveLength(0);
    expect((await call('POST', `/v1/comments/${c.id}/like`, { cookie: reporters[0].cookie })).status).toBe(404);

    const review = await bench('GET', '/v1/admin/review?since=2026-10-14T00:00:00Z');
    expect(review.status).toBe(200);
    const row = review.data.comments.find((x) => x.id === c.id);
    expect(row).toMatchObject({ hidden: true, hidden_reason: 'reports', reports: 3, report_reasons: ['rude', 'rude', 'rude'] });

    vi.setSystemTime(Date.now() + 1000);
    expect((await bench('POST', `/v1/admin/comments/${c.id}`, { action: 'restore' })).status).toBe(200);
    expect((await call('GET', '/v1/comments/rr')).data).toHaveLength(2);
    // After restore, old reports no longer count: one new report does not re-hide.
    expect((await report(reporters[3])).data.hidden).toBe(false);
    expect((await call('GET', '/v1/comments/rr')).data).toHaveLength(2);

    expect((await bench('POST', `/v1/admin/comments/${c.id}`, { action: 'hide' })).status).toBe(200);
    expect((await call('GET', '/v1/comments/rr')).data).toHaveLength(0);
    expect((await bench('POST', `/v1/admin/comments/${c.id}`, { action: 'nuke' })).status).toBe(400);
    expect((await bench('POST', `/v1/admin/comments/${c.id}`, { action: 'delete' })).status).toBe(200);
    const left = await env.DB.prepare('SELECT COUNT(*) AS n FROM comments WHERE id IN (?, ?)').bind(c.id, reply.id).first();
    expect(left.n).toBe(0);
    expect((await bench('POST', `/v1/admin/comments/${c.id}`, { action: 'restore' })).status).toBe(404);
  });
});

// ------------------------------------------------------------------ account deletion

describe('account deletion', () => {
  it('removes the user and picks, anonymises comments, clears the cookie', async () => {
    freezeAt(Date.parse('2026-10-14T10:00:00Z'));
    await bench('POST', '/v1/ingest/markets', [market('d1', { round_id: 'rd', opens_at: null })]);
    const a = await signIn(keyA, 'del-a');
    const b = await signIn(keyA, 'del-b');
    await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'd1', outcome: 'Yes' } });
    const c = (await call('POST', '/v1/comments/rd', { cookie: a.cookie, body: { text: 'bye' } })).data;
    const cb = (await call('POST', '/v1/comments/rd', { cookie: b.cookie, body: { text: 'hi' } })).data;
    await call('POST', `/v1/comments/${cb.id}/like`, { cookie: a.cookie });
    const res = await call('POST', '/v1/me/delete', { cookie: a.cookie });
    expect(res.status).toBe(200);
    expect(res.headers.get('Set-Cookie')).toMatch(/Max-Age=0/);
    // Old cookie no longer works.
    expect((await call('GET', '/v1/me', { cookie: a.cookie })).data.user).toBeNull();
    expect((await call('POST', '/v1/picks', { cookie: a.cookie, body: { market_id: 'd1', outcome: 'No' } })).status).toBe(401);
    const list = (await call('GET', '/v1/comments/rd')).data;
    const anon = list.find((x) => x.id === c.id);
    expect(anon.user).toEqual({ id: null, name: 'Deleted user' });
    expect(anon.text).toBe('bye');
    expect(list.find((x) => x.id === cb.id).likes).toBe(0);
    const picks = await env.DB.prepare('SELECT COUNT(*) AS n FROM picks WHERE user_id = ?').bind(a.user.id).first();
    expect(picks.n).toBe(0);
    expect(await env.DB.prepare('SELECT * FROM users WHERE id = ?').bind(a.user.id).first()).toBeNull();
    // Signing in again with the same Google account creates a fresh user.
    const again = await signIn(keyA, 'del-a');
    expect(again.user.id).not.toBe(a.user.id);
  });
});

it('client id constant matches test bindings', () => {
  expect(env.GOOGLE_CLIENT_ID).toBe(CLIENT_ID);
});
