// Endpoint handlers for docs/backend-api.md (v1) and docs/own-pick-spec.md (late picks, import, referrals, the
// challenge streak, X handle links, the funnel counters).
import {
  HttpError, json, readJson, parseTime, iso, periodStart, weekStart, seasonStart, randomId, MAX_INGEST_BODY, sha256hex,
} from './util.js';
import { verifyGoogleIdToken, makeSessionCookie, clearSessionCookie } from './auth.js';
import { generateName, checkName, containsBlocked } from './words.js';

export const AIS = ['claude', 'codex', 'agy', 'grok'];
const DELETED_NAME = 'Deleted user';
const REPORTS_TO_HIDE = 3;
const COMMENT_MAX = 500;
const COMMENT_LIMITS = [
  { windowMs: 10_000, max: 1, what: '1 comment per 10 seconds' },
  { windowMs: 60_000, max: 5, what: '5 comments per minute' },
  { windowMs: 86_400_000, max: 50, what: '50 comments per day' },
];
const REPORT_LIMITS = [{ windowMs: 86_400_000, max: 30, what: '30 reports per day' }];
const RENAME_LIMITS = [{ windowMs: 86_400_000, max: 10, what: '10 renames per day' }];
const ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const ARM_RE = /^[A-Za-z0-9._:-]{1,64}$/;
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const IMPORT_MAX = 100;                 // picks per POST /v1/picks/import
const IMPORT_LIMITS = [{ windowMs: 86_400_000, max: 20, what: '20 imports per day' }];
const LINK_LIMITS = [{ windowMs: 86_400_000, max: 10, what: '10 link codes per day' }];
const EVENT_LIMITS = [{ windowMs: 3_600_000, max: 120, what: '120 per hour' }];
const LINK_TTL_MS = 24 * 3600_000;      // an X link code is valid for a day
const LINK_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';   // no 0/O, 1/I: read off a screen, typed in a post
const BURST_ACCOUNTS = 20;              // more new accounts than this per network per UTC day are flagged (spec §7)
const CHALLENGE_MIN = 3;                // a challenge day needs 3 markets (docs/daily-challenge-spec.md)
const EVENT_KINDS = new Set(['cta', 'share']);

const utcDay = (ms) => new Date(ms).toISOString().slice(0, 10);

// The caller's network (IPv4 /24, IPv6 /48), hashed with SESSION_SECRET: counts sign-ups per network, never stored raw.
async function netHash(ctx) {
  const ip = ctx.request.headers.get('CF-Connecting-IP');
  if (!ip) return null;
  const net = ip.includes(':') ? ip.toLowerCase().split(':').slice(0, 3).join(':') : ip.split('.').slice(0, 3).join('.');
  return (await sha256hex(`${ctx.env.SESSION_SECRET || ''}|net|${net}`)).slice(0, 24);
}

async function ipKey(ctx) {
  const ip = ctx.request.headers.get('CF-Connecting-IP') || 'unknown';
  return (await sha256hex(`${ctx.env.SESSION_SECRET || ''}|${ip}`)).slice(0, 24);
}

const bad = (code, message) => new HttpError(400, code, message);

// ---------------------------------------------------------------- rate limits (D1-backed)

export async function checkRate(ctx, key, limits) {
  const longest = Math.max(...limits.map((l) => l.windowMs));
  const rows = await ctx.db
    .prepare('SELECT ts FROM rate WHERE key = ? AND ts > ? ORDER BY ts DESC')
    .bind(key, ctx.now - longest)
    .all();
  const ts = rows.results.map((r) => r.ts);
  for (const l of limits) {
    const inWindow = ts.filter((t) => t > ctx.now - l.windowMs);
    if (inWindow.length >= l.max) {
      const oldest = inWindow[inWindow.length - 1];
      const retry = Math.max(1, Math.ceil((oldest + l.windowMs - ctx.now) / 1000));
      throw new HttpError(429, 'rate_limited', `limit: ${l.what}`, { retry_after: retry });
    }
  }
  return [
    ctx.db.prepare('INSERT INTO rate (key, ts) VALUES (?, ?)').bind(key, ctx.now),
    ctx.db.prepare('DELETE FROM rate WHERE key = ? AND ts <= ?').bind(key, ctx.now - longest),
  ];
}

// ---------------------------------------------------------------- auth

export async function health() {
  return json({ ok: true, version: 'v1' });
}

export async function authGoogle(ctx) {
  const body = await readJson(ctx.request);
  const claims = await verifyGoogleIdToken(body.credential, ctx.env.GOOGLE_CLIENT_ID, ctx.now);
  let user = await ctx.db.prepare('SELECT id, name FROM users WHERE google_sub = ?').bind(claims.sub).first();
  if (!user) {
    for (let attempt = 0; attempt < 8 && !user; attempt++) {
      const id = randomId(9);
      const name = generateName();
      try {
        await ctx.db
          .prepare('INSERT INTO users (id, google_sub, name, name_lc, created_at, net) VALUES (?, ?, ?, ?, ?, ?)')
          .bind(id, claims.sub, name, name.toLowerCase(), ctx.now, await netHash(ctx))
          .run();
        user = { id, name };
      } catch (e) {
        // Name collision -> retry; concurrent first sign-in -> the other insert won, read it.
        user = await ctx.db.prepare('SELECT id, name FROM users WHERE google_sub = ?').bind(claims.sub).first();
        if (!user && !/UNIQUE/i.test(String(e && e.message))) throw e;
      }
    }
    if (!user) throw new HttpError(503, 'try_again', 'could not create account, try again');
  }
  const cookie = await makeSessionCookie(ctx.env.SESSION_SECRET, user.id, ctx.now);
  return json({ user: { id: user.id, name: user.name } }, 200, { 'Set-Cookie': cookie });
}

export async function logout() {
  return json({ ok: true }, 200, { 'Set-Cookie': clearSessionCookie() });
}

// ---------------------------------------------------------------- me / record

