// docs/own-pick-spec.md v0.3 (§9 Worker): late picks, the import, referrals and CTA arms, /v1/me's challenge and
// per-pick `late`, the public pick behind the referral bar, X handle links, the funnel counters, GET /v1/admin/picks.
import { beforeAll, beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { env } from 'cloudflare:test';
import { applySchema, makeGoogleKey, mockJwks, signIdToken, call, bench, signIn, market } from './helpers.js';

let key;

beforeAll(async () => {
  await applySchema();
  key = await makeGoogleKey('kid-op');
});

beforeEach(() => {
  mockJwks(() => [key]);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function freezeAt(ms) {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(ms);
}

const pick = (who, market_id, outcome, extra = {}) => call('POST', '/v1/picks', { cookie: who.cookie, body: { market_id, outcome, ...extra } });

describe('late picks (§4)', () => {
  it('a first pick after locks_at is stored late, never changes, and stays out of counts, record and scoreboard', async () => {
    freezeAt(Date.parse('2026-11-04T10:00:00Z'));
    await bench('POST', '/v1/ingest/markets', [
      market('op-l1', { opens_at: null, locks_at: '2026-11-04T12:00:00Z', end_date: '2026-11-04T20:00:00Z' }),
    ]);
    const early = await signIn(key, 'op-early');
    const lateU = await signIn(key, 'op-late');
    let r = await pick(early, 'op-l1', 'Yes');
    expect(r.status).toBe(200);
    expect(r.data.late).toBe(false);
    expect(r.data.pick.closes_at).toBe('2026-11-04T20:00:00.000Z');

    vi.setSystemTime(Date.parse('2026-11-04T13:00:00Z'));
    expect((await pick(early, 'op-l1', 'No')).status).toBe(409); // a counted pick never changes after the lock
    r = await pick(lateU, 'op-l1', 'Yes');
    expect(r.status).toBe(200);
    expect(r.data.late).toBe(true);
    expect(r.data.pick.late).toBe(true);
    r = await pick(lateU, 'op-l1', 'No'); // a late pick never changes
    expect(r.status).toBe(409);
    expect(r.data.error).toBe('locked');

    const m = await call('GET', '/v1/markets/op-l1', { cookie: lateU.cookie });
    expect(m.data.counts).toEqual({ Yes: 1, No: 0 }); // counted picks only
    expect(m.data.my_pick).toBe('Yes');
    expect(m.data.my_late).toBe(true);
    expect(m.data.late_open).toBe(true);
    expect(m.data.market.ai_picks).toBeNull(); // sealed until the result

    await bench('POST', '/v1/ingest/results', [{ market_id: 'op-l1', round_id: 'r-op-l1', winner: 'Yes', ai_picks: { claude: 'No' } }]);
    expect((await pick(await signIn(key, 'op-after'), 'op-l1', 'Yes')).status).toBe(409); // after the result: locked

    const me = await call('GET', '/v1/me', { cookie: lateU.cookie });
    const p = me.data.picks.find((x) => x.market_id === 'op-l1');
    expect(p).toMatchObject({ late: true, correct: true, vs: {} }); // scored for the challenge, never vs the AIs
    expect(me.data.record.season.picks).toBe(0);
    expect(me.data.record.vs.claude).toEqual({ w: 0, l: 0, t: 0 });
    const meE = await call('GET', '/v1/me', { cookie: early.cookie });
    expect(meE.data.picks.find((x) => x.market_id === 'op-l1')).toMatchObject({ late: false, vs: { claude: 'w' } });
    const vs = await env.DB.prepare("SELECT COUNT(*) AS n FROM pick_vs WHERE market_id = 'op-l1'").first();
    expect(vs.n).toBe(1);
    const sb = await call('GET', '/v1/scoreboard?period=season');
    const lateRows = await env.DB.prepare("SELECT COUNT(*) AS n FROM picks WHERE late = 1 AND correct = 1").first();
    expect(lateRows.n).toBeGreaterThan(0);
    const counted = await env.DB.prepare(
      "SELECT COUNT(p.correct) AS n FROM picks p JOIN markets m ON m.market_id = p.market_id WHERE p.late = 0 AND m.locks_at >= ?",
    ).bind(Date.parse('2026-10-01')).first();
    expect(sb.data.humans.picks).toBe(counted.n);
  });
});

describe('late picks stop at the reveal', () => {
  it('no late pick (or late import) once the read model says the round on the market is unsealed', async () => {
    freezeAt(Date.parse('2026-11-11T13:00:00Z'));
    await bench('POST', '/v1/ingest/markets', [market('op-rv', { opens_at: null, locks_at: '2026-11-11T12:00:00Z' })]);
    await env.DB.prepare(
      "INSERT INTO api_rounds (round_id, market_id, state, sealed, counts, data, updated_at) VALUES (9901, 'op-rv', 'running', 1, 0, '{}', 0)",
    ).run();
    const u = await signIn(key, 'op-rv-1');
    expect((await pick(u, 'op-rv', 'Yes')).data.late).toBe(true);
    await env.DB.prepare("UPDATE api_rounds SET sealed = 0, state = 'awaiting_resolution' WHERE round_id = 9901").run();
    const v = await signIn(key, 'op-rv-2');
    expect((await pick(v, 'op-rv', 'Yes')).status).toBe(409);
    const imp = await call('POST', '/v1/picks/import', { cookie: v.cookie, body: [{ market_id: 'op-rv', outcome: 'No' }] });
    expect(imp.data).toMatchObject({ late: 0, dropped: 1 });
  });
});

describe('import (§6)', () => {
  it('ignores the client time: counted before lock, late after, dropped after the result; existing picks win', async () => {
    freezeAt(Date.parse('2026-11-05T10:00:00Z'));
    await bench('POST', '/v1/ingest/markets', [
      market('op-i1', { opens_at: null, locks_at: '2026-11-05T12:00:00Z' }),
      market('op-i2', { opens_at: null, locks_at: '2026-11-05T09:00:00Z' }),
      market('op-i3', { opens_at: null, locks_at: '2026-11-05T08:00:00Z' }),
      market('op-i4', { opens_at: null, locks_at: '2026-11-05T12:00:00Z' }),
    ]);
    await bench('POST', '/v1/ingest/results', [{ market_id: 'op-i3', round_id: 'r-op-i3', winner: 'No', ai_picks: {} }]);
    const u = await signIn(key, 'op-imp');
    expect((await pick(u, 'op-i4', 'No')).status).toBe(200);
    const old = Date.parse('2026-11-01T00:00:00Z'); // "before every lock" on the device's clock: never trusted
    const r = await call('POST', '/v1/picks/import', {
      cookie: u.cookie,
      body: [
        { market_id: 'op-i1', outcome: 'Yes', t: old },
        { market_id: 'op-i2', outcome: 'Yes', t: old },
        { market_id: 'op-i3', outcome: 'No', t: old },
        { market_id: 'op-i4', outcome: 'Yes', t: old },
        { market_id: 'nope', outcome: 'Yes', t: old },
        { market_id: 'op-i1', outcome: 'Maybe' },
      ],
    });
    expect(r.status).toBe(200);
    expect(r.data).toMatchObject({ counted: 1, late: 1, dropped: 1, kept: 1, unknown: 2 });
    const me = await call('GET', '/v1/me', { cookie: u.cookie });
    const by = Object.fromEntries(me.data.picks.map((p) => [p.market_id, p]));
    expect(by['op-i1']).toMatchObject({ outcome: 'Yes', late: false });
    expect(by['op-i2']).toMatchObject({ outcome: 'Yes', late: true });
    expect(by['op-i3']).toBeUndefined();
    expect(by['op-i4']).toMatchObject({ outcome: 'No', late: false });
    expect((await call('POST', '/v1/picks/import', { cookie: u.cookie, body: new Array(101).fill({}) })).status).toBe(400);
    expect((await call('POST', '/v1/picks/import', { body: [] })).status).toBe(401);
  });
});

describe('referrals, CTA arms and the funnel (§5, §8)', () => {
  it('stores ref only on the first pick, never a self-ref; counts CTA opens and shares; the funnel adds it up', async () => {
    freezeAt(Date.parse('2027-01-05T10:00:00Z'));
    await bench('POST', '/v1/ingest/markets', [
      market('op-r1', { opens_at: null, locks_at: '2027-01-05T12:00:00Z' }),
      market('op-r2', { opens_at: null, locks_at: '2027-01-05T12:00:00Z' }),
    ]);
    const a = await signIn(key, 'op-ref-a');
    const b = await signIn(key, 'op-ref-b');
    const c = await signIn(key, 'op-ref-c');
    await pick(a, 'op-r1', 'Yes', { ref: a.user.id, cta_arm: 'arm-1' }); // self-ref ignored
    await pick(b, 'op-r1', 'No', { ref: a.user.id, cta_arm: 'arm-2' });
    await pick(b, 'op-r1', 'Yes', { cta_arm: 'arm-1' }); // a change keeps the first arm
    await pick(b, 'op-r2', 'Yes', { ref: a.user.id }); // not b's first pick: no ref
    await pick(c, 'op-r1', 'Yes', { ref: 'no-such-user', cta_arm: 'bad arm!' });
    const rows = await env.DB.prepare("SELECT user_id, market_id, ref_user, cta_arm FROM picks WHERE market_id LIKE 'op-r%'").all();
    const by = Object.fromEntries(rows.results.map((r) => [`${r.user_id}/${r.market_id}`, r]));
    expect(by[`${a.user.id}/op-r1`]).toMatchObject({ ref_user: null, cta_arm: 'arm-1' });
    expect(by[`${b.user.id}/op-r1`]).toMatchObject({ ref_user: a.user.id, cta_arm: 'arm-2' });
    expect(by[`${b.user.id}/op-r2`]).toMatchObject({ ref_user: null, cta_arm: null });
    expect(by[`${c.user.id}/op-r1`]).toMatchObject({ ref_user: null, cta_arm: null });

    const ip = { 'CF-Connecting-IP': '203.0.113.9' };
    for (const arm of ['arm-1', 'arm-1', 'arm-2']) {
      expect((await call('POST', '/v1/cta', { body: { arm, round_id: 7 }, headers: ip })).status).toBe(200);
    }
    expect((await call('POST', '/v1/cta', { body: { kind: 'share' }, headers: ip })).status).toBe(200);
    expect((await call('POST', '/v1/cta', { body: { kind: 'spam' }, headers: ip })).status).toBe(400);
    expect((await call('POST', '/v1/cta', { body: { arm: 'x' }, origin: 'https://evil.example.com' })).status).toBe(403);

    expect((await call('GET', '/v1/admin/funnel', { origin: null })).status).toBe(401);
    const f = await bench('GET', '/v1/admin/funnel?since=2027-01-05');
    expect(f.status).toBe(200);
    expect(f.data.days).toEqual([
      { day: '2027-01-05', cta: 3, signins: 3, picks: 4, late_picks: 0, shares: 1, ref_picks: 1, cta_picks: 2 },
    ]);
    expect(f.data.arms).toEqual({ 'arm-1': { opens: 2, picks: 1 }, 'arm-2': { opens: 1, picks: 1 } });
  });
});

describe('/v1/me challenge (§6)', () => {
  it('streak, best and days played from D1 with the v0.3 rules; days under 3 markets do not count', async () => {
    freezeAt(Date.parse('2027-02-01T00:00:00Z'));
    const days = ['2027-02-01', '2027-02-02', '2027-02-03', '2027-02-04', '2027-02-05', '2027-02-06'];
    const ms = [];
    for (const d of days) {
      const n = d === '2027-02-06' ? 2 : 3; // the last day is too small to be a challenge
      for (let i = 0; i < n; i++) ms.push(market(`op-c-${d}-${i}`, { opens_at: null, locks_at: `${d}T12:00:00Z`, challenge_day: d }));
    }
    await bench('POST', '/v1/ingest/markets', ms);
    const u = await signIn(key, 'op-ch');
    // played: 02-01, 02-02, 02-04 (one pick is enough), 02-05 (unfinished), 02-06 (not a challenge); 02-03 skipped
    for (const d of ['2027-02-01', '2027-02-02', '2027-02-04', '2027-02-05', '2027-02-06']) {
      expect((await pick(u, `op-c-${d}-0`, 'Yes')).status).toBe(200);
    }
    vi.setSystemTime(Date.parse('2027-02-06T00:00:00Z'));
    // re-ingest without challenge_day keeps the tag (membership is monotonic)
    await bench('POST', '/v1/ingest/markets', [market('op-c-2027-02-01-0', { opens_at: null, locks_at: '2027-02-01T12:00:00Z' })]);
    await bench('POST', '/v1/ingest/results', ms.filter((m) => m.challenge_day < '2027-02-05').map((m) => ({ market_id: m.market_id, winner: 'Yes', ai_picks: {} })));
    const me = await call('GET', '/v1/me', { cookie: u.cookie });
    expect(me.data.challenge).toEqual({ streak: 1, best: 2, days_played: 4 });
    expect(me.data.picks.find((p) => p.market_id === 'op-c-2027-02-01-0')).toMatchObject({ challenge_day: '2027-02-01', correct: true });
    expect((await bench('POST', '/v1/ingest/markets', [market('op-c-bad', { challenge_day: 'Feb 1' })])).status).toBe(400);
    // an unreadable end date is dropped, not a reason to refuse the push
    expect((await bench('POST', '/v1/ingest/markets', [market('op-c-end', { end_date: 'soon' })])).status).toBe(200);
    expect((await call('GET', '/v1/markets/op-c-end')).data.market.closes_at).toBeNull();
  });
});

describe('the referral bar (§5): GET /v1/users/:id?market=', () => {
  it("hides the friend's pick until lock, says whether it was right after the result, never shows a late pick", async () => {
    freezeAt(Date.parse('2026-11-06T10:00:00Z'));
    await bench('POST', '/v1/ingest/markets', [market('op-u1', { opens_at: null, locks_at: '2026-11-06T12:00:00Z' })]);
    const a = await signIn(key, 'op-pub-a');
    const b = await signIn(key, 'op-pub-b');
    await pick(a, 'op-u1', 'No');
    let r = await call('GET', `/v1/users/${a.user.id}?market=op-u1`);
    expect(r.data.user.name).toBe(a.user.name);
    expect(r.data.market).toEqual({ market_id: 'op-u1', picked: true, pick: null, correct: null });
    vi.setSystemTime(Date.parse('2026-11-06T13:00:00Z'));
    await pick(b, 'op-u1', 'Yes'); // late
    r = await call('GET', `/v1/users/${a.user.id}?market=op-u1`);
    expect(r.data.market).toMatchObject({ pick: 'No', correct: null });
    expect((await call('GET', `/v1/users/${b.user.id}?market=op-u1`)).data.market).toMatchObject({ picked: false, pick: null });
    await bench('POST', '/v1/ingest/results', [{ market_id: 'op-u1', winner: 'Yes', ai_picks: {} }]);
    r = await call('GET', `/v1/users/${a.user.id}?market=op-u1`);
    expect(r.data.market).toEqual({ market_id: 'op-u1', picked: true, pick: 'No', correct: false });
  });
});

describe('X handle link (Q2 b)', () => {
  it('a one-time code on /me/, linked by the bot from a mention; one X account, one site account', async () => {
    freezeAt(Date.parse('2026-11-07T10:00:00Z'));
    const a = await signIn(key, 'op-x-a');
    const b = await signIn(key, 'op-x-b');
    const c1 = await call('POST', '/v1/me/xlink', { cookie: a.cookie, body: {} });
    expect(c1.status).toBe(200);
    expect(c1.data.code).toMatch(/^[A-HJ-NP-Z2-9]{8}$/);
    let me = await call('GET', '/v1/me', { cookie: a.cookie });
    expect(me.data.x).toMatchObject({ username: null, code: c1.data.code });

    expect((await call('POST', '/v1/admin/xlink', { body: { code: c1.data.code, x_user_id: '42', username: 'alice' }, origin: null })).status).toBe(401);
    expect((await bench('POST', '/v1/admin/xlink', { code: 'ZZZZZZZZ', x_user_id: '42', username: 'alice' })).status).toBe(404);
    expect((await bench('POST', '/v1/admin/xlink', { code: c1.data.code, x_user_id: 'abc', username: 'alice' })).status).toBe(400);
    const l = await bench('POST', '/v1/admin/xlink', { code: c1.data.code.toLowerCase(), x_user_id: '42', username: '@alice' });
    expect(l.status).toBe(200);
    expect(l.data).toMatchObject({ user_id: a.user.id, x_user_id: '42', username: 'alice' });
    me = await call('GET', '/v1/me', { cookie: a.cookie });
    expect(me.data.x).toEqual({ username: 'alice', code: null, code_expires: null });
    expect((await bench('POST', '/v1/admin/xlink', { code: c1.data.code, x_user_id: '42', username: 'alice' })).status).toBe(404); // used

    // the same X account linked from another site account moves there
    const c2 = await call('POST', '/v1/me/xlink', { cookie: b.cookie, body: {} });
    vi.setSystemTime(Date.parse('2026-11-08T09:00:00Z'));
    expect((await bench('POST', '/v1/admin/xlink', { code: c2.data.code, x_user_id: '42', username: 'alice' })).status).toBe(200);
    expect((await call('GET', '/v1/me', { cookie: a.cookie })).data.x.username).toBeNull();
    expect((await call('GET', '/v1/me', { cookie: b.cookie })).data.x.username).toBe('alice');
    // codes expire after a day
    const c3 = await call('POST', '/v1/me/xlink', { cookie: a.cookie, body: {} });
    vi.setSystemTime(Date.parse('2026-11-09T09:00:01Z'));
    expect((await bench('POST', '/v1/admin/xlink', { code: c3.data.code, x_user_id: '43', username: 'al' })).status).toBe(404);
    expect((await call('POST', '/v1/me/xlink/remove', { cookie: b.cookie, body: {} })).status).toBe(200);
    expect((await call('GET', '/v1/me', { cookie: b.cookie })).data.x.username).toBeNull();
    expect((await call('POST', '/v1/me/xlink', { body: {} })).status).toBe(401);
  });
});

describe('GET /v1/admin/picks (§7)', () => {
  it('returns counted, resolved picks with the linked X account; flags sign-up bursts per network', async () => {
    freezeAt(Date.parse('2026-11-10T10:00:00Z'));
    await bench('POST', '/v1/ingest/markets', [
      market('op-a1', { opens_at: null, locks_at: '2026-11-10T12:00:00Z' }),
      market('op-a2', { opens_at: null, locks_at: '2026-11-10T12:00:00Z' }),
    ]);
    const signInFrom = async (sub, ip) => {
      const credential = await signIdToken(key, { sub });
      const res = await call('POST', '/v1/auth/google', { body: { credential }, headers: { 'CF-Connecting-IP': ip } });
      return { cookie: res.headers.get('Set-Cookie').split(';')[0], user: res.data.user };
    };
    const a = await signInFrom('op-ap-a', '198.51.100.7');
    const code = (await call('POST', '/v1/me/xlink', { cookie: a.cookie, body: {} })).data.code;
    await bench('POST', '/v1/admin/xlink', { code, x_user_id: '777', username: 'ada' });
    await pick(a, 'op-a1', 'Yes');
    await pick(a, 'op-a2', 'Yes');
    const bots = [];
    for (let i = 0; i < 21; i++) bots.push(await signInFrom(`op-ap-bot-${i}`, `192.0.2.${i + 1}`));
    await pick(bots[0], 'op-a1', 'No');
    vi.setSystemTime(Date.parse('2026-11-10T13:00:00Z'));
    const lateU = await signInFrom('op-ap-late', '198.51.100.8');
    await pick(lateU, 'op-a1', 'No'); // late: never pulled
    await bench('POST', '/v1/ingest/results', [{ market_id: 'op-a1', round_id: 501, winner: 'Yes', ai_picks: {} }]);

    expect((await call('GET', '/v1/admin/picks', { origin: null })).status).toBe(401);
    const r = await bench('GET', '/v1/admin/picks?since=2026-11-10T00:00:00Z');
    expect(r.status).toBe(200);
    const mine = r.data.picks.filter((p) => p.market_id.startsWith('op-a'));
    expect(mine).toHaveLength(2); // op-a2 has no result yet; the late pick is out
    const ada = mine.find((p) => p.user_public_id === a.user.id);
    expect(ada).toMatchObject({ name: a.user.name, market_id: 'op-a1', round_id: '501', outcome: 'Yes', correct: true, late: false, x_user_id: '777', x_username: 'ada', flagged: false });
    expect(mine.find((p) => p.user_public_id === bots[0].user.id)).toMatchObject({ flagged: true, correct: false });
    expect(r.data.flagged).toContain(bots[0].user.id);
    expect(r.data.flagged).not.toContain(a.user.id);
    expect(r.data.bursts).toEqual([{ day: '2026-11-10', accounts: 21 }]);
    const raw = await env.DB.prepare('SELECT net FROM users WHERE id = ?').bind(a.user.id).first();
    expect(raw.net).toMatch(/^[0-9a-f]{24}$/); // hashed, never the address
    expect(JSON.stringify(r.data)).not.toContain('198.51.100');
  });
});
