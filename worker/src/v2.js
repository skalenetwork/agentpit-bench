// The free public read API, /v2/ (docs/free-api-spec.md), plus what feeds and manages it:
// - POST /v1/ingest/rounds (bench key): the bot pushes the rounds as the static /api/v1/rounds/{n}.json has them;
// - /v1/me/keys (signed in): up to 3 API keys per person, stored as SHA-256 only;
// - /v1/admin/api-usage, /v1/admin/keys, /v1/admin/api (bench key): the digest line, revoke, the off switch.
//
// The embargo (§5): no pick, quote, rationale, transcript or other decision of a round leaves the API before every
// agent in it has finished. A round is sealed while its state is `running` or the bot marked it sealed; its entries
// are stripped when it is ingested and again on every read, so a bug upstream cannot leak a pick through here.
// No personal data: humans appear only as totals (/v2/humans); comments are not in the API.
import { HttpError, json, readJson, parseTime, iso, weekStart, seasonStart, randomId, MAX_INGEST_BODY } from './util.js';
import { containsBlocked } from './words.js';
import { checkRate } from './handlers.js';

const STATES = new Set(['running', 'awaiting_resolution', 'resolved', 'void']);
const LIVE_STATES = ['running', 'awaiting_resolution'];
// Decision fields of an entry: all null while the round is sealed (discover.PICK_FIELDS + exit_reason, summary).
export const PICK_FIELDS = [
  'outcome', 'avg_price', 'shares_filled', 'stake_filled', 'confidence', 'rationale', 'quote', 'statement',
  'decided_s', 'payout', 'pnl', 'won', 'transcript_url', 'exit_reason', 'model_reported', 'summary',
];
const ENTRY_FIELDS = ['agent', 'name', ...PICK_FIELDS];
const ROUND_FIELDS = [
  'round_id', 'market_id', 'question', 'event_title', 'category', 'outcomes', 'prices_at_start', 'prices_now', 'state',
  'exhibition', 'exclusion', 'counts', 'sealed', 'high_stakes', 'stake', 'started_at', 'end_date', 'resolved_at',
  'revealed_at', 'winner', 'split', 'market_link', 'url', 'crowd', 'methodology_version',
];
const AGENT_RE = /^[a-z0-9_-]{1,32}$/;
const KEY_RE = /^apb_[A-Za-z0-9_-]{20,64}$/;
const MAX_LIMIT = 100;
const DEFAULT_LIMIT = 20;
const LICENSE = 'CC-BY-4.0';
const ATTRIBUTION = 'Data: AgentpitBench (agentpitbench.org)';
const KEY_CREATE_LIMIT = 10; // keys created per person per day (revoked ones included)

const bad = (code, message) => new HttpError(400, code, message);
const num = (v, d) => {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : d;
};

export function limits(env) {
  return {
    anon_per_hour: num(env.API_ANON_PER_HOUR, 60),
    key_per_day: num(env.API_KEY_PER_DAY, 10000),
    key_per_second: num(env.API_KEY_PER_SECOND, 10),
    keys_per_user: num(env.API_KEYS_PER_USER, 3),
  };
}

// ---------------------------------------------------------------- the embargo

export function isSealed(r) {
  return !r || r.state === 'running' || r.sealed === true || r.sealed === 1;
}

function cleanEntry(e, sealed) {
  const out = {};
  for (const k of ENTRY_FIELDS) out[k] = e && e[k] !== undefined ? e[k] : null;
  if (typeof out.agent !== 'string') out.agent = null;
  out.sealed = !!sealed;
  if (sealed) for (const k of PICK_FIELDS) out[k] = null;
  return out;
}

// A round as v2 serves it: known fields only (nothing personal can ride along), entries sealed when needed.
export function cleanRound(r) {
  const sealed = isSealed(r);
  const out = {};
  for (const k of ROUND_FIELDS) out[k] = r[k] !== undefined ? r[k] : null;
  out.sealed = sealed;
  out.exhibition = !!r.exhibition;
  out.counts = r.state === 'resolved' && !r.exhibition;
  if (sealed) {
    out.split = false;
    out.winner = null;
  }
  out.entries = (Array.isArray(r.entries) ? r.entries : []).map((e) => cleanEntry(e, sealed));
  return out;
}

// ---------------------------------------------------------------- helpers

async function sha256hex(s) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

const dayOf = (ms) => new Date(ms).toISOString().slice(0, 10);

async function kvGet(db, k) {
  const row = await db.prepare('SELECT v, updated_at FROM kv WHERE k = ?').bind(k).first();
  if (!row) return null;
  try {
    return { v: JSON.parse(row.v), at: row.updated_at };
  } catch {
    return null;
  }
}

const kvPut = (db, k, v, now) =>
  db.prepare('INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at')
    .bind(k, JSON.stringify(v), now);

async function meta(ctx) {
  if (ctx._meta === undefined) {
    const m = await kvGet(ctx.db, 'bench_meta');
    ctx._meta = m ? { ...m.v, _at: m.at } : { _at: null };
  }
  return ctx._meta;
}

