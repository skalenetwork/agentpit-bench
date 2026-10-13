"""X analytics: the metrics puller and the query layer (docs/x-analytics-spec.md).

`pull_due` is the only function here that reads from X; it is the only writer of `tweet_metrics` and
`account_metrics`. Everything else is a pure query over the DB, and sibling code (A/B, ops, Match of the Day,
the digest) imports these functions instead of writing SQL against the tables. Every query joins `tweets`,
so metric rows of a post the bench deleted (a voided round) are invisible.

Numbers stay private: nothing here is exported to the site. The digest goes to the log and
var/export/x-daily.json, which site.py does not copy.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import time
from datetime import datetime, timedelta, timezone

from . import northstar
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

BUCKETS = {"1h": 3600, "24h": 86400, "72h": 3 * 86400, "7d": 7 * 86400}   # canonical snapshot ages
NON_PUBLIC_MAX_AGE = 29 * 86400     # X serves non_public_metrics for 30 days; ask only below 29
SERIES_MAX_AGE = 48 * 3600          # free intraday re-reads: posts younger than this
TIMELINE_MISSING_CYCLES = 2         # a due id absent from its timeline range this often gets one lookup
METRIC_COLS = ("impressions", "likes", "replies", "reposts", "quotes", "bookmarks", "engagements", "link_clicks",
               "profile_clicks", "video_views", "p0", "p25", "p50", "p75", "p100", "organic_impressions")
REWARD_KEYS = ("impressions", "likes", "replies", "reposts", "quotes", "bookmarks", "engagements", "link_clicks",
               "profile_clicks", "video_views", "age_s", "tweet_id", "post_key", "kind", "round_id", "posted_at")
_DEFAULTS = Settings()
_unknown_logged: set[str] = set()   # timeline posts with no tweets row: logged once per id per process


def _s(s: Settings | None) -> Settings:
    return s if s is not None else _DEFAULTS


def utc_day(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime("%Y-%m-%d")


def is_video(image_path: str | None) -> bool:
    return any(p.strip().lower().endswith(".mp4") for p in (image_path or "").split(","))


def _real(tweet_id: str | None) -> bool:
    """A numeric X id: outbox (`dry-...`) ids are never read."""
    return bool(tweet_id) and str(tweet_id).isdigit()


# ---------------------------------------------------------------- due rule

def due_bucket(age_s: float, video: bool, taken: set[str]) -> str | None:
    """The bucket to take now for a post of this age, or None. A bucket is due once its age is reached and it
    has no row. 1h / 24h / 72h are never backfilled for a post already older than 2x that age (nothing could use
    such a row: the freshness rule rejects it); 7d always backfills. When two are due, the oldest bucket wins."""
    due = None
    for b, a in BUCKETS.items():
        if b == "72h" and not video:
            continue
        if b in taken or age_s < a:
            continue
        if b != "7d" and age_s >= 2 * a:
            continue
        due = b
    return due


def due_posts(db: DB, now: float) -> dict[str, dict]:
    """{tweet_id: {bucket, posted_at, age_s}} for every own post with a canonical snapshot due now."""
    taken: dict[str, set[str]] = {}
    for r in db.q("SELECT m.tweet_id, m.bucket FROM tweet_metrics m WHERE m.bucket IS NOT NULL"):
        taken.setdefault(r["tweet_id"], set()).add(r["bucket"])
    out = {}
    for r in db.q("SELECT tweet_id, image_path, posted_at FROM tweets WHERE tweet_id IS NOT NULL"
                  " AND posted_at IS NOT NULL AND deleted=0 AND posted_at<=?", now):
        if not _real(r["tweet_id"]):
            continue
        age = now - r["posted_at"]
        b = due_bucket(age, is_video(r["image_path"]), taken.get(r["tweet_id"], set()))
        if b:
            out[r["tweet_id"]] = {"bucket": b, "posted_at": r["posted_at"], "age_s": age}
    return out


def _billed_posts(db: DB, day: str) -> set[str]:
    return {r["id"] for r in db.q("SELECT id FROM x_reads WHERE day=? AND resource IN ('post','owned')", day)}


def series_posts(db: DB, now: float, exclude=()) -> dict[str, dict]:
    """Own posts already billed today and < 48 h old: re-read for free (X deduplicates per UTC day).
    Reply and summon posts that `replies` / `like_counts` logged are not in `tweets`, so never included."""
    billed = _billed_posts(db, utc_day(now))
    out = {}
    for r in db.q("SELECT tweet_id, posted_at FROM tweets WHERE tweet_id IS NOT NULL AND deleted=0"
                  " AND posted_at>?", now - SERIES_MAX_AGE):
        if r["tweet_id"] in billed and r["tweet_id"] not in exclude and _real(r["tweet_id"]):
            out[r["tweet_id"]] = {"bucket": None, "posted_at": r["posted_at"], "age_s": now - r["posted_at"]}
    return out


# ---------------------------------------------------------------- parsing and storage

def parse(tw: dict, media: dict[str, dict]) -> dict:
    """One v2 post object (+ its expanded media) -> tweet_metrics columns."""
    pm, npm = tw.get("public_metrics") or {}, tw.get("non_public_metrics") or {}
    om = tw.get("organic_metrics") or {}
    row = {
        "impressions": pm.get("impression_count", npm.get("impression_count")),
        "likes": pm.get("like_count"), "replies": pm.get("reply_count"), "reposts": pm.get("retweet_count"),
        "quotes": pm.get("quote_count"), "bookmarks": pm.get("bookmark_count"),
        "engagements": npm.get("engagements"), "link_clicks": npm.get("url_link_clicks"),
        "profile_clicks": npm.get("user_profile_clicks"),
        "organic_impressions": om.get("impression_count"),
    }
    for c in ("video_views", "p0", "p25", "p50", "p75", "p100"):
        row[c] = None
    for key in (tw.get("attachments") or {}).get("media_keys", []):
        m = media.get(key) or {}
        mp, mn = m.get("public_metrics") or {}, m.get("non_public_metrics") or {}
        if "view_count" in mp:
            row["video_views"] = (row["video_views"] or 0) + int(mp["view_count"])
        for q in (0, 25, 50, 75, 100):
            v = mn.get(f"playback_{q}_count")
            if v is not None:
                row[f"p{q}"] = (row[f"p{q}"] or 0) + int(v)
    return row


def _store(db: DB, tweet_id: str, posted_at: float, now: float, bucket: str | None, row: dict, raw: dict) -> bool:
    cols = ("tweet_id", "taken_at", "age_s", "bucket", *METRIC_COLS, "raw_json")
    vals = (tweet_id, now, now - posted_at, bucket, *(row.get(c) for c in METRIC_COLS), json.dumps(raw))
    db.x(f"INSERT OR IGNORE INTO tweet_metrics({','.join(cols)}) VALUES({','.join('?' * len(cols))})", *vals)
    stored = db.one("SELECT 1 FROM tweet_metrics WHERE tweet_id=? AND taken_at=?", tweet_id, now) is not None
    if stored:
        db.x("UPDATE tweets SET metrics_json=? WHERE tweet_id=?",
             json.dumps({k: v for k, v in row.items() if v is not None} | {"taken_at": now}), tweet_id)
    return stored


def _ingest(db: DB, j: dict, targets: dict[str, dict], posts: dict[str, float], now: float, res: dict) -> set[str]:
    """Store every returned post that has a tweets row; flag Not Found ids deleted. Returns the ids seen."""
    media = {m.get("media_key"): m for m in (j.get("includes") or {}).get("media", [])}
    seen = set()
    for tw in j.get("data") or []:
        tid = str(tw.get("id"))
        seen.add(tid)
        if tid not in posts:
            if tid not in _unknown_logged:
                _unknown_logged.add(tid)
                log.info("x-metrics: post %s has no tweets row (posted by hand?); skipped", tid)
            continue
        bucket = (targets.get(tid) or {}).get("bucket")
        if _store(db, tid, posts[tid], now, bucket, parse(tw, media), tw):
            res["stored"] += 1
    for e in j.get("errors") or []:
        if "Not Found" in (e.get("title") or ""):
            tid = str(e.get("resource_id") or e.get("value") or "")
            if tid and tid in posts:
                db.x("UPDATE tweets SET deleted=1 WHERE tweet_id=?", tid)
                res["deleted"] += 1
                seen.add(tid)
                log.warning("x-metrics: post %s is gone from X (Not Found); never read again", tid)
    return seen


# ---------------------------------------------------------------- the puller

def _spend(db: DB, s: Settings, day: str, callers: tuple[str, ...] | None = None) -> float:
    sql = "SELECT resource, COUNT(*) n FROM x_reads WHERE day=?"
    args: list = [day]
    if callers:
        sql += f" AND caller IN ({','.join('?' * len(callers))})"
        args += callers
    return sum(r["n"] * s.x_read_prices.get(r["resource"], 0) for r in db.q(sql + " GROUP BY resource", *args))


def _metrics_rows(db: DB, day: str) -> int:
    return db.one("SELECT COUNT(*) n FROM x_reads WHERE day=? AND caller IN ('metrics','account')", day)["n"]


def pull_due(s: Settings, db: DB, poster, now: float | None = None, force_budget: bool = False) -> dict:
    """One cycle: the daily account read, due canonical snapshots, free series re-reads. Respects the daily
    metrics budget (unless force_budget), a 429's reset time and the read prerequisites. Never posts."""
    from .twitter import PermanentError, RateLimited, TransientError
    now = time.time() if now is None else now
    res = {"requested": 0, "stored": 0, "billed_new": 0, "skipped_budget": 0, "deleted": 0}
    if poster is None or not poster.can_read:
        return res
    resume = db.get("x_read_resume_at")
    if resume and now < float(resume):
        log.info("x-metrics: rate-limited until %s; skipping", resume)
        return res
    day = utc_day(now)
    rows_before = _metrics_rows(db, day)
    try:
        _account(s, db, poster, now, day, force_budget)
        _posts(s, db, poster, now, day, force_budget, res)
    except RateLimited as e:
        db.put("x_read_resume_at", e.reset if e.reset > now else now + 900)
        log.warning("x-metrics: 429 from X; reads resume at %s", db.get("x_read_resume_at"))
        return _finish(s, db, day, rows_before, res)
    except TransientError as e:
        log.warning("x-metrics: X unavailable (%s); retrying next cycle", e)
        return _finish(s, db, day, rows_before, res)
    except PermanentError as e:
        if getattr(e, "status", None) in (401, 403):
            n = int(db.get("x_read_auth_failures", 0)) + 1
            db.put("x_read_auth_failures", n)
            log.error("x-metrics: X refused the read (%s), %d in a row; the token may lack tweet.read/users.read",
                      e, n)
            return _finish(s, db, day, rows_before, res)
        raise
    db.put("x_read_auth_failures", 0)
    db.put("x_metrics_last_ok", now)
    return _finish(s, db, day, rows_before, res)


