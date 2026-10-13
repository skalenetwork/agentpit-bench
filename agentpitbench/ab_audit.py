"""A/B v2 milestone M0 (docs/ab-algorithm-v2-plan.md §3): the data audit behind `agentpitbench ab audit`.

Read-only over the database: per slot posts/day and live arms, which slots still get posts under bench.toml
`post_kinds`, the reward distribution per kind, a self-engagement spot check, and covariate coverage. The window
starts at `ab_audit_since` (default 2026-10-12: X Premium made reach jump ~10x that night, docs/virality-2-spec.md
§2b, so only post-step data is a valid baseline). `--since 2026-10-09` reproduces the plan's §0 table.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime, timezone

from . import ab, xstats
from .config import Settings
from .db import DB

# which tweet kinds feed each slot ("site" = the site's round pages, not an X post)
SLOT_KINDS: dict[str, set[str]] = {
    "reveal.hook": {"reveal"}, "reveal.cta": {"reveal"}, "reveal.media": {"reveal"},
    "results.hook": {"results"}, "results.cta": {"results"}, "results.media": {"results"}, "tone": {"results"},
    "tags": {"reveal", "results"},
    "press.head": {"press"},   # press statements are replies to the results post
    "generic.cta": set(ab.GENERIC_KINDS),
    "site.round_cta": {"site"},
}
REPLY_KINDS = {"press"}        # posted as replies: dead whenever post_kinds is set (it allows no replies)


def ts(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def quantile(xs: list[float], q: float) -> float | None:
    """Linear interpolation between order statistics (numpy's default)."""
    if not xs:
        return None
    v = sorted(xs)
    h = (len(v) - 1) * q
    lo = math.floor(h)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (h - lo)


def _sd(xs: list[float]) -> float | None:
    return statistics.stdev(xs) if len(xs) > 1 else None


def post_rows(db: DB, s: Settings, since: float, until: float | None = None, bucket: str = "24h") -> list[dict]:
    """Every eligible post with a fresh `bucket` snapshot: counts, our own replies/quotes, R before and after the
    self-engagement subtraction, and y = log1p(R) exactly as ab.blend computes it."""
    out = []
    for r in xstats.rewards(db, bucket, since=since, until=until, s=s):
        sr, sq = ab._self_counts(db, r["tweet_id"])
        raw = sum(w * (r.get(k) or 0) for k, w in s.ab_weights.items())
        y = ab.blend(s, r, sr, sq)
        out.append({**r, "self_replies": sr, "self_quotes": sq, "R_raw": raw, "R": math.expm1(y), "y": y})
    return out


def dist(rows: list[dict]) -> dict:
    ys = [r["y"] for r in rows]
    imps = [r["impressions"] for r in rows if r["impressions"] is not None]
    return {"n": len(rows), "y_mean": round(statistics.mean(ys), 3) if ys else None,
            "y_sd": None if _sd(ys) is None else round(_sd(ys), 3),
            "zero_share": round(sum(1 for y in ys if y == 0) / len(ys), 3) if ys else None,
            "imps_median": quantile(imps, 0.5), "imps_p90": quantile(imps, 0.9), "imps_max": max(imps) if imps else None}


def slot_report(db: DB, s: Settings, since: float, until: float) -> list[dict]:
    """Per slot: assignments and posted assignments per day in the window, live arms, and whether bench.toml
    post_kinds still lets the slot's kinds reach X (dead = no kind allowed; reduced = some kinds dropped)."""
    days = max(1e-9, (until - since) / 86400)
    allowed = set(s.post_kinds or [])
    out = []
    for sl in ab.SLOTS:
        kinds = SLOT_KINDS.get(sl, set())
        n = db.one("SELECT COUNT(*) c FROM ab_assignments WHERE slot=? AND assigned_at>=? AND assigned_at<?",
                   sl, since, until)["c"]
        posted = db.one("SELECT COUNT(*) c FROM ab_assignments a JOIN tweets t ON t.post_key=a.post_key"
                        " WHERE a.slot=? AND t.tweet_id IS NOT NULL AND t.posted_at>=? AND t.posted_at<?",
                        sl, since, until)["c"]
        if kinds == {"site"}:
            state, live_kinds = "site (not X)", kinds
        elif not allowed:
            state, live_kinds = "live", kinds
        else:
            live_kinds = (kinds & allowed) - REPLY_KINDS
            state = "dead" if not live_kinds else ("reduced" if live_kinds != kinds else "live")
        top = [k for k in live_kinds if k != "site"]
        hist = db.one(f"SELECT COUNT(*) c FROM tweets WHERE tweet_id IS NOT NULL AND reply_to IS NULL"
                      f" AND kind IN ({','.join('?' * len(top)) or 'NULL'}) AND posted_at>=? AND posted_at<?",
                      *top, since, until)["c"] if top else 0
        out.append({"slot": sl, "state": state, "kinds_on_x": sorted(live_kinds),
                    "live_arms": len(ab.arms(db, sl)), "assignments": n, "posted": posted,
                    "posts_per_day": round(posted / days, 2),
                    "kind_posts_per_day": round(hist / days, 2) if top else None})
    return out


def self_check(rows: list[dict], k: int = 10) -> list[dict]:
    """The posts the self-engagement subtraction touched most, then the most recent ones, for a hand check."""
    pick = sorted(rows, key=lambda r: (-(r["self_replies"] + r["self_quotes"]), -(r["posted_at"] or 0)))[:k]
    return [{"post_key": r["post_key"], "kind": r["kind"], "replies": r["replies"], "quotes": r["quotes"],
             "self_replies": r["self_replies"], "self_quotes": r["self_quotes"],
             "R_raw": r["R_raw"], "R": round(r["R"], 6)} for r in pick]


def covariates(db: DB, rows: list[dict]) -> dict:
    """How much of each M3 covariate exists for the window's posts (docs/ab-algorithm-v2-plan.md §2.1 item 4)."""
    n = len(rows) or 1
    days = {datetime.fromtimestamp(r["posted_at"], timezone.utc).strftime("%Y-%m-%d") for r in rows}
    am = {r["day"] for r in db.q("SELECT day FROM account_metrics WHERE followers IS NOT NULL")}
    in_round = [r for r in rows if r["round_id"] is not None]
    vol = 0
    for r in in_round:
        snap = db.one("SELECT snapshot_json FROM rounds WHERE round_id=?", r["round_id"])
        try:
            m = json.loads(snap["snapshot_json"]) if snap else {}
        except ValueError:
            m = {}
        if float(m.get("volume24hr") or 0) > 0:
            vol += 1
    has_sit = "situation" in {r["name"] for r in db.q("PRAGMA table_info(ab_assignments)")}
    hook = db.one("SELECT COUNT(*) c, " + ("SUM(situation IS NOT NULL)" if has_sit else "0") + " s FROM ab_assignments"
                  " WHERE slot IN ('reveal.hook','results.hook')")
    return {"hour_bucket": 1.0, "kind": 1.0,
            "followers (account_metrics that day)": round(sum(1 for r in rows if datetime.fromtimestamp(
                r["posted_at"], timezone.utc).strftime("%Y-%m-%d") in am) / n, 3),
            "account_metrics days / posting days": f"{len(days & am)}/{len(days)}",
            "exhibition (round posts)": round(len(in_round) / n, 3),
            "market volume24hr > 0 at round start": round(vol / max(1, len(in_round)), 3),
            "situation stored (hook assignments)": f"{hook['s'] or 0}/{hook['c']}"}


def audit(db: DB, s: Settings, since: str | None = None, until: float | None = None) -> dict:
    since = since or s.ab_audit_since
    t0, t1 = ts(since), until or time.time()
    rows = post_rows(db, s, t0, t1)
    kinds = sorted({r["kind"] for r in rows})
    first = min((r["posted_at"] for r in rows), default=t0)
    results_days = max(1e-9, (t1 - max(t0, first)) / 86400)
    slots = slot_report(db, s, t0, t1)
    return {
        "since": since, "until": datetime.fromtimestamp(t1, timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "post_kinds": list(s.post_kinds or []),
        "all": dist(rows),
        "by_kind": {k: dist([r for r in rows if r["kind"] == k]) for k in kinds},
        "results_per_day": round(sum(1 for r in rows if r["kind"] == "results") / results_days, 2),
        "slots": slots,
        "dead_slots": [x["slot"] for x in slots if x["state"] == "dead"],
        "reduced_slots": [x["slot"] for x in slots if x["state"] == "reduced"],
        "slots_with_3_live_arms": sum(1 for x in slots if x["live_arms"] >= 3),
        "self_engagement_check": self_check(rows),
        "covariates": covariates(db, rows),
    }


def summary(a: dict) -> dict:
    """The compact form the daily digest carries."""
    return {"since": a["since"], "posts_24h": a["all"]["n"], "imps_median": a["all"]["imps_median"],
            "zero_share": a["all"]["zero_share"], "dead_slots": a["dead_slots"], "reduced_slots": a["reduced_slots"],
            "posts_per_day": {x["slot"]: x["kind_posts_per_day"] for x in a["slots"]}}


def print_audit(a: dict) -> None:
    print(f"A/B audit {a['since']} -> {a['until']} UTC; post_kinds = {a['post_kinds'] or 'all'}")
    al = a["all"]
    print(f"posts with a 24 h snapshot: {al['n']}; impressions median {al['imps_median']}, p90 {al['imps_p90']}, "
          f"max {al['imps_max']}; zero external engagement {al['zero_share']}; y mean {al['y_mean']} sd {al['y_sd']}")
    print(f"results posts/day: {a['results_per_day']}; slots with 3 live arms: {a['slots_with_3_live_arms']}"
          f" of {len(a['slots'])}")
    print("\nreward by kind:")
    for k, d in a["by_kind"].items():
        print(f"  {k:<11} n={d['n']:<4} y {d['y_mean']} ± {d['y_sd']}  zeros {d['zero_share']}  "
              f"imps med {d['imps_median']} p90 {d['imps_p90']} max {d['imps_max']}")
    print("\nslots (posts/day = top-level posts of the slot's kinds that X still gets):")
    for x in a["slots"]:
        print(f"  {x['slot']:<15} {x['state']:<13} arms={x['live_arms']} assigned={x['assignments']:<4} "
              f"posted={x['posted']:<4} kind posts/day={x['kind_posts_per_day']}  kinds on X={','.join(x['kinds_on_x'])}")
    print(f"dead slots: {', '.join(a['dead_slots']) or 'none'}; reduced: {', '.join(a['reduced_slots']) or 'none'}")
    print("\nself-engagement spot check (R_raw -> R after removing our own replies/quotes):")
    for r in a["self_engagement_check"]:
        print(f"  {r['post_key']:<26} {r['kind']:<10} replies {r['replies']} (self {r['self_replies']}) "
              f"quotes {r['quotes']} (self {r['self_quotes']})  R {r['R_raw']} -> {r['R']:g}")
    print("\ncovariate coverage:")
    for k, v in a["covariates"].items():
        print(f"  {k}: {v}")