async function recordFor(ctx, uid) {
  const week = weekStart(ctx.now);
  const season = seasonStart(ctx.env);
  const tally = (start) =>
    ctx.db
      .prepare(
        `SELECT COUNT(p.correct) AS picks, COALESCE(SUM(p.correct), 0) AS correct,
                COALESCE(SUM(CASE WHEN m.resolved_at IS NULL THEN 1 ELSE 0 END), 0) AS pending
           FROM picks p JOIN markets m ON m.market_id = p.market_id
          WHERE p.user_id = ? AND m.locks_at >= ? AND p.late = 0`,
      )
      .bind(uid, start);
  const vsStmt = ctx.db
    .prepare(
      `SELECT v.ai, v.result, COUNT(*) AS n FROM pick_vs v JOIN markets m ON m.market_id = v.market_id
        WHERE v.user_id = ? AND m.locks_at >= ? GROUP BY v.ai, v.result`,
    )
    .bind(uid, season);
  const [w, s, vs] = await ctx.db.batch([tally(week), tally(season), vsStmt]);
  const out = {};
  for (const ai of AIS) out[ai] = { w: 0, l: 0, t: 0 };
  for (const r of vs.results) {
    out[r.ai] = out[r.ai] || { w: 0, l: 0, t: 0 };
    out[r.ai][r.result] = r.n;
  }
  return { week: w.results[0], season: s.results[0], vs: out };
}

function publicMarket(m) {
  return {
    market_id: m.market_id,
    round_id: m.round_id,
    question: m.question,
    event_title: m.event_title,
    outcomes: JSON.parse(m.outcomes),
    opens_at: iso(m.opens_at),
    locks_at: iso(m.locks_at),
    url: m.url,
    closes_at: iso(m.closes_at),
    challenge_day: m.challenge_day,
    resolved: m.resolved_at !== null,
    winner: m.resolved_at !== null ? m.winner : null,
    ai_picks: m.resolved_at !== null && m.ai_picks ? JSON.parse(m.ai_picks) : null,
  };
}

// The challenge (docs/daily-challenge-spec.md v0.3 rules, from D1): a day counts once it has CHALLENGE_MIN markets;
// it is finished when every market of it has a result (or was voided); the streak is the run of finished days played
// (any pick, late ones too), newest last; a finished day without a pick ends it; days with no set are skipped.
async function challengeFor(ctx, uid) {
  const rows = await ctx.db
    .prepare(
      `SELECT m.challenge_day AS day, COUNT(*) AS n,
              SUM(CASE WHEN m.resolved_at IS NULL THEN 0 ELSE 1 END) AS done,
              SUM(CASE WHEN p.user_id IS NULL THEN 0 ELSE 1 END) AS mine
         FROM markets m LEFT JOIN picks p ON p.market_id = m.market_id AND p.user_id = ?
        WHERE m.challenge_day IS NOT NULL GROUP BY m.challenge_day HAVING COUNT(*) >= ? ORDER BY m.challenge_day`,
    )
    .bind(uid, CHALLENGE_MIN)
    .all();
  let cur = 0;
  let best = 0;
  let played = 0;
  for (const r of rows.results) {
    if (r.mine > 0) played++;
    if (r.done < r.n) continue; // not finished yet: neither extends nor ends the streak
    cur = r.mine > 0 ? cur + 1 : 0;
    best = Math.max(best, cur);
  }
  return { streak: cur, best, days_played: played };
}

export async function me(ctx) {
  const user = await ctx.user();
  if (!user) return json({ user: null, picks: [], record: null });
  const rows = await ctx.db
    .prepare(
      `SELECT p.market_id, p.outcome, p.correct, p.late, p.updated_at, m.round_id, m.question, m.event_title,
              m.locks_at, m.closes_at, m.resolved_at, m.winner, m.challenge_day
         FROM picks p JOIN markets m ON m.market_id = p.market_id
        WHERE p.user_id = ? ORDER BY m.locks_at DESC LIMIT 500`,
    )
    .bind(user.id)
    .all();
  const x = await ctx.db.prepare('SELECT x_username, link_code, link_code_exp FROM users WHERE id = ?').bind(user.id).first();
  const vsRows = await ctx.db.prepare('SELECT market_id, ai, result FROM pick_vs WHERE user_id = ?').bind(user.id).all();
  const vsBy = {};
  for (const r of vsRows.results) (vsBy[r.market_id] = vsBy[r.market_id] || {})[r.ai] = r.result;
  const picks = rows.results.map((r) => ({
    market_id: r.market_id,
    round_id: r.round_id,
    question: r.question,
    event_title: r.event_title,
    outcome: r.outcome,
    locks_at: iso(r.locks_at),
    closes_at: iso(r.closes_at),
    locked: ctx.now >= r.locks_at || r.resolved_at !== null,
    late: r.late === 1,
    resolved: r.resolved_at !== null,
    winner: r.resolved_at !== null ? r.winner : null,
    correct: r.correct === null ? null : r.correct === 1,
    vs: vsBy[r.market_id] || {},
    challenge_day: r.challenge_day,
    updated_at: iso(r.updated_at),
  }));
  const live = x && x.link_code && x.link_code_exp > ctx.now;
  return json({
    user,
    picks,
    record: await recordFor(ctx, user.id),
    challenge: await challengeFor(ctx, user.id),
    x: { username: (x && x.x_username) || null, code: live ? x.link_code : null, code_expires: live ? iso(x.link_code_exp) : null },
  });
}

// A public profile: the name and record. With ?market=<id> (the referral bar, spec §5) also that person's pick on
// the market, but only once the market has locked (a friend's open pick stays hidden), and whether it was right
// once resolved. A late pick is never shown here.
export async function publicUser(ctx, { id }) {
  const user = await ctx.db.prepare('SELECT id, name FROM users WHERE id = ?').bind(id).first();
  if (!user) throw new HttpError(404, 'not_found', 'no such user');
  const out = { user, record: await recordFor(ctx, user.id) };
  const mid = ctx.url.searchParams.get('market');
  if (mid && ID_RE.test(mid)) {
    const r = await ctx.db
      .prepare(
        `SELECT p.outcome, p.correct, m.locks_at, m.resolved_at FROM picks p JOIN markets m ON m.market_id = p.market_id
          WHERE p.user_id = ? AND p.market_id = ? AND p.late = 0`,
      )
      .bind(user.id, mid)
      .first();
    const locked = r && (ctx.now >= r.locks_at || r.resolved_at !== null);
    out.market = {
      market_id: mid,
      picked: !!r,
      pick: locked ? r.outcome : null,
      correct: r && r.resolved_at !== null && r.correct !== null ? r.correct === 1 : null,
    };
  }
  return json(out);
}