def _finish(s: Settings, db: DB, day: str, rows_before: int, res: dict) -> dict:
    res["billed_new"] = _metrics_rows(db, day) - rows_before
    total = _spend(db, s, day)
    if total > s.x_read_alert_usd and not db.get(f"x_read_alert_{day}"):
        db.put(f"x_read_alert_{day}", round(total, 3))
        log.warning("X reads today cost $%.2f, above x_read_alert_usd $%.2f (see `agentpitbench x-check`)",
                    total, s.x_read_alert_usd)
    return res


def _account(s: Settings, db: DB, poster, now: float, day: str, force: bool) -> None:
    """Followers etc., once per UTC day (the first cycle after 00:00 UTC). Also refreshes kv x_user_id."""
    if db.get("x_account_day") == day:
        return
    uid = db.get("x_user_id")
    billed = uid and db.one("SELECT 1 FROM x_reads WHERE day=? AND resource='user' AND id=?", day, str(uid))
    price = 0 if billed else s.x_read_prices.get("user", 0)
    if not force and price > s.x_metrics_budget_usd - _spend(db, s, day, ("metrics", "account")) + 1e-9:
        _budget_skip(db, day, ["account"])
        return
    me = poster.account_metrics(now=now)
    pm = me.get("public_metrics") or {}
    db.x("INSERT OR REPLACE INTO account_metrics(day,followers,following,tweets,listed,taken_at) VALUES(?,?,?,?,?,?)",
         day, pm.get("followers_count"), pm.get("following_count"), pm.get("tweet_count"), pm.get("listed_count"), now)
    if me.get("id"):
        db.put("x_user_id", me["id"])
    db.put("x_account_day", day)


