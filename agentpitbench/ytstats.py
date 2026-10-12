"""YouTube Shorts metrics and the YouTube cycle (docs/youtube-shorts-spec.md §7-8). The query layer mirrors
`xstats`: pure functions over the DB; `pull_due` (and `hide_round`) are the only ones that call Google.
Sibling code imports these functions and writes no SQL against the yt_* tables."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from . import youtube
from .config import Settings

log = logging.getLogger(__name__)

BUCKETS = {"1h": 3600, "24h": 86400, "72h": 3 * 86400, "7d": 7 * 86400}   # data (videos.list), from published_at
A_BUCKETS = {"7d": 7 * 86400, "28d": 28 * 86400}                           # analytics reports
LOCKED_AFTER_S = 1800
RETENTION_POINTS = (0.25, 0.5, 0.75, 1.0)
A_METRICS = ("views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,"
             "comments,shares,subscribersGained")


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def due_bucket(age_s: float, taken: set[str], buckets: dict = BUCKETS) -> str | None:
    """Same rule as xstats.due_bucket: due once its age is reached and it has no row; 1h/24h/72h are never
    backfilled past 2x their age (stale); the oldest due bucket wins."""
    due = None
    for b, a in buckets.items():
        if b in taken or age_s < a:
            continue
        if b in ("1h", "24h", "72h") and age_s >= 2 * a:
            continue
        due = b
    return due


# ---------------------------------------------------------------- the cycle

def film_candidates(s: Settings, now: float) -> list[tuple[dict, object]]:
    """film.json for today's and yesterday's UTC date (17:00 -> midnight edge); never older (no backfill)."""
    from .motd import film_dir
    out = []
    for d in (0, 1):
        day = _utc_day(now - d * 86400)
        f = film_dir(s, day) / "film.json"
        if f.exists():
            try:
                out.append((json.loads(f.read_text()), f.parent))
            except (OSError, ValueError):
                log.warning("yt: unreadable %s", f)
    return out


def round_export(s: Settings, db, round_id: int) -> dict | None:
    from . import exports
    r = db.round(round_id)
    return exports.round_record(s, db, r) if r is not None else None


def pull_due(s: Settings, db, client, now: float | None = None) -> dict:
    """One YouTube cycle: uploads due, status polls, comment, data buckets, analytics buckets, channel row.
    Each step is isolated: a failure in one is logged and the rest still run."""
    now = time.time() if now is None else now
    out = {"uploaded": [], "polled": 0, "comments": 0, "buckets": 0, "analytics": 0, "channel": False}
    for rec, d in film_candidates(s, now):
        key = youtube.post_key(rec["date"])
        cur = youtube.row(db, key)
        if cur and cur["state"] != "uploading":
            continue
        try:
            r = youtube.upload(s, db, client, rec, d, round_export(s, db, rec["round_id"]))
            out["uploaded"].append({key: r["state"]})
        except youtube.ChannelMismatch:
            pass  # logged at ERROR by the guard; no row change, retried next cycle once the config is fixed
        except Exception as e:
            log.exception("yt %s: upload step failed", key)
            out["uploaded"].append({key: f"error: {e}"})
    if client is None or s.dry_run:
        return out
    for step, fn in (("poll", _poll), ("analytics", _analytics), ("channel", _channel)):
        try:
            fn(s, db, client, now, out)
        except youtube.QuotaSkip:
            pass
        except Exception:
            log.exception("yt %s step failed", step)
    return out


def _poll(s: Settings, db, client, now: float, out: dict) -> None:
    """videos.list for every video still worth reading: status until public and processed, then statistics
    for the data buckets (one call serves both). Sets published_at, posts the comment, records buckets."""
    rows = [dict(r) for r in db.q("SELECT * FROM yt_videos WHERE video_id IS NOT NULL AND state IN ('uploaded')")]
    want = []
    for r in rows:
        if r["published_at"] is None or r["upload_status"] != "processed":
            want.append(r)
            continue
        taken = {x["bucket"] for x in db.q("SELECT bucket FROM yt_metrics WHERE video_id=? AND source='data' "
                                           "AND bucket IS NOT NULL", r["video_id"])}
        if due_bucket(now - r["published_at"], taken) or (s.youtube_comment and not r["comment_id"]):
            want.append(r)
    if not want:
        return
    items = {v["id"]: v for v in client.videos([r["video_id"] for r in want])}
    out["polled"] = len(want)
    for r in want:
        v = items.get(r["video_id"])
        if v is None:
            continue
        st = v.get("status") or {}
        f = {"privacy": st.get("privacyStatus"), "upload_status": st.get("uploadStatus")}
        if st.get("uploadStatus") in ("rejected", "failed"):
            f.update(state="rejected", error=st.get("rejectionReason") or st.get("failureReason") or st["uploadStatus"])
        if r["published_at"] is None and st.get("privacyStatus") == "public":
            f["published_at"] = now
        db.x(f"UPDATE yt_videos SET {', '.join(f'{k}=?' for k in f)} WHERE post_key=?", *f.values(), r["post_key"])
        r.update(f)
        if r.get("state") == "rejected" or r["published_at"] is None or r["upload_status"] != "processed":
            continue
        if s.youtube_comment and not r["comment_id"] and not s.posting_paused:
            from .motd import film_dir
            fj = film_dir(s, r["post_key"][len("motd-"):]) / "film.json"
            rec = json.loads(fj.read_text()) if fj.exists() else {"round_id": r["round_id"], "date": r["post_key"][5:]}
            try:
                cid = client.comment(r["video_id"], youtube.comment_text(s, rec))
                out["comments"] += 1
            except youtube.YouTubeError as e:
                if e.status is None or e.status >= 500:
                    raise
                # a 4xx (comments off, missing scope): record it once rather than paying 50 units every cycle
                log.error("yt %s: comment refused (%s %s); not retried", r["post_key"], e.status, e.reason)
                cid = f"failed:{e.reason or e.status}"
            db.x("UPDATE yt_videos SET comment_id=? WHERE post_key=?", cid or "?", r["post_key"])
        stats = v.get("statistics") or {}
        age = now - r["published_at"]
        taken = {x["bucket"] for x in db.q("SELECT bucket FROM yt_metrics WHERE video_id=? AND source='data' "
                                           "AND bucket IS NOT NULL", r["video_id"])}
        b = due_bucket(age, taken)
        if b:
            db.x("INSERT OR IGNORE INTO yt_metrics(video_id, taken_at, age_s, source, bucket, views, likes, comments, "
                 "raw_json) VALUES(?,?,?,?,?,?,?,?,?)", r["video_id"], now, age, "data", b,
                 _int(stats.get("viewCount")), _int(stats.get("likeCount")), _int(stats.get("commentCount")),
                 json.dumps(v))
            out["buckets"] += 1


def _int(v):
    return int(v) if v not in (None, "") else None


def parse_totals(rep: dict) -> dict:
    """An Analytics totals report (columnHeaders + one row) -> yt_metrics columns."""
    rows = rep.get("rows") or []
    if not rows:
        return {}
    vals = dict(zip([h["name"] for h in rep.get("columnHeaders", [])], rows[0]))
    return {"views": _int(vals.get("views")), "engaged_views": _int(vals.get("engagedViews")),
            "minutes_watched": vals.get("estimatedMinutesWatched"), "avg_view_s": vals.get("averageViewDuration"),
            "avg_view_pct": vals.get("averageViewPercentage"), "likes": _int(vals.get("likes")),
            "comments": _int(vals.get("comments")), "shares": _int(vals.get("shares")),
            "subs_gained": _int(vals.get("subscribersGained"))}


def parse_retention(rep: dict) -> list[list]:
    """[[elapsed_ratio, audience_watch_ratio, relative_perf|None], ...]; relativeRetentionPerformance may be absent."""
    names = [h["name"] for h in rep.get("columnHeaders", [])]
    out = []
    for row in rep.get("rows") or []:
        d = dict(zip(names, row))
        out.append([d.get("elapsedVideoTimeRatio"), d.get("audienceWatchRatio"),
                    d.get("relativeRetentionPerformance")])
    return out


def _analytics(s: Settings, db, client, now: float, out: dict) -> None:
    rows = db.q("SELECT * FROM yt_videos WHERE video_id IS NOT NULL AND published_at IS NOT NULL AND state='uploaded'")
    for r in rows:
        taken = {x["bucket"] for x in db.q("SELECT bucket FROM yt_metrics WHERE video_id=? AND source='analytics' "
                                           "AND bucket IS NOT NULL", r["video_id"])}
        age = now - r["published_at"]
        b = due_bucket(age, taken, A_BUCKETS)
        if not b:
            continue
        start = _utc_day(r["published_at"])
        end = _utc_day(now)
        base = {"startDate": start, "endDate": end, "filters": f"video=={r['video_id']}"}
        tot = client.report({**base, "metrics": A_METRICS})
        ret = client.report({**base, "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                             "dimensions": "elapsedVideoTimeRatio"})
        cols = parse_totals(tot)
        db.x("INSERT OR IGNORE INTO yt_metrics(video_id, taken_at, age_s, source, bucket, views, likes, comments, "
             "engaged_views, minutes_watched, avg_view_s, avg_view_pct, shares, subs_gained, retention_json, "
             "data_through, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             r["video_id"], now, age, "analytics", b, cols.get("views"), cols.get("likes"), cols.get("comments"),
             cols.get("engaged_views"), cols.get("minutes_watched"), cols.get("avg_view_s"), cols.get("avg_view_pct"),
             cols.get("shares"), cols.get("subs_gained"), json.dumps(parse_retention(ret)),
             tot.get("dataThrough") or end, json.dumps({"totals": tot, "retention": ret}))
        out["analytics"] += 1


def _channel(s: Settings, db, client, now: float, out: dict) -> None:
    day = _utc_day(now)
    if db.one("SELECT 1 FROM yt_channel_metrics WHERE day=?", day):
        return
    st = client.channel_stats()
    db.x("INSERT OR REPLACE INTO yt_channel_metrics(day, subscribers, views, videos, taken_at) VALUES(?,?,?,?,?)",
         day, _int(st.get("subscriberCount")), _int(st.get("viewCount")), _int(st.get("videoCount")), now)
    out["channel"] = True


def hide_round(s: Settings, db, client, round_id: int) -> bool:
    """A voided round's film goes private (never deleted, so the id stays auditable). Never raises."""
    try:
        r = db.one("SELECT post_key, video_id FROM yt_videos WHERE round_id=? AND video_id IS NOT NULL", round_id)
        if not r:
            return False
        if client is None or s.dry_run:
            log.warning("yt %s: round %s voided, but YouTube isn't reachable here; hide it in Studio",
                        r["post_key"], round_id)
            return False
        client.set_private(r["video_id"])
        db.x("UPDATE yt_videos SET state='hidden', privacy='private' WHERE post_key=?", r["post_key"])
        return True
    except Exception:
        log.exception("yt: hiding round %s's film failed", round_id)
        return False