export async function rename(ctx) {
  const user = await ctx.requireUser();
  const body = await readJson(ctx.request);
  const checked = checkName(body.name);
  if (checked.error) throw bad('bad_name', checked.error);
  const rate = await checkRate(ctx, `n:${user.id}`, RENAME_LIMITS);
  try {
    await ctx.db.batch([
      ctx.db.prepare('UPDATE users SET name = ?, name_lc = ? WHERE id = ?').bind(checked.name, checked.name.toLowerCase(), user.id),
      ...rate,
    ]);
  } catch (e) {
    if (/UNIQUE/i.test(String(e && e.message))) throw new HttpError(409, 'name_taken', 'that name is taken');
    throw e;
  }
  return json({ user: { id: user.id, name: checked.name } });
}

export async function deleteAccount(ctx) {
  const user = await ctx.requireUser();
  const db = ctx.db;
  const uid = user.id;
  await db.batch([
    db.prepare('UPDATE comments SET user_id = NULL WHERE user_id = ?').bind(uid),
    db.prepare('UPDATE comments SET likes = likes - 1 WHERE id IN (SELECT comment_id FROM likes WHERE user_id = ?)').bind(uid),
    db.prepare('DELETE FROM likes WHERE user_id = ?').bind(uid),
    db.prepare('DELETE FROM pick_vs WHERE user_id = ?').bind(uid),
    db.prepare('DELETE FROM picks WHERE user_id = ?').bind(uid),
    db.prepare("DELETE FROM rate WHERE key IN ('c:' || ?, 'r:' || ?, 'n:' || ?, 'k:' || ?, 'i:' || ?, 'x:' || ?)").bind(uid, uid, uid, uid, uid, uid),
    db.prepare('UPDATE picks SET ref_user = NULL WHERE ref_user = ?').bind(uid),
    // API keys (docs/free-api-spec.md) go with the account, and their usage rows with them.
    db.prepare('DELETE FROM api_usage WHERE key_id IN (SELECT id FROM api_keys WHERE user_id = ?)').bind(uid),
    db.prepare('DELETE FROM api_keys WHERE user_id = ?').bind(uid),
    db.prepare('DELETE FROM users WHERE id = ?').bind(uid),
  ]);
  return json({ ok: true }, 200, { 'Set-Cookie': clearSessionCookie() });
}

// ---------------------------------------------------------------- markets and picks

function str(v, max) {
  return typeof v === 'string' && v.length > 0 && v.length <= max;
}

// The bench's round_id is an integer and market ids may be numeric: accept numbers, store as strings.
const asId = (v) => (typeof v === 'number' && Number.isFinite(v) ? String(v) : v);

function validateMarket(raw, i) {
  const where = `markets[${i}]`;
  if (!raw || typeof raw !== 'object') throw bad('bad_market', `${where} must be an object`);
  const m = { ...raw, market_id: asId(raw.market_id), round_id: asId(raw.round_id) };
  if (!str(m.market_id, 128) || !ID_RE.test(m.market_id)) throw bad('bad_market', `${where}.market_id invalid`);
  if (m.round_id != null && (!str(m.round_id, 128) || !ID_RE.test(m.round_id))) throw bad('bad_market', `${where}.round_id invalid`);
  if (!str(m.question, 1000)) throw bad('bad_market', `${where}.question required`);
  if (m.event_title != null && typeof m.event_title !== 'string') throw bad('bad_market', `${where}.event_title must be a string`);
  if (!Array.isArray(m.outcomes) || m.outcomes.length < 2 || m.outcomes.length > 50 || !m.outcomes.every((o) => str(o, 200))) {
    throw bad('bad_market', `${where}.outcomes must be 2-50 non-empty strings`);
  }
  if (new Set(m.outcomes).size !== m.outcomes.length) throw bad('bad_market', `${where}.outcomes must be distinct`);
  const locks = parseTime(m.locks_at);
  if (!locks) throw bad('bad_market', `${where}.locks_at required (ISO 8601 or epoch)`);
  const opens = parseTime(m.opens_at);
  if (opens === undefined) throw bad('bad_market', `${where}.opens_at invalid`);
  if (m.url != null && !str(m.url, 2000)) throw bad('bad_market', `${where}.url invalid`);
  if (m.challenge_day != null && (typeof m.challenge_day !== 'string' || !DAY_RE.test(m.challenge_day))) {
    throw bad('bad_market', `${where}.challenge_day must be YYYY-MM-DD`);
  }
  const closes = parseTime(m.end_date);
  if (closes === undefined) throw bad('bad_market', `${where}.end_date invalid`);
  return { ...m, locks, opens, closes };
}

export async function ingestMarkets(ctx) {
  const body = await readJson(ctx.request, MAX_INGEST_BODY);
  const items = Array.isArray(body) ? body : body.markets;
  if (!Array.isArray(items) || items.length > 2000) throw bad('bad_body', 'expected an array of markets (max 2000)');
  const valid = items.map(validateMarket);
  const stmts = valid.map((m) =>
    ctx.db
      .prepare(
        `INSERT INTO markets (market_id, round_id, question, event_title, outcomes, opens_at, locks_at, url, updated_at,
                              challenge_day, closes_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(market_id) DO UPDATE SET
           round_id = COALESCE(excluded.round_id, markets.round_id), question = excluded.question,
           event_title = excluded.event_title, outcomes = excluded.outcomes, opens_at = excluded.opens_at,
           locks_at = excluded.locks_at, url = excluded.url, updated_at = excluded.updated_at,
           challenge_day = COALESCE(excluded.challenge_day, markets.challenge_day),
           closes_at = COALESCE(excluded.closes_at, markets.closes_at)`,
      )
      .bind(m.market_id, m.round_id ?? null, m.question, m.event_title ?? null, JSON.stringify(m.outcomes),
        m.opens ?? null, m.locks, m.url ?? null, ctx.now, m.challenge_day ?? null, m.closes ?? null),
  );
  for (let i = 0; i < stmts.length; i += 100) await ctx.db.batch(stmts.slice(i, i + 100));
  return json({ ok: true, upserted: stmts.length });
}

async function getMarketRow(ctx, id) {
  if (!ID_RE.test(id)) throw new HttpError(404, 'not_found', 'no such market');
  const m = await ctx.db.prepare('SELECT * FROM markets WHERE market_id = ?').bind(id).first();
  if (!m) throw new HttpError(404, 'not_found', 'no such market');
  return m;
}

