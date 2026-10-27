"""X engagement (operator 2026-10-27, docs/posts-v3-spec.md "Engagement"): like human pick replies, shout out a human
who beat every AI, follow back humans with several picks.

Each feature is a knob (x_like_replies, x_shoutouts, x_follow_back; off by default) and idempotent: what was done is
recorded in engage_x_done, so a restart or a repeated tick never repeats it. Likes and follows need the OAuth scopes
like.write / follows.write (twitter.ENGAGE_SCOPES, requested by x-login, reported by x-check): a token without them
makes those two features no-op with ONE warning per process, never a crash or an alert. They are live-only (no outbox
for a like). Shout-outs are fixed templates (no AI-written text) posted as kind "shoutout", an X-only reply.

Endpoints: POST /2/users/:id/likes {tweet_id} and POST /2/users/:id/following {target_user_id}. X bills writes per
request on its pay-per-use plan (price not recorded here; the daily caps bound the spend).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .config import Settings
from .db import DB
from .twitter import SHOUTOUT, PermanentError, RateLimited, TransientError

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS engage_x_done (   -- one row per like (key: reply tweet id), follow (user id), shoutout (round id)
    kind TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL, ok INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (kind, key)
);
"""
MAX_AGE_DAYS = 7          # never back-fill: only replies / resolutions from the last week
LIKES_PER_TICK = 30
TEMPLATES = (
    "@{u} beat all four AIs on this one. 🧠 The machines are taking notes.",
    "All four AIs missed it. @{u} didn't. Respect. 🏆",
    "@{u} called it while Claude, ChatGPT, Gemini and Grok all swung and missed. 🎯",
    "Four AIs, one human, and @{u} was right. The robots owe you a drink. 🥃",
)
_warned: set[str] = set()


def ensure_schema(db: DB) -> None:
    db.conn.executescript(SCHEMA)


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def _today(db: DB, kind: str, now: float) -> int:
    start = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return db.one("SELECT COUNT(*) n FROM engage_x_done WHERE kind=? AND at>=?", kind, start)["n"]


def _done(db: DB, kind: str, key) -> bool:
    return bool(db.one("SELECT 1 FROM engage_x_done WHERE kind=? AND key=?", kind, str(key)))


def _mark(db: DB, kind: str, key, now: float, ok: bool = True) -> None:
    db.x("INSERT OR IGNORE INTO engage_x_done(kind,key,at,ok) VALUES(?,?,?,?)", kind, str(key), now, int(ok))


def _has_scope(poster, scope: str, feature: str) -> bool:
    try:
        scopes = poster.oauth2.scopes()
    except Exception:
        scopes = set()
    if scope in scopes:
        return True
    _warn_once(scope, "engage_x: X token lacks %s, so %s is off (re-run `agentpitbench x-login`, see x-check)",
               scope, feature)
    return False


def _opted_out(s: Settings, db: DB, uid: str, username: str | None) -> bool:
    from . import replyback
    replyback.ensure_schema(db)
    return bool(db.one("SELECT 1 FROM reply_optouts WHERE user_id=?", uid)) or replyback._excluded(s, uid, username)


async def _write(db: DB, call, kind: str, key: str, now: float, stop: set[str]) -> bool:
    """One like/follow. True when done. A 401/403 (scope or account state) turns the feature off for the tick with
    one warning; other permanent errors (deleted tweet, suspended user) are recorded as failed, not retried."""
    try:
        await asyncio.to_thread(call, key)
    except RateLimited:
        _warn_once(f"rl-{kind}", "engage_x: X rate-limited %s; resuming on a later tick", kind)
        stop.add(kind)
        return False
    except TransientError as e:
        log.warning("engage_x: X unavailable (%s)", e)
        stop.add(kind)
        return False
    except PermanentError as e:
        if e.status in (401, 403):
            _warn_once(f"deny-{kind}", "engage_x: X refused %s (%s); check the token scopes", kind, e)
            stop.add(kind)
            return False
        log.info("engage_x: %s %s failed permanently (%s); not retried", kind, key, e)
        _mark(db, kind, key, now, ok=False)
        return False
    _mark(db, kind, key, now)
    return True


