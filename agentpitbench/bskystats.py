"""Bluesky metrics (docs/bluesky-spec.md §6): likes, reposts, replies and quotes of the mirrored posts.

Public AppView reads (app.bsky.feed.getPosts, 25 uris per call): no auth, no bill, no ledger. Posts younger than
7 days are read at most hourly; each read is one bsky_metrics row, joinable to tweets on bsky_posts.post_key.
"""
from __future__ import annotations

import logging
import time

import requests

from .bluesky import PUBLIC_APPVIEW
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

MAX_AGE_S = 7 * 86400
EVERY_S = 3600


def due(db: DB, now: float) -> list[tuple[str, float]]:
    rows = db.q("SELECT b.uri, b.posted_at, (SELECT MAX(taken_at) FROM bsky_metrics m WHERE m.uri=b.uri) last"
                " FROM bsky_posts b WHERE b.deleted=0 AND b.uri LIKE 'at://%' AND b.posted_at>=?", now - MAX_AGE_S)
    return [(r["uri"], r["posted_at"]) for r in rows if not r["last"] or now - r["last"] >= EVERY_S]


def pull(s: Settings, db: DB, http=requests, now: float | None = None) -> int:
    """One cycle; returns rows written. Does nothing when Bluesky is off."""
    if not s.bluesky_enabled:
        return 0
    now = now or time.time()
    posts = due(db, now)
    posted = dict(posts)
    n = 0
    for i in range(0, len(posts), 25):
        uris = [u for u, _ in posts[i:i + 25]]
        try:
            r = http.get(f"{PUBLIC_APPVIEW}/xrpc/app.bsky.feed.getPosts", params={"uris": uris}, timeout=30)
            r.raise_for_status()
        except Exception:
            log.exception("bluesky getPosts failed")
            continue
        for p in r.json().get("posts", []):
            db.x("INSERT OR IGNORE INTO bsky_metrics(uri,taken_at,age_s,likes,reposts,replies,quotes)"
                 " VALUES(?,?,?,?,?,?,?)", p["uri"], now, now - posted.get(p["uri"], now),
                 p.get("likeCount", 0), p.get("repostCount", 0), p.get("replyCount", 0), p.get("quoteCount", 0))
            n += 1
    return n