# ---------------------------------------------------------------- queries (no network)

def _vid(db, post_key: str) -> dict | None:
    r = db.one("SELECT * FROM yt_videos WHERE post_key=?", post_key)
    return dict(r) if r else None


def snapshot(db, post_key: str, bucket: str, source: str = "data") -> dict | None:
    v = _vid(db, post_key)
    if not v or not v["video_id"]:
        return None
    r = db.one("SELECT * FROM yt_metrics WHERE video_id=? AND source=? AND bucket=?", v["video_id"], source, bucket)
    return dict(r) if r else None


def video_retention(db, post_key: str) -> dict | None:
    """Same keys as xstats.video_retention, from the latest analytics row. The p-values are audienceWatchRatio
    at elapsed 0.25/0.5/0.75/1.0 (nearest point): watch ratios, which can exceed 1 on Shorts (replays)."""
    v = _vid(db, post_key)
    if not v or not v["video_id"]:
        return None
    r = db.one("SELECT * FROM yt_metrics WHERE video_id=? AND source='analytics' ORDER BY taken_at DESC LIMIT 1",
               v["video_id"])
    if not r:
        return None
    pts = [p for p in json.loads(r["retention_json"] or "[]") if p[0] is not None and p[1] is not None]

    def near(x):
        return round(min(pts, key=lambda p: abs(p[0] - x))[1], 4) if pts else None
    return {"plays": r["views"], "p25": near(.25), "p50": near(.5), "p75": near(.75), "p100": near(1.0),
            "video_views": r["engaged_views"], "age_s": r["age_s"], "taken_at": r["taken_at"]}