function siteUrl(m) {
  const u = typeof m.site_url === 'string' && /^https:\/\/[^\s"]+$/.test(m.site_url) ? m.site_url : 'https://agentpitbench.org';
  return u.replace(/\/+$/, '');
}

// Every v2 response: {api_version, as_of, methodology_version, source_url, citation, attribution, license, data}.
async function envelope(ctx, sourcePath, data, status = 200) {
  const m = await meta(ctx);
  const site = siteUrl(m);
  const source = `${site}/${sourcePath}`;
  const asOf = parseTime(m.as_of) || m._at || ctx.now;
  const mv = m.methodology_version || null;
  return json(
    {
      api_version: 'v2',
      as_of: iso(asOf),
      methodology_version: mv,
      source_url: source,
      citation: `AgentpitBench (SKALE Labs), data as of ${dayOf(asOf)}${mv ? `, methodology v${mv}` : ''}. ${source}`,
      attribution: ATTRIBUTION,
      license: LICENSE,
      data,
    },
    status,
    { 'Cache-Control': 'public, max-age=30' },
  );
}

function limitParam(url) {
  const raw = url.searchParams.get('limit');
  if (raw === null || raw === '') return DEFAULT_LIMIT;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1 || n > MAX_LIMIT) throw bad('bad_limit', `limit must be 1-${MAX_LIMIT}`);
  return n;
}

function cursorParam(url) {
  const raw = url.searchParams.get('cursor');
  if (raw === null || raw === '') return null;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1) throw bad('bad_cursor', 'cursor must be a round id');
  return n;
}

function timeParam(url, name) {
  const t = parseTime(url.searchParams.get(name));
  if (t === undefined) throw bad(`bad_${name}`, `${name} must be ISO 8601 or epoch`);
  return t;
}

// Period start/end (epoch ms) for week | month | season | all.
export function periodRange(period, env, now, month) {
  if (period === 'week') return [weekStart(now), null];
  if (period === 'season') return [seasonStart(env), null];
  if (period === 'all') return [null, null];
  if (period === 'month') {
    let y;
    let m;
    if (month) {
      const mm = /^(\d{4})-(\d{2})$/.exec(month);
      if (!mm || +mm[2] < 1 || +mm[2] > 12) throw bad('bad_month', 'month must be YYYY-MM');
      y = +mm[1];
      m = +mm[2] - 1;
    } else {
      const d = new Date(now);
      y = d.getUTCFullYear();
      m = d.getUTCMonth();
    }
    return [Date.UTC(y, m, 1), Date.UTC(y, m + 1, 1)];
  }
  throw bad('bad_period', 'period must be week, month, season or all');
}

function summary(r, base) {
  return {
    round_id: r.round_id,
    question: r.question,
    event_title: r.event_title,
    category: r.category,
    state: r.state,
    exhibition: r.exhibition,
    exclusion: r.exclusion,
    counts: r.counts,
    sealed: r.sealed,
    started_at: r.started_at,
    end_date: r.end_date,
    resolved_at: r.resolved_at,
    winner: r.winner,
    url: r.url,
    api_url: `${base}/rounds/${r.round_id}`,
    picks: r.entries.map((e) => ({ agent: e.agent, name: e.name, outcome: e.outcome, won: e.won, pnl: e.pnl })),
  };
}

const parseRow = (row) => {
  const d = JSON.parse(row.data);
  return cleanRound({ ...d, state: row.state, sealed: row.sealed === 1 || d.sealed === true });
};
const apiBase = (ctx) => `${ctx.url.origin}/v2`;

// ---------------------------------------------------------------- ingest (bench key)

function validRound(raw, i) {
  const where = `rounds[${i}]`;
  if (!raw || typeof raw !== 'object') throw bad('bad_round', `${where} must be an object`);
  if (!Number.isInteger(raw.round_id) || raw.round_id < 1) throw bad('bad_round', `${where}.round_id must be a positive integer`);
  if (!STATES.has(raw.state)) throw bad('bad_round', `${where}.state invalid`);
  if (typeof raw.question !== 'string' || !raw.question) throw bad('bad_round', `${where}.question required`);
  if (raw.entries !== undefined && !Array.isArray(raw.entries)) throw bad('bad_round', `${where}.entries must be an array`);
  for (const k of ['started_at', 'resolved_at', 'revealed_at']) {
    if (parseTime(raw[k]) === undefined) throw bad('bad_round', `${where}.${k} invalid`);
  }
  return cleanRound(raw);
}