const isLocked = (ctx, m) => ctx.now >= m.locks_at || m.resolved_at !== null;

const lockedError = (m) => new HttpError(409, 'locked', 'picks are locked for this market', { locks_at: iso(m.locks_at) });

// A referral (spec §5): kept only on the person's first pick, never their own id, only an existing user.
async function refFor(ctx, uid, raw) {
  if (typeof raw !== 'string' || !ID_RE.test(raw) || raw === uid) return null;
  const [first, ref] = await ctx.db.batch([
    ctx.db.prepare('SELECT 1 AS x FROM picks WHERE user_id = ? LIMIT 1').bind(uid),
    ctx.db.prepare('SELECT id FROM users WHERE id = ?').bind(raw),
  ]);
  return first.results.length === 0 && ref.results.length ? raw : null;
}

// One pick per user per market (spec §4). Before locks_at: stored or changed (a counted pick). From locks_at until the
// result: stored once as a late pick (late = 1: the challenge only), never changed. After the result: 409 locked.
export async function postPick(ctx) {
  const user = await ctx.requireUser();
  const body = await readJson(ctx.request);
  const marketId = asId(body.market_id);
  if (typeof marketId !== 'string') throw bad('bad_pick', 'market_id required');
  const m = await getMarketRow(ctx, marketId);
  const outcomes = JSON.parse(m.outcomes);
  if (!outcomes.includes(body.outcome)) throw bad('bad_outcome', 'outcome must be one of the market outcomes');
  if (m.resolved_at !== null) throw lockedError(m);
  const late = ctx.now >= m.locks_at;
  if (!late && m.opens_at !== null && ctx.now < m.opens_at) {
    throw new HttpError(409, 'not_open', 'picks are not open yet', { opens_at: iso(m.opens_at) });
  }
  const existing = await ctx.db.prepare('SELECT late FROM picks WHERE user_id = ? AND market_id = ?').bind(user.id, m.market_id).first();
  if (existing && (late || existing.late === 1)) throw lockedError(m);
  const arm = typeof body.cta_arm === 'string' && ARM_RE.test(body.cta_arm) ? body.cta_arm : null;
  const ref = existing ? null : await refFor(ctx, user.id, body.ref);
  if (late) {
    const ins = await ctx.db
      .prepare(
        `INSERT OR IGNORE INTO picks (user_id, market_id, outcome, created_at, updated_at, late, ref_user, cta_arm)
         VALUES (?, ?, ?, ?, ?, 1, ?, ?)`,
      )
      .bind(user.id, m.market_id, body.outcome, ctx.now, ctx.now, ref, arm)
      .run();
    if (!ins.meta || ins.meta.changes === 0) throw lockedError(m); // a concurrent first pick won
  } else {
    await ctx.db
      .prepare(
        `INSERT INTO picks (user_id, market_id, outcome, created_at, updated_at, ref_user, cta_arm) VALUES (?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(user_id, market_id) DO UPDATE SET outcome = excluded.outcome, updated_at = excluded.updated_at,
           cta_arm = COALESCE(picks.cta_arm, excluded.cta_arm)
         WHERE picks.late = 0`,
      )
      .bind(user.id, m.market_id, body.outcome, ctx.now, ctx.now, ref, arm)
      .run();
  }
  const p = await ctx.db.prepare('SELECT * FROM picks WHERE user_id = ? AND market_id = ?').bind(user.id, m.market_id).first();
  return json({
    pick: {
      market_id: p.market_id, outcome: p.outcome, created_at: iso(p.created_at), updated_at: iso(p.updated_at),
      locks_at: iso(m.locks_at), closes_at: iso(m.closes_at), late: p.late === 1,
    },
    late: p.late === 1,
  });
}

// First sign-in imports the picks made on this device (spec §6). The client's time is never trusted: on the server's
// clock a pick is counted before locks_at, late after it, dropped once the market has a result. Existing picks win.
export async function importPicks(ctx) {
  const user = await ctx.requireUser();
  const body = await readJson(ctx.request);
  const items = Array.isArray(body) ? body : body.picks;
  if (!Array.isArray(items) || items.length > IMPORT_MAX) throw bad('bad_body', `expected an array of picks (max ${IMPORT_MAX})`);
  const rate = await checkRate(ctx, `i:${user.id}`, IMPORT_LIMITS);
  const out = { counted: 0, late: 0, dropped: 0, kept: 0, unknown: 0 };
  const stmts = [...rate];
  const seen = new Set();
  for (const raw of items) {
    const id = raw && typeof raw === 'object' ? asId(raw.market_id) : null;
    if (typeof id !== 'string' || !ID_RE.test(id) || seen.has(id)) {
      out.unknown++;
      continue;
    }
    seen.add(id);
    const m = await ctx.db.prepare('SELECT * FROM markets WHERE market_id = ?').bind(id).first();
    if (!m || !JSON.parse(m.outcomes).includes(raw.outcome)) {
      out.unknown++;
      continue;
    }
    if (m.resolved_at !== null) {
      out.dropped++;
      continue;
    }
    const mine = await ctx.db.prepare('SELECT 1 AS x FROM picks WHERE user_id = ? AND market_id = ?').bind(user.id, id).first();
    if (mine) {
      out.kept++;
      continue;
    }
    const late = ctx.now >= m.locks_at ? 1 : 0;
    if (!late && m.opens_at !== null && ctx.now < m.opens_at) {
      out.unknown++;
      continue;
    }
    out[late ? 'late' : 'counted']++;
    stmts.push(
      ctx.db
        .prepare('INSERT OR IGNORE INTO picks (user_id, market_id, outcome, created_at, updated_at, late) VALUES (?, ?, ?, ?, ?, ?)')
        .bind(user.id, id, raw.outcome, ctx.now, ctx.now, late),
    );
  }
  await ctx.db.batch(stmts);
  return json({ ok: true, ...out });
}

