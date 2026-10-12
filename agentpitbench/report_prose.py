"""Weekly report prose (docs/ai-automation-spec.md §8): a model-written headline and 2-4 paragraphs for the
"State of the AIs" page. The model writes placeholders, never numbers; code fills them from the report dict.
Off by default: add "report" to opai_tasks. The X thread stays templated."""
from __future__ import annotations

import asyncio
import json
import logging
import re

from . import opai
from .config import AGENT_NAMES, Settings
from .db import DB

log = logging.getLogger(__name__)

PH = re.compile(r"\{([a-z_]+)(?:\.([a-z_]+))?\}")
BANNED = ("significant", "statistically", "proves")
SUPERLATIVE = re.compile(r"\b(best|most accurate)\b", re.I)


def flat(report: dict) -> dict[str, str]:
    m: dict[str, str] = {"markets": str(report.get("markets", 0)), "rounds": str(report.get("rounds", 0))}
    if report.get("crowd_brier") is not None:
        m["crowd_brier"] = f"{report['crowd_brier']:.3f}"
    for a in report.get("forecast") or []:
        m[f"brier.{a['agent']}"] = f"{a['brier']:.3f}"
        m[f"forecasts.{a['agent']}"] = str(a["n"])
        m[f"name.{a['agent']}"] = a["name"]
    for b in report.get("betting") or []:
        m[f"wins.{b['agent']}"] = str(b["wins"])
        m[f"losses.{b['agent']}"] = str(b["losses"])
        m[f"profit.{b['agent']}"] = f"{b['profit']:+.0f}"
        m[f"name.{b['agent']}"] = b["name"]
    if report.get("contrarian"):
        m["contrarian.right"] = str(report["contrarian"]["right"])
        m["contrarian.total"] = str(report["contrarian"]["total"])
    return m


def _key(mt: re.Match) -> str:
    return mt.group(1) + (f".{mt.group(2)}" if mt.group(2) else "")


def validate(prose: dict, report: dict) -> str | None:
    m = flat(report)
    texts = [prose["headline"], *prose["paragraphs"]]
    if len(prose["headline"]) > 90 or any(len(p) > 400 for p in prose["paragraphs"]):
        return "too long"
    if not 2 <= len(prose["paragraphs"]) <= 4:
        return "paragraph count"
    best = (report.get("forecast") or [{}])[0].get("agent")
    for t in texts:
        if re.search(r"\d", PH.sub("", t)):
            return "digit outside a placeholder"
        if "{" in PH.sub("", t) or "}" in PH.sub("", t):
            return "malformed placeholder"
        for mt in PH.finditer(t):
            if _key(mt) not in m:
                return f"unknown placeholder {_key(mt)}"
        low = t.lower()
        if any(w in low for w in BANNED):
            return "banned word"
        for sentence in re.split(r"(?<=[.!?])\s+", t):
            if SUPERLATIVE.search(sentence):
                named = {mt.group(2) for mt in PH.finditer(sentence) if mt.group(1) in ("name", "brier")}
                named |= {a for a, n in AGENT_NAMES.items() if re.search(rf"\b{re.escape(n)}\b", sentence)}
                if named and named != {best}:
                    return "superlative for an agent without the lowest Brier"
    return None


def fill(prose: dict, report: dict) -> dict:
    m = flat(report)
    sub = lambda t: PH.sub(lambda mt: m[_key(mt)], t)  # noqa: E731  (validated: every key exists)
    return {"headline": sub(prose["headline"]), "paragraphs": [sub(p) for p in prose["paragraphs"]]}


async def write(s: Settings, db: DB, report: dict) -> bool:
    week = report["week"]
    if latest(db, week):
        return False
    payload = {"report": report, "placeholders": sorted(flat(report))}
    res = await opai.acall(s, db, "report", payload, subject=f"week:{week}",
                           validate=lambda d: validate(d, report))
    if res:
        db.x("UPDATE operator_ai_outputs SET public=1 WHERE output_id=?", res.output_id)
    return res is not None


def latest(db: DB, week: str) -> dict | None:
    r = db.one("SELECT content FROM operator_ai_outputs WHERE purpose='report' AND subject=? AND accepted=1 "
               "ORDER BY output_id DESC LIMIT 1", f"week:{week}")
    return json.loads(r["content"]) if r else None


def attach(db: DB, reports: list[dict]) -> None:
    """exports.weekly_reports: add the filled prose to weeks that have a valid output."""
    for rep in reports:
        p = latest(db, rep["week"])
        if p and validate(p, rep) is None:  # numbers may have moved since: placeholders refill from today's dict
            rep["prose"] = fill(p, rep)


_tasks: set[asyncio.Task] = set()


def schedule(s: Settings, db: DB, report: dict) -> None:
    async def run():
        try:
            await write(s, db, report)
        except Exception:
            log.exception("report prose for %s failed", report.get("week"))
    t = asyncio.get_running_loop().create_task(run(), name=f"report-prose-{report.get('week')}")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
