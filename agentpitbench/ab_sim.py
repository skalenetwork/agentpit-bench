"""A/B v2 milestone M1 (docs/ab-algorithm-v2-plan.md §3): simulator and power calculator. Offline only: nothing
here touches live assignment.

The simulator bootstraps real standardised rewards (post-step data from `ab_audit_since` by default), injects a
known challenger effect δ (fixed, or drawn from a fat-tailed prior), and replays allocation policies with a daily
decision check and a one-day reward delay (a post's 24 h reward is visible from the next day's check):

- policies: `v1` (forced exploration to 20 each, then Thompson sampling on v1's independent posterior), `ts`
  (Thompson on the v2 posterior), `ttts` (top-two Thompson, β), `es` (exploration sampling, Kasy & Sautmann),
  `uniform`; v2 policies are mixed with the uniform floor.
- rules: `v1` (the digest's p_best ≥ 0.9 reading once every arm has 20 posts) and `v2` (§2.5: P(δ>0) ≥ p_win
  and expected loss < ε; loser symmetric; futility at max_days or max_posts on the challenger).

Outputs per policy and scenario: winner / loser / no-decision rates, wrong-winner rate, median days to decision,
traffic share on the worse arm. The M1 gate: the v2 rule's false-winner rate under daily peeking ≤ 5%.
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
import time
from dataclasses import dataclass, field, replace

SQRT2 = math.sqrt(2.0)


def Phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


# ---------- power calculator (§0 / §2.4)

Z = {0.05: 1.959964, 0.8: 0.841621}


def n_per_arm(d: float, alpha: float = 0.05, power: float = 0.8, var_factor: float = 1.0) -> float:
    """Posts per arm for a two-arm comparison on the unit-variance z scale: 2 (z_α/2 + z_β)² σ² / d² (≈ 15.7/d²).
    var_factor < 1 is the variance left after covariate adjustment."""
    za = Z.get(alpha) or _inv_phi(1 - alpha / 2)
    zb = Z.get(power) or _inv_phi(power)
    return 2 * (za + zb) ** 2 * var_factor / (d * d)


def _inv_phi(p: float) -> float:
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if Phi(mid) < p else (lo, mid)
    return (lo + hi) / 2


def days_to_detect(posts_per_day: float, d: float, arms: int = 2, var_factor: float = 1.0) -> float | None:
    if posts_per_day <= 0:
        return None
    return n_per_arm(d, var_factor=var_factor) * arms / posts_per_day


def power_lines(slot: str, posts_per_day: float, arms: int = 2, var_factor: float = 1.0,
                max_days: float = 60) -> list[str]:
    out = [f"{slot}: {posts_per_day:.2f} posts/day, {arms} arms" + (f", variance x{var_factor:.2f}" if var_factor != 1 else "")]
    for d in (0.8, 0.5, 0.3, 0.2):
        days = days_to_detect(posts_per_day, d, arms, var_factor)
        n = n_per_arm(d, var_factor=var_factor)
        flag = "" if days is None or days <= max_days else f"  (> {max_days:g} days: don't start)"
        out.append(f"  d = {d}: {math.ceil(n)} posts per arm -> " + ("no posts in the window" if days is None else
                                                                     f"{days:.0f} days") + flag)
    return out


# ---------- data

def load_pool(db, s, since: str | None = None, kinds: list[str] | None = None) -> tuple[list[float], dict]:
    """Standardised y (mean 0, sd 1) of the window's top-level posts: the bootstrap pool. Kinds are pooled
    (post-step data holds too few posts per kind); kinds=None uses every top-level kind."""
    from . import ab_audit
    since = since or s.ab_audit_since
    rows = [r for r in ab_audit.post_rows(db, s, ab_audit.ts(since))
            if (kinds is None or r["kind"] in kinds)
            and db.one("SELECT reply_to FROM tweets WHERE tweet_id=?", r["tweet_id"])["reply_to"] is None]
    ys = [r["y"] for r in rows]
    if len(ys) < 5:
        raise ValueError(f"only {len(ys)} posts since {since}: too few to bootstrap")
    mu, sd = statistics.mean(ys), statistics.stdev(ys)
    pool = [(y - mu) / sd for y in ys]
    info = {"since": since, "n": len(ys), "y_mean": round(mu, 3), "y_sd": round(sd, 3),
            "zero_share": round(sum(1 for y in ys if y == 0) / len(ys), 3),
            "kinds": {k: sum(1 for r in rows if r["kind"] == k) for k in sorted({r["kind"] for r in rows})}}
    return pool, info


def gaussian_pool(n: int = 2000, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    xs = [rng.gauss(0, 1) for _ in range(n)]
    mu, sd = statistics.mean(xs), statistics.stdev(xs)
    return [(x - mu) / sd for x in xs]


# ---------- model

@dataclass
class Params:
    rule: str = "v2"            # v1 | v2
    policy: str = "uniform"     # v1 | ts | ttts | es | uniform
    p_win: float = 0.95
    eps: float = 0.02
    tau: float = 0.3
    kappa0: float = 2.0
    half_life: float = 90.0
    floor: float = 0.15
    beta: float = 0.5
    max_days: int = 45
    max_posts: int = 150
    v1_min_posts: int = 20
    v1_half_life: float = 14.0
    v1_prior_n: float = 2.0
    v1_pbest: float = 0.9
    pbest_draws: int = 200


def _inv(P: list[list[float]]) -> list[list[float]]:
    n = len(P)
    A = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(P)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(A[r][c]))
        A[c], A[piv] = A[piv], A[c]
        f = A[c][c]
        A[c] = [x / f for x in A[c]]
        for r in range(n):
            if r != c and A[r][c]:
                g = A[r][c]
                A[r] = [x - g * y for x, y in zip(A[r], A[c])]
    return [row[n:] for row in A]


def v2_posterior(W: list[float], S: list[float], p: Params) -> tuple[list[float], list[list[float]]]:
    """Posterior of (m0, δ_1..δ_{k-1}) under m0 ~ N(0, 1/κ0), δ_a ~ N(0, τ²), z ~ N(m0 + δ_arm, 1) with decayed
    weights W (sum of weights per arm) and S (weighted sum of z per arm). Returns (mean, covariance)."""
    k = len(W)
    P = [[0.0] * k for _ in range(k)]
    P[0][0] = p.kappa0 + sum(W)
    for a in range(1, k):
        P[0][a] = P[a][0] = W[a]
        P[a][a] = 1 / p.tau ** 2 + W[a]
    b = [sum(S)] + S[1:]
    C = _inv(P)
    return [sum(C[i][j] * b[j] for j in range(k)) for i in range(k)], C


def v1_posterior(W: list[float], S: list[float], p: Params) -> tuple[list[float], list[float]]:
    return [s / (w + p.v1_prior_n) for w, s in zip(W, S)], [1 / math.sqrt(w + p.v1_prior_n) for w in W]


def _chol(C: list[list[float]]) -> list[list[float]]:
    n = len(C)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            v = C[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            L[i][j] = math.sqrt(max(v, 1e-15)) if i == j else v / L[j][j]
    return L


def arm_means_dist(mu: list[float], C: list[list[float]]) -> tuple[list[float], list[list[float]]]:
    """(m0, δ) -> per-arm means θ_a = m0 + δ_a (θ_0 = m0): mean and covariance."""
    k = len(mu)
    T = [[1.0 if (j == 0 or j == a) else 0.0 for j in range(k)] for a in range(k)]
    m = [sum(T[a][j] * mu[j] for j in range(k)) for a in range(k)]
    V = [[sum(T[a][i] * C[i][j] * T[b][j] for i in range(k) for j in range(k)) for b in range(k)] for a in range(k)]
    return m, V


def p_best(m: list[float], V: list[list[float]], rng: random.Random, draws: int) -> list[float]:
    k = len(m)
    if k == 2:
        sd = math.sqrt(max(V[0][0] + V[1][1] - 2 * V[0][1], 1e-15))
        p1 = Phi((m[1] - m[0]) / sd)
        return [1 - p1, p1]
    L = _chol(V)
    wins = [0] * k
    for _ in range(draws):
        e = [rng.gauss(0, 1) for _ in range(k)]
        th = [m[a] + sum(L[a][j] * e[j] for j in range(a + 1)) for a in range(k)]
        wins[max(range(k), key=th.__getitem__)] += 1
    return [w / draws for w in wins]


def allocation(policy: str, pb: list[float], p: Params) -> list[float]:
    """Assignment probabilities from p_best: ts = p_best, ttts = β p_a + (1-β) p_a Σ_{I≠a} p_I/(1-p_I),
    es ∝ p(1-p); then the uniform floor mix p = (1 - k f) π + f."""
    k = len(pb)
    if policy == "uniform":
        return [1 / k] * k
    if policy == "ts":
        pi = pb[:]
    elif policy == "ttts":
        pi = [p.beta * pb[a] + (1 - p.beta) * pb[a] * sum(pb[i] / (1 - pb[i]) for i in range(k) if i != a and pb[i] < 1)
              for a in range(k)]
        if all(x == 0 for x in pi):
            pi = [1 / k] * k
    elif policy == "es":
        pi = [x * (1 - x) for x in pb]
    else:
        raise ValueError(policy)
    tot = sum(pi) or 1.0
    pi = [x / tot for x in pi] if sum(pi) > 0 else [1 / k] * k
    f = min(p.floor, 1 / k)
    return [(1 - k * f) * x + f for x in pi]


def _poisson(rng: random.Random, lam: float) -> int:
    L, n, q = math.exp(-lam), 0, rng.random()
    while q > L:
        n += 1
        q *= rng.random()
    return n


def decide(W, S, counts, p: Params, rng: random.Random) -> tuple[str, int] | None:
    """('winner'|'loser', arm) for the first challenger a rule fires on, else None."""
    k = len(W)
    if p.rule == "v1":
        if min(counts) < p.v1_min_posts:
            return None
        m, sd = v1_posterior(W, S, p)
        V = [[sd[a] ** 2 if a == b else 0.0 for b in range(k)] for a in range(k)]
        pb = p_best(m, V, rng, max(p.pbest_draws, 1000))
        for a in range(1, k):
            if pb[a] >= p.v1_pbest:
                return "winner", a
        for a in range(1, k):
            if pb[a] <= 0.05:
                return "loser", a
        return None
    mu, C = v2_posterior(W, S, p)
    for a in range(1, k):
        m, s = mu[a], math.sqrt(C[a][a])
        if Phi(m / s) >= p.p_win and s * phi(m / s) - m * Phi(-m / s) < p.eps:
            return "winner", a
    for a in range(1, k):
        m, s = mu[a], math.sqrt(C[a][a])
        if Phi(-m / s) >= p.p_win and s * phi(m / s) + m * Phi(m / s) < p.eps:
            return "loser", a
    return None


def run_test(rng: random.Random, pool: list[float], deltas: list[float], p: Params, rate: float) -> dict:
    """One test: arm 0 = control (mean 0), arm a = control + deltas[a]. Daily: check the rule on the data visible
    so far (posts up to yesterday), then post today's Poisson(rate) posts under the policy."""
    k = len(deltas)
    hl = p.v1_half_life if p.rule == "v1" else p.half_life
    keep = 0.5 ** (1 / hl)
    keep1 = 0.5 ** (1 / p.v1_half_life)
    W, S = [0.0] * k, [0.0] * k          # rule's decayed sums (visible data)
    W1, S1 = [0.0] * k, [0.0] * k        # v1 policy's own decayed sums
    counts = [0] * k
    pending: list[tuple[int, float]] = []
    best = max(range(k), key=deltas.__getitem__)
    worse = 0
    total = 0
    for day in range(p.max_days + 1):
        W = [w * keep for w in W]
        S = [s * keep for s in S]
        W1 = [w * keep1 for w in W1]
        S1 = [s * keep1 for s in S1]
        for a, z in pending:
            W[a] += 1
            S[a] += z
            W1[a] += 1
            S1[a] += z
        pending = []
        if day > 0:
            d = decide(W, S, counts, p, rng)
            if d:
                return {"decision": d[0], "arm": d[1], "day": day, "counts": counts, "worse_share": worse / max(1, total)}
        if day == p.max_days or max(counts[1:]) >= p.max_posts:
            break
        if p.policy != "v1":
            mu, C = v2_posterior(W, S, p)
            m, V = arm_means_dist(mu, C)
            probs = allocation(p.policy, p_best(m, V, rng, p.pbest_draws), p)
        for _ in range(_poisson(rng, rate)):
            if p.policy == "v1":
                if min(counts) < p.v1_min_posts:
                    a = min(range(k), key=lambda i: (counts[i], i))
                else:
                    mm, sd = v1_posterior(W1, S1, p)
                    th = [rng.gauss(mm[i], sd[i]) for i in range(k)]
                    a = max(range(k), key=th.__getitem__)
            else:
                u, a = rng.random(), 0
                while a < k - 1 and u > probs[a]:
                    u -= probs[a]
                    a += 1
            counts[a] += 1
            total += 1
            worse += deltas[a] < deltas[best]
            pending.append((a, rng.choice(pool) + deltas[a]))
    return {"decision": "none", "arm": None, "day": None, "counts": counts, "worse_share": worse / max(1, total)}