export async function ingestRounds(ctx) {
  const body = await readJson(ctx.request, MAX_INGEST_BODY);
  const items = Array.isArray(body) ? body : body.rounds;
  if (!Array.isArray(items) || items.length > 500) throw bad('bad_body', 'expected {meta, rounds: [...]} (max 500 rounds)');
  const rounds = items.map(validRound);
  const db = ctx.db;
  const stmts = [];
  for (const r of rounds) {
    const started = parseTime(r.started_at) ?? null;
    const resolved = parseTime(r.resolved_at) ?? null;
    stmts.push(
      db.prepare(
        `INSERT INTO api_rounds (round_id, market_id, state, category, sealed, counts, started_at, resolved_at, data, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(round_id) DO UPDATE SET market_id = excluded.market_id, state = excluded.state,
           category = excluded.category, sealed = excluded.sealed, counts = excluded.counts,
           started_at = excluded.started_at, resolved_at = excluded.resolved_at, data = excluded.data,
           updated_at = excluded.updated_at`,
      ).bind(r.round_id, r.market_id == null ? null : String(r.market_id), r.state,
        typeof r.category === 'string' ? r.category : null, r.sealed ? 1 : 0, r.counts ? 1 : 0, started, resolved,
        JSON.stringify(r), ctx.now),
      db.prepare('DELETE FROM api_entries WHERE round_id = ?').bind(r.round_id),
    );
    for (const e of r.entries) {
      if (!e.agent || !AGENT_RE.test(e.agent)) continue;
      stmts.push(
        db.prepare(
          `INSERT OR REPLACE INTO api_entries (round_id, agent, outcome, won, pnl, stake_filled, model_reported)
           VALUES (?, ?, ?, ?, ?, ?, ?)`,
        ).bind(r.round_id, e.agent, typeof e.outcome === 'string' ? e.outcome : null,
          e.won === true ? 1 : e.won === false ? 0 : null, Number.isFinite(e.pnl) ? e.pnl : null,
          Number.isFinite(e.stake_filled) ? e.stake_filled : null,
          typeof e.model_reported === 'string' ? e.model_reported.slice(0, 200) : null),
      );
    }
  }
  if (body && !Array.isArray(body) && body.meta && typeof body.meta === 'object') {
    const m = body.meta;
    const agents = Array.isArray(m.agents)
      ? m.agents.filter((a) => a && AGENT_RE.test(a.agent)).slice(0, 20).map((a) => ({
        agent: a.agent, name: String(a.name || a.agent).slice(0, 60), model: a.model ?? null,
        model_id: a.model_id ?? null, reasoning: a.reasoning ?? null,
      }))
      : [];
    stmts.push(kvPut(db, 'bench_meta', {
      methodology_version: typeof m.methodology_version === 'string' ? m.methodology_version : null,
      site_url: typeof m.site_url === 'string' ? m.site_url : null,
      as_of: iso(parseTime(m.as_of) || ctx.now), agents,
    }, ctx.now));
  }
  for (let i = 0; i < stmts.length; i += 100) await db.batch(stmts.slice(i, i + 100));
  return json({ ok: true, upserted: rounds.length, sealed: rounds.filter((r) => r.sealed).length });
}

// ---------------------------------------------------------------- v2 read endpoints

export async function index(ctx) {
  const base = apiBase(ctx);
  return envelope(ctx, 'developers/', {
    endpoints: ['/rounds', '/rounds/{n}', '/upcoming', '/live', '/leaderboard', '/agents/{id}', '/agents/{id}/rounds',
      '/humans', '/openapi.json'].map((p) => base + p),
    limits: limits(ctx.env),
  });
}

export async function rounds(ctx) {
  const u = ctx.url.searchParams;
  const where = [];
  const args = [];
  const state = u.get('state');
  if (state) {
    if (!STATES.has(state)) throw bad('bad_state', 'state must be running, awaiting_resolution, resolved or void');
    where.push('r.state = ?');
    args.push(state);
  }
  const category = u.get('category');
  if (category) {
    where.push('lower(r.category) = lower(?)');
    args.push(category.slice(0, 40));
  }
  const agent = u.get('agent');
  if (agent) {
    if (!AGENT_RE.test(agent)) throw bad('bad_agent', 'agent invalid');
    where.push('EXISTS (SELECT 1 FROM api_entries e WHERE e.round_id = r.round_id AND e.agent = ?)');
    args.push(agent);
  }
  const since = timeParam(ctx.url, 'since');
  if (since !== null) {
    where.push('r.started_at >= ?');
    args.push(since);
  }
  const until = timeParam(ctx.url, 'until');
  if (until !== null) {
    where.push('r.started_at < ?');
    args.push(until);
  }
  const cursor = cursorParam(ctx.url);
  if (cursor !== null) {
    where.push('r.round_id < ?');
    args.push(cursor);
  }
  const limit = limitParam(ctx.url);
  const rows = await ctx.db
    .prepare(`SELECT r.round_id, r.state, r.sealed, r.data FROM api_rounds r ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
              ORDER BY r.round_id DESC LIMIT ?`)
    .bind(...args, limit + 1)
    .all();
  const page = rows.results.slice(0, limit).map((row) => summary(parseRow(row), apiBase(ctx)));
  const next = rows.results.length > limit ? page[page.length - 1].round_id : null;
  return envelope(ctx, 'data/', { rounds: page, next_cursor: next });
}

export async function round(ctx, { n }) {
  const id = Number(n);
  const row = Number.isInteger(id) && id > 0
    ? await ctx.db.prepare('SELECT round_id, state, sealed, data FROM api_rounds WHERE round_id = ?').bind(id).first()
    : null;
  if (!row) throw new HttpError(404, 'not_found', 'no such round');
  const r = parseRow(row);
  const site = siteUrl(await meta(ctx));
  return envelope(ctx, `round/${id}/`, { ...r, static_url: `${site}/api/v1/rounds/${id}.json` });
}