def _budget_skip(db: DB, day: str, keys: list[str]) -> None:
    k = f"x_metrics_skipped_{day}"
    db.put(k, sorted(set(db.get(k, [])) | set(keys)))
    if not db.get(f"x_metrics_budget_warned_{day}"):
        db.put(f"x_metrics_budget_warned_{day}", True)
        log.warning("x-metrics: daily budget reached; %d snapshot(s) skipped, catching up tomorrow", len(keys))


def _order(due: dict[str, dict]) -> list[str]:
    """Reward buckets first (1h, then 24h, ...), oldest post first within a bucket."""
    return sorted(due, key=lambda t: (BUCKETS[due[t]["bucket"]], due[t]["posted_at"]))


def _posts(s: Settings, db: DB, poster, now: float, day: str, force: bool, res: dict) -> None:
    due = due_posts(db, now)
    series = series_posts(db, now, exclude=due) if s.x_series_free_rereads else {}
    billed = _billed_posts(db, day)
    posts = {r["tweet_id"]: r["posted_at"] for r in db.q(
        "SELECT tweet_id, posted_at FROM tweets WHERE tweet_id IS NOT NULL AND posted_at IS NOT NULL AND deleted=0")}
    remaining = s.x_metrics_budget_usd - _spend(db, s, day, ("metrics", "account"))
    if s.x_app_owned:
        _timeline(s, db, poster, now, day, force, res, due, series, billed, posts, remaining)
    else:
        _lookup_mode(s, db, poster, now, day, force, res, due, series, billed, posts, remaining)