def t3(rng: random.Random) -> float:
    return rng.gauss(0, 1) / math.sqrt(sum(rng.gauss(0, 1) ** 2 for _ in range(3)) / 3)


def scenario(pool, p: Params, delta: float | str, k: int = 2, rate: float = 3.0, reps: int = 1000,
             seed: int = 1, prior_scale: float = 0.1) -> dict:
    """delta: a fixed challenger effect (arm 1; other challengers 0), or 'prior' for δ ~ prior_scale · t_3."""
    rng = random.Random(f"{seed}-{p.rule}-{p.policy}-{delta}-{k}-{p.p_win}-{p.tau}-{rate}")
    wins = losses = wrong = right = 0
    days, worse = [], []
    for _ in range(reps):
        if delta == "prior":
            deltas = [0.0] + [prior_scale * t3(rng) for _ in range(k - 1)]
        else:
            deltas = [0.0, float(delta)] + [0.0] * (k - 2)
        r = run_test(rng, pool, deltas, p, rate)
        worse.append(r["worse_share"])
        if r["decision"] == "none":
            continue
        days.append(r["day"])
        true = deltas[r["arm"]]
        if r["decision"] == "winner":
            wins += 1
            wrong += true <= 0
            right += true > 0
        else:
            losses += 1
            wrong += true >= 0 and delta != 0   # under the null a loser call is "no change": counted separately
    return {"reps": reps, "winner": wins / reps, "loser": losses / reps, "none": 1 - (wins + losses) / reps,
            "wins": wins, "wrong": wrong, "right": right,
            "median_days": statistics.median(days) if days else None,
            "worse_share": statistics.mean(worse) if delta not in (0, 0.0) else None}