export async function upcoming(ctx) {
  const rows = await ctx.db
    .prepare(
      `SELECT market_id, question, event_title, outcomes, opens_at, locks_at, url FROM markets
        WHERE round_id IS NULL AND resolved_at IS NULL AND locks_at > ? ORDER BY locks_at LIMIT 100`,
    )
    .bind(ctx.now)
    .all();
  return envelope(ctx, '', {
    markets: rows.results.map((m) => ({
      market_id: m.market_id, question: m.question, event_title: m.event_title, outcomes: JSON.parse(m.outcomes),
      opens_at: iso(m.opens_at), locks_at: iso(m.locks_at), url: m.url,
    })),
    note: 'locks_at: when people\'s picks close and the AIs start.',
  });
}

export async function live(ctx) {
  const rows = await ctx.db
    .prepare(`SELECT round_id, state, sealed, data FROM api_rounds WHERE state IN (?, ?) ORDER BY round_id DESC LIMIT 200`)
    .bind(...LIVE_STATES)
    .all();
  return envelope(ctx, 'c/live/', {
    rounds: rows.results.map((row) => {
      const r = parseRow(row);
      return { ...summary(r, apiBase(ctx)), outcomes: r.outcomes, prices_at_start: r.prices_at_start, prices_now: r.prices_now };
    }),
  });
}

function streakOf(results) {
  let streak = 0;
  for (let i = results.length - 1; i >= 0; i--) {
    const won = results[i];
    if (streak === 0) streak = won ? 1 : -1;
    else if ((streak > 0) === won) streak += won ? 1 : -1;
    else break;
  }
  return streak;
}

const round4 = (x) => Math.round(x * 10000) / 10000;
const round2 = (x) => Math.round(x * 100) / 100;

// The betting record over counted rounds resolved in [start, end): the same rules as db.leaderboard (a no-bet is a
// loss; ranked by profit, then win rate) and exports.crowd_record for The Crowd.
async function board(ctx, start, end) {
  const m = await meta(ctx);
  const lo = start ?? -1;
  const hi = end ?? 8.64e15;
  const [ent, rnd] = await ctx.db.batch([
    ctx.db.prepare(
      `SELECT e.agent, e.outcome, e.won, e.pnl, e.stake_filled FROM api_entries e JOIN api_rounds r ON r.round_id = e.round_id
        WHERE r.counts = 1 AND r.sealed = 0 AND r.resolved_at >= ? AND r.resolved_at < ? ORDER BY r.resolved_at, r.round_id`,
    ).bind(lo, hi),
    ctx.db.prepare('SELECT data FROM api_rounds WHERE counts = 1 AND sealed = 0 AND resolved_at >= ? AND resolved_at < ? ORDER BY resolved_at')
      .bind(lo, hi),
  ]);
  const names = Object.fromEntries((m.agents || []).map((a) => [a.agent, a.name]));
  const acc = {};
  for (const a of m.agents || []) acc[a.agent] = [];
  for (const e of ent.results) (acc[e.agent] = acc[e.agent] || []).push(e);
  const agents = Object.entries(acc).map(([agent, rows]) => {
    const played = rows.length;
    const wins = rows.filter((r) => r.outcome && r.won === 1).length;
    const staked = rows.reduce((t, r) => t + (r.stake_filled || 0), 0);
    const net = rows.reduce((t, r) => t + (r.pnl || 0), 0);
    return {
      agent, name: names[agent] || agent, played, wins, losses: played - wins,
      no_bets: rows.filter((r) => !r.outcome).length,
      win_rate: played ? round4(wins / played) : 0, net_pnl: round2(net), staked: round2(staked),
      roi: staked ? round4(net / staked) : 0, streak: streakOf(rows.map((r) => !!r.outcome && r.won === 1)),
    };
  });
  agents.sort((a, b) => b.net_pnl - a.net_pnl || b.win_rate - a.win_rate);
  agents.forEach((a, i) => { a.rank = i + 1; });
  let played = 0;
  let wins = 0;
  let net = 0;
  for (const row of rnd.results) {
    const c = JSON.parse(row.data).crowd;
    if (!c || !c.outcome) continue;
    played++;
    if (c.won) wins++;
    net += Number(c.pnl) || 0;
  }
  const crowd = { name: 'The Crowd', played, wins, losses: played - wins, win_rate: played ? round4(wins / played) : 0, net_pnl: round2(net) };
  return { agents, crowd, rounds_counted: rnd.results.length };
}

export async function leaderboard(ctx) {
  const period = ctx.url.searchParams.get('period') || 'season';
  const [start, end] = periodRange(period, ctx.env, ctx.now, ctx.url.searchParams.get('month'));
  return envelope(ctx, 'leaderboard/', { period, since: iso(start), until: iso(end), ...(await board(ctx, start, end)) });
}