def _fit(ids: list[str], price: float, remaining: float) -> int:
    if price <= 0:
        return len(ids)
    return max(0, min(len(ids), math.floor((remaining + 1e-9) / price)))


def _lookup(db: DB, poster, ids: list[str], targets: dict, posts: dict, now: float, res: dict) -> set[str]:
    """GET /2/tweets?ids= in two groups: < 29 days with non-public fields, older with public fields only."""
    young = [i for i in ids if now - posts[i] < NON_PUBLIC_MAX_AGE]
    old = [i for i in ids if now - posts[i] >= NON_PUBLIC_MAX_AGE]
    seen: set[str] = set()
    for group, non_public in ((young, True), (old, False)):
        for k in range(0, len(group), 100):
            batch = group[k:k + 100]
            res["requested"] += len(batch)
            seen |= _ingest(db, poster.tweet_metrics(batch, non_public, now=now), targets, posts, now, res)
    return seen


def _lookup_mode(s, db, poster, now, day, force, res, due, series, billed, posts, remaining) -> None:
    order = _order(due)
    new = [t for t in order if t not in billed]
    free = [t for t in order if t in billed]
    n = len(new) if force else _fit(new, s.x_read_prices.get("post", 0), remaining)
    if n < len(new):
        res["skipped_budget"] += len(new) - n
        _budget_skip(db, day, [f"{t}:{due[t]['bucket']}" for t in new[n:]])
    ids = free + new[:n] + list(series)
    targets = {**series, **{t: due[t] for t in free + new[:n]}}
    if ids:
        _lookup(db, poster, ids, targets, posts, now, res)


def _timeline(s, db, poster, now, day, force, res, due, series, billed, posts, remaining) -> None:
    """GET /2/users/{me}/tweets over the time range of the due (and series) posts: every post in the range comes
    back, due or not, so undue ones become series rows. Priced as owned reads. The budget is checked against the
    own posts in the range not yet billed today."""
    price = s.x_read_prices.get("owned", 0)
    take = _order(due)

    def window(ids):
        ts = [posts[t] for t in ids if t in posts]
        return (min(ts) - 60, max(ts) + 60) if ts else None

    def cost(w):
        if not w:
            return 0.0
        n = sum(1 for t, p in posts.items() if w[0] <= p <= w[1] and t not in billed and _real(t))
        return n * price

    while take and not force and cost(window(take + list(series))) > remaining + 1e-9:
        take.pop()   # drop the lowest-priority due post until the range fits
    skipped = [t for t in _order(due) if t not in take]
    if skipped:
        res["skipped_budget"] += len(skipped)
        _budget_skip(db, day, [f"{t}:{due[t]['bucket']}" for t in skipped])
    targets = {**series, **{t: due[t] for t in take}}
    w = window(list(targets))
    if not w:
        return
    if not force and cost(w) > remaining + 1e-9:
        return   # even the free series range would cost: skip
    cut = now - NON_PUBLIC_MAX_AGE
    seen: set[str] = set()
    for start, end, non_public in ((w[0], min(w[1], cut), False), (max(w[0], cut), w[1], True)):
        if start < end:
            res["requested"] += sum(1 for t in targets if start <= posts.get(t, -1) <= end)
            seen |= _ingest(db, poster.timeline_metrics(start, end, non_public, now=now), targets, posts, now, res)
    missing = db.get("x_timeline_missing", {})
    recheck = []
    for t in take:
        if t in seen:
            missing.pop(t, None)
            continue
        missing[t] = missing.get(t, 0) + 1
        if missing[t] >= TIMELINE_MISSING_CYCLES:
            recheck.append(t)
            missing.pop(t)
    db.put("x_timeline_missing", missing)
    if recheck:   # timeline can't see Not Found: one lookup request settles it
        _lookup(db, poster, recheck, targets, posts, now, res)


def boost_check(db: DB, poster, tweet_id: str, now: float | None = None) -> dict:
    """x-boosted --check: one adhoc read with organic + promoted metrics; stores organic_impressions in a series
    row. An empty promoted block means the post was never promoted (X's documented known issue)."""
    now = time.time() if now is None else now
    t = db.one("SELECT posted_at FROM tweets WHERE tweet_id=?", tweet_id)
    if not t:
        raise SystemExit(f"{tweet_id} is not one of the bench's posts")
    j = poster.boost_check(tweet_id)
    tw = next((d for d in j.get("data") or [] if str(d.get("id")) == str(tweet_id)), None)
    if not tw:
        return {"found": False, "errors": j.get("errors")}
    _store(db, tweet_id, t["posted_at"], now, None, parse(tw, {}), tw)
    return {"found": True, "organic_impressions": (tw.get("organic_metrics") or {}).get("impression_count"),
            "promoted": tw.get("promoted_metrics") or None}


