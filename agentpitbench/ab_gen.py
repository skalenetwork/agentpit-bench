"""The A/B copy generator (docs/ab-testing-spec.md section 6): Claude writes challenger templates, the validator
accepts or rejects each, and accepted ones become live or queued arms. It never sees a round, never runs on the
posting path, never inside a contestant sandbox, and writes no `runs` row (so it can't trip the circuit breaker).

The call goes through `opai.acall(task="ab")` (2 calls/day). The guards here stay A/B's own: once per UTC day at
or after ab_gen_hour_utc, only while no round is running and the forecast sweep can't be running, holding the
contestant Claude login lock.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import ab
from . import ab_opai as opai  # A/B's own runner until it moves onto the shared opai runner (docs/ai-automation-spec.md)
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

PROMPT_FILE = Path(__file__).parent / "ab_prompt.txt"
AB_PROMPT_VERSION = hashlib.sha256(PROMPT_FILE.read_bytes()).hexdigest()[:12]
PER_SEAT = 2

HELP = {
    "reveal.hook": ("the reveal post's headline, once every AI has picked", {
        "agree": "every AI picked the same outcome",
        "alone": "all but one agree; {lone} is the one that picked differently; {score} like 3–1",
        "dead_split": "an even two-way split; {score} like 2–2",
        "nobody_agrees": "every AI picked something different; {score} like 1–1–1–1",
        "nway": "an uneven split over three or more outcomes; {score} like 2–1–1, {n} = number of outcomes picked"}),
    "results.hook": ("the results post's first line; {winner} is the outcome that won, {record} like 3-1 (right-wrong)", {
        "sweep": "every AI was right", "wipeout": "every AI picked {loser_pick} and was wrong",
        "lone_winner": "only {lone} was right", "lone_loser": "everyone but {lone} was right",
        "nobody": "the AIs split and none was right", "split": "{winners} were right, {losers} were wrong"}),
    "reveal.cta": ("a short lead-in line placed right before 'Reply with your pick: ...' (invite people to reply)", {}),
    "results.cta": ("a short line placed right before the round link on the results post", {}),
    "press.head": ("the heading of the reply that quotes the losing AIs' post-match statements", {}),
    "generic.cta": ("a short closing line on other posts (milestones, weekly race, awards, reports, films)", {}),
    "site.round_cta": ("the button on a round's web page that sends people to bet on the market themselves", {}),
}


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def wanted(db: DB, s: Settings) -> dict[str, int]:
    """Generated slots with a free seat and no queued proposal -> free seats."""
    out = {}
    for slot in ab.GENERATED:
        if db.one("SELECT 1 FROM ab_arms WHERE slot=? AND status='queued'", slot):
            continue
        n = ab.free_seats(db, s, slot)
        if n:
            out[slot] = n
    return out


def prompt(db: DB, s: Settings, seats: dict[str, int]) -> str:
    lines, live = [], []
    for slot, n in seats.items():
        d = ab.SLOTS[slot]
        what, sits = HELP[slot]
        lines.append(f"- {slot} ({n} free seat{'s' if n > 1 else ''}): {what}. Max {d['max_len']} characters.")
        if d["kind"] == "hook":
            lines.append(f"  placeholders allowed: {', '.join('{' + p + '}' for p in sorted(d['allowed']))}")
            for sit in d["situations"]:
                req = ", ".join("{" + p + "}" for p in sorted(d["required"][sit])) or "none"
                lines.append(f"  situation {sit}: {sits[sit]} (required: {req})")
        else:
            lines.append("  no placeholders; give {\"text\": \"...\"}")
        for a in ab.posterior(db, s, slot, draws=0):
            txt = (ab.CONTROL_TEXT.get(slot, "(today's code copy)") if a["is_control"]
                   else ab.template_text(a) or a["template_json"])
            live.append(f"- {slot}: {txt} (mean {a['mean']:+.2f} ± {a['sd']:.2f})")
    t = PROMPT_FILE.read_text()
    return (t.replace("{slots}", "\n".join(lines)).replace("{live}", "\n".join(live) or "(none)")
             .replace("{per_seat}", str(PER_SEAT)).replace("{{", "{").replace("}}", "}"))


def guard(db: DB, s: Settings, idle: Callable[[], bool], now: float, force: bool = False) -> str | None:
    """Why the generator must not run now, or None."""
    d = datetime.fromtimestamp(now, timezone.utc)
    day = _day(now)
    if not force:
        if not s.ab_gen_enabled:
            return "disabled (ab_gen_enabled = false)"
        if db.get(f"ab_gen_{day}"):
            return "already ran today"
        if d.hour < s.ab_gen_hour_utc:
            return "before ab_gen_hour_utc"
        if d.hour >= s.ab_gen_hour_utc + 6:
            db.put(f"ab_gen_{day}", {"status": "skipped", "why": "no idle window by ab_gen_hour_utc + 6"})
            return "no idle window today"
    if not idle():
        return "a round is running"
    if d.hour >= s.sweep_hour_utc and not db.get(f"sweep_{day}"):
        return "the forecast sweep may be running"
    if not wanted(db, s):
        return "no generated slot has a free seat (or a queued proposal waits)"
    return None


async def run(s: Settings, db: DB, idle: Callable[[], bool], now: float | None = None, dry: bool = False,
              force: bool = False, runner=None) -> dict:
    """One generator run. Returns {status: ok/failed/skipped, why, accepted, rejected, candidates}."""
    now = now or time.time()
    day = _day(now)
    why = guard(db, s, idle, now, force=force or dry)
    if why:
        return {"status": "skipped", "why": why}
    seats = wanted(db, s)
    raw_dir = s.data_dir / "ab_gen" / day
    payload = {"prompt": prompt(db, s, seats), "model": s.ab_gen_model, "timeout_s": s.ab_gen_timeout_s,
               "raw_dir": str(raw_dir)}
    from . import orchestrator
    lock = orchestrator._auth_locks.setdefault("claude", asyncio.Lock())
    async with lock:   # a contestant's login sync can't interleave with the generator's token refresh
        if runner is None and opai.SHIM:
            try:
                await orchestrator.warm_login("claude")
            except Exception:
                log.exception("warm_login before the A/B generator failed")
        res = await opai.acall(s, db, "ab", payload, subject=f"ab:{day}", runner=runner, now=now)
    if res is None:
        out = {"status": "failed", "why": "no usable answer (quota, timeout, crash or bad JSON)",
               "accepted": 0, "rejected": 0}
        if not dry:
            db.put(f"ab_gen_{day}", out)
        return out
    accepted, rejected, cands = 0, 0, []
    for slot, items in (res.data or {}).items():
        if slot not in seats or not isinstance(items, list):
            continue
        for tpl in items:
            errs = ab.validate(slot, tpl)
            cands.append({"slot": slot, "template": tpl, "errors": errs})
            if dry:
                continue
            oid = db.x("INSERT INTO operator_ai_outputs(purpose,model,prompt_version,created_at,content,accepted,"
                       "reject_reason,run_raw_path,call_id,subject) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       f"ab:{slot}", res.model, AB_PROMPT_VERSION, time.time(), json.dumps(tpl, ensure_ascii=False),
                       int(not errs), "; ".join(errs) or None, str(raw_dir), None, f"ab:{day}")
            if errs:
                rejected += 1
                continue
            ab.add_arm(db, s, slot, tpl, output_id=oid)
            accepted += 1
    out = {"status": "ok" if (accepted or dry) else "failed", "model": res.model, "accepted": accepted,
           "rejected": rejected if not dry else sum(1 for c in cands if c["errors"]),
           "reasons": sorted({e for c in cands for e in c["errors"]})[:20]}
    if not accepted and not dry:
        out["why"] = "all candidates rejected"
    if not dry:
        db.put(f"ab_gen_{day}", out)
    return {**out, "candidates": cands}
