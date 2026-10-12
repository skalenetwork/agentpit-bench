"""Newsworthiness rating (docs/ai-automation-spec.md §5), shipped in shadow mode.

Every new eligible market gets one model rating (0-100, topic only: no prices, no agent data), rated once and
never refreshed, published per market in data/newsworthiness.json. Selection uses it only after the operator
approves `selection.scoring = model` in the methodology lock (v1.1); until then the keyword rule decides and
`agentpitbench news shadow` compares the two.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import opai
from .config import Settings
from .db import DB
from .virality import _num, category, clean_quote, market_priority, news_tier

log = logging.getLogger(__name__)

KEYWORD_SCORE = {0: 10, 1: 35, 2: 60, 3: 85}
MAX_TRIES = 3
MIN_GAP_S = 600     # at most one rating call per 10 min
_pending: dict[str, dict] = {}   # market id -> market dict (from the watcher), until rated
_tries: dict[str, int] = {}
_tasks: set[asyncio.Task] = set()


def rating_for(db: DB, market_id) -> int | None:
    r = db.one("SELECT score FROM market_ratings WHERE market_id=?", str(market_id))
    return r["score"] if r else None


def priority(m: dict, db: DB | None = None, s: Settings | None = None) -> tuple:
    """Selection order. Keyword mode (today, and whenever no rating exists) is exactly the old ordering."""
    if s is not None and db is not None and s.selection_scoring == "model":
        kw, r = news_tier(m), rating_for(db, m["id"])
        if r is not None and not (kw == 0 and r >= 50):  # keyword floor: esports/minor leagues stay down
            return (r, _num(m, "volume"), _num(m, "liquidity"), int(m["id"]))
        return (KEYWORD_SCORE[kw], _num(m, "volume"), _num(m, "liquidity"), int(m["id"]))
    return market_priority(m)


def nw_of(m: dict, db: DB, s: Settings) -> dict:
    kw, r = news_tier(m), rating_for(db, m["id"])
    if s.selection_scoring == "model" and r is not None and not (kw == 0 and r >= 50):
        return {"score": r, "source": "model"}
    return {"score": KEYWORD_SCORE[kw], "source": "keyword"}


def observe(markets: list[dict]) -> None:
    for m in markets:
        _pending.setdefault(str(m["id"]), m)


def payload(db: DB, limit: int = 40) -> dict:
    ids = [mid for mid in list(_pending) if not db.one("SELECT 1 FROM market_ratings WHERE market_id=?", mid)]
    for mid in list(_pending):
        if mid not in ids:
            _pending.pop(mid, None)
    ms = [_pending[mid] for mid in ids[:limit]]
    return {"markets": [{"market_id": str(m["id"]), "question": m.get("question", ""),
                         "description": (m.get("description") or "")[:400],
                         "category": category(m.get("question", "")), "end_date": m.get("endDate")} for m in ms]}


def validator(p: dict):
    asked = {m["market_id"] for m in p["markets"]}

    def validate(d: dict) -> str | None:
        for r in d["ratings"]:
            if r["market_id"] not in asked:
                return f"unknown market {r['market_id'][:20]}"
        return None
    return validate


def _store(db: DB, m: dict, score, reason, output_id) -> None:
    db.x("INSERT OR IGNORE INTO market_ratings(market_id,score,reason,keyword_tier,output_id,rated_at,question)"
         " VALUES(?,?,?,?,?,?,?)", str(m["id"]), score, reason, news_tier(m), output_id, time.time(),
         m.get("question"))


async def rate_pending(s: Settings, db: DB, limit: int | None = None, force_gap: bool = True) -> int:
    """One batch call. Returns markets rated (with a score), -1 when skipped or failed."""
    if force_gap and time.time() - (db.get("opai_rate_last") or 0) < MIN_GAP_S:
        return -1
    p = payload(db, limit or s.opai_rate_batch)
    if not p["markets"]:
        return 0
    if not opai.available(s, db, "rate")[0]:
        opai._skip(db, "rate", opai.available(s, db, "rate")[1])
        return -1
    db.put("opai_rate_last", time.time())
    res = await opai.acall(s, db, "rate", p, subject=f"markets:{len(p['markets'])}", validate=validator(p))
    got = {}
    if res is not None:
        for r in res.data["ratings"]:
            reason, why = clean_quote(r["reason"], 80)
            if why or len(r["reason"]) > 80:
                continue
            got[r["market_id"]] = (r["score"], reason)
    n = 0
    for pm in p["markets"]:
        m = _pending.get(pm["market_id"])
        if m is None:
            continue
        if pm["market_id"] in got:
            _store(db, m, *got[pm["market_id"]], res.output_id)
            n += 1
        else:
            _tries[pm["market_id"]] = _tries.get(pm["market_id"], 0) + 1
            if _tries[pm["market_id"]] >= MAX_TRIES:  # keyword for good
                _store(db, m, None, None, None)
        if db.one("SELECT 1 FROM market_ratings WHERE market_id=?", pm["market_id"]):
            _pending.pop(pm["market_id"], None)
    if res is not None and n:
        db.x("UPDATE operator_ai_outputs SET public=1 WHERE output_id=?", res.output_id)  # methodology: public
    return n if res is not None else -1


def schedule(s: Settings, db: DB, markets: list[dict]) -> None:
    """From Watcher.poll: remember the new eligible markets and rate them off the polling path."""
    if not s.opai_enabled or "rate" not in s.opai_tasks:
        return
    observe(markets)
    if not _pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def run():
        try:
            await rate_pending(s, db)
        except Exception:
            log.exception("newsworthiness rating failed")
    t = loop.create_task(run(), name="rate")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------- publishing and the shadow report

def export_rows(db: DB, days: int = 30) -> list[dict]:
    rows = db.q("SELECT mr.*, o.model, o.prompt_version FROM market_ratings mr LEFT JOIN operator_ai_outputs o "
                "USING(output_id) WHERE mr.rated_at>? ORDER BY mr.rated_at DESC", time.time() - days * 86400)
    from .methodology import load
    scoring = load()["selection"]["scoring"]
    return [{"market_id": r["market_id"], "question": r["question"], "score": r["score"], "reason": r["reason"],
             "keyword_tier": r["keyword_tier"],
             "source_used": "model" if scoring == "model" and r["score"] is not None and not (
                 r["keyword_tier"] == 0 and r["score"] >= 50) else "keyword",
             "rated_at": r["rated_at"], "prompt_version": r["prompt_version"], "model": r["model"]} for r in rows]


def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None

    def ranks(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(x):
            j = i
            while j + 1 < len(x) and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra) ** .5, sum((y - mb) ** 2 for y in rb) ** .5
    return round(cov / (va * vb), 3) if va and vb else None


def shadow_report(db: DB, days: int = 7, per_day: int = 5) -> str:
    from datetime import datetime, timezone
    rows = [dict(r) for r in db.q("SELECT * FROM market_ratings WHERE score IS NOT NULL AND rated_at>?",
                                  time.time() - days * 86400)]
    if not rows:
        return f"no model ratings in the last {days} days"
    out = []
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(datetime.fromtimestamp(r["rated_at"], timezone.utc).strftime("%Y-%m-%d"), []).append(r)
    for day, rs in sorted(by_day.items()):
        kw = sorted(rs, key=lambda r: (KEYWORD_SCORE[r["keyword_tier"]], int(r["market_id"])), reverse=True)[:per_day]
        md = sorted(rs, key=lambda r: (r["score"] if not (r["keyword_tier"] == 0 and r["score"] >= 50)
                                       else KEYWORD_SCORE[0], int(r["market_id"])), reverse=True)[:per_day]
        overlap = len({r["market_id"] for r in kw} & {r["market_id"] for r in md})
        out.append(f"{day}: {len(rs)} rated; top {per_day} overlap {overlap}/{min(per_day, len(rs))}")
        out.append("  keyword: " + " | ".join(r["question"][:50] for r in kw))
        out.append("  model:   " + " | ".join(f"{r['question'][:44]} ({r['score']})" for r in md))
    rho = _spearman([r["score"] for r in rows], [KEYWORD_SCORE[r["keyword_tier"]] for r in rows])
    out.append(f"Spearman(model score, keyword score) over {len(rows)} markets: {rho}")
    out.append("Largest disagreements:")
    for r in sorted(rows, key=lambda r: -abs(r["score"] - KEYWORD_SCORE[r["keyword_tier"]]))[:10]:
        out.append(f"  model {r['score']:3} vs keyword {KEYWORD_SCORE[r['keyword_tier']]:2}  {r['question'][:70]}"
                   f"  — {r['reason']}")
    return "\n".join(out)