def set_boosted(db: DB, tweet_id: str, on: bool = True) -> bool:
    db.x("UPDATE tweets SET boosted=? WHERE tweet_id=?", int(on), tweet_id)
    return db.one("SELECT 1 FROM tweets WHERE tweet_id=?", tweet_id) is not None


# ---------------------------------------------------------------- post-level queries

_POST = ("SELECT m.*, t.post_key, t.kind, t.round_id, t.posted_at, t.boosted, t.deleted FROM tweet_metrics m"
         " JOIN tweets t ON t.tweet_id=m.tweet_id")


def snapshot(db: DB, tweet_id: str, bucket: str) -> dict | None:
    r = db.one(_POST + " WHERE m.tweet_id=? AND m.bucket=?", tweet_id, bucket)
    return dict(r) if r else None


def latest(db: DB, tweet_id: str) -> dict | None:
    r = db.one(_POST + " WHERE m.tweet_id=? ORDER BY m.taken_at DESC LIMIT 1", tweet_id)
    return dict(r) if r else None


def _fresh(row, bucket: str, s: Settings | None) -> bool:
    return row["age_s"] <= _s(s).x_reward_max_age_ratio * BUCKETS[bucket]


def _eligible(row) -> bool:
    return not row["boosted"] and not row["deleted"] and _real(row["tweet_id"])


def reward_inputs(db: DB, tweet_id: str, bucket: str, s: Settings | None = None) -> dict | None:
    """The raw counts A/B blends, or None: not taken yet, boosted, deleted, outbox, or stale (freshness rule)."""
    r = db.one(_POST + " WHERE m.tweet_id=? AND m.bucket=?", tweet_id, bucket)
    if not r or not _eligible(r) or not _fresh(r, bucket, s):
        return None
    return {k: r[k] for k in REWARD_KEYS}


def rewards(db: DB, bucket: str, *, kinds=None, since: float | None = None, until: float | None = None,
            s: Settings | None = None) -> list[dict]:
    """reward_inputs for every eligible, fresh post with this bucket taken, plus headline_variant, exhibition
    and exclusion from its round (None for posts outside a round)."""
    sql = ("SELECT m.*, t.post_key, t.kind, t.round_id, t.posted_at, t.boosted, t.deleted, ro.headline_variant,"
           " ro.exhibition, ro.exclusion FROM tweet_metrics m JOIN tweets t ON t.tweet_id=m.tweet_id"
           " LEFT JOIN rounds ro ON ro.round_id=t.round_id WHERE m.bucket=? AND t.posted_at>=? AND t.posted_at<?")
    args: list = [bucket, since if since is not None else float("-inf"), until if until is not None else float("inf")]
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        sql += f" AND t.kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    out = []
    for r in db.q(sql + " ORDER BY t.posted_at", *args):
        if _eligible(r) and _fresh(r, bucket, s):
            out.append({**{k: r[k] for k in REWARD_KEYS}, "headline_variant": r["headline_variant"],
                        "exhibition": r["exhibition"], "exclusion": r["exclusion"]})
    return out


def interactions(r: dict) -> int:
    return sum(r.get(k) or 0 for k in ("likes", "replies", "reposts", "quotes"))


def reward_by_variant(db: DB, bucket: str, *, kind: str = "results", since: float | None = None,
                      s: Settings | None = None) -> dict[str, dict]:
    """The existing headline-tone arm (rounds.headline_variant: fun / plain)."""
    groups: dict[str, list[dict]] = {}
    for r in rewards(db, bucket, kinds=[kind], since=since, s=s):
        if r["headline_variant"]:
            groups.setdefault(r["headline_variant"], []).append(r)
    out = {}
    for v, rows in sorted(groups.items()):
        imps = [r["impressions"] for r in rows if r["impressions"] is not None]
        out[v] = {"n": len(rows), "impressions_median": statistics.median(imps) if imps else None,
                  "interactions_sum": sum(interactions(r) for r in rows),
                  "link_clicks_sum": sum(r["link_clicks"] or 0 for r in rows),
                  "impressions_sum": sum(imps)}
    return out


def impressions_series(db: DB, tweet_id: str) -> list[dict]:
    rows = db.q("SELECT m.taken_at, m.age_s, m.impressions, m.engagements, m.link_clicks, m.bucket FROM tweet_metrics m"
                " JOIN tweets t ON t.tweet_id=m.tweet_id WHERE m.tweet_id=? ORDER BY m.age_s", tweet_id)
    return [dict(r) for r in rows]