async function agentOr404(ctx, id) {
  if (!AGENT_RE.test(id)) throw new HttpError(404, 'not_found', 'no such agent');
  const m = await meta(ctx);
  const known = (m.agents || []).find((a) => a.agent === id);
  if (known) return known;
  const any = await ctx.db.prepare('SELECT 1 FROM api_entries WHERE agent = ? LIMIT 1').bind(id).first();
  if (!any) throw new HttpError(404, 'not_found', 'no such agent');
  return { agent: id, name: id, model: null, model_id: null, reasoning: null };
}

export async function agent(ctx, { id }) {
  const a = await agentOr404(ctx, id);
  const models = await ctx.db
    .prepare(
      `SELECT e.model_reported AS model, COUNT(*) AS played,
              SUM(CASE WHEN e.outcome IS NOT NULL AND e.won = 1 THEN 1 ELSE 0 END) AS wins,
              COALESCE(SUM(e.pnl), 0) AS net_pnl, MIN(r.round_id) AS first_round, MAX(r.round_id) AS last_round
         FROM api_entries e JOIN api_rounds r ON r.round_id = e.round_id
        WHERE e.agent = ? AND r.counts = 1 AND r.sealed = 0
        GROUP BY e.model_reported ORDER BY MIN(r.round_id)`,
    )
    .bind(id)
    .all();
  const rec = async (start, end) => (await board(ctx, start, end)).agents.find((x) => x.agent === id) || null;
  const [ws] = periodRange('week', ctx.env, ctx.now);
  const [ms, me] = periodRange('month', ctx.env, ctx.now);
  return envelope(ctx, `agent/${id}/`, {
    ...a,
    record: {
      week: await rec(ws, null), month: await rec(ms, me), season: await rec(seasonStart(ctx.env), null), all: await rec(null, null),
    },
    models: models.results.map((r) => ({
      model_reported: r.model, played: r.played, wins: r.wins, losses: r.played - r.wins, net_pnl: round2(r.net_pnl),
      first_round: r.first_round, last_round: r.last_round,
    })),
    rounds_url: `${apiBase(ctx)}/agents/${id}/rounds`,
  });
}

export async function agentRounds(ctx, { id }) {
  await agentOr404(ctx, id);
  const cursor = cursorParam(ctx.url);
  const limit = limitParam(ctx.url);
  const rows = await ctx.db
    .prepare(
      `SELECT r.round_id, r.state, r.sealed, r.data FROM api_rounds r JOIN api_entries e ON e.round_id = r.round_id
        WHERE e.agent = ? ${cursor !== null ? 'AND r.round_id < ?' : ''} ORDER BY r.round_id DESC LIMIT ?`,
    )
    .bind(...(cursor !== null ? [id, cursor, limit + 1] : [id, limit + 1]))
    .all();
  const out = rows.results.slice(0, limit).map((row) => {
    const r = parseRow(row);
    const e = r.entries.find((x) => x.agent === id) || cleanEntry({ agent: id }, true);
    return {
      round_id: r.round_id, question: r.question, category: r.category, state: r.state, counts: r.counts,
      exclusion: r.exclusion, sealed: r.sealed, started_at: r.started_at, resolved_at: r.resolved_at,
      outcome: e.outcome, won: e.won, pnl: e.pnl, model_reported: e.model_reported,
    };
  });
  return envelope(ctx, `agent/${id}/`, { agent: id, rounds: out, next_cursor: rows.results.length > limit ? out[out.length - 1].round_id : null });
}

// "Humans vs the AIs": totals only (no names, no ids), from the same tables as /v1/scoreboard.
export async function humans(ctx) {
  const period = ctx.url.searchParams.get('period') || 'season';
  const [start, end] = periodRange(period, ctx.env, ctx.now, ctx.url.searchParams.get('month'));
  const lo = start ?? -1;
  const hi = end ?? 8.64e15;
  const db = ctx.db;
  const [h, vs, people, polls] = await db.batch([
    db.prepare(
      `SELECT COUNT(p.correct) AS picks, COALESCE(SUM(p.correct), 0) AS correct
         FROM picks p JOIN markets m ON m.market_id = p.market_id WHERE m.locks_at >= ? AND m.locks_at < ?`,
    ).bind(lo, hi),
    db.prepare(
      `SELECT v.ai, v.result, COUNT(*) AS n FROM pick_vs v JOIN markets m ON m.market_id = v.market_id
        WHERE m.locks_at >= ? AND m.locks_at < ? GROUP BY v.ai, v.result`,
    ).bind(lo, hi),
    db.prepare(
      `SELECT COUNT(DISTINCT p.user_id) AS n, COUNT(DISTINCT p.market_id) AS markets FROM picks p JOIN markets m ON m.market_id = p.market_id
        WHERE m.locks_at >= ? AND m.locks_at < ? AND p.correct IS NOT NULL`,
    ).bind(lo, hi),
    db.prepare('SELECT x_poll, winner FROM markets WHERE locks_at >= ? AND locks_at < ? AND x_poll IS NOT NULL AND winner IS NOT NULL')
      .bind(lo, hi),
  ]);
  const byAi = {};
  const key = { w: 'human_wins', l: 'ai_wins', t: 'ties' };
  for (const r of vs.results) {
    byAi[r.ai] = byAi[r.ai] || { human_wins: 0, ai_wins: 0, ties: 0 };
    byAi[r.ai][key[r.result]] = r.n;
  }
  const xPoll = { markets: 0, votes: 0, correct: 0 };
  for (const r of polls.results) {
    let poll;
    try {
      poll = JSON.parse(r.x_poll);
    } catch {
      continue;
    }
    xPoll.markets++;
    for (const [label, votes] of Object.entries(poll)) {
      const n = Number(votes) || 0;
      xPoll.votes += n;
      if (label === r.winner) xPoll.correct += n;
    }
  }
  return envelope(ctx, '', {
    period, since: iso(start), until: iso(end),
    humans: { ...h.results[0], people: people.results[0].n, markets: people.results[0].markets },
    by_ai: byAi, x_poll: xPoll,
  });
}

