"""Posting-cadence policy (docs/ai-automation-spec.md §9): which optional posts go out, and when. Never
methodology, never copy (A/B owns copy), never the reveal or results post of a round (skipping those would be
cherry-picking the record).

Readers call `get(s, db, key)`: the applied policy in kv['cadence_policy'] if any, else the config default. The
daily 07:00 UTC task decides in one of three modes:

- shadow: under a week of 24 h X snapshots: no model call, the inputs are recorded for the digest;
- recommend: enough data, `cadence_apply` off: one opai call, the clamped recommendation goes in the digest;
- apply: enough data and `cadence_apply` on: the recommendation is applied (at most one knob a day, clamped to
  BOUNDS, logged in policy_changes).

Nothing runs unless `cadence_enabled`. With the defaults (both flags off, no kv) every reader returns today's
config value, so behaviour is unchanged.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

# knob -> (kind, lo, hi): the hard bounds. Anything not listed here can never be changed by the policy.
BOUNDS: dict[str, tuple] = {
    "press_max_replies": ("int", 0, 1),
    "post_replay": ("bool", None, None),
    "post_milestones": ("bool", None, None),
    "post_awards": ("bool", None, None),
    "post_contrarian": ("bool", None, None),
    "race_hour_utc": ("int", 12, 22),
    "optional_posts_per_day": ("int", 0, 8),
}
# defaults for knobs without a Settings attribute (press_max_replies and race_hour_utc come from Settings)
DEFAULTS = {"post_replay": True, "post_milestones": True, "post_awards": True, "post_contrarian": True,
            "optional_posts_per_day": 8}
# tweets.kind values the optional_posts_per_day cap counts and gates. reveal/results are never in here.
OPTIONAL_KINDS = ("replay", "milestone", "awards", "contrarian", "press")
NEVER = ("reveal", "results")

MIN_DAYS = 7          # a week of 24 h snapshots before the policy decides anything
MIN_SNAPSHOTS = 20
HOUR_UTC = 7
KV = "cadence_policy"
LAST = "cadence_last"


# ---------------------------------------------------------------- reading

def defaults(s: Settings) -> dict:
    return {"press_max_replies": s.press_max_replies, "race_hour_utc": s.race_hour_utc, **DEFAULTS}


def current(s: Settings, db: DB) -> dict:
    """The policy in force: config defaults overlaid with the applied kv policy (clamped again on read)."""
    out = defaults(s)
    applied = db.get(KV) or {}
    if isinstance(applied, dict):
        out.update(clamp(applied))
    return out


def get(s: Settings, db: DB | None, key: str):
    if key not in BOUNDS:
        raise KeyError(f"{key} is not a cadence knob")
    if db is None:
        return defaults(s)[key]
    try:
        return current(s, db)[key]
    except Exception:
        log.exception("cadence policy read failed; config default for %s", key)
        return defaults(s)[key]


def optional_today(db: DB, now: float | None = None) -> int:
    now = time.time() if now is None else now
    d0 = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    q = ",".join("?" * len(OPTIONAL_KINDS))
    return db.one(f"SELECT COUNT(*) n FROM tweets WHERE kind IN ({q}) AND posted_at>=?", *OPTIONAL_KINDS, d0)["n"]


def allows(s: Settings, db: DB | None, kind: str, now: float | None = None) -> bool:
    """Whether an optional post of this kind may go out now. Non-optional kinds (reveal, results, race, ...)
    are always allowed: the policy cannot touch them."""
    if kind not in OPTIONAL_KINDS or db is None:
        return True
    knob = {"replay": "post_replay", "milestone": "post_milestones", "awards": "post_awards",
            "contrarian": "post_contrarian"}.get(kind)
    if knob and not get(s, db, knob):
        log.info("cadence policy: %s post skipped (%s off)", kind, knob)
        return False
    if kind == "press" and get(s, db, "press_max_replies") < 1:
        return False
    cap = get(s, db, "optional_posts_per_day")
    if optional_today(db, now) >= cap:
        log.info("cadence policy: %s post skipped (optional_posts_per_day=%s reached)", kind, cap)
        return False
    return True


# ---------------------------------------------------------------- bounds

def clamp(policy: dict) -> dict:
    """Unknown keys dropped, bools coerced, ints clamped to BOUNDS."""
    out = {}
    for k, v in (policy or {}).items():
        if k not in BOUNDS:
            continue
        kind, lo, hi = BOUNDS[k]
        if kind == "bool":
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, str) and v.lower() in ("true", "false"):
                out[k] = v.lower() == "true"
            continue
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        out[k] = min(max(n, lo), hi)
    return out


def diff(cur: dict, new: dict) -> dict:
    return {k: v for k, v in new.items() if k in BOUNDS and cur.get(k) != v}


# ---------------------------------------------------------------- inputs and data sufficiency

def sufficiency(db: DB, now: float | None = None) -> dict:
    """Enough X data to decide: the oldest 24 h snapshot of an own post is >= MIN_DAYS old and there are at
    least MIN_SNAPSHOTS of them."""
    now = time.time() if now is None else now
    r = db.one("SELECT MIN(t.posted_at) first, COUNT(*) n FROM tweet_metrics m JOIN tweets t ON t.tweet_id=m.tweet_id"
               " WHERE m.bucket='24h' AND t.boosted=0")
    days = (now - r["first"]) / 86400 if r and r["first"] else 0.0
    n = r["n"] if r else 0
    return {"days": round(days, 2), "snapshots": n, "ok": days >= MIN_DAYS and n >= MIN_SNAPSHOTS,
            "need": {"days": MIN_DAYS, "snapshots": MIN_SNAPSHOTS}}


def payload(s: Settings, db: DB, now: float | None = None) -> dict:
    from . import xstats
    now = time.time() if now is None else now
    since = now - 14 * 86400
    kinds = xstats.by_kind(db, since, "24h", now)
    baselines = {k["kind"]: {"1h": xstats.kind_baseline(db, k["kind"], "1h", 14, now),
                             "24h": xstats.kind_baseline(db, k["kind"], "24h", 14, now)} for k in kinds}
    per_day: dict[str, dict[str, int]] = {}
    for r in db.q("SELECT kind, posted_at FROM tweets WHERE posted_at>=? AND kind IS NOT NULL", since):
        day = datetime.fromtimestamp(r["posted_at"], timezone.utc).strftime("%Y-%m-%d")
        per_day.setdefault(day, {}).setdefault(r["kind"], 0)
        per_day[day][r["kind"]] += 1
    curves = {}
    for kind in OPTIONAL_KINDS:   # how fast the latest optional posts gathered impressions
        rows = db.q("SELECT tweet_id FROM tweets WHERE kind=? AND posted_at>=? AND tweet_id IS NOT NULL "
                    "ORDER BY posted_at DESC LIMIT 3", kind, since)
        curves[kind] = [[{"age_h": round(p["age_s"] / 3600, 1), "impressions": p["impressions"]}
                         for p in xstats.impressions_series(db, r["tweet_id"])][-6:] for r in rows]
    return {"by_kind_24h": kinds, "baselines": baselines, "posts_per_day": dict(sorted(per_day.items())),
            "impressions_curves": curves, "policy": current(s, db),
            "bounds": {k: ({"type": "bool"} if b[0] == "bool" else {"type": "int", "min": b[1], "max": b[2]})
                       for k, b in BOUNDS.items()},
            "never": "round reveal and results posts, methodology, post copy, max_reply_reads"}


# ---------------------------------------------------------------- the daily decision

def validator(cur: dict):
    def v(data: dict) -> str | None:
        pol = data.get("policy")
        if not isinstance(pol, dict):
            return "policy is not an object"
        unknown = [k for k in pol if k not in BOUNDS]
        if unknown:
            return f"unknown knob {unknown[0]}"
        if len(diff(cur, clamp(pol))) > 1:
            return "more than one knob changed"
        if not isinstance(data.get("reason"), str) or not data["reason"].strip() or len(data["reason"]) > 300:
            return "reason missing or too long"
        return None
    return v


def mode(s: Settings, db: DB, now: float | None = None) -> str:
    if not s.cadence_enabled:
        return "off"
    if not sufficiency(db, now)["ok"]:
        return "shadow"
    return "apply" if s.cadence_apply else "recommend"


def _changed_today(db: DB, now: float) -> bool:
    d0 = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return db.one("SELECT 1 FROM policy_changes WHERE applied_at>=?", d0) is not None


def _record(s: Settings, db: DB, out: dict) -> dict:
    db.put(LAST, out)
    try:
        s.export_dir.mkdir(parents=True, exist_ok=True)
        (s.export_dir / "cadence.json").write_text(json.dumps(out, indent=1, default=str))  # private, not on the site
    except OSError:
        log.exception("could not write cadence.json")
    log.info("cadence %s (%s): %s", out["day"], out["mode"], out.get("summary"))
    return out


async def decide(s: Settings, db: DB, now: float | None = None) -> dict:
    """One daily decision. Returns the digest record (also kv['cadence_last'] and var/export/cadence.json)."""
    from . import opai
    now = time.time() if now is None else now
    day = opai.utc_day(now)
    m = mode(s, db, now)
    cur = current(s, db)
    out = {"day": day, "mode": m, "at": now, "policy": cur, "data": sufficiency(db, now)}
    if m == "off":
        return out
    if m == "shadow":
        p = payload(s, db, now)
        out["inputs"] = {"by_kind_24h": p["by_kind_24h"], "posts_per_day": p["posts_per_day"]}
        out["summary"] = (f"shadow: {out['data']['days']:.1f} days / {out['data']['snapshots']} 24 h snapshots "
                          f"(need {MIN_DAYS} / {MIN_SNAPSHOTS}); policy unchanged")
        return _record(s, db, out)
    res = await opai.acall(s, db, "cadence", payload(s, db, now), subject=f"cadence:{day}", validate=validator(cur))
    if res is None:
        out["summary"] = "no recommendation (opai unavailable or output rejected); policy unchanged"
        return _record(s, db, out)
    change = diff(cur, clamp(res.data["policy"]))
    out.update(recommendation=change, reason=res.data["reason"], output_id=res.output_id)
    if not change:
        out["summary"] = "recommendation: keep the current policy"
    elif m == "recommend":
        out["summary"] = f"recommendation (not applied, cadence_apply is off): {change}"
    elif _changed_today(db, now):
        out["summary"] = f"recommendation {change} not applied: a knob already changed today"
    else:
        applied = {**(db.get(KV) or {}), **change}
        db.put(KV, applied)
        db.x("INSERT INTO policy_changes(applied_at,policy_json,reason,output_id) VALUES(?,?,?,?)",
             now, json.dumps({**cur, **change}), res.data["reason"], res.output_id)
        out["applied"] = change
        out["policy"] = current(s, db)
        out["summary"] = f"applied {change}: {res.data['reason']}"
    return _record(s, db, out)


async def due(s: Settings, db: DB, now: float | None = None) -> dict | None:
    """Once a day, on the first call at or after 07:00 UTC, when cadence_enabled."""
    if not s.cadence_enabled:
        return None
    now = time.time() if now is None else now
    d = datetime.fromtimestamp(now, timezone.utc)
    key = f"cadence_{d.strftime('%Y-%m-%d')}"
    if d.hour < HOUR_UTC or db.get(key):
        return None
    db.put(key, True)
    return await decide(s, db, now)


def reset(db: DB) -> None:
    """Operator: drop the applied policy (config defaults apply again)."""
    db.put(KV, {})
