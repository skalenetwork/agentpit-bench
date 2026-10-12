"""Weekly selection review (docs/ai-automation-spec.md §5.3): the operator AI reads the last 28 days of market
selection, the X results per category and the `news shadow` report, and drafts at most one methodology
proposal into var/proposals/.

With `selection_auto_apply` off, that's all it does: nothing changes until the operator runs
`agentpitbench method approve <id>` at a terminal. With it on (operator decision 2026-10-12), a valid proposal
goes straight to methodology.auto_apply, which re-checks everything (one market-selection key, PROPOSAL_BOUNDS,
at most one change per 7 days), bumps the version, writes a changelog line generated from the change (never
the model's text) and publishes the evidence; an info alert follows either way. The model's own
changelog_text and rationale only ever appear in the published evidence file. The operator can undo a change
with `agentpitbench method revert <version>`. This module never calls methodology.approve.

Off unless `selection_review_enabled` (and "proposal" in opai_tasks).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from . import methodology
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

WEEKDAY, HOUR_UTC = 6, 19      # Sunday 19:00 UTC
DAYS = 28
# the keys the model may propose: PROPOSAL_BOUNDS minus the rater prompt version (a new prompt file is a code
# change the operator makes, not a model's call); the same set methodology.auto_apply accepts
KEYS = methodology.AUTO_KEYS
TEXT_MAX = 300
LAST = "selection_review_last"

# the output is either {"none": reason} or {"changes": {key: value}, "rationale", "evidence"?, "changelog_text"};
# the CLI gets one flat object schema (opai.SCHEMAS["proposal"]) and validate() enforces the either/or


def rounds(db: DB, now: float) -> list[dict]:
    """Finished rounds only (a running round's picks are sealed): category, newsworthiness, favourite price at
    start, split or unanimous, void."""
    from .virality import category
    from .xstats import round_split
    out = []
    for r in db.q("SELECT round_id, question, snapshot_json, state, nw_score, nw_source, exhibition, started_at "
                  "FROM rounds WHERE started_at>=? AND state!='running' ORDER BY round_id", now - DAYS * 86400):
        try:
            prices = [float(p) for p in json.loads(r["snapshot_json"]).get("prices_list") or []]
        except (ValueError, TypeError):
            prices = []
        out.append({"round_id": r["round_id"], "category": category(r["question"] or ""),
                    "nw_score": r["nw_score"], "nw_source": r["nw_source"],
                    "favourite_price": round(max(prices), 3) if prices else None,
                    "split": None if r["state"] == "void" else round_split(db, r["round_id"]),
                    "void": r["state"] == "void", "exhibition": bool(r["exhibition"])})
    return out


def payload(s: Settings, db: DB, now: float) -> dict:
    from . import rating, xstats
    lock = methodology.load()
    current = {k: (lock["selection"]["scoring"] if k == "selection.scoring" else lock["params"].get(k)) for k in KEYS}
    return {"rounds": rounds(db, now), "x_by_category": xstats.by_category(db, now - DAYS * 86400, "24h", now),
            "shadow_report": rating.shadow_report(db, 7),
            "current": current, "methodology_version": lock["version"],
            "bounds": {k: (list(v) if v else None) for k, v in methodology.PROPOSAL_BOUNDS.items() if k in KEYS}}


def validate(data: dict) -> str | None:
    if "none" in data:
        if len(data) > 1:
            return "none together with a change"
        return None if isinstance(data["none"], str) and data["none"].strip() else "empty none reason"
    ch = data.get("changes")
    if not isinstance(ch, dict) or len(ch) != 1:
        return "exactly one change"
    key, value = next(iter(ch.items()))
    if key not in KEYS:
        return f"key {key} not allowed"
    try:
        methodology._check_change(methodology.load(), key, value, "opai")
    except methodology.MethodologyError as e:
        return str(e)
    from .virality import clean_quote
    for f in ("changelog_text", "rationale"):
        txt = data.get(f)
        if not isinstance(txt, str) or not txt.strip() or len(txt) > TEXT_MAX:
            return f"{f} missing or too long"
    for f in ("changelog_text", "rationale"):   # both are published with the evidence
        q, why = clean_quote(data[f], TEXT_MAX)
        if q is None:
            return f"{f} rejected: {why}"
    return None


def open_proposal(s: Settings) -> dict | None:
    return next((p for p in methodology.list_proposals(s)
                 if p.get("author") == "opai" and p.get("state", "open") == "open"), None)


async def review(s: Settings, db: DB, now: float | None = None) -> dict:
    """One review. Returns the digest record (also kv['selection_review_last'])."""
    from . import opai
    now = time.time() if now is None else now
    day = opai.utc_day(now)
    out = {"day": day, "at": now}
    pending = open_proposal(s)
    if pending:
        out["summary"] = f"skipped: proposal {pending['id']} from opai still awaits the operator"
        out["proposal"] = pending["id"]
    else:
        p = payload(s, db, now)
        res = await opai.acall(s, db, "proposal", p, subject=f"proposal:{day}", validate=validate)
        if res is None:
            out["summary"] = "no review (opai unavailable or output rejected)"
        elif "none" in res.data:
            out["summary"] = f"no change proposed: {res.data['none'][:200]}"
        else:
            evidence = (f"news shadow --days 7:\n{p['shadow_report']}\n\nrounds reviewed: {len(p['rounds'])} "
                        f"(last {DAYS} days)")
            if res.data.get("evidence"):
                evidence += f"\n\nmodel's reading: {res.data['evidence'][:1000]}"
            try:
                prop = methodology.propose(s, res.data["changes"], res.data["changelog_text"], "opai",
                                           rationale=res.data["rationale"], evidence=evidence,
                                           output_id=res.output_id)
            except methodology.MethodologyError as e:
                db.x("UPDATE operator_ai_outputs SET accepted=0, reject_reason=? WHERE output_id=?", str(e),
                     res.output_id)
                out["summary"] = f"proposal refused: {e}"
            else:
                out["proposal"] = prop["id"]
                if s.selection_auto_apply:
                    out.update(_auto(s, db, prop, now))
                else:
                    out["summary"] = (f"proposal {prop['id']}: {prop['changes']} (review: agentpitbench method "
                                      f"show {prop['id']}; apply only with method approve)")
    db.put(LAST, out)
    log.info("selection review %s: %s", day, out["summary"])
    return out


def _auto(s: Settings, db: DB, prop: dict, now: float) -> dict:
    """Apply through methodology.auto_apply; a refusal (rate limit, bounds, a value already in force) rejects
    the proposal, so it can't block next week's review. Either way the operator gets an info alert."""
    from . import alerts
    (key, value), = prop["changes"].items()
    try:
        new = methodology.auto_apply(s, prop["id"], now=now)
    except methodology.MethodologyError as e:
        methodology.reject(s, prop["id"], f"auto-apply refused: {e}")
        alerts.send(s, db, "info", "selection-auto-apply", f"Selection review: proposal {prop['id']} not applied",
                    f"{key} → {value} refused: {e}. The proposal is rejected; nothing changed.")
        return {"summary": f"proposal {prop['id']} not applied: {e}", "applied": None}
    entry = new["changelog"][-1]
    v = new["version"]
    alerts.send(s, db, "info", "selection-auto-apply", f"Methodology v{v}: {key} auto-applied",
                f"{entry['change']['old']} → {entry['change']['new']} (proposal {prop['id']}; evidence "
                f"{methodology.change_url(s, v)}). New rounds use it; earlier rounds keep their recorded version.",
                action=f"methodology.json is now v{v} in the running (live) checkout, a detached HEAD",
                resume=f"Carry it to main before the next deploy (a deploy resets the live checkout; the drift "
                       f"detector would then pause new rounds): copy agentpitbench/methodology.json from the live "
                       f"checkout into the main checkout, then there: git add agentpitbench/methodology.json && "
                       f"git commit -m 'Methodology v{v}: {key} {entry['change']['old']} -> "
                       f"{entry['change']['new']} (automatic weekly review)', then ship. "
                       f"Undo: agentpitbench method revert {v}")
    return {"summary": f"v{v}: {key} {entry['change']['old']} → {entry['change']['new']} auto-applied "
                       f"(proposal {prop['id']})", "applied": v}


async def due(s: Settings, db: DB, now: float | None = None) -> dict | None:
    """Sundays, on the first call at or after 19:00 UTC, when selection_review_enabled."""
    if not s.selection_review_enabled:
        return None
    now = time.time() if now is None else now
    d = datetime.fromtimestamp(now, timezone.utc)
    key = f"selection_review_{d.strftime('%G-W%V')}"
    if d.weekday() != WEEKDAY or d.hour < HOUR_UTC or db.get(key):
        return None
    db.put(key, True)
    return await review(s, db, now)