export async function openapi(ctx) {
  const m = await meta(ctx);
  const str = { type: 'string' };
  const nstr = { type: ['string', 'null'] };
  const q = (name, schema, description) => ({ name, in: 'query', required: false, schema, description });
  const path = (name) => ({ name, in: 'path', required: true, schema: str });
  const period = q('period', { type: 'string', enum: ['week', 'month', 'season', 'all'] },
    'week: ISO week (Monday UTC); month: calendar month (UTC), or ?month=YYYY-MM; season: since the season start; all');
  const ok = (description) => ({ 200: { description }, 429: { description: 'Rate limited; retry_after (s) in the body' } });
  const get = (summary, params = []) => ({ get: { summary, parameters: params, responses: ok(summary) } });
  return json({
    openapi: '3.1.0',
    info: {
      title: 'AgentpitBench free API', version: '2.0',
      description: 'Live, read-only. No key: 60 requests/hour per IP. Free key (Authorization: Bearer apb_...): 10,000/day, '
        + '10/second. Data CC BY 4.0: credit "Data: AgentpitBench (agentpitbench.org)" with a link. Not betting advice. '
        + 'Picks of a running round are null until every AI has finished (the embargo).',
      license: { name: 'CC BY 4.0 (data)', url: 'https://creativecommons.org/licenses/by/4.0/' },
    },
    servers: [{ url: 'https://api.agentpitbench.org/v2' }],
    components: {
      securitySchemes: { key: { type: 'http', scheme: 'bearer', description: 'Optional free key from /developers/' } },
      schemas: { Envelope: { type: 'object', properties: {
        api_version: str, as_of: str, methodology_version: nstr, source_url: str, citation: str, attribution: str,
        license: str, data: { type: 'object' } } } },
    },
    security: [{}, { key: [] }],
    paths: {
      '/rounds': get('Rounds, newest first, filtered and paginated', [
        q('state', { type: 'string', enum: [...STATES] }), q('category', str), q('agent', str),
        q('since', str, 'started at or after (ISO 8601 or epoch)'), q('until', str, 'started before'),
        q('cursor', { type: 'integer' }, 'next_cursor from the previous page'), q('limit', { type: 'integer', maximum: MAX_LIMIT })]),
      '/rounds/{n}': get('One round; picks are null while it is sealed', [path('n')]),
      '/upcoming': get('Markets the AIs play next; locks_at = when the AIs start'),
      '/live': get('Rounds in progress or awaiting resolution, with the chance now'),
      '/leaderboard': get('The betting record and The Crowd', [period, q('month', str)]),
      '/agents/{id}': get('One agent: models as reported, record by period', [path('id')]),
      '/agents/{id}/rounds': get("One agent's rounds, newest first", [path('id'), q('cursor', { type: 'integer' }), q('limit', { type: 'integer' })]),
      '/humans': get('Humans vs the AIs: totals only', [period, q('month', str)]),
    },
    'x-methodology-version': m.methodology_version || null,
  }, 200, { 'Cache-Control': 'public, max-age=300' });
}

// ---------------------------------------------------------------- access: off switch, keys, rate limits

export async function v2Enabled(env, db) {
  if (env.API_V2_ENABLED === '0') return false;
  const off = await kvGet(db, 'api_v2_off');
  return !(off && off.v === true);
}

function bump(db, k, exp) {
  return db.prepare('INSERT INTO api_counters (k, n, exp) VALUES (?, 1, ?) ON CONFLICT(k) DO UPDATE SET n = n + 1 RETURNING n')
    .bind(k, exp);
}

function usage(db, keyId, day) {
  return db.prepare('INSERT INTO api_usage (key_id, day, n) VALUES (?, ?, 1) ON CONFLICT(key_id, day) DO UPDATE SET n = n + 1 RETURNING n')
    .bind(keyId, day);
}

const refuse = (db, keyId, day) =>
  db.prepare('UPDATE api_usage SET n = MAX(n - 1, 0), limited = limited + 1 WHERE key_id = ? AND day = ?').bind(keyId, day);

function limited(what, retry, headers) {
  const e = new HttpError(429, 'rate_limited', `limit: ${what}`, { retry_after: retry });
  e.headers = { ...headers, 'Retry-After': String(retry) };
  return e;
}