export async function getMarket(ctx, { id }) {
  const m = await getMarketRow(ctx, id);
  const locked = isLocked(ctx, m);
  const user = await ctx.user();
  let myPick = null;
  let myLate = false;
  if (user) {
    const p = await ctx.db.prepare('SELECT outcome, late FROM picks WHERE user_id = ? AND market_id = ?').bind(user.id, m.market_id).first();
    myPick = p ? p.outcome : null;
    myLate = !!(p && p.late === 1);
  }
  let counts = null;
  if (locked) {
    // counted picks only: a late pick never enters the split
    counts = {};
    for (const o of JSON.parse(m.outcomes)) counts[o] = 0;
    const rows = await ctx.db
      .prepare('SELECT outcome, COUNT(*) AS n FROM picks WHERE market_id = ? AND late = 0 GROUP BY outcome')
      .bind(m.market_id)
      .all();
    for (const r of rows.results) counts[r.outcome] = r.n;
  }
  // late_open: the AIs have started and there is no result yet, so a late pick (the challenge only) can be stored
  return json({ market: publicMarket(m), counts, my_pick: myPick, my_late: myLate, locked, late_open: locked && m.resolved_at === null });
}

export async function ingestResults(ctx) {
  const body = await readJson(ctx.request, MAX_INGEST_BODY);
  const items = Array.isArray(body) ? body : body.results;
  if (!Array.isArray(items) || items.length > 2000) throw bad('bad_body', 'expected an array of results (max 2000)');
  const db = ctx.db;
  const unknown = [];
  let scored = 0;
  let voided = 0;
  for (const [i, raw] of items.entries()) {
    const r = raw && typeof raw === 'object' ? { ...raw, market_id: asId(raw.market_id), round_id: asId(raw.round_id) } : raw;
    if (!r || typeof r.market_id !== 'string') throw bad('bad_result', `results[${i}].market_id required`);
    const m = await db.prepare('SELECT * FROM markets WHERE market_id = ?').bind(r.market_id).first();
    if (!m) {
      unknown.push(r.market_id);
      continue;
    }
    const outcomes = JSON.parse(m.outcomes);
    const aiPicks = r.ai_picks && typeof r.ai_picks === 'object' ? r.ai_picks : {};
    const xPoll = r.x_poll && typeof r.x_poll === 'object' ? r.x_poll : null;
    // A winner outside the outcomes (null, "void", cancelled) voids the market: picks are not scored.
    const winner = typeof r.winner === 'string' && outcomes.includes(r.winner) ? r.winner : null;
    const roundId = typeof r.round_id === 'string' && ID_RE.test(r.round_id) ? r.round_id : null;
    const stmts = [
      db
        .prepare(
          `UPDATE markets SET winner = ?, ai_picks = ?, x_poll = COALESCE(?, x_poll),
             round_id = COALESCE(?, round_id), resolved_at = ?, updated_at = ? WHERE market_id = ?`,
        )
        .bind(winner, JSON.stringify(aiPicks), xPoll ? JSON.stringify(xPoll) : null, roundId, ctx.now, ctx.now, m.market_id),
      db.prepare('DELETE FROM pick_vs WHERE market_id = ?').bind(m.market_id),
    ];
    if (winner === null) {
      stmts.push(db.prepare('UPDATE picks SET correct = NULL WHERE market_id = ?').bind(m.market_id));
      voided++;
    } else {
      stmts.push(db.prepare('UPDATE picks SET correct = CASE WHEN outcome = ? THEN 1 ELSE 0 END WHERE market_id = ?').bind(winner, m.market_id));
      for (const [ai, aiOutcome] of Object.entries(aiPicks)) {
        if (typeof aiOutcome !== 'string' || !/^[a-z0-9_-]{1,32}$/.test(ai)) continue; // AI without a pick: skipped
        const aiRight = aiOutcome === winner ? 1 : 0;
        stmts.push(
          db
            .prepare(
              `INSERT INTO pick_vs (user_id, market_id, ai, result)
               SELECT user_id, market_id, ?,
                      CASE WHEN correct = 1 AND ? = 0 THEN 'w' WHEN correct = 0 AND ? = 1 THEN 'l' ELSE 't' END
                 FROM picks WHERE market_id = ? AND late = 0`,
            )
            .bind(ai, aiRight, aiRight, m.market_id),
        );
      }
      scored++;
    }
    await db.batch(stmts);
  }
  return json({ ok: true, scored, voided, unknown });
}

// ---------------------------------------------------------------- scoreboard

export async function scoreboard(ctx) {
  const period = ctx.url.searchParams.get('period') || 'week';
  const start = periodStart(period, ctx.env, ctx.now);
  const db = ctx.db;
  const [humans, vs, top, polls] = await db.batch([
    db
      .prepare(
        `SELECT COUNT(p.correct) AS picks, COALESCE(SUM(p.correct), 0) AS correct
           FROM picks p JOIN markets m ON m.market_id = p.market_id WHERE m.locks_at >= ? AND p.late = 0`,
      )
      .bind(start),
    db
      .prepare(
        `SELECT v.ai, v.result, COUNT(*) AS n FROM pick_vs v JOIN markets m ON m.market_id = v.market_id
          WHERE m.locks_at >= ? GROUP BY v.ai, v.result`,
      )
      .bind(start),
    db
      .prepare(
        `SELECT u.id, u.name, COALESCE(SUM(p.correct), 0) AS correct, COUNT(p.correct) AS picks
           FROM picks p JOIN markets m ON m.market_id = p.market_id JOIN users u ON u.id = p.user_id
          WHERE m.locks_at >= ? AND p.correct IS NOT NULL AND p.late = 0
          GROUP BY u.id HAVING COUNT(p.correct) >= 5
          ORDER BY correct DESC, picks ASC, MIN(p.created_at) ASC LIMIT 20`,
      )
      .bind(start),
    db
      .prepare('SELECT x_poll, winner FROM markets WHERE locks_at >= ? AND x_poll IS NOT NULL AND winner IS NOT NULL')
      .bind(start),
  ]);
  const byAi = {};
  for (const ai of AIS) byAi[ai] = { human_wins: 0, ai_wins: 0, ties: 0 };
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
  return json(
    {
      period,
      since: iso(start),
      humans: humans.results[0],
      by_ai: byAi,
      top: top.results.map((r) => ({ id: r.id, name: r.name, correct: r.correct, picks: r.picks })),
      x_poll: xPoll,
    },
    200,
    { 'Cache-Control': 'public, max-age=60' },
  );
}

// ---------------------------------------------------------------- comments

function commentOut(r, viewerLikes) {
  return {
    id: r.id,
    user: r.user_id ? { id: r.user_id, name: r.name } : { id: null, name: DELETED_NAME },
    pick: r.pick ?? null,
    text: r.text,
    likes: r.likes,
    liked: viewerLikes ? viewerLikes.has(r.id) : false,
    created_at: iso(r.created_at),
    parent_id: r.parent_id ?? null,
    hidden: false,
  };
}