def video_retention(db: DB, tweet_id: str) -> dict | None:
    """p25..p100 as fractions of playback starts (p0), from the latest row with playback counts (< 29 days)."""
    r = db.one(_POST + " WHERE m.tweet_id=? AND m.p0 IS NOT NULL ORDER BY m.taken_at DESC LIMIT 1", tweet_id)
    if not r:
        return None
    p0 = r["p0"]
    frac = {f"p{q}": (round(r[f"p{q}"] / p0, 4) if p0 and r[f"p{q}"] is not None else None)
            for q in (25, 50, 75, 100)}
    return {"plays": p0, **frac, "video_views": r["video_views"], "age_s": r["age_s"], "taken_at": r["taken_at"]}


# ---------------------------------------------------------------- aggregates

def _agg(rows: list) -> dict:
    """n, median / mean impressions, interaction rate and link CTR (pooled; rows with 0 / NULL impressions are
    skipped in rates; CTR only over rows with non-public counts)."""
    imps = [r["impressions"] for r in rows if r["impressions"] is not None]
    rated = [r for r in rows if r["impressions"]]
    ctr = [r for r in rated if r["link_clicks"] is not None]
    return {
        "n": len(rows),
        "impressions_median": statistics.median(imps) if imps else None,
        "impressions_mean": round(statistics.fmean(imps), 1) if imps else None,
        "interaction_rate": (round(sum(interactions(dict(r)) for r in rated) / sum(r["impressions"] for r in rated), 5)
                             if rated else None),
        "link_ctr": (round(sum(r["link_clicks"] for r in ctr) / sum(r["impressions"] for r in ctr), 5)
                     if ctr else None),
    }


def _bucket_rows(db: DB, bucket: str, since: float | None, until: float | None = None, rounds_only=False) -> list:
    """Canonical rows of one bucket for own, non-boosted posts posted in [since, until)."""
    sql = (_POST.replace("FROM", ", ro.question, ro.state, ro.winner FROM") + " LEFT JOIN rounds ro ON ro.round_id=t.round_id"
           " WHERE m.bucket=? AND t.boosted=0 AND t.posted_at>=? AND t.posted_at<?")
    if rounds_only:
        sql += " AND t.round_id IS NOT NULL"
    return db.q(sql + " ORDER BY t.posted_at", bucket, since if since is not None else float("-inf"),
                until if until is not None else float("inf"))


def by_kind(db: DB, since: float | None, bucket: str = "24h", until: float | None = None) -> list[dict]:
    groups: dict[str, list] = {}
    for r in _bucket_rows(db, bucket, since, until):
        groups.setdefault(r["kind"] or "?", []).append(r)
    return sorted(({"kind": k, **_agg(v)} for k, v in groups.items()), key=lambda d: -d["n"])


def round_winners(db: DB, round_id: int) -> list[str]:
    """Winning agents: runs x bets of the round with bets.outcome = rounds.winner (the leaderboard rule)."""
    return [r["agent"] for r in db.q(
        "SELECT r.agent FROM runs r JOIN bets b USING(run_id) JOIN rounds ro USING(round_id)"
        " WHERE r.round_id=? AND ro.state='resolved' AND b.outcome IS NOT NULL AND b.outcome=ro.winner"
        " ORDER BY r.agent", round_id)]


def round_split(db: DB, round_id: int) -> bool:
    """split_headline's rule: more than one distinct outcome among the agents that bet."""
    return db.one("SELECT COUNT(DISTINCT b.outcome) n FROM runs r JOIN bets b USING(run_id)"
                  " WHERE r.round_id=? AND b.outcome IS NOT NULL", round_id)["n"] > 1


def by_agent(db: DB, since: float | None, bucket: str = "24h", until: float | None = None) -> list[dict]:
    """Round posts grouped two ways: by winning agent (a round with two winners counts for both; `none` when
    nobody won or it hasn't resolved) and by split vs agree. Which stories travel, not the methodology."""
    by_winner: dict[str, list] = {}
    by_split: dict[str, list] = {}
    cache: dict[int, tuple[list[str], bool]] = {}
    for r in _bucket_rows(db, bucket, since, until, rounds_only=True):
        rid = r["round_id"]
        if rid not in cache:
            cache[rid] = (round_winners(db, rid), round_split(db, rid))
        winners, split = cache[rid]
        for a in winners or ["none"]:
            by_winner.setdefault(a, []).append(r)
        by_split.setdefault("split" if split else "agree", []).append(r)
    out = [{"group": "winner", "key": k, **_agg(v)} for k, v in sorted(by_winner.items())]
    out += [{"group": "split", "key": k, **_agg(v)} for k, v in sorted(by_split.items())]
    return out