// Counts the request and returns the rate headers, or throws 401 (bad key) / 429 (over a limit).
export async function admit(ctx) {
  const L = limits(ctx.env);
  const db = ctx.db;
  const now = ctx.now;
  const day = dayOf(now);
  const sweep = Math.random() < 0.02 ? [db.prepare('DELETE FROM api_counters WHERE exp < ?').bind(now)] : [];
  const auth = ctx.request.headers.get('Authorization') || '';
  if (auth) {
    const m = /^Bearer\s+(\S+)$/i.exec(auth);
    if (!m || !KEY_RE.test(m[1])) throw new HttpError(401, 'bad_key', 'Authorization must be "Bearer apb_..."');
    const key = await db.prepare('SELECT id, daily_limit FROM api_keys WHERE hash = ? AND revoked_at IS NULL')
      .bind(await sha256hex(m[1])).first();
    if (!key) throw new HttpError(401, 'bad_key', 'unknown or revoked key');
    const perDay = key.daily_limit || L.key_per_day;
    const sec = Math.floor(now / 1000);
    const [s, d] = await db.batch([
      bump(db, `ks:${key.id}:${sec}`, (sec + 2) * 1000),
      usage(db, key.id, day),
      db.prepare('UPDATE api_keys SET last_used_at = ? WHERE id = ?').bind(now, key.id),
      ...sweep,
    ]);
    const used = d.results[0].n;
    const reset = Date.parse(`${day}T00:00:00Z`) + 86_400_000;
    const headers = {
      'X-RateLimit-Limit': String(perDay),
      'X-RateLimit-Remaining': String(Math.max(0, perDay - used)),
      'X-RateLimit-Reset': String(Math.ceil(reset / 1000)),
    };
    if (used > perDay) {
      await refuse(db, key.id, day).run();
      throw limited(`${perDay} requests per day per key`, Math.max(1, Math.ceil((reset - now) / 1000)), headers);
    }
    if (s.results[0].n > L.key_per_second) {
      await refuse(db, key.id, day).run();
      throw limited(`${L.key_per_second} requests per second per key`, 1, headers);
    }
    return headers;
  }
  // No key: per IP per hour. The IP is hashed with the session secret before it touches storage.
  const ip = ctx.request.headers.get('CF-Connecting-IP') || 'unknown';
  const ipHash = (await sha256hex(`${ctx.env.SESSION_SECRET || ''}|${ip}`)).slice(0, 24);
  const hour = Math.floor(now / 3_600_000) * 3_600_000;
  const [c] = await db.batch([bump(db, `ip:${ipHash}:${hour}`, hour + 3_600_000), usage(db, 'anon', day), ...sweep]);
  const used = c.results[0].n;
  const headers = {
    'X-RateLimit-Limit': String(L.anon_per_hour),
    'X-RateLimit-Remaining': String(Math.max(0, L.anon_per_hour - used)),
    'X-RateLimit-Reset': String(Math.ceil((hour + 3_600_000) / 1000)),
  };
  if (used > L.anon_per_hour) {
    await refuse(db, 'anon', day).run();
    throw limited(`${L.anon_per_hour} requests per hour without a key (a free key gives ${L.key_per_day}/day)`,
      Math.max(1, Math.ceil((hour + 3_600_000 - now) / 1000)), headers);
  }
  return headers;
}

// ---------------------------------------------------------------- keys (signed in, /v1/me/keys)

async function usage7(db, ids, now) {
  if (!ids.length) return {};
  const from = dayOf(now - 6 * 86_400_000);
  const rows = await db.prepare(
    `SELECT key_id, day, n, limited FROM api_usage WHERE day >= ? AND key_id IN (${ids.map(() => '?').join(',')}) ORDER BY day`,
  ).bind(from, ...ids).all();
  const out = {};
  for (const r of rows.results) (out[r.key_id] = out[r.key_id] || []).push({ day: r.day, n: r.n, limited: r.limited });
  return out;
}

const keyOut = (k, use) => ({
  id: k.id, label: k.label, prefix: k.prefix, created_at: iso(k.created_at), last_used_at: iso(k.last_used_at),
  revoked: k.revoked_at !== null, daily_limit: k.daily_limit, usage: use || [],
});

export async function listKeys(ctx) {
  const user = await ctx.requireUser();
  const rows = await ctx.db.prepare('SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC').bind(user.id).all();
  const use = await usage7(ctx.db, rows.results.map((k) => k.id), ctx.now);
  return json({ keys: rows.results.map((k) => keyOut(k, use[k.id])), limits: limits(ctx.env) });
}