def views_series(db, post_key: str) -> list[dict]:
    v = _vid(db, post_key)
    if not v or not v["video_id"]:
        return []
    return [dict(r) for r in db.q("SELECT taken_at, age_s, source, bucket, views, engaged_views, likes, comments "
                                  "FROM yt_metrics WHERE video_id=? ORDER BY taken_at", v["video_id"])]


def reward_inputs(db, post_key: str, bucket: str, max_age_ratio: float = 1.5) -> dict | None:
    v = _vid(db, post_key)
    if not v or not v["published_at"] or v["state"] != "uploaded":
        return None
    d = snapshot(db, post_key, bucket)
    if not d or d["age_s"] > max_age_ratio * BUCKETS[bucket]:
        return None
    a = snapshot(db, post_key, "7d", "analytics") or {}
    return {"views": d["views"], "engaged_views": a.get("engaged_views"), "likes": d["likes"],
            "comments": d["comments"], "avg_view_pct": a.get("avg_view_pct"), "age_s": d["age_s"],
            "post_key": post_key, "round_id": v["round_id"], "published_at": v["published_at"]}


def by_film(db, since: float) -> list[dict]:
    """One row per film: X (24 h / 7 d impressions, video views, retention) beside YouTube."""
    from . import xstats
    out = []
    for v in db.q("SELECT * FROM yt_videos WHERE post_key>=? ORDER BY post_key DESC", "motd-" + _utc_day(since)):
        key = v["post_key"]
        t = db.one("SELECT tweet_id FROM tweets WHERE post_key=?", key)
        tid = t["tweet_id"] if t else None
        x24 = xstats.snapshot(db, tid, "24h") if tid else None
        x7 = xstats.snapshot(db, tid, "7d") if tid else None
        y24, y7 = snapshot(db, key, "24h"), snapshot(db, key, "7d")
        a7 = snapshot(db, key, "7d", "analytics") or {}
        out.append({"post_key": key, "yt_state": v["state"], "yt_privacy": v["privacy"],
                    "x_imp_24h": (x24 or {}).get("impressions"), "x_imp_7d": (x7 or {}).get("impressions"),
                    "x_video_views_7d": (x7 or {}).get("video_views"),
                    "x_p50": ((xstats.video_retention(db, tid) if tid else None) or {}).get("p50"),
                    "yt_views_24h": (y24 or {}).get("views"), "yt_views_7d": (y7 or {}).get("views"),
                    "yt_engaged_7d": a7.get("engaged_views"), "yt_avg_view_pct_7d": a7.get("avg_view_pct"),
                    "yt_watch_p50": (video_retention(db, key) or {}).get("p50")})
    return out