def by_category(db: DB, since: float | None, bucket: str = "24h", until: float | None = None) -> list[dict]:
    """Round posts by virality.category(rounds.question): the watcher's category classifier."""
    from .virality import category
    groups: dict[str, list] = {}
    for r in _bucket_rows(db, bucket, since, until, rounds_only=True):
        groups.setdefault(category(r["question"] or ""), []).append(r)
    return sorted(({"category": k, **_agg(v)} for k, v in groups.items()), key=lambda d: -d["n"])


def _kind_imps(db: DB, kind: str, bucket: str, days: float, until: float | None) -> list[int]:
    until = time.time() if until is None else until
    return [r["impressions"] for r in _bucket_rows(db, bucket, until - days * 86400, until)
            if r["kind"] == kind and r["impressions"] is not None]


def kind_baseline(db: DB, kind: str, bucket: str = "1h", days: int = 14, until: float | None = None) -> float | None:
    """Median impressions for a kind over the trailing window ending at `until` (default now)."""
    imps = _kind_imps(db, kind, bucket, days, until)
    return statistics.median(imps) if imps else None


DROP_POSTS, DROP_FRACTION, DROP_MIN_BASELINE = 6, 0.2, 10


def visibility_drop(db: DB, now: float | None = None) -> dict | None:
    """Fires when the last 6 posts of one kind all have 1 h impressions below 20% of that kind's baseline
    (median over the 14 days before those posts, needing >= 10 posts). An alert only: posting continues."""
    now = time.time() if now is None else now
    kinds = [r["kind"] for r in db.q("SELECT DISTINCT kind FROM tweets WHERE kind IS NOT NULL ORDER BY kind")]
    for kind in kinds:
        last = [r for r in _bucket_rows(db, "1h", None, now) if r["kind"] == kind and r["impressions"] is not None]
        last = last[-DROP_POSTS:]
        if len(last) < DROP_POSTS:
            continue
        base_imps = _kind_imps(db, kind, "1h", 14, last[0]["posted_at"])
        if len(base_imps) < DROP_MIN_BASELINE:
            continue
        baseline = statistics.median(base_imps)
        if baseline and all(r["impressions"] < DROP_FRACTION * baseline for r in last):
            return {"kind": kind, "posts": [r["tweet_id"] for r in last], "baseline": baseline}
    return None


def followers_series(db: DB, since: float | None = None) -> list[dict]:
    day = utc_day(since) if since is not None else ""
    return [dict(r) for r in db.q("SELECT day, followers FROM account_metrics WHERE day>=? ORDER BY day", day)]


# ---------------------------------------------------------------- bench-specific

def reply_cap_hit(db: DB, round_id: int, max_reply_reads: int | None = None) -> bool | None:
    """True when the opener's latest reply_count is above max_reply_reads ("first N picks counted"); None
    without a snapshot. reply_count includes every reply (ours and bots' too), so it's an upper bound."""
    cap = _DEFAULTS.max_reply_reads if max_reply_reads is None else max_reply_reads
    r = db.round(round_id)
    if not r or not r["thread_tweet_id"]:
        return None
    row = latest(db, r["thread_tweet_id"])
    if not row or row["replies"] is None:
        return None
    return row["replies"] > cap


# ---------------------------------------------------------------- ops and accounting

def reads(db: DB, day: str | None = None, s: Settings | None = None) -> dict:
    """Billed reads and $ for a UTC day (default today), by resource and by caller, from x_reads x x_read_prices.
    metrics_usd is what counts against x_metrics_budget_usd (callers metrics + account)."""
    s = _s(s)
    day = day or utc_day()
    by_res: dict[str, dict] = {}
    by_caller: dict[str, dict] = {}
    for r in db.q("SELECT resource, caller, COUNT(*) n FROM x_reads WHERE day=? GROUP BY resource, caller", day):
        usd = r["n"] * s.x_read_prices.get(r["resource"], 0)
        for d, k in ((by_res, r["resource"]), (by_caller, r["caller"])):
            e = d.setdefault(k, {"n": 0, "usd": 0.0})
            e["n"] += r["n"]
            e["usd"] = round(e["usd"] + usd, 4)
    return {"day": day, "n": sum(v["n"] for v in by_res.values()),
            "usd": round(sum(v["usd"] for v in by_res.values()), 4),
            "metrics_usd": round(sum(v["usd"] for k, v in by_caller.items() if k in ("metrics", "account")), 4),
            "by_resource": by_res, "by_caller": dict(sorted(by_caller.items()))}


def health(db: DB, now: float | None = None) -> dict:
    day = utc_day(now)
    resume = db.get("x_read_resume_at")
    return {"last_ok": db.get("x_metrics_last_ok"),
            "consecutive_auth_failures": int(db.get("x_read_auth_failures", 0)),
            "rate_limited_until": resume if resume and float(resume) > (time.time() if now is None else now) else None,
            "skipped_for_budget_today": len(db.get(f"x_metrics_skipped_{day}", []))}