async def like_replies(s: Settings, db: DB, poster, now: float) -> int:
    if not (s.x_like_replies and poster.live and _has_scope(poster, "like.write", "liking pick replies")):
        return 0
    room = min(LIKES_PER_TICK, s.x_like_daily_cap - _today(db, "like", now))
    if room <= 0:
        return 0
    n, stop = 0, set()
    rows = db.q("SELECT user_id, username, tweet_id FROM humans WHERE tweet_id IS NOT NULL"
                " AND tweet_id NOT LIKE 'dry-%' AND created_at>=? ORDER BY created_at", now - MAX_AGE_DAYS * 86400)
    for h in rows:
        if n >= room or "like" in stop:
            break
        if _done(db, "like", h["tweet_id"]) or _opted_out(s, db, str(h["user_id"]), h["username"]):
            continue
        n += await _write(db, poster.like, "like", str(h["tweet_id"]), now, stop)
    if n:
        log.info("engage_x: liked %d pick replies", n)
    return n


async def follow_back(s: Settings, db: DB, poster, now: float) -> int:
    if not (s.x_follow_back and poster.live and _has_scope(poster, "follows.write", "following back")):
        return 0
    room = s.x_follow_daily_cap - _today(db, "follow", now)
    if room <= 0:
        return 0
    n, stop = 0, set()
    me = str(await asyncio.to_thread(poster.my_user_id))
    rows = db.q("SELECT user_id, MAX(username) username, COUNT(DISTINCT round_id) picks FROM humans"
                " WHERE tweet_id IS NOT NULL AND tweet_id NOT LIKE 'dry-%' GROUP BY user_id"
                " HAVING picks>=? ORDER BY MAX(created_at)", s.x_follow_min_picks)
    for h in rows:
        uid = str(h["user_id"])
        if n >= room or "follow" in stop:
            break
        if uid == me or _done(db, "follow", uid) or _opted_out(s, db, uid, h["username"]):
            continue
        n += await _write(db, poster.follow, "follow", uid, now, stop)
    if n:
        log.info("engage_x: followed back %d humans", n)
    return n


def _beat_all(db: DB, s: Settings, round_id: int, winner: str) -> bool:
    """Every contestant bet and lost (a pass or timeout is not a loss: the human did not beat that AI)."""
    bets = {r["agent"]: r["outcome"] for r in db.q(
        "SELECT r.agent, b.outcome FROM runs r LEFT JOIN bets b USING(run_id) WHERE r.round_id=?", round_id)}
    return bool(s.agents) and all(bets.get(a) and bets[a] != winner for a in s.agents)


async def shout_out(s: Settings, db: DB, poster, now: float) -> int:
    """At most one reply per round, under the results post, to the earliest human who picked the winner."""
    if not s.x_shoutouts or SHOUTOUT not in (s.post_kinds or []):
        return 0
    from . import replyback
    if replyback.killed(db):
        return 0
    n = 0
    for r in db.q("SELECT round_id, winner, results_tweet_id, resolved_at FROM rounds WHERE state='resolved'"
                  " AND COALESCE(exhibition,0)=0 AND winner IS NOT NULL AND results_tweet_id IS NOT NULL"
                  " AND resolved_at>=? ORDER BY round_id", now - MAX_AGE_DAYS * 86400):
        rid = r["round_id"]
        if _done(db, "shoutout", rid):
            continue
        if poster.live and str(r["results_tweet_id"]).startswith("dry-"):
            continue
        if not _beat_all(db, s, rid, r["winner"]):
            continue
        for h in db.q("SELECT user_id, username FROM humans WHERE round_id=? AND pick=? AND username IS NOT NULL"
                      " ORDER BY created_at, user_id", rid, r["winner"]):
            if _opted_out(s, db, str(h["user_id"]), h["username"]):
                continue
            text = TEMPLATES[rid % len(TEMPLATES)].format(u=h["username"])
            tid = await poster.post(f"shoutout-{rid}", SHOUTOUT, text, reply_to=r["results_tweet_id"], round_id=rid)
            if tid:
                _mark(db, "shoutout", rid, now)
                n += 1
            break   # one per round: the earliest eligible human
    return n


async def run(s: Settings, db: DB, poster, now: float | None = None) -> dict:
    """One tick (hourly from cli.scheduled). Each feature is isolated: one failing never blocks the others."""
    res = {"likes": 0, "shoutouts": 0, "follows": 0}
    if poster is None or not (s.x_like_replies or s.x_shoutouts or s.x_follow_back):
        return res
    ensure_schema(db)
    now = time.time() if now is None else now
    for key, fn in (("shoutouts", shout_out), ("likes", like_replies), ("follows", follow_back)):
        try:
            res[key] = await fn(s, db, poster, now)
        except Exception:
            log.exception("engage_x %s failed", key)
    return res