const PICK_SUBQUERY = `(SELECT p.outcome FROM picks p JOIN markets m ON m.market_id = p.market_id
   WHERE m.round_id = c.round_id AND p.user_id = c.user_id ORDER BY m.locks_at LIMIT 1)`;

function checkRound(round) {
  if (!ID_RE.test(round)) throw bad('bad_round', 'round_id invalid');
}

export async function listComments(ctx, { round }) {
  checkRound(round);
  const sort = ctx.url.searchParams.get('sort') || 'new';
  if (sort !== 'new' && sort !== 'top') throw bad('bad_sort', 'sort must be new or top');
  const pickersOnly = ctx.url.searchParams.get('pickers_only') === '1';
  const order = sort === 'top' ? 'likes DESC, created_at DESC, id DESC' : 'created_at DESC, id DESC';
  const rows = await ctx.db
    .prepare(
      `SELECT * FROM (
         SELECT c.id, c.user_id, c.parent_id, c.text, c.likes, c.created_at, u.name, ${PICK_SUBQUERY} AS pick
           FROM comments c LEFT JOIN users u ON u.id = c.user_id
          WHERE c.round_id = ? AND c.hidden = 0
            AND (c.parent_id IS NULL OR c.parent_id IN (SELECT id FROM comments WHERE round_id = ? AND hidden = 0))
       ) ${pickersOnly ? 'WHERE pick IS NOT NULL' : ''}
       ORDER BY ${order} LIMIT 1000`,
    )
    .bind(round, round)
    .all();
  let viewerLikes = null;
  const user = await ctx.user();
  if (user && rows.results.length) {
    const liked = await ctx.db
      .prepare('SELECT l.comment_id FROM likes l JOIN comments c ON c.id = l.comment_id WHERE l.user_id = ? AND c.round_id = ?')
      .bind(user.id, round)
      .all();
    viewerLikes = new Set(liked.results.map((r) => r.comment_id));
  }
  return json(rows.results.map((r) => commentOut(r, viewerLikes)));
}

