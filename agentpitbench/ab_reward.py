"""A/B v2 milestone M3 (docs/ab-algorithm-v2-plan.md §2.1): reward v2, behind `ab_reward_version` (default 1 = v1,
so live choices are unchanged until the operator switches it).

1. R as in v1 (weighted blend, our own replies/quotes removed), capped at the kind's trailing-90-day p99 once that
   window holds ≥ 50 posts of the kind (below that: no cap). The cap is logged when computed.
2. y = log1p(R).
3. Covariate adjustment (CUPED-style), within kind: ỹ = y − (x − x̄_kind)·β̂. x holds only what is known before
   posting: hook situation, UTC hour bucket (4), log followers from that day's account_metrics row, round exhibition flag,
   log market volume24hr at round start (missing values: column mean plus a missing indicator). β̂ is a ridge fit
   on all posts of the last 90 days, whatever their arm, cross-fitted in 2 folds by post_key hash: a post is
   adjusted with the other fold's β̂, so it never adjusts itself. Arms are randomised independently of x, so the
   adjustment can't bias an arm comparison; it only removes noise. Impressions and anything measured after
   posting are never covariates (they're outcomes the copy itself moves).
The 90-day standardisation and half-life are applied by ab.py when the flag is 2.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import statistics
from datetime import datetime, timezone

from . import xstats
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

WINDOW_DAYS = 90
CAP_MIN_POSTS = 50
RIDGE_LAMBDA = 1.0          # on standardised columns
MIN_FIT = 8                 # fewer posts in the other fold: no adjustment for that fold
HOUR_BUCKETS = 4
FORBIDDEN = {"impressions", "engagements", "video_views", "profile_clicks", "likes", "replies", "reposts", "quotes",
             "bookmarks", "link_clicks"}   # post-treatment: never a covariate (test)


def fold(post_key: str) -> int:
    return hashlib.sha256(post_key.encode()).digest()[0] % 2


def quantile(xs: list[float], q: float) -> float:
    v = sorted(xs)
    h = (len(v) - 1) * q
    lo = math.floor(h)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (h - lo)


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        f = M[c][c]
        M[c] = [x / f for x in M[c]]
        for r in range(n):
            if r != c and M[r][c]:
                g = M[r][c]
                M[r] = [x - g * y for x, y in zip(M[r], M[c])]
    return [M[i][n] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def ridge(X: list[list[float]], y: list[float], lam: float = RIDGE_LAMBDA) -> tuple[list[float], list[float], list[float], float]:
    """(β on the raw columns, column means, column sds, mean y). Columns are standardised for the penalty;
    constant columns get β = 0."""
    n, p = len(X), len(X[0]) if X else 0
    mx = [sum(r[j] for r in X) / n for j in range(p)]
    sx = [math.sqrt(sum((r[j] - mx[j]) ** 2 for r in X) / n) for j in range(p)]
    my = sum(y) / n
    keep = [j for j in range(p) if sx[j] > 1e-9]
    Z = [[(r[j] - mx[j]) / sx[j] for j in keep] for r in X]
    A = [[sum(Z[i][a] * Z[i][b] for i in range(n)) + (lam if a == b else 0.0) for b in range(len(keep))]
         for a in range(len(keep))]
    rhs = [sum(Z[i][a] * (y[i] - my) for i in range(n)) for a in range(len(keep))]
    bz = _solve(A, rhs) if keep else []
    beta = [0.0] * p
    for k, j in enumerate(keep):
        beta[j] = bz[k] / sx[j]
    return beta, mx, sx, my


class Features:
    """Pre-treatment covariates of a post, from the database. Columns are fixed by the training rows."""

    def __init__(self, db: DB):
        self.db = db
        self._am = {r["day"]: r["followers"] for r in db.q(
            "SELECT day, followers FROM account_metrics WHERE followers IS NOT NULL ORDER BY day")}
        self._round: dict = {}
        self._sit: dict = {}

    def followers(self, t: float) -> float | None:
        day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        prior = [d for d in self._am if d <= day]
        return math.log1p(self._am[max(prior)]) if prior else None

    def round_info(self, round_id) -> tuple[float, float | None]:
        if round_id is None:
            return 0.0, None
        if round_id not in self._round:
            r = self.db.one("SELECT exhibition, snapshot_json FROM rounds WHERE round_id=?", round_id)
            vol = None
            if r:
                try:
                    v = float((json.loads(r["snapshot_json"] or "{}") or {}).get("volume24hr") or 0)
                    vol = math.log1p(v) if v > 0 else None
                except (ValueError, TypeError):
                    vol = None
            self._round[round_id] = (float(r["exhibition"] or 0) if r else 0.0, vol)
        return self._round[round_id]

    def situation(self, post_key: str) -> str | None:
        if post_key not in self._sit:
            try:
                r = self.db.one("SELECT situation FROM ab_assignments WHERE post_key=? AND situation IS NOT NULL"
                                " LIMIT 1", post_key)
            except Exception:   # a database from before M2 has no situation column
                r = None
            self._sit[post_key] = r["situation"] if r else None
        return self._sit[post_key]

    def raw(self, r: dict) -> dict:
        t = r["posted_at"]
        ex, vol = self.round_info(r.get("round_id"))
        return {"kind": r.get("kind") or "", "situation": self.situation(r.get("post_key") or ""),
                "hour": int(datetime.fromtimestamp(t, timezone.utc).hour // (24 // HOUR_BUCKETS)),
                "followers": self.followers(t), "exhibition": ex, "volume": vol}


def design(raws: list[dict], spec: dict | None = None) -> tuple[list[list[float]], dict]:
    """One-hot situation/hour (first level dropped), numeric followers/volume with a missing indicator, exhibition.
    Kind is not a column: the adjustment works within kind (columns are centred per kind, see Model), because z is
    standardised per kind anyway."""
    if spec is None:
        cats = {c: sorted({str(x[c]) for x in raws})[1:] for c in ("situation", "hour")}
        means = {c: (statistics.mean([x[c] for x in raws if x[c] is not None])
                     if any(x[c] is not None for x in raws) else 0.0) for c in ("followers", "volume")}
        spec = {"cats": cats, "means": means}
    rows = []
    for x in raws:
        row = []
        for c, levels in spec["cats"].items():
            row += [1.0 if str(x[c]) == lv else 0.0 for lv in levels]
        for c, m in spec["means"].items():
            row += [x[c] if x[c] is not None else m, 0.0 if x[c] is not None else 1.0]
        row.append(float(x["exhibition"]))
        rows.append(row)
    return rows, spec


def y_of(s: Settings, r: dict, self_replies: int, self_quotes: int, cap: float | None) -> float:
    v = dict(r)
    v["replies"] = max(0, (v.get("replies") or 0) - self_replies)
    v["quotes"] = max(0, (v.get("quotes") or 0) - self_quotes)
    R = max(0.0, sum(w * (v.get(k) or 0) for k, w in s.ab_weights.items()))
    return math.log1p(min(R, cap) if cap is not None else R)


class Model:
    """Reward v2 for one snapshot bucket at time `now`: caps, the two cross-fitted ridge fits, and ỹ per post."""

    def __init__(self, db: DB, s: Settings, now: float, bucket: str = "24h", days: float = WINDOW_DAYS):
        from .ab import _self_counts
        self.db, self.s, self.bucket = db, s, bucket
        self._self = _self_counts
        self.feat = Features(db)
        rows = xstats.rewards(db, bucket, since=now - days * 86400, until=now, s=s)
        self.caps = self._caps(rows)
        self.rows = rows
        self.y = [y_of(s, r, *_self_counts(db, r["tweet_id"]), self.caps.get(r["kind"])) for r in rows]
        self.raws = [self.feat.raw(r) for r in rows]
        X, self.spec = design(self.raws) if rows else ([], None)
        # centre every column within kind: x only explains variation around the kind's mean (x is pre-treatment,
        # so these means use no outcome)
        self.xmean: dict[str, list[float]] = {}
        for k in {r["kind"] for r in rows}:
            idx = [i for i, r in enumerate(rows) if r["kind"] == k]
            self.xmean[k] = [sum(X[i][j] for i in idx) / len(idx) for j in range(len(X[0]))]
        self.X = [[v - m for v, m in zip(X[i], self.xmean[r["kind"]])] for i, r in enumerate(rows)]
        self.fits: dict[int, tuple | None] = {}
        for f in (0, 1):
            idx = [i for i, r in enumerate(rows) if fold(r["post_key"] or r["tweet_id"]) != f]   # the OTHER fold
            ym: dict[str, list[float]] = {}
            for i in idx:
                ym.setdefault(rows[i]["kind"], []).append(self.y[i])
            yc = [self.y[i] - statistics.mean(ym[rows[i]["kind"]]) for i in idx]   # within-kind, that fold only
            self.fits[f] = ridge([self.X[i] for i in idx], yc) if len(idx) >= MIN_FIT else None
        self._adj = {r["tweet_id"]: self.adjust_row(r, self.y[i], self.X[i]) for i, r in enumerate(rows)}
        # Guard: adjust only if the cross-fitted ỹ has less within-kind variance than y over the window. With few
        # posts and weak covariates the two folds' fits are noise and adjusting would add variance. The check
        # ignores arms, so it can't favour one.
        ss_y, ss_a = within_ss([(r["kind"], self.y[i], self._adj[r["tweet_id"]]) for i, r in enumerate(rows)])
        self.cv_reduction_pct = round(100 * (1 - ss_a / ss_y), 1) if ss_y else None
        self.applied = bool(ss_y) and ss_a < ss_y
        if not self.applied:
            self.fits = {0: None, 1: None}
            self._adj = {r["tweet_id"]: self.y[i] for i, r in enumerate(rows)}

    def _caps(self, rows) -> dict[str, float]:
        caps = {}
        by: dict[str, list[float]] = {}
        for r in rows:
            v = dict(r)
            sr, sq = self._self(self.db, r["tweet_id"])
            v["replies"] = max(0, (v.get("replies") or 0) - sr)
            v["quotes"] = max(0, (v.get("quotes") or 0) - sq)
            by.setdefault(r["kind"], []).append(max(0.0, sum(w * (v.get(k) or 0) for k, w in self.s.ab_weights.items())))
        for k, Rs in by.items():
            if len(Rs) >= CAP_MIN_POSTS:
                caps[k] = quantile(Rs, 0.99)
                log.info("A/B reward v2: %s %s cap R <= %.2f (p99 of %d posts)", self.bucket, k, caps[k], len(Rs))
        return caps

    def adjust_row(self, r: dict, y: float, x: list[float] | None = None) -> float:
        fit = self.fits.get(fold(r["post_key"] or r["tweet_id"]))
        if fit is None or self.spec is None:
            return y
        if x is None:   # a post outside the fitted window: centre with its kind's means (none: no adjustment)
            raw = design([self.feat.raw(r)], self.spec)[0][0]
            km = self.xmean.get(r.get("kind") or "")
            if km is None:
                return y
            x = [v - m for v, m in zip(raw, km)]
        return y - sum(b * xi for b, xi in zip(fit[0], x))

    def y_adj(self, r: dict) -> float:
        """ỹ for a post (r: reward_inputs plus post_key/kind/round_id/posted_at)."""
        if r["tweet_id"] in self._adj:
            return self._adj[r["tweet_id"]]
        y = y_of(self.s, r, *self._self(self.db, r["tweet_id"]), self.caps.get(r["kind"]))
        return self.adjust_row(r, y)

    def kind_values(self, kind: str) -> list[float]:
        return [self._adj[r["tweet_id"]] for r in self.rows if r["kind"] == kind]


def within_ss(rows: list[tuple[str, float, float]]) -> tuple[float, float]:
    """Within-kind sums of squares of two outcomes: rows (kind, a, b)."""
    by: dict[str, list[tuple[float, float]]] = {}
    for k, a, b in rows:
        by.setdefault(k, []).append((a, b))
    ssa = ssb = 0.0
    for v in by.values():
        ma, mb = statistics.mean(a for a, _ in v), statistics.mean(b for _, b in v)
        ssa += sum((a - ma) ** 2 for a, _ in v)
        ssb += sum((b - mb) ** 2 for _, b in v)
    return ssa, ssb


def variance_report(db: DB, s: Settings, now: float, bucket: str = "24h", days: float = WINDOW_DAYS,
                    since: float | None = None) -> dict:
    """Pooled within-kind variance of y (v1) and of the cap + cross-fitted adjustment ỹ (v2), on the same posts.
    z divides by the kind's sd, so 1 − var(ỹ)/var(y) is the noise the adjustment removes in v1's sd units."""
    m = Model(db, s, now, bucket, days)
    from .ab import _self_counts
    keep = [i for i, r in enumerate(m.rows) if since is None or r["posted_at"] >= since]
    by: dict[str, tuple[list[float], list[float]]] = {}
    for i in keep:
        r = m.rows[i]
        y1 = y_of(s, r, *_self_counts(db, r["tweet_id"]), None)
        a, b = by.setdefault(r["kind"], ([], []))
        a.append(y1)
        b.append(m._adj[r["tweet_id"]])
    ss1 = ss2 = 0.0
    n = df = 0
    for a, b in by.values():
        if len(a) < 2:
            continue
        ma, mb = statistics.mean(a), statistics.mean(b)
        ss1 += sum((x - ma) ** 2 for x in a)
        ss2 += sum((x - mb) ** 2 for x in b)
        n += len(a)
        df += len(a) - 1
    out = {"n": n, "var_v1": None, "var_v2": None, "reduction_pct": None,
           "caps": {k: round(v, 2) for k, v in m.caps.items()},
           "adjustment_applied": m.applied, "cross_fitted_reduction_pct": m.cv_reduction_pct}
    if df and ss1:
        out.update(var_v1=round(ss1 / df, 4), var_v2=round(ss2 / df, 4), reduction_pct=round(100 * (1 - ss2 / ss1), 1))
    return out
