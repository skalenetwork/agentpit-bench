"""Per-round agent summaries (docs/ai-automation-spec.md §7): two lines per agent on how it reached its pick,
linked to the full transcript. Written only after the round left `running`; validated line by line against the
transcript and the round record. English, like the transcripts. A failing agent simply gets no lines.

Inputs come only from exports.round_record (which blanks sealed fields) and the round's own transcripts via
virality.reasoning_lines. Never the forecast sweep: no forecasts, sweep_markets or sweep transcripts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from . import opai
from .config import Settings
from .db import DB
from .virality import clean_quote, reasoning_lines

log = logging.getLogger(__name__)

MAX_LINE = 110
MAX_REASONING = 60
BANNED = {"significant", "proves", "always", "never", "genius", "stupid"}
_DIGITS = re.compile(r"\d+")


def _transcript_lines(s: Settings, round_id: int, agent: str) -> list[str]:
    p = s.transcripts_dir / str(round_id) / f"{agent}.txt"
    if not p.exists():
        return []
    return reasoning_lines(p.read_text(errors="replace"), max_lines=MAX_REASONING)


def build_payload(s: Settings, db: DB, round_id: int) -> tuple[dict, dict]:
    """(payload, grounding) for the bet-placing agents. Raises SealedError on a running round before reading
    anything."""
    opai.sealed_guard(db, round_id)
    from .exports import round_record
    rec = round_record(s, db, db.round(round_id))
    agents, grounding = [], {}
    for e in rec["entries"]:
        if not e.get("outcome"):
            continue
        lines = _transcript_lines(s, round_id, e["agent"])
        agents.append({"agent": e["agent"], "name": e["name"], "outcome": e["outcome"],
                       "avg_price": e["avg_price"], "confidence": e["confidence"], "quote": e["quote"],
                       "reasoning": lines})
        nums = set(_DIGITS.findall(" ".join(lines) + " " + rec["question"]))
        for v in (e["avg_price"], e["confidence"], *rec["prices_at_start"]):
            if isinstance(v, (int, float)):
                nums |= set(_DIGITS.findall(f"{v}")) | {str(round(v * 100)), f"{v * 100:.1f}".rstrip("0").rstrip(".")}
        for v in (e["stake_filled"], e["shares_filled"], e["decided_s"]):
            if isinstance(v, (int, float)):
                nums |= set(_DIGITS.findall(f"{v}")) | {str(round(v))}
        nums |= set(_DIGITS.findall(str(rec["round_id"])))
        grounding[e["agent"]] = {"nums": nums, "outcome": e["outcome"]}
    payload = {"question": rec["question"], "outcomes": rec["outcomes"], "prices_at_start": rec["prices_at_start"],
               "agents": agents}
    return payload, {"grounding": grounding, "outcomes": rec["outcomes"]}


def check_line(line: str, own: str, outcomes: list[str], nums: set[str]) -> str | None:
    line = " ".join(line.split())
    if not line:
        return "empty"
    if len(line) > MAX_LINE:
        return "too long"
    q, why = clean_quote(line, 400)
    if why:
        return why
    bad = [n for n in _DIGITS.findall(line) if n not in nums]
    if bad:
        return "ungrounded number"
    for o in outcomes:
        if o != own and re.search(r"(?<!\w)" + re.escape(o) + r"(?!\w)", line):
            return "names another outcome"
    if set(re.findall(r"[a-z]+", line.lower())) & BANNED:
        return "banned word"
    return None


def validate(data: dict, ctx: dict) -> dict[str, tuple[str, str] | str]:
    """Per agent: (line1, line2) or the reject reason."""
    out: dict[str, tuple[str, str] | str] = {}
    for agent, g in ctx["grounding"].items():
        d = (data.get("agents") or {}).get(agent)
        if not d:
            out[agent] = "missing"
            continue
        reasons = [check_line(d[k], g["outcome"], ctx["outcomes"], g["nums"]) for k in ("line1", "line2")]
        why = next((r for r in reasons if r), None)
        out[agent] = why or (" ".join(d["line1"].split()), " ".join(d["line2"].split()))
    return out


async def write(s: Settings, db: DB, round_id: int, rewrite: bool = False) -> int:
    """Writes the round's summaries; returns how many agents got lines (-1: opai unavailable or failed)."""
    opai.sealed_guard(db, round_id)  # first: a running round raises before anything is read
    r = db.round(round_id)
    if r["state"] == "void":
        return 0
    if not rewrite and db.one("SELECT 1 FROM round_summaries WHERE round_id=?", round_id):
        return 0
    ok, why = opai.available(s, db, "summary")
    if not ok:  # cheap exit before reading transcripts; the catch-up retries
        opai._skip(db, "summary", why)
        return -1
    payload, ctx = build_payload(s, db, round_id)
    if not payload["agents"]:
        return 0
    res = await opai.acall(s, db, "summary", payload, subject=f"round:{round_id}")
    if res is None:
        return -1
    verdict = validate(res.data, ctx)
    n = 0
    for agent, v in verdict.items():
        if isinstance(v, str):
            log.info("round %s: summary for %s rejected (%s)", round_id, agent, v)
            continue
        db.x("INSERT INTO round_summaries(round_id,agent,line1,line2,output_id) VALUES(?,?,?,?,?) "
             "ON CONFLICT(round_id,agent) DO UPDATE SET line1=excluded.line1, line2=excluded.line2, "
             "output_id=excluded.output_id", round_id, agent, v[0], v[1], res.output_id)
        n += 1
    rejected = {a: v for a, v in verdict.items() if isinstance(v, str)}
    if rejected:
        db.x("UPDATE operator_ai_outputs SET reject_reason=? WHERE output_id=?",
             "; ".join(f"{a}: {v}" for a, v in rejected.items()), res.output_id)
    if n:
        db.x("UPDATE operator_ai_outputs SET public=1 WHERE output_id=?", res.output_id)
    return n


def due(db: DB, now: float | None = None) -> list[int]:
    now = time.time() if now is None else now
    return [r["round_id"] for r in db.q(
        "SELECT ro.round_id FROM rounds ro WHERE ro.state IN ('awaiting_resolution','resolved') AND "
        "ro.started_at>? AND NOT EXISTS (SELECT 1 FROM round_summaries x WHERE x.round_id=ro.round_id) AND "
        "EXISTS (SELECT 1 FROM runs JOIN bets b USING(run_id) WHERE runs.round_id=ro.round_id AND b.outcome IS NOT NULL) "
        "ORDER BY ro.round_id DESC", now - 7 * 86400)]


async def atick(s: Settings, db: DB) -> None:
    """Catch-up (ops tick): at most one round per tick, newest first; skipped while opai is unavailable."""
    if not opai.available(s, db, "summary")[0]:
        return
    for rid in due(db)[:1]:
        await write(s, db, rid)


_tasks: set[asyncio.Task] = set()


def schedule(s: Settings, db: DB, round_id: int) -> None:
    """Fire-and-forget from on_bets_done; the round never awaits it."""
    async def run():
        try:
            await write(s, db, round_id)
        except Exception:
            log.exception("summaries for round %s failed", round_id)
    t = asyncio.create_task(run(), name=f"summaries-{round_id}")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