function cleanText(raw) {
  if (typeof raw !== 'string') return null;
  // Plain text: drop control characters except newline/tab, normalise newlines, trim.
  return raw
    .replace(/\r\n?/g, '\n')
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export async function postComment(ctx, { round }) {
  checkRound(round);
  const user = await ctx.requireUser();
  const body = await readJson(ctx.request);
  const text = cleanText(body.text);
  if (!text) throw bad('bad_text', 'text required');
  if ([...text].length > COMMENT_MAX) throw bad('too_long', `at most ${COMMENT_MAX} characters`);
  if (containsBlocked(text)) throw bad('blocked_words', 'comment contains blocked words');
  let parentId = null;
  if (body.parent_id !== undefined && body.parent_id !== null) {
    parentId = Number(body.parent_id);
    const parent = Number.isInteger(parentId)
      ? await ctx.db.prepare('SELECT id, round_id, parent_id, hidden FROM comments WHERE id = ?').bind(parentId).first()
      : null;
    if (!parent || parent.round_id !== round || parent.hidden) throw bad('bad_parent', 'parent comment not found');
    if (parent.parent_id !== null) throw bad('bad_parent', 'replies are one level deep');
  }
  const rate = await checkRate(ctx, `c:${user.id}`, COMMENT_LIMITS);
  const [ins] = await ctx.db.batch([
    ctx.db
      .prepare('INSERT INTO comments (round_id, user_id, parent_id, text, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id')
      .bind(round, user.id, parentId, text, ctx.now),
    ...rate,
  ]);
  const id = ins.results[0].id;
  const row = await ctx.db
    .prepare(
      `SELECT c.id, c.user_id, c.parent_id, c.text, c.likes, c.created_at, u.name, ${PICK_SUBQUERY} AS pick
         FROM comments c LEFT JOIN users u ON u.id = c.user_id WHERE c.id = ?`,
    )
    .bind(id)
    .first();
  return json(commentOut(row, null), 201);
}

async function visibleComment(ctx, id) {
  const n = Number(id);
  const c = Number.isInteger(n) ? await ctx.db.prepare('SELECT * FROM comments WHERE id = ?').bind(n).first() : null;
  if (!c || c.hidden) throw new HttpError(404, 'not_found', 'no such comment');
  return c;
}

export async function likeComment(ctx, { id }) {
  const user = await ctx.requireUser();
  const c = await visibleComment(ctx, id);
  const existing = await ctx.db.prepare('SELECT 1 FROM likes WHERE comment_id = ? AND user_id = ?').bind(c.id, user.id).first();
  const recount = ctx.db
    .prepare('UPDATE comments SET likes = (SELECT COUNT(*) FROM likes WHERE comment_id = ?) WHERE id = ? RETURNING likes')
    .bind(c.id, c.id);
  const change = existing
    ? ctx.db.prepare('DELETE FROM likes WHERE comment_id = ? AND user_id = ?').bind(c.id, user.id)
    : ctx.db.prepare('INSERT OR IGNORE INTO likes (comment_id, user_id, created_at) VALUES (?, ?, ?)').bind(c.id, user.id, ctx.now);
  const [, counted] = await ctx.db.batch([change, recount]);
  return json({ id: c.id, liked: !existing, likes: counted.results[0].likes });
}

export async function reportComment(ctx, { id }) {
  const user = await ctx.requireUser();
  const body = await readJson(ctx.request);
  const c = await visibleComment(ctx, id);
  if (c.user_id === user.id) throw bad('own_comment', 'you cannot report your own comment');
  const reason = typeof body.reason === 'string' ? body.reason.slice(0, 200) : null;
  const already = await ctx.db.prepare('SELECT 1 FROM reports WHERE comment_id = ? AND user_id = ?').bind(c.id, user.id).first();
  if (already) return json({ id: c.id, reported: true, hidden: false });
  const rate = await checkRate(ctx, `r:${user.id}`, REPORT_LIMITS);
  // Hide once REPORTS_TO_HIDE distinct users reported it since the last operator restore.
  const [, hide] = await ctx.db.batch([
    ctx.db.prepare('INSERT OR IGNORE INTO reports (comment_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)').bind(c.id, user.id, reason, ctx.now),
    ctx.db
      .prepare(
        `UPDATE comments SET hidden = 1, hidden_reason = 'reports'
          WHERE id = ? AND hidden = 0
            AND (SELECT COUNT(DISTINCT user_id) FROM reports
                  WHERE comment_id = ? AND created_at > COALESCE(comments.restored_at, -1)) >= ?
          RETURNING id`,
      )
      .bind(c.id, c.id, REPORTS_TO_HIDE),
    ...rate,
  ]);
  return json({ id: c.id, reported: true, hidden: hide.results.length > 0 });
}

// ---------------------------------------------------------------- operator

export async function adminReview(ctx) {
  const sinceParam = ctx.url.searchParams.get('since');
  const parsed = parseTime(sinceParam);
  if (parsed === undefined) throw bad('bad_since', 'since must be ISO 8601 or epoch');
  const since = parsed ?? ctx.now - 86_400_000;
  const rows = await ctx.db
    .prepare(
      `SELECT c.*, u.name,
              (SELECT COUNT(*) FROM reports r WHERE r.comment_id = c.id) AS reports,
              (SELECT json_group_array(r.reason) FROM reports r WHERE r.comment_id = c.id AND r.reason IS NOT NULL) AS reasons
         FROM comments c LEFT JOIN users u ON u.id = c.user_id
        WHERE c.created_at >= ? OR c.id IN (SELECT comment_id FROM reports WHERE created_at >= ?)
        ORDER BY c.hidden DESC, reports DESC, c.created_at DESC LIMIT 1000`,
    )
    .bind(since, since)
    .all();
  return json({
    since: iso(since),
    comments: rows.results.map((r) => ({
      id: r.id,
      round_id: r.round_id,
      user: r.user_id ? { id: r.user_id, name: r.name } : { id: null, name: DELETED_NAME },
      text: r.text,
      likes: r.likes,
      created_at: iso(r.created_at),
      parent_id: r.parent_id ?? null,
      hidden: !!r.hidden,
      hidden_reason: r.hidden_reason,
      reports: r.reports,
      report_reasons: JSON.parse(r.reasons || '[]'),
    })),
  });
}

export async function adminComment(ctx, { id }) {
  const body = await readJson(ctx.request);
  const n = Number(id);
  const c = Number.isInteger(n) ? await ctx.db.prepare('SELECT id FROM comments WHERE id = ?').bind(n).first() : null;
  if (!c) throw new HttpError(404, 'not_found', 'no such comment');
  const db = ctx.db;
  if (body.action === 'hide') {
    await db.prepare("UPDATE comments SET hidden = 1, hidden_reason = 'operator' WHERE id = ?").bind(n).run();
  } else if (body.action === 'restore') {
    await db.prepare('UPDATE comments SET hidden = 0, hidden_reason = NULL, restored_at = ? WHERE id = ?').bind(ctx.now, n).run();
  } else if (body.action === 'delete') {
    const family = '(SELECT id FROM comments WHERE id = ?1 OR parent_id = ?1)';
    await db.batch([
      db.prepare(`DELETE FROM likes WHERE comment_id IN ${family}`).bind(n),
      db.prepare(`DELETE FROM reports WHERE comment_id IN ${family}`).bind(n),
      db.prepare('DELETE FROM comments WHERE id = ?1 OR parent_id = ?1').bind(n),
    ]);
  } else {
    throw bad('bad_action', 'action must be hide, restore or delete');
  }
  return json({ ok: true, id: n, action: body.action });
}

// ---------------------------------------------------------------- X handle link (own-pick spec, Q2 b)

// A one-time code for /me/: the person posts "@agentpitbench link <code>" on X; the bot reads it from its mentions
// and calls POST /v1/admin/xlink. The site never posts anything; the bot never replies to it.
export async function xlinkCode(ctx) {
  const user = await ctx.requireUser();
  await readJson(ctx.request);
  const rate = await checkRate(ctx, `x:${user.id}`, LINK_LIMITS);
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  const code = [...bytes].map((b) => LINK_ALPHABET[b % LINK_ALPHABET.length]).join('');
  await ctx.db.batch([
    ctx.db.prepare('UPDATE users SET link_code = ?, link_code_exp = ? WHERE id = ?').bind(code, ctx.now + LINK_TTL_MS, user.id),
    ...rate,
  ]);
  return json({ code, code_expires: iso(ctx.now + LINK_TTL_MS) });
}

export async function xlinkRemove(ctx) {
  const user = await ctx.requireUser();
  await readJson(ctx.request);
  await ctx.db
    .prepare('UPDATE users SET x_user_id = NULL, x_username = NULL, link_code = NULL, link_code_exp = NULL WHERE id = ?')
    .bind(user.id)
    .run();
  return json({ ok: true });
}

// The bot (bench key): link the X account that posted the code. One X account belongs to one site account: linking it
// again moves it.
export async function adminXlink(ctx) {
  const body = await readJson(ctx.request);
  const code = typeof body.code === 'string' ? body.code.trim().toUpperCase() : '';
  const xid = asId(body.x_user_id);
  const uname = typeof body.username === 'string' ? body.username.replace(/^@/, '') : '';
  if (!/^[A-Z0-9]{6,12}$/.test(code)) throw bad('bad_code', 'code invalid');
  if (typeof xid !== 'string' || !/^\d{1,32}$/.test(xid)) throw bad('bad_x_user', 'x_user_id must be numeric');
  if (!/^[A-Za-z0-9_]{1,15}$/.test(uname)) throw bad('bad_x_user', 'username invalid');
  const u = await ctx.db.prepare('SELECT id FROM users WHERE link_code = ? AND link_code_exp > ?').bind(code, ctx.now).first();
  if (!u) throw new HttpError(404, 'unknown_code', 'no such code, or it expired');
  await ctx.db.batch([
    ctx.db.prepare('UPDATE users SET x_user_id = NULL, x_username = NULL WHERE x_user_id = ? AND id != ?').bind(xid, u.id),
    ctx.db
      .prepare('UPDATE users SET x_user_id = ?, x_username = ?, link_code = NULL, link_code_exp = NULL WHERE id = ?')
      .bind(xid, uname, u.id),
  ]);
  return json({ ok: true, user_id: u.id, x_user_id: xid, username: uname });
}

// ---------------------------------------------------------------- funnel counters (spec §8)

// POST /v1/cta {arm, round_id, kind?}: kind 'cta' (the default: a button open) or 'share' (a share sheet used); no sign-in, counted per UTC day (and per arm for 'cta'),
// rate-limited per hashed IP. Nothing about the person is stored.
export async function postEvent(ctx) {
  const body = await readJson(ctx.request);
  const kind = body.kind || 'cta';
  if (!EVENT_KINDS.has(kind)) throw bad('bad_kind', 'kind must be cta or share');
  const arm = kind === 'cta' && typeof body.arm === 'string' && ARM_RE.test(body.arm) ? body.arm : '';
  const rate = await checkRate(ctx, `e:${await ipKey(ctx)}`, EVENT_LIMITS);
  await ctx.db.batch([
    ctx.db
      .prepare('INSERT INTO events (day, kind, arm, n) VALUES (?, ?, ?, 1) ON CONFLICT(day, kind, arm) DO UPDATE SET n = n + 1')
      .bind(utcDay(ctx.now), kind, arm),
    ...rate,
  ]);
  return json({ ok: true });
}

function sinceParam(ctx, defaultDays) {
  const parsed = parseTime(ctx.url.searchParams.get('since'));
  if (parsed === undefined) throw bad('bad_since', 'since must be ISO 8601 or epoch');
  return parsed ?? ctx.now - defaultDays * 86_400_000;
}

// The digest's funnel (bench key): per UTC day, CTA opens -> sign-ins -> picks -> shares -> referral picks, plus the
// CTA arms' opens and the picks that carried each arm (sent by the client within 30 minutes of the click).
export async function adminFunnel(ctx) {
  const since = sinceParam(ctx, 7);
  const day0 = utcDay(since);
  const db = ctx.db;
  const dayExpr = (col) => `strftime('%Y-%m-%d', ${col} / 1000, 'unixepoch')`;
  const [ev, signins, picks, arms] = await db.batch([
    db.prepare('SELECT day, kind, arm, n FROM events WHERE day >= ? ORDER BY day').bind(day0),
    db.prepare(`SELECT ${dayExpr('created_at')} AS day, COUNT(*) AS n FROM users WHERE created_at >= ? GROUP BY day`).bind(since),
    db
      .prepare(
        `SELECT ${dayExpr('created_at')} AS day, COUNT(*) AS n, SUM(late) AS late,
                SUM(CASE WHEN ref_user IS NULL THEN 0 ELSE 1 END) AS ref, SUM(CASE WHEN cta_arm IS NULL THEN 0 ELSE 1 END) AS cta
           FROM picks WHERE created_at >= ? GROUP BY day`,
      )
      .bind(since),
    db.prepare('SELECT cta_arm AS arm, COUNT(*) AS n FROM picks WHERE created_at >= ? AND cta_arm IS NOT NULL GROUP BY cta_arm').bind(since),
  ]);
  const days = {};
  const at = (d) => (days[d] = days[d] || { day: d, cta: 0, signins: 0, picks: 0, late_picks: 0, shares: 0, ref_picks: 0, cta_picks: 0 });
  const byArm = {};
  for (const r of ev.results) {
    if (r.kind === 'cta') {
      at(r.day).cta += r.n;
      if (r.arm) (byArm[r.arm] = byArm[r.arm] || { opens: 0, picks: 0 }).opens += r.n;
    } else if (r.kind === 'share') at(r.day).shares += r.n;
  }
  for (const r of signins.results) at(r.day).signins = r.n;
  for (const r of picks.results) Object.assign(at(r.day), { picks: r.n, late_picks: r.late || 0, ref_picks: r.ref || 0, cta_picks: r.cta || 0 });
  for (const r of arms.results) (byArm[r.arm] = byArm[r.arm] || { opens: 0, picks: 0 }).picks = r.n;
  const list = Object.values(days).sort((a, b) => (a.day < b.day ? -1 : 1));
  const total = { cta: 0, signins: 0, picks: 0, late_picks: 0, shares: 0, ref_picks: 0, cta_picks: 0 };
  for (const d of list) for (const k of Object.keys(total)) total[k] += d[k];
  return json({ since: iso(since), days: list, total, arms: byArm });
}

// ---------------------------------------------------------------- the bot's pull of site picks (spec §7)

// Counted (late = 0, stored before locks_at), resolved picks whose market resolved at or after `since`. Accounts from
// a burst of sign-ups (more than BURST_ACCOUNTS on one network in one UTC day) are flagged; the bot skips them.
export async function adminPicks(ctx) {
  const since = sinceParam(ctx, 7);
  const db = ctx.db;
  const [rows, bursts] = await db.batch([
    db
      .prepare(
        `SELECT u.id AS uid, u.name, u.x_user_id, u.x_username, p.market_id, m.round_id, p.outcome, p.correct, p.created_at,
                m.resolved_at, m.winner
           FROM picks p JOIN users u ON u.id = p.user_id JOIN markets m ON m.market_id = p.market_id
          WHERE p.late = 0 AND p.created_at < m.locks_at AND m.resolved_at IS NOT NULL AND m.resolved_at >= ?
          ORDER BY m.resolved_at, p.created_at LIMIT 5000`,
      )
      .bind(since),
    db
      .prepare(
        `SELECT net, strftime('%Y-%m-%d', created_at / 1000, 'unixepoch') AS day, COUNT(*) AS n,
                json_group_array(id) AS ids
           FROM users WHERE net IS NOT NULL GROUP BY net, day HAVING COUNT(*) > ?`,
      )
      .bind(BURST_ACCOUNTS),
  ]);
  const flagged = new Set();
  const burstOut = [];
  for (const b of bursts.results) {
    for (const id of JSON.parse(b.ids || '[]')) flagged.add(id);
    burstOut.push({ day: b.day, accounts: b.n });
  }
  return json({
    since: iso(since),
    picks: rows.results.map((r) => ({
      user_public_id: r.uid,
      name: r.name,
      market_id: r.market_id,
      round_id: r.round_id,
      outcome: r.outcome,
      created_at: iso(r.created_at),
      resolved_at: iso(r.resolved_at),
      correct: r.correct === null ? null : r.correct === 1,
      late: false,
      x_user_id: r.x_user_id || null,
      x_username: r.x_username || null,
      flagged: flagged.has(r.uid),
    })),
    flagged: [...flagged],
    bursts: burstOut,
  });
}
