"""A/B reporting: `ab status`, the daily digest (var/export/ab-daily.json) and the public /experiments/ data.

The public view (operator decision) shows engagement numbers only from posts older than ab_public_delay_days, so
a live test can't be gamed, and never the generator's model, prompt or output ids.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from . import ab
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def _text(db: DB, arm: dict, before: float | None = None) -> str:
    """An already-posted example, else the template with its placeholders (never rendered against a live round)."""
    ex = ab.example(db, arm["arm_id"], before)
    if ex:
        return ex
    if arm["is_control"]:
        return ab.CONTROL_TEXT.get(arm["slot"], "(today's copy)") if arm.get("code_key") is None else repr(arm["code_key"])
    if arm.get("code_key") is not None:
        return repr(arm["code_key"])
    t = json.loads(arm["template_json"] or "{}")
    if "situations" in t:
        return " | ".join(f"{k}: {v}" for k, v in t["situations"].items())
    return t.get("text", "")


def status(db: DB, s: Settings, slot: str | None = None, now: float | None = None) -> dict[str, list[dict]]:
    ab.seed(db)
    out = {}
    for sl in ([slot] if slot else list(ab.SLOTS)):
        out[sl] = [{"arm_id": a["arm_id"], "status": a["status"], "control": bool(a["is_control"]), "n": a["n"],
                    "n_eff": a["n_eff"], "mean": round(a["mean"], 3), "sd": round(a["sd"], 3),
                    "p_best": None if a["p_best"] is None else round(a["p_best"], 3),
                    "example": _text(db, a)} for a in ab.posterior(db, s, sl, now, status=None)]
    return out


def print_status(db: DB, s: Settings, slot: str | None = None) -> None:
    for sl, rows in status(db, s, slot).items():
        print(f"{sl}:")
        for r in rows:
            pb = "-" if r["p_best"] is None else f"{r['p_best']:.2f}"
            print(f"  #{r['arm_id']:<4} {r['status']:<8}{'C' if r['control'] else ' '} n={r['n']:<4} "
                  f"n_eff={r['n_eff']:<7} {r['mean']:+.2f} ± {r['sd']:.2f}  p_best={pb}  {r['example'][:90]!r}")


def digest(db: DB, s: Settings, date: str | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    date = date or _day(now)
    prev = db.get(f"ab_digest_{(datetime.strptime(date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')}") or {}
    t0 = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    slots, ready = {}, []
    for sl in ab.SLOTS:
        rows = [a for a in ab.posterior(db, s, sl, now) if a["status"] == "live"]
        before = {str(a["arm_id"]): a["mean"] for a in (prev.get("slots") or {}).get(sl, [])}
        slots[sl] = [{"arm_id": a["arm_id"], "control": bool(a["is_control"]), "n": a["n"], "mean": round(a["mean"], 3),
                      "sd": round(a["sd"], 3), "p_best": a["p_best"] and round(a["p_best"], 3),
                      "change": (round(a["mean"] - before[str(a["arm_id"])], 3) if str(a["arm_id"]) in before else None)}
                     for a in rows]
        if len(rows) > 1 and all(a["n"] >= ab._min_n(s, sl) for a in rows) and \
                any((a["p_best"] or 0) >= 0.9 or (a["p_best"] or 0) <= 0.05 for a in rows):
            for a in rows:
                if not a["is_control"] and (a["p_best"] or 0) <= 0.05:
                    ready.append({"slot": sl, "arm_id": a["arm_id"], "suggest": f"agentpitbench ab retire {a['arm_id']}"})
    fallbacks = [dict(r) for r in db.q(
        "SELECT post_key, slot, arm_id, realized_arm_id FROM ab_assignments WHERE arm_id<>realized_arm_id"
        " AND assigned_at>=? AND assigned_at<?", t0, t0 + 86400)]
    excluded = [dict(r) for r in db.q(
        "SELECT DISTINCT a.post_key, COALESCE(a.excluded, 'boosted') why FROM ab_assignments a"
        " LEFT JOIN tweets t ON t.post_key=a.post_key WHERE a.excluded IS NOT NULL OR t.boosted=1")]
    out = {"date": date, "slots": slots, "ready_to_judge": ready, "generator": db.get(f"ab_gen_{date}") or
           {"status": "not run"}, "fallbacks": fallbacks, "excluded": excluded,
           "site_clicks": "measured" if db.get("cf_last_ok") else "no Cloudflare token yet: site arms earn no reward"}
    try:   # A/B v2 M0: the data audit's headline numbers (dead slots under post_kinds, posts/day per slot)
        from . import ab_audit
        out["audit"] = ab_audit.summary(ab_audit.audit(db, s, until=now))
        from . import ab_reward   # M3: how much noise reward v2 removes on the same posts (operator-only)
        out["reward_v2"] = {"active": s.ab_reward_version >= 2,
                            **ab_reward.variance_report(db, s, now, since=ab_audit.ts(s.ab_audit_since))}
    except Exception as e:   # never blocks the digest
        log.warning("A/B audit for the digest failed: %s", e)
    return out


def digest_due(db: DB, s: Settings, now: float | None = None) -> dict | None:
    """At ab_digest_hour_utc, once per date: log it and write var/export/ab-daily.json."""
    now = now or time.time()
    d = datetime.fromtimestamp(now, timezone.utc)
    date = d.strftime("%Y-%m-%d")
    if d.hour < s.ab_digest_hour_utc or db.get(f"ab_digest_{date}"):
        return None
    out = digest(db, s, date, now)
    s.export_dir.mkdir(parents=True, exist_ok=True)
    (s.export_dir / "ab-daily.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    db.put(f"ab_digest_{date}", {"slots": out["slots"]})
    log.info("A/B digest %s: %s", date, json.dumps({k: v for k, v in out.items() if k != "slots"})[:2000])
    return out


def experiments(db: DB, s: Settings, now: float | None = None) -> dict:
    """The public /experiments/ data: numbers only from posts older than ab_public_delay_days; no model, prompt
    or output ids. Also written to var/export/experiments.json."""
    now = now or time.time()
    until = now - s.ab_public_delay_days * 86400
    ab.seed(db)
    out = {"as_of": _day(until), "delay_days": s.ab_public_delay_days,
           "weights": dict(s.ab_weights), "half_life_days": s.ab_half_life_days, "min_posts": s.ab_min_posts,
           "slots": []}
    for sl in ab.SLOTS:
        rows = ab.posterior(db, s, sl, now, until=until, status=None)
        arms = []
        for a in rows:
            if a["status"] == "queued":
                continue
            arms.append({"arm": a["arm_id"], "control": bool(a["is_control"]), "status": a["status"],
                         "example": _text(db, a), "n": a["n"], "mean": round(a["mean"], 3),
                         "sd": round(a["sd"], 3), "p_best": None if a["p_best"] is None else round(a["p_best"], 3),
                         "live_from": _day(a["live_at"]) if a.get("live_at") else None,
                         "retired_on": _day(a["retired_at"]) if a.get("retired_at") else None})
        out["slots"].append({"slot": sl, "arms": arms})
    s.export_dir.mkdir(parents=True, exist_ok=True)
    (s.export_dir / "experiments.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out