def followers_series(db, since: float | None = None) -> list[dict]:
    day = _utc_day(since) if since else "0000"
    return [dict(r) for r in db.q("SELECT day, subscribers, views, videos FROM yt_channel_metrics WHERE day>=? "
                                  "ORDER BY day", day)]


def quota(db, day_pt: str | None = None) -> dict:
    day = day_pt or youtube.day_pt()
    return {"day_pt": day, **{r["bucket"]: r["units"] for r in db.q("SELECT bucket, units FROM yt_quota "
                                                                    "WHERE day_pt=?", day)}}


def health(db, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    locked = [r["post_key"] for r in db.q(
        "SELECT post_key FROM yt_videos WHERE privacy_requested='public' AND COALESCE(privacy,'private')='private' "
        "AND state='uploaded' AND uploaded_at IS NOT NULL AND uploaded_at < ?", now - LOCKED_AFTER_S)]
    last = db.one("SELECT state FROM yt_videos ORDER BY post_key DESC LIMIT 1")
    return {"last_ok": db.get("yt_last_ok"), "consecutive_auth_failures": int(db.get("yt_auth_failures", 0) or 0),
            "last_upload_state": last["state"] if last else None, "locked_private": locked,
            "rejected": [dict(r) for r in db.q("SELECT post_key, error FROM yt_videos WHERE state='rejected'")],
            "uploads_failed_7d": db.one("SELECT COUNT(*) c FROM yt_videos WHERE state='failed' AND post_key>=?",
                                        "motd-" + _utc_day(now - 7 * 86400))["c"]}


def digest_block(s: Settings, db, now: float | None = None) -> dict:
    """The "YouTube" block of the 08:00 UTC digest: yesterday's upload, the last 7 films, subscribers delta,
    quota, health warnings."""
    now = time.time() if now is None else now
    yday = _utc_day(now - 86400)
    y = _vid(db, youtube.post_key(yday))
    subs = followers_series(db, now - 2 * 86400)
    h = health(db, now)
    warn = []
    if y and y["state"] == "failed":
        warn.append("yt-upload-failed")
    if h["consecutive_auth_failures"] >= 3:
        warn.append("yt-auth-failures")
    if h["locked_private"]:
        warn.append("yt-locked-private")
    if h["rejected"]:
        warn.append("yt-rejected")
    return {"yesterday": {"post_key": youtube.post_key(yday), "state": y["state"] if y else None},
            "films": by_film(db, now - 8 * 86400)[:7],
            "subscribers": subs[-1]["subscribers"] if subs else None,
            "subscribers_delta": (subs[-1]["subscribers"] - subs[0]["subscribers"])
            if len(subs) >= 2 and None not in (subs[-1]["subscribers"], subs[0]["subscribers"]) else None,
            "quota": quota(db), "health": h, "warnings": warn}
