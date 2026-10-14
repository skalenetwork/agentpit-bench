// The free API v2 (docs/free-api-spec.md): read model, embargo, keys, rate limits, off switch, no personal data.
import { beforeAll, beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { env } from 'cloudflare:test';
import { applySchema, makeGoogleKey, mockJwks, call, bench, signIn, market } from './helpers.js';
import { PICK_FIELDS } from '../src/v2.js';

let gkey;

beforeAll(async () => {
  await applySchema();
  gkey = await makeGoogleKey('kid-v2');
});

beforeEach(() => {
  mockJwks(() => [gkey]);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const DAY = 86_400_000;
const AGENTS = ['claude', 'codex', 'agy', 'grok'];
const META = {
  methodology_version: '2.1', site_url: 'https://agentpitbench.org', as_of: '2026-10-14T08:00:00Z',
  agents: AGENTS.map((a) => ({ agent: a, name: a.toUpperCase(), model: `${a}-model`, model_id: `${a}-1`, reasoning: 'high' })),
};

// A round as discover.api_round shapes it. `leak`: the decision fields filled even though it is running
// (an upstream bug the Worker must not pass on).
function rnd(id, { state = 'resolved', winner = 'Yes', picks = {}, resolvedAt, startedAt, category = 'Sports', exhibition = false, leak = false } = {}) {
  const started = startedAt ?? Date.now() - 2 * DAY;
  return {
    round_id: id, market_id: `m${id}`, question: `Will thing ${id} happen?`, event_title: `Event ${id}`, category,
    outcomes: ['Yes', 'No'], prices_at_start: [0.6, 0.4], state, exhibition, exclusion: exhibition ? 'summon' : null,
    sealed: state === 'running' && !leak, high_stakes: false, stake: 100,
    started_at: new Date(started).toISOString(), end_date: '2026-10-20T00:00:00Z',
    resolved_at: state === 'resolved' ? new Date(resolvedAt ?? Date.now() - DAY).toISOString() : null,
    winner: state === 'resolved' ? winner : null, split: false, market_link: `https://agentpit.example/m${id}`,
    url: `https://agentpitbench.org/round/${id}/`, crowd: { outcome: 'Yes', price: 0.6, won: state === 'resolved' ? winner === 'Yes' : null, pnl: state === 'resolved' ? (winner === 'Yes' ? 66.67 : -100) : null },
    humans: { winners: [{ username: 'private_person', pick: 'Yes' }] },   // never stored or served
    entries: AGENTS.map((a) => {
      const outcome = a in picks ? picks[a] : 'Yes';
      const decided = state !== 'running' || leak;
      return {
        agent: a, name: a.toUpperCase(), model_reported: decided ? `${a}-model` : null, wallet: '0xabc',
        outcome: decided ? outcome : null, avg_price: decided ? 0.6 : null, shares_filled: decided ? 166 : null,
        stake_filled: decided && outcome ? 100 : null, confidence: decided ? 0.7 : null,
        rationale: decided ? `secret rationale ${a}` : null, quote: decided ? `secret quote ${a}` : null,
        statement: null, decided_s: decided ? 42 : null, exit_reason: decided ? 'done' : null,
        payout: null, pnl: state === 'resolved' ? (outcome === winner ? 66.67 : -100) : null,
        won: state === 'resolved' ? outcome === winner : null,
        transcript_url: decided ? `https://agentpitbench.org/transcripts/${id}/${a}.txt` : null, sealed: !decided,
      };
    }),
  };
}

const ingest = (rounds, meta = META) => bench('POST', '/v1/ingest/rounds', { meta, rounds });
const get = (path, opts = {}) => call('GET', path, { origin: null, ...opts });
const keyed = (path, secret) => get(path, { headers: { Authorization: `Bearer ${secret}` } });

function assertNoPicks(obj) {
  const s = JSON.stringify(obj);
  expect(s).not.toContain('secret rationale');
  expect(s).not.toContain('secret quote');
  expect(s).not.toContain('/transcripts/');
}

describe('read model and endpoints', () => {
  it('ingest needs the bench key, validates and drops unknown (personal) fields', async () => {
    expect((await call('POST', '/v1/ingest/rounds', { origin: null, body: { rounds: [rnd(1)] } })).status).toBe(401);
    expect((await ingest([{ ...rnd(1), state: 'bogus' }])).status).toBe(400);
    expect((await ingest([{ ...rnd(1), round_id: 'x' }])).status).toBe(400);
    const res = await ingest([rnd(1), rnd(2, { state: 'running' })]);
    expect(res.data).toEqual({ ok: true, upserted: 2, sealed: 1 });
    const row = await env.DB.prepare('SELECT data FROM api_rounds WHERE round_id = 1').first();
    expect(row.data).not.toContain('private_person');
    expect(row.data).not.toContain('0xabc');
  });

  it('serves rounds newest first with filters and cursor paging, in the envelope', async () => {
    await ingest([rnd(1, { category: 'Crypto' }), rnd(2), rnd(3, { picks: { grok: 'No' } }), rnd(4, { state: 'running' })]);
    const all = await get('/v2/rounds?limit=2');
    expect(all.status).toBe(200);
    expect(all.headers.get('Access-Control-Allow-Origin')).toBe('*');
    expect(all.data).toMatchObject({ api_version: 'v2', methodology_version: '2.1', license: 'CC-BY-4.0', attribution: 'Data: AgentpitBench (agentpitbench.org)' });
    expect(all.data.citation).toContain('AgentpitBench');
    expect(all.data.as_of).toBe('2026-10-14T08:00:00.000Z');
    expect(all.data.data.rounds.map((r) => r.round_id)).toEqual([4, 3]);
    expect(all.data.data.next_cursor).toBe(3);
    const page2 = await get('/v2/rounds?limit=2&cursor=3');
    expect(page2.data.data.rounds.map((r) => r.round_id)).toEqual([2, 1]);
    expect(page2.data.data.next_cursor).toBeNull();
    expect((await get('/v2/rounds?state=running')).data.data.rounds.map((r) => r.round_id)).toEqual([4]);
    expect((await get('/v2/rounds?category=crypto')).data.data.rounds.map((r) => r.round_id)).toEqual([1]);
    expect((await get('/v2/rounds?agent=grok&limit=100')).data.data.rounds).toHaveLength(4);
    expect((await get('/v2/rounds?agent=nobody')).data.data.rounds).toHaveLength(0);
    expect((await get('/v2/rounds?limit=101')).status).toBe(400);
    expect((await get('/v2/rounds?since=nope')).status).toBe(400);
    const one = await get('/v2/rounds/3');
    expect(one.data.data.entries.find((e) => e.agent === 'grok')).toMatchObject({ outcome: 'No', won: false, pnl: -100 });
    expect(one.data.data.static_url).toBe('https://agentpitbench.org/api/v1/rounds/3.json');
    expect(one.data.source_url).toBe('https://agentpitbench.org/round/3/');
    expect((await get('/v2/rounds/99')).status).toBe(404);
  });

  it('upcoming lists open markets only; live lists running and awaiting rounds with the chance now', async () => {
    await bench('POST', '/v1/ingest/markets', [
      market('u1', { round_id: null }), market('u2', { round_id: null, locks_at: new Date(Date.now() - 1000).toISOString() }),
      market('u3'),
    ]);
    const up = await get('/v2/upcoming');
    expect(up.data.data.markets.map((m) => m.market_id)).toEqual(['u1']);
    expect(Object.keys(up.data.data.markets[0]).sort()).toEqual(['event_title', 'locks_at', 'market_id', 'opens_at', 'outcomes', 'question', 'url']);
    await ingest([rnd(5, { state: 'running' }), { ...rnd(6, { state: 'awaiting_resolution' }), prices_now: { prices: [0.7, 0.3], at: 1 } }, rnd(7)]);
    const lv = await get('/v2/live');
    expect(lv.data.data.rounds.map((r) => r.round_id)).toEqual([6, 5]);
    expect(lv.data.data.rounds[0].prices_now).toEqual({ prices: [0.7, 0.3], at: 1 });
  });

  it('leaderboard by period follows the betting rules (no-bet = loss, ranked by profit) with The Crowd', async () => {
    const now = Date.now();
    await ingest([
      rnd(1, { resolvedAt: now - 1000, picks: { grok: 'No', codex: null } }),
      rnd(2, { resolvedAt: now - 1000, winner: 'No' }),
      rnd(3, { resolvedAt: now - 400 * DAY }),
      rnd(4, { resolvedAt: now - 1000, exhibition: true }),
      rnd(5, { state: 'running' }),
    ]);
    const lb = await get('/v2/leaderboard?period=week');
    const by = Object.fromEntries(lb.data.data.agents.map((a) => [a.agent, a]));
    expect(by.claude).toMatchObject({ played: 2, wins: 1, losses: 1, no_bets: 0, net_pnl: -33.33, win_rate: 0.5 });
    expect(by.grok).toMatchObject({ played: 2, wins: 0, losses: 2, net_pnl: -200 });
    expect(by.codex).toMatchObject({ played: 2, wins: 0, no_bets: 1, losses: 2 });
    expect(lb.data.data.agents[0].rank).toBe(1);
    expect(lb.data.data.agents.map((a) => a.net_pnl)).toEqual([...lb.data.data.agents.map((a) => a.net_pnl)].sort((a, b) => b - a));
    expect(lb.data.data.crowd).toMatchObject({ played: 2, wins: 1, losses: 1 });
    expect(lb.data.data.rounds_counted).toBe(2);
    expect((await get('/v2/leaderboard?period=all')).data.data.rounds_counted).toBe(3);
    expect((await get('/v2/leaderboard?period=month&month=2020-01')).data.data.rounds_counted).toBe(0);
    expect((await get('/v2/leaderboard?period=decade')).status).toBe(400);
  });

  it('agent history: models as reported, record, rounds', async () => {
    await ingest([rnd(1), rnd(2, { state: 'running' })]);
    const a = await get('/v2/agents/claude');
    expect(a.data.data).toMatchObject({ agent: 'claude', name: 'CLAUDE', model: 'claude-model' });
    expect(a.data.data.models).toEqual([expect.objectContaining({ model_reported: 'claude-model', played: 1, wins: 1 })]);
    expect(a.data.data.record.all).toMatchObject({ played: 1, wins: 1 });
    const rs = await get('/v2/agents/claude/rounds');
    expect(rs.data.data.rounds.map((r) => r.round_id)).toEqual([2, 1]);
    expect((await get('/v2/agents/nobody')).status).toBe(404);
  });

  it('humans: totals only, no names or ids', async () => {
    const { cookie } = await signIn(gkey, 'h1');
    await bench('POST', '/v1/ingest/markets', [market('h1', { round_id: '9' })]);
    await call('POST', '/v1/picks', { cookie, body: { market_id: 'h1', outcome: 'Yes' } });
    await bench('POST', '/v1/ingest/results', [{ market_id: 'h1', round_id: 9, winner: 'Yes', ai_picks: { claude: 'No', grok: 'Yes' } }]);
    const h = await get('/v2/humans?period=all');
    expect(h.data.data.humans).toEqual({ picks: 1, correct: 1, people: 1, markets: 1 });
    expect(h.data.data.by_ai.claude).toEqual({ human_wins: 1, ai_wins: 0, ties: 0 });
    expect(h.data.data).not.toHaveProperty('top');
    expect(JSON.stringify(h.data)).not.toMatch(/"name"|user/);
  });

  it('has no comments endpoint and is read-only', async () => {
    expect((await get('/v2/comments/1')).status).toBe(404);
    expect((await call('POST', '/v2/rounds', { origin: null, body: {} })).status).toBe(405);
    const pre = await call('OPTIONS', '/v2/rounds', { origin: 'https://someone.example' });
    expect(pre.status).toBe(204);
    expect(pre.headers.get('Access-Control-Allow-Headers')).toContain('Authorization');
    expect((await get('/v2/openapi.json')).data.paths['/rounds']).toBeTruthy();
  });
});

describe('embargo: nothing of the AIs\' decisions before every agent finished', () => {
  it('a running round shows no picks on any surface, even if the bot sent them; they appear once it is revealed', async () => {
    const leaky = rnd(10, { state: 'running', leak: true });
    expect(leaky.entries[0].rationale).toContain('secret');
    await ingest([leaky, rnd(11, { state: 'awaiting_resolution', leak: true }), { ...rnd(12, { state: 'awaiting_resolution' }), sealed: true }]);
    // nothing reached storage either
    const stored = await env.DB.prepare('SELECT data FROM api_rounds WHERE round_id IN (10, 12)').all();
    for (const r of stored.results) assertNoPicks(r.data);
    const ent = await env.DB.prepare('SELECT * FROM api_entries WHERE round_id IN (10, 12) AND (outcome IS NOT NULL OR model_reported IS NOT NULL)').all();
    expect(ent.results).toHaveLength(0);

    for (const n of [10, 12]) {
      const one = await get(`/v2/rounds/${n}`);
      expect(one.data.data.sealed).toBe(true);
      for (const e of one.data.data.entries) {
        for (const f of PICK_FIELDS) expect(e[f], `${n} ${e.agent}.${f}`).toBeNull();
        expect(e.sealed).toBe(true);
      }
      assertNoPicks(one.data);
    }
    const list = await get('/v2/rounds?limit=100');
    assertNoPicks(list.data.data.rounds.filter((r) => r.round_id !== 11));
    for (const r of list.data.data.rounds.filter((x) => x.round_id !== 11)) for (const p of r.picks) expect(p.outcome).toBeNull();
    const lv = await get('/v2/live');
    for (const r of lv.data.data.rounds.filter((x) => x.round_id !== 11)) for (const p of r.picks) expect(p.outcome).toBeNull();
    const ar = await get('/v2/agents/claude/rounds?limit=100');
    for (const r of ar.data.data.rounds.filter((x) => x.round_id !== 11)) {
      expect(r.outcome).toBeNull();
      expect(r.model_reported).toBeNull();
    }
    const lb = await get('/v2/leaderboard?period=all');
    expect(lb.data.data.rounds_counted).toBe(0);

    // every agent finished: the round leaves `running`, the bot re-sends it, the picks appear
    await ingest([rnd(10, { state: 'awaiting_resolution' })]);
    const after = await get('/v2/rounds/10');
    expect(after.data.data.sealed).toBe(false);
    expect(after.data.data.entries[0]).toMatchObject({ outcome: 'Yes', rationale: 'secret rationale claude', sealed: false });
  });

  it('an awaiting round flagged sealed by the bot stays sealed', async () => {
    await ingest([{ ...rnd(20, { state: 'resolved' }), sealed: true }]);
    const one = await get('/v2/rounds/20');
    expect(one.data.data.entries.every((e) => e.outcome === null)).toBe(true);
    expect(one.data.data.winner).toBeNull();
  });
});

describe('keys and limits', () => {
  it('60 requests per hour per IP without a key, then 429 with retry_after', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(Date.UTC(2026, 9, 14, 10, 30));
    const ip = { 'CF-Connecting-IP': '203.0.113.7' };
    for (let i = 0; i < 60; i++) {
      const r = await get('/v2/upcoming', { headers: ip });
      expect(r.status).toBe(200);
    }
    const r = await get('/v2/upcoming', { headers: ip });
    expect(r.status).toBe(429);
    expect(r.data.retry_after).toBe(1800);
    expect(r.headers.get('Retry-After')).toBe('1800');
    expect(r.headers.get('Access-Control-Allow-Origin')).toBe('*');
    expect((await get('/v2/upcoming', { headers: { 'CF-Connecting-IP': '203.0.113.8' } })).status).toBe(200);
    vi.setSystemTime(Date.UTC(2026, 9, 14, 11, 0, 1));
    expect((await get('/v2/upcoming', { headers: ip })).status).toBe(200);
    // the raw IP is never stored
    const rows = await env.DB.prepare('SELECT k FROM api_counters').all();
    expect(JSON.stringify(rows.results)).not.toContain('203.0.113');
    const use = await bench('GET', '/v1/admin/api-usage?since=2026-10-14');
    expect(use.data).toMatchObject({ anon_requests: 62, limited: 1, enabled: true });
  });

  it('a signed-in person creates up to 3 keys, uses them, sees usage, revokes; keys are hashed at rest', async () => {
    const { cookie } = await signIn(gkey, 'dev1');
    expect((await call('POST', '/v1/me/keys', { body: { label: 'x' } })).status).toBe(401);
    const made = [];
    for (let i = 0; i < 3; i++) {
      const r = await call('POST', '/v1/me/keys', { cookie, body: { label: `bot ${i}` } });
      expect(r.status).toBe(201);
      expect(r.data.secret).toMatch(/^apb_/);
      made.push(r.data);
    }
    expect((await call('POST', '/v1/me/keys', { cookie, body: { label: 'four' } })).status).toBe(409);
    expect((await call('POST', '/v1/me/keys', { cookie, body: { label: '' } })).status).toBe(400);
    const stored = await env.DB.prepare('SELECT * FROM api_keys').all();
    for (const s of made) expect(JSON.stringify(stored.results)).not.toContain(s.secret);
    const r = await keyed('/v2/rounds', made[0].secret);
    expect(r.status).toBe(200);
    expect(r.headers.get('X-RateLimit-Limit')).toBe('10000');
    expect(r.headers.get('X-RateLimit-Remaining')).toBe('9999');
    const list = await call('GET', '/v1/me/keys', { cookie });
    const k0 = list.data.keys.find((k) => k.id === made[0].key.id);
    expect(k0.usage[0].n).toBe(1);
    expect(JSON.stringify(list.data)).not.toContain(made[0].secret);
    // someone else cannot revoke it
    const other = await signIn(gkey, 'dev2');
    expect((await call('POST', `/v1/me/keys/${made[0].key.id}/revoke`, { cookie: other.cookie, body: {} })).status).toBe(404);
    expect((await call('POST', `/v1/me/keys/${made[0].key.id}/revoke`, { cookie, body: {} })).status).toBe(200);
    expect((await keyed('/v2/rounds', made[0].secret)).status).toBe(401);
    expect((await keyed('/v2/rounds', 'apb_notarealkeynotarealkey123')).status).toBe(401);
    expect((await get('/v2/rounds', { headers: { Authorization: 'Basic abc' } })).status).toBe(401);
    // a revoked key frees a slot
    expect((await call('POST', '/v1/me/keys', { cookie, body: { label: 'again' } })).status).toBe(201);
  });

  it('per-key daily quota and per-second limit; the operator can raise a limit or revoke', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(Date.UTC(2026, 9, 14, 12, 0, 0));
    const { cookie } = await signIn(gkey, 'dev3');
    const made = (await call('POST', '/v1/me/keys', { cookie, body: { label: 'q' } })).data;
    expect((await bench('POST', `/v1/admin/keys/${made.key.id}`, { action: 'limit', daily_limit: 3 })).status).toBe(200);
    for (let i = 0; i < 3; i++) {
      vi.setSystemTime(Date.UTC(2026, 9, 14, 12, 0, i + 1));
      expect((await keyed('/v2/live', made.secret)).status).toBe(200);
    }
    vi.setSystemTime(Date.UTC(2026, 9, 14, 12, 0, 9));
    const over = await keyed('/v2/live', made.secret);
    expect(over.status).toBe(429);
    expect(over.data.retry_after).toBe(12 * 3600 - 9);
    await bench('POST', `/v1/admin/keys/${made.key.id}`, { action: 'limit', daily_limit: null });
    // 10 per second
    vi.setSystemTime(Date.UTC(2026, 9, 14, 13, 0, 0));
    for (let i = 0; i < 10; i++) expect((await keyed('/v2/live', made.secret)).status).toBe(200);
    expect((await keyed('/v2/live', made.secret)).status).toBe(429);
    vi.setSystemTime(Date.UTC(2026, 9, 14, 13, 0, 1));
    expect((await keyed('/v2/live', made.secret)).status).toBe(200);
    const use = await bench('GET', '/v1/admin/api-usage?since=2026-10-14');
    expect(use.data).toMatchObject({ keyed_requests: 14, active_keys: 1, keys: 1 });
    expect(use.data.top[0]).toMatchObject({ id: made.key.id, requests: 14, limited: 2 });
    expect(JSON.stringify(use.data)).not.toContain(made.secret);
    await bench('POST', `/v1/admin/keys/${made.key.id}`, { action: 'revoke' });
    expect((await keyed('/v2/live', made.secret)).status).toBe(401);
  });

  it('account deletion removes the person\'s keys', async () => {
    const { cookie } = await signIn(gkey, 'dev4');
    const made = (await call('POST', '/v1/me/keys', { cookie, body: { label: 'gone' } })).data;
    await keyed('/v2/live', made.secret);
    expect((await call('POST', '/v1/me/delete', { cookie, body: {} })).status).toBe(200);
    expect((await env.DB.prepare('SELECT COUNT(*) AS n FROM api_keys').first()).n).toBe(0);
    expect((await env.DB.prepare("SELECT COUNT(*) AS n FROM api_usage WHERE key_id != 'anon'").first()).n).toBe(0);
    expect((await keyed('/v2/live', made.secret)).status).toBe(401);
  });
});

describe('off switch', () => {
  it('the operator switch and the env var both turn /v2/ off; v1 keeps working', async () => {
    expect((await bench('POST', '/v1/admin/api', { enabled: false })).data.enabled).toBe(false);
    const r = await get('/v2/rounds');
    expect(r.status).toBe(503);
    expect(r.data.error).toBe('api_disabled');
    expect((await get('/v1/health')).status).toBe(200);
    await bench('POST', '/v1/admin/api', { enabled: true });
    expect((await get('/v2/rounds')).status).toBe(200);
    const prev = env.API_V2_ENABLED;
    env.API_V2_ENABLED = '0';
    try {
      expect((await get('/v2/rounds')).status).toBe(503);
    } finally {
      env.API_V2_ENABLED = prev;
    }
    expect((await call('POST', '/v1/admin/api', { origin: null, body: { enabled: false } })).status).toBe(401);
  });
});