export async function createKey(ctx) {
  const user = await ctx.requireUser();
  const body = await readJson(ctx.request);
  const label = typeof body.label === 'string' ? body.label.replace(/[\u0000-\u001f\u007f]/g, '').trim() : '';
  if (label.length < 1 || label.length > 40) throw bad('bad_label', 'label: 1-40 characters');
  if (containsBlocked(label)) throw bad('bad_label', 'label contains blocked words');
  const L = limits(ctx.env);
  const active = await ctx.db.prepare('SELECT COUNT(*) AS n FROM api_keys WHERE user_id = ? AND revoked_at IS NULL').bind(user.id).first();
  if (active.n >= L.keys_per_user) throw new HttpError(409, 'too_many_keys', `at most ${L.keys_per_user} keys; revoke one first`);
  const rate = await checkRate(ctx, `k:${user.id}`, [{ windowMs: 86_400_000, max: KEY_CREATE_LIMIT, what: `${KEY_CREATE_LIMIT} new keys per day` }]);
  const secret = `apb_${randomId(24)}`;
  const id = randomId(9);
  await ctx.db.batch([
    ctx.db.prepare('INSERT INTO api_keys (id, user_id, label, hash, prefix, created_at) VALUES (?, ?, ?, ?, ?, ?)')
      .bind(id, user.id, label, await sha256hex(secret), secret.slice(0, 12), ctx.now),
    ...rate,
  ]);
  const k = await ctx.db.prepare('SELECT * FROM api_keys WHERE id = ?').bind(id).first();
  return json({ key: keyOut(k, []), secret, note: 'Shown once. Only a hash is stored.' }, 201);
}

export async function revokeKey(ctx, { id }) {
  const user = await ctx.requireUser();
  const r = await ctx.db.prepare('UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND user_id = ? RETURNING id')
    .bind(ctx.now, id, user.id).all();
  if (!r.results.length) throw new HttpError(404, 'not_found', 'no such key');
  return json({ ok: true, id, revoked: true });
}

// ---------------------------------------------------------------- operator (bench key)

export async function adminUsage(ctx) {
  const parsed = parseTime(ctx.url.searchParams.get('since'));
  if (parsed === undefined) throw bad('bad_since', 'since must be ISO 8601 or epoch');
  const since = dayOf(parsed ?? ctx.now - 86_400_000);
  const db = ctx.db;
  const [tot, anon, top, keys, active] = await db.batch([
    db.prepare('SELECT COALESCE(SUM(n), 0) AS n, COALESCE(SUM(limited), 0) AS limited FROM api_usage WHERE day >= ?').bind(since),
    db.prepare("SELECT COALESCE(SUM(n), 0) AS n, COALESCE(SUM(limited), 0) AS limited FROM api_usage WHERE day >= ? AND key_id = 'anon'").bind(since),
    db.prepare(
      `SELECT u.key_id, k.prefix, k.label, SUM(u.n) AS n, SUM(u.limited) AS limited FROM api_usage u JOIN api_keys k ON k.id = u.key_id
        WHERE u.day >= ? GROUP BY u.key_id ORDER BY n DESC LIMIT 5`,
    ).bind(since),
    db.prepare('SELECT COUNT(*) AS n FROM api_keys WHERE revoked_at IS NULL'),
    db.prepare("SELECT COUNT(DISTINCT key_id) AS n FROM api_usage WHERE day >= ? AND key_id != 'anon' AND n > 0").bind(since),
  ]);
  return json({
    since, enabled: await v2Enabled(ctx.env, db),
    requests: tot.results[0].n, limited: tot.results[0].limited,
    anon_requests: anon.results[0].n, keyed_requests: tot.results[0].n - anon.results[0].n,
    active_keys: active.results[0].n, keys: keys.results[0].n,
    top: top.results.map((r) => ({ id: r.key_id, prefix: r.prefix, label: r.label, requests: r.n, limited: r.limited })),
  });
}

export async function adminKeys(ctx) {
  const rows = await ctx.db.prepare('SELECT * FROM api_keys ORDER BY created_at DESC LIMIT 1000').all();
  const use = await usage7(ctx.db, rows.results.map((k) => k.id), ctx.now);
  return json({ keys: rows.results.map((k) => ({ ...keyOut(k, use[k.id]), user_id: k.user_id })) });
}

export async function adminKey(ctx, { id }) {
  const body = await readJson(ctx.request);
  const k = await ctx.db.prepare('SELECT id FROM api_keys WHERE id = ?').bind(id).first();
  if (!k) throw new HttpError(404, 'not_found', 'no such key');
  if (body.action === 'revoke') {
    await ctx.db.prepare('UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?').bind(ctx.now, id).run();
  } else if (body.action === 'limit') {
    const n = body.daily_limit === null ? null : Number(body.daily_limit);
    if (n !== null && (!Number.isInteger(n) || n < 1 || n > 10_000_000)) throw bad('bad_limit', 'daily_limit: a positive integer or null');
    await ctx.db.prepare('UPDATE api_keys SET daily_limit = ? WHERE id = ?').bind(n, id).run();
  } else {
    throw bad('bad_action', 'action must be revoke or limit');
  }
  return json({ ok: true, id, action: body.action });
}

export async function adminSwitch(ctx) {
  const body = await readJson(ctx.request);
  if (typeof body.enabled !== 'boolean') throw bad('bad_body', '{enabled: true|false} required');
  await kvPut(ctx.db, 'api_v2_off', !body.enabled, ctx.now).run();
  return json({ ok: true, enabled: await v2Enabled(ctx.env, ctx.db), env_enabled: ctx.env.API_V2_ENABLED !== '0' });
}
