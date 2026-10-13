"""TikTok metrics and the TikTok cycle (docs/tiktok-spec.md §5-7), mirroring ytstats: pure functions over the DB;
`pull_due` is the only one that calls TikTok. Sibling code imports these functions and writes no SQL against
the tt_* tables."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from . import tiktok
from .config import Settings
from .ytstats import _utc_day, due_bucket, film_candidates

log = logging.getLogger(__name__)

BUCKETS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}   # spec §6, from published_at
FYP_SAMPLE = 5                                          # spec §3/§7: the first 5 public videos
DRAFT_STALE_S = 36 * 3600                               # a draft nobody published: a digest warning


def due_at(s: Settings, day: str) -> float:
    """The film of `day` goes to TikTok at tiktok_post_hour_utc on that day (18:00 UTC, operator decision)."""
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=int(s.tiktok_post_hour_utc),
                                                      tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------- the cycle

def pull_due(s: Settings, db, client, now: float | None = None) -> dict:
    """One TikTok cycle: the upload due, status polls (draft -> published), metric buckets, removal check.
    Each step is isolated: a failure in one is logged and the rest still run."""
    now = time.time() if now is None else now
    out = {"uploaded": [], "polled": 0, "matched": 0, "buckets": 0, "removed": 0}
    if not tiktok.killed(db):
        for rec, d in film_candidates(s, now):
            if now < due_at(s, rec["date"]):
                continue
            key = tiktok.post_key(rec["date"])
            cur = tiktok.row(db, key)
            if cur and cur["state"] != "uploading":
                continue
            try:
                r = tiktok.upload(s, db, client, rec, d)
                out["uploaded"].append({key: r["state"]})
            except tiktok.AccountMismatch:
                pass  # logged at ERROR by the guard; retried next cycle once the config is fixed
            except Exception as e:
                log.exception("tt %s: upload step failed", key)
                out["uploaded"].append({key: f"error: {e}"})
    if client is None or s.dry_run:
        return out
    for step, fn in (("status", _status), ("match", _match), ("metrics", _metrics)):
        try:
            fn(s, db, client, now, out)
        except Exception:
            log.exception("tt %s step failed", step)
    return out


def _status(s: Settings, db, client, now: float, out: dict) -> None:
    """status/fetch for every sent upload: FAILED fails it (a stop code also stops TikTok); a public post id
    (Direct Post, or a draft the operator published: verify) makes it 'published'."""
    for r in db.q("SELECT * FROM tt_videos WHERE state='sent' AND publish_id IS NOT NULL"):
        try:
            d = client.status(r["publish_id"])
        except tiktok.TikTokError as e:
            if e.code in tiktok.STOP_CODES:
                tiktok.kill(s, db, f"{e.code} on status of {r['post_key']}")
            raise
        out["polled"] += 1
        st = d.get("status")
        f = {"status": st}
        if st == "FAILED":
            why = d.get("fail_reason") or "FAILED"
            f.update(state="failed", error=why)
            if why in tiktok.STOP_CODES:
                tiktok.kill(s, db, f"{why} on {r['post_key']}")
        ids = d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or []
        if ids:
            f.update(state="published", video_id=str(ids[0]), published_at=r["published_at"] or now)
        db.x(f"UPDATE tt_videos SET {', '.join(f'{k}=?' for k in f)} WHERE post_key=?", *f.values(), r["post_key"])


def _norm(t: str) -> str:
    return " ".join((t or "").lower().split())


def _match(s: Settings, db, client, now: float, out: dict) -> None:
    """A draft the operator published may never get a post id through status/fetch (verify): find it in
    video/list instead, created after the upload and with the market name in its caption."""
    waiting = [dict(r) for r in db.q("SELECT * FROM tt_videos WHERE state='sent' AND video_id IS NULL "
                                     "AND mode='inbox' ORDER BY post_key")]
    if not waiting:
        return
    claimed = {r["video_id"] for r in db.q("SELECT video_id FROM tt_videos WHERE video_id IS NOT NULL")}
    vids = [v for v in client.recent_videos(20) if str(v.get("id")) not in claimed]
    for r in waiting:
        q = _norm(r["question"])
        for v in vids:
            if (v.get("create_time") or 0) + 60 < (r["uploaded_at"] or 0):
                continue
            if q and q in _norm(v.get("title")):
                ct = float(v.get("create_time") or now)
                db.x("UPDATE tt_videos SET state='published', video_id=?, share_url=?, published_at=? "
                     "WHERE post_key=?", str(v["id"]), v.get("share_url"), ct, r["post_key"])
                vids.remove(v)
                out["matched"] += 1
                break


def _int(v):
    return int(v) if v not in (None, "") else None


def _metrics(s: Settings, db, client, now: float, out: dict) -> None:
    """video/query for published videos with a bucket due. A video TikTok no longer returns was removed or
    restricted: TikTok stops (spec §7)."""
    rows = [dict(r) for r in db.q("SELECT * FROM tt_videos WHERE state='published' AND video_id IS NOT NULL")]
    want = []
    for r in rows:
        taken = {x["bucket"] for x in db.q("SELECT bucket FROM tt_metrics WHERE video_id=?", r["video_id"])}
        if due_bucket(now - (r["published_at"] or now), taken, BUCKETS):
            want.append(r)
    if not want:
        return
    got = {str(v.get("id")): v for v in client.videos([r["video_id"] for r in want])}
    for r in want:
        v = got.get(r["video_id"])
        if v is None:
            db.x("UPDATE tt_videos SET state='removed', error='not returned by video/query' WHERE post_key=?",
                 r["post_key"])
            out["removed"] += 1
            tiktok.kill(s, db, f"video {r['video_id']} ({r['post_key']}) is no longer returned: removed or "
                               "restricted by TikTok")
            continue
        age = now - (r["published_at"] or now)
        b = due_bucket(age, {x["bucket"] for x in db.q("SELECT bucket FROM tt_metrics WHERE video_id=?",
                                                       r["video_id"])}, BUCKETS)
        if v.get("share_url") and not r.get("share_url"):
            db.x("UPDATE tt_videos SET share_url=? WHERE post_key=?", v["share_url"], r["post_key"])
        db.x("INSERT OR IGNORE INTO tt_metrics(video_id, bucket, taken_at, age_s, views, likes, comments, shares, "
             "raw_json) VALUES(?,?,?,?,?,?,?,?,?)", r["video_id"], b, now, age, _int(v.get("view_count")),
             _int(v.get("like_count")), _int(v.get("comment_count")), _int(v.get("share_count")), json.dumps(v))
        out["buckets"] += 1


# ---------------------------------------------------------------- the For You check (operator-reported)

def record_foryou(s: Settings, db, key: str, pct: float) -> dict:
    """The operator reads 'For You' traffic share in TikTok Studio (not in the public API) and records it.
    Once the first 5 published videos all have a share, a mean under tiktok_min_fyp_pct stops TikTok."""
    if not tiktok.row(db, key):
        raise ValueError(f"no TikTok upload {key}")
    db.x("UPDATE tt_videos SET fyp_pct=? WHERE post_key=?", float(pct), key)
    return foryou_check(s, db)


def foryou_check(s: Settings, db, act: bool = True) -> dict:
    first = [dict(r) for r in db.q("SELECT post_key, fyp_pct FROM tt_videos WHERE video_id IS NOT NULL "
                                   "ORDER BY published_at, post_key LIMIT ?", FYP_SAMPLE)]
    have = [r["fyp_pct"] for r in first if r["fyp_pct"] is not None]
    res = {"sample": first, "reported": len(have), "mean_pct": round(sum(have) / len(have), 2) if have else None,
           "threshold_pct": s.tiktok_min_fyp_pct, "verdict": "pending"}
    if len(have) >= FYP_SAMPLE:
        res["verdict"] = "restricted" if res["mean_pct"] < s.tiktok_min_fyp_pct else "ok"
        if res["verdict"] == "restricted" and act:
            tiktok.kill(s, db, f"For You share {res['mean_pct']}% across the first {FYP_SAMPLE} videos "
                               f"(under {s.tiktok_min_fyp_pct}%): treated as restricted")
    return res


def void_round(s: Settings, db, round_id: int) -> bool:
    """A voided round's film: the API can't hide or delete a TikTok video, so the operator does it. Never raises."""
    try:
        r = db.one("SELECT post_key, video_id, state FROM tt_videos WHERE round_id=?", round_id)
        if not r or r["state"] not in ("sent", "published"):
            return False
        from . import alerts
        alerts.send(s, db, "warn", "tiktok", f"Voided round on TikTok ({r['post_key']})",
                    f"Round {round_id} was voided after its film went to TikTok "
                    f"({'video ' + r['video_id'] if r['video_id'] else 'draft, maybe unpublished'}).",
                    action="none (the API can't hide a TikTok video)",
                    resume="in the TikTok app: delete the draft, or set the video to Only me")
        return True
    except Exception:
        log.exception("tt: voided round %s alert failed", round_id)
        return False


# ---------------------------------------------------------------- queries (no network)

def snapshot(db, key: str, bucket: str) -> dict | None:
    v = tiktok.row(db, key)
    if not v or not v["video_id"]:
        return None
    r = db.one("SELECT * FROM tt_metrics WHERE video_id=? AND bucket=?", v["video_id"], bucket)
    return dict(r) if r else None


def by_film(db, since: float) -> list[dict]:
    """One row per film: X impressions beside TikTok views."""
    from . import xstats
    out = []
    for v in db.q("SELECT * FROM tt_videos WHERE post_key>=? ORDER BY post_key DESC", "tt-" + _utc_day(since)):
        day = v["post_key"][3:]
        t = db.one("SELECT tweet_id FROM tweets WHERE post_key=?", f"motd-{day}")
        tid = t["tweet_id"] if t else None
        x24 = xstats.snapshot(db, tid, "24h") if tid else None
        t24, t7 = snapshot(db, v["post_key"], "24h"), snapshot(db, v["post_key"], "7d")
        out.append({"post_key": v["post_key"], "tt_state": v["state"], "tt_status": v["status"],
                    "x_imp_24h": (x24 or {}).get("impressions"),
                    "tt_views_24h": (t24 or {}).get("views"), "tt_views_7d": (t7 or {}).get("views"),
                    "tt_shares_7d": (t7 or {}).get("shares"), "fyp_pct": v["fyp_pct"]})
    return out


def health(db, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    stale = [r["post_key"] for r in db.q("SELECT post_key FROM tt_videos WHERE state='sent' AND uploaded_at < ?",
                                         now - DRAFT_STALE_S)]
    last = db.one("SELECT state FROM tt_videos ORDER BY post_key DESC LIMIT 1")
    return {"last_ok": db.get("tt_last_ok"), "consecutive_auth_failures": int(db.get("tt_auth_failures", 0) or 0),
            "last_upload_state": last["state"] if last else None, "killed": tiktok.killed(db),
            "drafts_unpublished": stale,
            "uploads_failed_7d": db.one("SELECT COUNT(*) c FROM tt_videos WHERE state='failed' AND post_key>=?",
                                        "tt-" + _utc_day(now - 7 * 86400))["c"]}


def digest_block(s: Settings, db, now: float | None = None) -> dict:
    """The "TikTok" block of the 08:00 UTC digest."""
    now = time.time() if now is None else now
    yday = _utc_day(now - 86400)
    y = tiktok.row(db, tiktok.post_key(yday))
    h = health(db, now)
    warn = []
    if y and y["state"] == "failed":
        warn.append("tt-upload-failed")
    if h["killed"]:
        warn.append("tt-stopped")
    if h["consecutive_auth_failures"] >= 3:
        warn.append("tt-auth-failures")
    if h["drafts_unpublished"]:
        warn.append("tt-draft-unpublished")
    return {"yesterday": {"post_key": tiktok.post_key(yday), "state": y["state"] if y else None},
            "films": by_film(db, now - 8 * 86400)[:7], "foryou": foryou_check(s, db, act=False),
            "health": h, "warnings": warn}