# ---------- the committed table

def _pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def report(pool, info: dict, rate: float, reps: int, out: str | None = None, extra_pools: dict | None = None) -> str:
    t0 = time.time()
    L = ["# A/B v2 simulator results (milestone M1)", "",
         f"Generated by `agentpitbench ab sim` on {time.strftime('%Y-%m-%d', time.gmtime())}. "
         f"Bootstrap pool: {info['n']} top-level posts with a fresh 24 h snapshot since {info['since']} "
         f"(post-step data, docs/virality-2-spec.md §2b), kinds {info['kinds']}; y = log1p(R) mean {info['y_mean']}, "
         f"sd {info['y_sd']}, {_pct(info['zero_share'])} zeros, standardised to mean 0, sd 1 and pooled over kinds "
         "(too few posts per kind to bootstrap each). A challenger effect δ is added to the resampled z "
         "(a location shift).",
         "", f"Posts per day per slot: Poisson({rate}). Daily decision check; a post's reward is visible from the "
         f"next day's check. Futility cap: {Params.max_days} days or {Params.max_posts} posts on the challenger. "
         f"{reps} replications per cell; the binomial standard error of a 5% rate is "
         f"{100 * math.sqrt(0.05 * 0.95 / reps):.1f} points.", ""]

    def two_arm_table(title, rows, pool_, rate_):
        L.extend([f"## {title}", "",
                  "| rule | null: false winner | null: false loser | d=0.3 winner | d=0.5 winner | d=0.8 winner "
                  "| d=0.8 median days | prior δ~0.1·t₃: winners | wrong among winners |",
                  "|---|---|---|---|---|---|---|---|---|"])
        res = {}
        for name, p in rows:
            null = scenario(pool_, p, 0.0, rate=rate_, reps=reps)
            r3 = scenario(pool_, p, 0.3, rate=rate_, reps=reps)
            r5 = scenario(pool_, p, 0.5, rate=rate_, reps=reps)
            r8 = scenario(pool_, p, 0.8, rate=rate_, reps=reps)
            pr = scenario(pool_, p, "prior", rate=rate_, reps=reps)
            res[name] = null
            L.append(f"| {name} | **{_pct(null['winner'])}** | {_pct(null['loser'])} | {_pct(r3['winner'])} "
                     f"| {_pct(r5['winner'])} | {_pct(r8['winner'])} | {r8['median_days']} | {_pct(pr['winner'])} "
                     f"| {_pct(pr['wrong'] / pr['wins']) if pr['wins'] else '-'} |")
        L.append("")
        return res

    v2 = Params(rule="v2", policy="uniform")
    cand = {0.95: v2, 0.975: replace(v2, p_win=0.975), 0.99: replace(v2, p_win=0.99)}
    name = {pw: f"v2: P(δ>0) ≥ {pw}, loss < 0.02, τ 0.3" for pw in cand}
    v1row = ("v1: p_best ≥ 0.9, n ≥ 20 each (v1 TS allocation)", Params(rule="v1", policy="v1"))
    rows = [v1row] + [(name[pw], p) for pw, p in cand.items()] + [
        ("v2: P(δ>0) ≥ 0.95, loss < 0.02, τ 0.5", replace(v2, tau=0.5)),
        ("v2: P(δ>0) ≥ 0.95, loss < 0.01, τ 0.3", replace(v2, eps=0.01))]
    worst = {pw: [] for pw in cand}
    res = two_arm_table("Two arms (v2's setting while reach is low: TTTS, ES and the floor all give 50/50)",
                        rows, pool, rate)
    sens = [(f"Sensitivity: twice the traffic (Poisson({2 * rate}))", pool, 2 * rate)]
    sens += [(f"Sensitivity: {nm}", pl, rate) for nm, pl in (extra_pools or {}).items()]
    for pw in cand:
        worst[pw].append(res[name[pw]]["winner"])
    for title, pool_, rate_ in sens:
        r = two_arm_table(title, [v1row] + [(name[pw], p) for pw, p in cand.items()], pool_, rate_)
        for pw in cand:
            worst[pw].append(r[name[pw]]["winner"])
    ok = [pw for pw in cand if max(worst[pw]) <= 0.05]
    pick = min(ok) if ok else max(cand)

    L.extend([f"## Three arms: allocation policies (v2 rule with P(δ>0) ≥ {pick}, τ 0.3; the v1 row uses the v1 rule)",
              "", "| policy | null: any false winner | d=0.5 (one challenger): correct winner | worse-arm traffic, d=0.5 "
              "| median days |", "|---|---|---|---|---|"])
    reps3 = max(200, reps // 3)
    for pol in ("v1", "uniform", "ts", "ttts", "es"):
        p = Params(rule="v1", policy="v1") if pol == "v1" else replace(cand[pick], policy=pol)
        null = scenario(pool, p, 0.0, k=3, rate=rate, reps=reps3)
        r5 = scenario(pool, p, 0.5, k=3, rate=rate, reps=reps3)
        L.append(f"| {pol} | {_pct(null['winner'])} | {_pct(r5['right'] / reps3)} | {_pct(r5['worse_share'])} "
                 f"| {r5['median_days']} |")
    g = res[name[0.95]]["winner"]
    L.extend(["", f"({reps3} replications per three-arm cell.)", "",
              "## Gate (false-winner rate under daily peeking ≤ 5%)", "",
              f"- As written in the plan (P(δ>0) ≥ 0.95, expected loss < 0.02 sd, τ = 0.3): **{_pct(g)}** at "
              f"{rate} posts/day, worst case over the sensitivity runs **{_pct(max(worst[0.95]))}** → "
              f"**{'PASS' if max(worst[0.95]) <= 0.05 else 'FAIL'}**."]
             + [f"- P(δ>0) ≥ {pw}: worst case {_pct(max(worst[pw]))} → **{'PASS' if pw in ok else 'FAIL'}**."
                for pw in cand if pw != 0.95]
             + ["", f"**Recommended for M5: P(δ>0) ≥ {pick}** (the lowest threshold that passes in every run), "
                "τ = 0.3, ε = 0.02, 45 days / 150 posts. Expected loss < ε never binds at these sample sizes "
                "(the ε = 0.01 row equals the ε = 0.02 row): P(δ>0) does all the work. The false-winner rate "
                "grows with traffic (more posts inside the 45 days means more chances to cross), so the gate is "
                "re-run whenever posts/day per slot changes a lot.", "",
                "Definitions: *false winner* = the rule declares the challenger better at any daily check although "
                "δ = 0. *wrong among winners* = share of declared winners whose true δ ≤ 0 when δ is drawn from a "
                "fat-tailed prior (0.1·t₃: most effects tiny, a few large); most of those are near-zero effects on "
                "either side. *worse-arm traffic* = share of posts that went to an arm with a lower true mean.", "",
                f"Runtime {time.time() - t0:.0f} s."])
    text = "\n".join(L) + "\n"
    if out:
        with open(out, "w") as f:
            f.write(text)
    return text


def main(argv=None) -> None:
    from .config import load
    from .db import DB
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="bench.toml")
    ap.add_argument("--db", help="database path (default: the config's); opened read-only")
    ap.add_argument("--since")
    ap.add_argument("--rate", type=float, default=3.0)
    ap.add_argument("--reps", type=int, default=1000)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    s = load(a.config)
    db = DB.readonly(a.db or s.db_path)
    pool, info = load_pool(db, s, a.since)
    extra = {}
    try:
        p9, i9 = load_pool(db, s, "2026-10-09")
        extra[f"pre- and post-step data since 2026-10-09 (n = {i9['n']})"] = p9
    except ValueError:
        pass
    extra["Gaussian rewards"] = gaussian_pool()
    print(report(pool, info, a.rate, a.reps, a.out, extra))


if __name__ == "__main__":
    main()