# ---------------------------------------------------------------- daily digest (operator only, private)

DIGEST_HOUR_UTC = 8


def digest(s: Settings, db: DB, now: float | None = None) -> dict:
    """Yesterday (UTC) for the operator: posts, impressions, interactions, link clicks, followers delta, top 3,
    reads and $ by caller, snapshots skipped for budget, and the visibility-drop check. Written to the log and
    var/export/x-daily.json (site.py copies an explicit file list, so this never reaches the site)."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    y0, y1 = (today - timedelta(days=1)).timestamp(), today.timestamp()
    yday = utc_day(y0)
    posts = []
    for t in db.q("SELECT tweet_id, post_key, kind FROM tweets WHERE posted_at>=? AND posted_at<? AND tweet_id IS NOT NULL",
                  y0, y1):
        if not _real(t["tweet_id"]):
            continue
        last = latest(db, t["tweet_id"]) or {}
        posts.append({"tweet_id": t["tweet_id"], "post_key": t["post_key"], "kind": t["kind"],
                      "impressions": last.get("impressions"), "interactions": interactions(last) if last else None,
                      "link_clicks": last.get("link_clicks"), "age_s": last.get("age_s")})
    fol = {r["day"]: r["followers"] for r in db.q("SELECT day, followers FROM account_metrics WHERE day IN (?,?)",
                                                  yday, utc_day(now))}
    f_now, f_then = fol.get(utc_day(now)), fol.get(yday)
    spend = reads(db, yday, s)
    out = {
        "day": yday, "generated_at": now,
        "posts": len(posts),
        "impressions": sum(p["impressions"] or 0 for p in posts),
        "interactions": sum(p["interactions"] or 0 for p in posts),
        "link_clicks": sum(p["link_clicks"] or 0 for p in posts),
        "followers": f_now, "followers_delta": (f_now - f_then) if f_now is not None and f_then is not None else None,
        "top": sorted(posts, key=lambda p: -(p["impressions"] or 0))[:3],
        "reads": spend, "read_alert": spend["usd"] > s.x_read_alert_usd,
        "skipped_for_budget": len(db.get(f"x_metrics_skipped_{yday}", [])),
        "visibility_drop": visibility_drop(db, now), "health": health(db, now),
        # docs/ai-automation-spec.md §9-§11: the latest cadence decision, open proposals, last ops-agent run
        "cadence": db.get("cadence_last"), "selection_review": db.get("selection_review_last"),
        "ops_agent": db.get("ops_last"),
    }
    try:   # the month-1 North Star (docs/marketing-strategy.md): running totals, private
        out["northstar"] = northstar.status(db, now)
    except Exception:
        log.exception("North Star digest block failed")
    if s.youtube_enabled:   # docs/youtube-shorts-spec.md §8: the YouTube block
        try:
            from . import ytstats
            out["youtube"] = ytstats.digest_block(s, db, now)
        except Exception:
            log.exception("YouTube digest block failed")
    if s.tiktok_enabled:    # docs/tiktok-spec.md §6: the TikTok block
        try:
            from . import ttstats
            out["tiktok"] = ttstats.digest_block(s, db, now)
        except Exception:
            log.exception("TikTok digest block failed")
    s.export_dir.mkdir(parents=True, exist_ok=True)
    (s.export_dir / "x-daily.json").write_text(json.dumps(out, indent=1))
    log.info("x-daily %s: %d posts, %d impressions, %d interactions, %d link clicks, followers %s (%s), "
             "reads $%.3f%s, %d skipped for budget", yday, out["posts"], out["impressions"], out["interactions"],
             out["link_clicks"], f_now, out["followers_delta"], spend["usd"],
             " ABOVE ALERT" if out["read_alert"] else "", out["skipped_for_budget"])
    if out.get("northstar"):
        log.info("x-daily: %s", northstar.line(out["northstar"]))
    if out["visibility_drop"]:
        v = out["visibility_drop"]
        log.warning("x-daily: visibility drop on %s: last %d posts all under 20%% of the %s-impression baseline",
                    v["kind"], len(v["posts"]), v["baseline"])
    return out


def digest_due(s: Settings, db: DB, now: float | None = None) -> dict | None:
    """Once a day, on the first call at or after 08:00 UTC."""
    now = time.time() if now is None else now
    day = utc_day(now)
    if datetime.fromtimestamp(now, timezone.utc).hour < DIGEST_HOUR_UTC or db.get(f"x_digest_{day}"):
        return None
    out = digest(s, db, now)
    db.put(f"x_digest_{day}", True)
    return out
