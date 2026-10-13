// Endpoint handlers for docs/backend-api.md (v1).
import {
  HttpError, json, readJson, parseTime, iso, periodStart, weekStart, seasonStart, randomId, MAX_INGEST_BODY,
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

const bad = (code, message) => new HttpError(400, code, message);

// ---------------------------------------------------------------- rate limits (D1-backed)

async function checkRate(ctx, key, limits) {
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
          .prepare('INSERT INTO users (id, google_sub, name, name_lc, created_at) VALUES (?, ?, ?, ?, ?)')
          .bind(id, claims.sub, name, name.toLowerCase(), ctx.now)
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
          WHERE p.user_id = ? AND m.locks_at >= ?`,
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
    resolved: m.resolved_at !== null,
    winner: m.resolved_at !== null ? m.winner : null,
    ai_picks: m.resolved_at !== null && m.ai_picks ? JSON.parse(m.ai_picks) : null,
  };
}

export async function me(ctx) {
  const user = await ctx.user();
  if (!user) return json({ user: null, picks: [], record: null });
  const rows = await ctx.db
    .prepare(
      `SELECT p.market_id, p.outcome, p.correct, p.updated_at, m.round_id, m.question, m.event_title,
              m.locks_at, m.resolved_at, m.winner
         FROM picks p JOIN markets m ON m.market_id = p.market_id
        WHERE p.user_id = ? ORDER BY m.locks_at DESC LIMIT 500`,
    )
    .bind(user.id)
    .all();
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
    locked: ctx.now >= r.locks_at || r.resolved_at !== null,
    resolved: r.resolved_at !== null,
    winner: r.resolved_at !== null ? r.winner : null,
    correct: r.correct === null ? null : r.correct === 1,
    vs: vsBy[r.market_id] || {},
    updated_at: iso(r.updated_at),
  }));
  return json({ user, picks, record: await recordFor(ctx, user.id) });
}

export async function publicUser(ctx, { id }) {
  const user = await ctx.db.prepare('SELECT id, name FROM users WHERE id = ?').bind(id).first();
  if (!user) throw new HttpError(404, 'not_found', 'no such user');
  return json({ user, record: await recordFor(ctx, user.id) });
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
    db.prepare("DELETE FROM rate WHERE key IN ('c:' || ?, 'r:' || ?, 'n:' || ?)").bind(uid, uid, uid),
    db.prepare('DELETE FROM users WHERE id = ?').bind(uid),
  ]);
  return json({ ok: true }, 200, { 'Set-Cookie': clearSessionCookie() });
}

// ---------------------------------------------------------------- markets and picks

function str(v, max) {
  return typeof v === 'string' && v.length > 0 && v.length <= max;
}

function validateMarket(m, i) {
  const where = `markets[${i}]`;
  if (!m || typeof m !== 'object') throw bad('bad_market', `${where} must be an object`);
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
  return { ...m, locks, opens };
}

export async function ingestMarkets(ctx) {
  const body = await readJson(ctx.request, MAX_INGEST_BODY);
  const items = Array.isArray(body) ? body : body.markets;
  if (!Array.isArray(items) || items.length > 2000) throw bad('bad_body', 'expected an array of markets (max 2000)');
  const valid = items.map(validateMarket);
  const stmts = valid.map((m) =>
    ctx.db
      .prepare(
        `INSERT INTO markets (market_id, round_id, question, event_title, outcomes, opens_at, locks_at, url, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(market_id) DO UPDATE SET
           round_id = COALESCE(excluded.round_id, markets.round_id), question = excluded.question,
           event_title = excluded.event_title, outcomes = excluded.outcomes, opens_at = excluded.opens_at,
           locks_at = excluded.locks_at, url = excluded.url, updated_at = excluded.updated_at`,
      )
      .bind(m.market_id, m.round_id ?? null, m.question, m.event_title ?? null, JSON.stringify(m.outcomes),
        m.opens ?? null, m.locks, m.url ?? null, ctx.now),
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

export async function postPick(ctx) {
  const user = await ctx.requireUser();
  const body = await readJson(ctx.request);
  if (typeof body.market_id !== 'string') throw bad('bad_pick', 'market_id required');
  const m = await getMarketRow(ctx, body.market_id);
  const outcomes = JSON.parse(m.outcomes);
  if (!outcomes.includes(body.outcome)) throw bad('bad_outcome', 'outcome must be one of the market outcomes');
  if (isLocked(ctx, m)) throw new HttpError(409, 'locked', 'picks are locked for this market', { locks_at: iso(m.locks_at) });
  if (m.opens_at !== null && ctx.now < m.opens_at) {
    throw new HttpError(409, 'not_open', 'picks are not open yet', { opens_at: iso(m.opens_at) });
  }
  await ctx.db
    .prepare(
      `INSERT INTO picks (user_id, market_id, outcome, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
       ON CONFLICT(user_id, market_id) DO UPDATE SET outcome = excluded.outcome, updated_at = excluded.updated_at`,
    )
    .bind(user.id, m.market_id, body.outcome, ctx.now, ctx.now)
    .run();
  const p = await ctx.db.prepare('SELECT * FROM picks WHERE user_id = ? AND market_id = ?').bind(user.id, m.market_id).first();
  return json({
    pick: { market_id: p.market_id, outcome: p.outcome, created_at: iso(p.created_at), updated_at: iso(p.updated_at), locks_at: iso(m.locks_at) },
  });
}

export async function getMarket(ctx, { id }) {
  const m = await getMarketRow(ctx, id);
  const locked = isLocked(ctx, m);
  const user = await ctx.user();
  let myPick = null;
  if (user) {
    const p = await ctx.db.prepare('SELECT outcome FROM picks WHERE user_id = ? AND market_id = ?').bind(user.id, m.market_id).first();
    myPick = p ? p.outcome : null;
  }
  let counts = null;
  if (locked) {
    counts = {};
    for (const o of JSON.parse(m.outcomes)) counts[o] = 0;
    const rows = await ctx.db.prepare('SELECT outcome, COUNT(*) AS n FROM picks WHERE market_id = ? GROUP BY outcome').bind(m.market_id).all();
    for (const r of rows.results) counts[r.outcome] = r.n;
  }
  return json({ market: publicMarket(m), counts, my_pick: myPick, locked });
}

export async function ingestResults(ctx) {
  const body = await readJson(ctx.request, MAX_INGEST_BODY);
  const items = Array.isArray(body) ? body : body.results;
  if (!Array.isArray(items) || items.length > 2000) throw bad('bad_body', 'expected an array of results (max 2000)');
  const db = ctx.db;
  const unknown = [];
  let scored = 0;
  let voided = 0;
  for (const [i, r] of items.entries()) {
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
                 FROM picks WHERE market_id = ?`,
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
           FROM picks p JOIN markets m ON m.market_id = p.market_id WHERE m.locks_at >= ?`,
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
          WHERE m.locks_at >= ? AND p.correct IS NOT NULL
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
