"""Human picks, second pass (docs/ai-automation-spec.md §6): free-form replies ("Barca easy", "no way it
passes") mapped to an outcome by the operator AI, counted at or above opai_pick_threshold.

The exact pass (virality.human_picks) runs first and wins: a user with any exact match is never sent to the
model. The model pass reads only the stored `human_replies` (the one capped X read), never X itself.
"""
from __future__ import annotations

import json
import logging
import re
import time

from . import opai
from .config import Settings
from .db import DB
from .virality import _ts, parse_pick, unmatched

log = logging.getLogger(__name__)

MAX_REPLIES = 100
_MENTION = re.compile(r"(^|\s)[@＠]\w+")


def store_replies(db: DB, round_id: int, replies: list[dict], outcomes: list[str], exclude_ids: set[str]) -> int:
    n = 0
    for r in replies:
        if r.get("author_id") in exclude_ids or not r.get("author_id"):
            continue
        db.x("INSERT OR IGNORE INTO human_replies(round_id,reply_id,user_id,username,text,created_at,exact_pick)"
             " VALUES(?,?,?,?,?,?,?)", round_id, str(r["id"]), str(r["author_id"]), r.get("username"),
             r.get("text") or "", _ts(r.get("created_at")), parse_pick(r.get("text", ""), outcomes))
        n += 1
    return n


def _as_replies(rows) -> list[dict]:
    from datetime import datetime, timezone
    return [{"id": r["reply_id"], "author_id": r["user_id"], "username": r["username"], "text": r["text"],
             "created_at": datetime.fromtimestamp(r["created_at"], timezone.utc).isoformat()
             if r["created_at"] is not None else None} for r in rows]


def candidates(db: DB, round_id: int) -> list[dict]:
    r = db.round(round_id)
    rows = db.q("SELECT * FROM human_replies WHERE round_id=?", round_id)
    matched = {h["user_id"] for h in db.q("SELECT user_id FROM humans WHERE round_id=?", round_id)}
    matched |= {x["user_id"] for x in rows if x["exact_pick"]}
    return unmatched(_as_replies(rows), json.loads(r["outcomes"]), r["end_date"], set(), matched)[:MAX_REPLIES]


def validate_for(sent: set[str]):
    def validate(d: dict) -> str | None:
        for p in d["picks"]:
            if p["reply_id"] not in sent:
                return f"unknown reply_id {p['reply_id'][:20]}"
        return None
    return validate


async def model_pass(s: Settings, db: DB, round_id: int, force: bool = False) -> int:
    """Adds model-read picks; never removes exact ones. Returns the number of picks added, -1 when opai was
    unavailable or failed (humans_ai_done stays 0, so model_pass_due retries)."""
    r = db.round(round_id)
    if r is None or (r["humans_ai_done"] and not force):
        return 0
    if (r["thread_tweet_id"] or "").startswith("dry-"):
        db.set_round(round_id, humans_ai_done=1)
        return 0
    cands = candidates(db, round_id)
    if not cands:
        db.set_round(round_id, humans_ai_done=1)
        return 0
    outcomes = json.loads(r["outcomes"])
    payload = {"question": r["question"], "outcomes": outcomes,
               "replies": [{"reply_id": c["id"], "text": _MENTION.sub(" ", c["text"]).strip()[:280]} for c in cands]}
    res = await opai.acall(s, db, "picks", payload, subject=f"round:{round_id}",
                           validate=validate_for({c["id"] for c in cands}))
    if res is None:
        return -1
    by_id = {p["reply_id"]: p for p in res.data["picks"]}
    added = 0
    seen: set[str] = set()
    for c in cands:  # earliest first: the earliest confident reply per user wins
        p = by_id.get(c["id"])
        uid = c["author_id"]
        if uid in seen or not p or p["outcome"] is None or p["confidence"] < s.opai_pick_threshold:
            continue
        cur = db.conn.execute(
            "INSERT OR IGNORE INTO humans(round_id,user_id,username,pick,tweet_id,created_at,source,confidence,"
            "output_id) VALUES(?,?,?,?,?,?,'model',?,?)",
            (round_id, uid, c["username"], p["outcome"], c["id"], _ts(c["created_at"]), p["confidence"],
             res.output_id))
        seen.add(uid)
        added += cur.rowcount
    db.set_round(round_id, humans_ai_done=1)
    if added:
        db.x("UPDATE operator_ai_outputs SET public=1 WHERE output_id=?", res.output_id)
    if r["state"] == "resolved":
        db.x("UPDATE humans SET won=(pick=?) WHERE round_id=?", r["winner"], round_id)
    log.info("round %s: %d picks read from %d free-form replies", round_id, added, len(cands))
    return added


def due(db: DB, now: float | None = None) -> list[int]:
    now = time.time() if now is None else now
    return [r["round_id"] for r in db.q(
        "SELECT round_id FROM rounds WHERE humans_collected=1 AND humans_ai_done=0 AND state IN "
        "('awaiting_resolution','resolved') AND started_at>? ORDER BY round_id", now - 14 * 86400)]
