"""A/B v2 milestones M0-M3 (docs/ab-algorithm-v2-plan.md §3): audit, simulator, propensity logging, reward v2.
None of these may change a live choice or post text (that's M4/M5)."""
import time

import pytest

from agentpitbench import ab, ab_audit, ab_report
from agentpitbench.config import Settings
from agentpitbench.db import DB

DAY = 86400
T0 = ab_audit.ts("2026-10-12")


@pytest.fixture
def env(tmp_path):
    s = Settings(data_dir=tmp_path / "var", dry_run=True, post_kinds=["reveal", "results", "week", "motd"])
    db = DB(s.db_path)
    ab.seed(db)
    return s, db


def tweet(db, key, kind="results", at=T0 + 3600, reply_to=None, quote_of=None, round_id=None, **counts):
    """A posted tweet with a fresh 24 h snapshot."""
    tid = str(abs(hash(key)) % 10 ** 15 + 10 ** 15)
    db.x("INSERT INTO tweets(post_key,tweet_id,kind,posted_at,reply_to,quote_of,round_id) VALUES(?,?,?,?,?,?,?)",
         key, tid, kind, at, reply_to, quote_of, round_id)
    c = {"impressions": 10, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0, "bookmarks": 0, "link_clicks": 0}
    c.update(counts)
    db.x("INSERT INTO tweet_metrics(tweet_id,taken_at,age_s,bucket,impressions,likes,replies,reposts,quotes,bookmarks,"
         "link_clicks) VALUES(?,?,?,?,?,?,?,?,?,?,?)", tid, at + DAY, DAY, "24h", *c.values())
    return tid


# ---------- M0: audit

def test_audit_dead_slots_and_self_engagement(env):
    s, db = env
    tid = tweet(db, "r1-results", replies=2, likes=1, impressions=90)
    tweet(db, "r1-self", reply_to=tid, kind="results-link")       # our own reply: not engagement
    tweet(db, "r1-press-1", kind="press", reply_to=tid)
    tweet(db, "r0-results", at=T0 - DAY, replies=5)                # before the window
    a = ab_audit.audit(db, s, until=T0 + 3 * DAY)
    assert a["since"] == "2026-10-12"
    assert a["dead_slots"] == ["press.head"] and a["reduced_slots"] == ["generic.cta"]
    assert a["by_kind"]["results"]["n"] == 1
    row = next(r for r in a["self_engagement_check"] if r["post_key"] == "r1-results")
    assert (row["self_replies"], row["R_raw"], row["R"]) == (2, 11, 1)    # 2 replies x5 + 1 like; both replies are ours (link + press)
    slots = {x["slot"]: x for x in a["slots"]}
    assert slots["results.hook"]["kind_posts_per_day"] == round(1 / 3, 2)
    assert set(a["covariates"]) >= {"followers (account_metrics that day)", "market volume24hr > 0 at round start"}
    ab_audit.print_audit(a)


def test_audit_all_kinds_without_post_kinds(env):
    s, db = env
    s.post_kinds = []
    a = ab_audit.audit(db, s, until=T0 + DAY)
    assert a["dead_slots"] == [] and a["reduced_slots"] == []


def test_readonly_db_never_writes(env, tmp_path):
    s, db = env
    ro = DB.readonly(s.db_path)
    assert ro.one("SELECT COUNT(*) c FROM ab_arms")["c"] > 0
    with pytest.raises(Exception):
        ro.x("DELETE FROM ab_arms")


def test_digest_carries_the_audit(env):
    s, db = env
    out = ab_report.digest(db, s, "2026-10-13", now=T0 + 2 * DAY)
    assert out["audit"]["dead_slots"] == ["press.head"]
    assert out["reward_v2"]["active"] is False and "reduction_pct" in out["reward_v2"]


# ---------- M1: simulator and power calculator

def test_power_reproduces_plan_table():
    from agentpitbench import ab_sim
    assert round(ab_sim.n_per_arm(0.5)) == 63 and round(ab_sim.n_per_arm(0.8)) == 25
    assert round(ab_sim.n_per_arm(0.2)) == 392                       # the plan rounds 15.7/d² to 393
    assert round(ab_sim.days_to_detect(5, 0.5, arms=2)) == 25 and round(ab_sim.days_to_detect(5, 0.3, arms=3)) == 105
    assert ab_sim.days_to_detect(0, 0.5) is None
    assert any("don't start" in x for x in ab_sim.power_lines("tags", 2.4))


def test_sim_posterior_and_allocation():
    from agentpitbench import ab_sim
    p = ab_sim.Params()
    mu, C = ab_sim.v2_posterior([0.0, 0.0], [0.0, 0.0], p)
    assert C[0][0] == pytest.approx(1 / p.kappa0) and C[1][1] == pytest.approx(p.tau ** 2)   # the prior
    mu, C = ab_sim.v2_posterior([1e6, 1e6], [0.0, 0.3e6], p)
    assert mu[1] == pytest.approx(0.3, abs=1e-3)                     # data overwhelm the prior
    for pol in ("ttts", "es", "uniform"):                            # two arms: all 50/50 (plan §2.3)
        assert ab_sim.allocation(pol, [0.9, 0.1], p) == pytest.approx([0.5, 0.5])
    a = ab_sim.allocation("ts", [0.98, 0.01, 0.01], p)
    assert min(a) >= p.floor - 1e-12 and sum(a) == pytest.approx(1.0)


def test_sim_runs_and_v1_peeks_more_than_v2():
    from agentpitbench import ab_sim
    pool = ab_sim.gaussian_pool(500)
    v1 = ab_sim.scenario(pool, ab_sim.Params(rule="v1", policy="v1"), 0.0, reps=150)
    v2 = ab_sim.scenario(pool, ab_sim.Params(p_win=0.99), 0.0, reps=150)
    big = ab_sim.scenario(pool, ab_sim.Params(p_win=0.99), 0.8, reps=60)
    assert v1["winner"] > v2["winner"] and v2["winner"] <= 0.05
    assert big["winner"] > 0.8 and big["median_days"] < 45
    three = ab_sim.scenario(pool, ab_sim.Params(policy="ttts"), 0.5, k=3, reps=20)
    assert three["reps"] == 20


# ---------- M2: propensity logging

def _three_arm_posterior(s, db, monkeypatch):
    """tags: 3 live arms, 3 observed posts each, overlapping posteriors; exploration over."""
    from agentpitbench import xstats
    s.ab_min_posts = 0
    table, now = {}, time.time()
    for arm, replies in zip(ab.arms(db, "tags"), (1, 2, 0)):
        for i in range(3):
            key = f"seed-{arm['arm_id']}-{i}"
            db.x("INSERT INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at) VALUES(?,?,?,?,?,?)",
                 key, "tags", arm["arm_id"], arm["arm_id"], "results", now)
            tid = tweet(db, key, at=now - 2 * DAY)
            table[(tid, "24h")] = {"replies": replies}
    monkeypatch.setattr(xstats, "reward_inputs", lambda db, tid, bucket, s=None: table.get((tid, bucket)))
    monkeypatch.setattr(xstats, "rewards", lambda *a, **k: [])
    return now


def test_propensity_control_only_and_exploration(env):
    s, db = env
    s.ab_min_posts = 20
    ab.choose(db, s, "tags", "r1-results", kind="results")
    r = db.one("SELECT propensity, policy FROM ab_assignments WHERE slot='tags'")
    assert (r["propensity"], r["policy"]) == (1.0, "v1-explore")
    db.x("UPDATE ab_arms SET status='retired' WHERE slot='press.head' AND is_control=0")
    ab.choose(db, s, "press.head", "r1-press", kind="press")
    r = db.one("SELECT propensity, policy FROM ab_assignments WHERE slot='press.head'")
    assert (r["propensity"], r["policy"]) == (1.0, "control-only")


def test_propensity_matches_monte_carlo(env, monkeypatch):
    import random
    s, db = env
    now = _three_arm_posterior(s, db, monkeypatch)
    post = {p["arm_id"]: p for p in ab.posterior(db, s, "tags", now, draws=0)}
    live = ab.arms(db, "tags")
    rng = random.Random(1)
    wins = {a["arm_id"]: 0 for a in live}
    for _ in range(20_000):
        d = {k: rng.gauss(post[k]["mean"], post[k]["sd"]) for k in wins}
        wins[max(d, key=d.get)] += 1
    for key in ("p-a", "p-b"):
        c = ab.choose(db, s, "tags", key, now=now, kind="results")
        r = db.one("SELECT propensity, policy FROM ab_assignments WHERE post_key=? AND slot='tags'", key)
        assert r["policy"] == "v1-ts"
        assert r["propensity"] == pytest.approx(wins[c.arm_id] / 20_000, abs=0.01)
        assert 0.0 < r["propensity"] < 1.0
    params = [(post[a["arm_id"]]["mean"], post[a["arm_id"]]["sd"]) for a in live]
    for i in range(3):   # every arm's closed-form / integrated propensity against the Monte Carlo
        assert ab.ts_propensity(params, i) == pytest.approx(wins[live[i]["arm_id"]] / 20_000, abs=0.01)
    assert sum(ab.ts_propensity(params, i) for i in range(3)) == pytest.approx(1.0, abs=1e-4)
    assert ab.ts_propensity(params[:2], 0) + ab.ts_propensity(params[:2], 1) == pytest.approx(1.0)


def test_propensity_logging_leaves_choices_unchanged(env, monkeypatch):
    """The arm is the v1 Thompson draw from the seeded rng, exactly as before M2."""
    s, db = env
    now = _three_arm_posterior(s, db, monkeypatch)
    post = {p["arm_id"]: p for p in ab.posterior(db, s, "tags", now, draws=0)}
    live = ab.arms(db, "tags")
    picked = set()
    for i in range(40):
        key = f"k{i}"
        rng = ab._rng(key, "tags")
        samples = {a["arm_id"]: rng.gauss(post[a["arm_id"]]["mean"], post[a["arm_id"]]["sd"]) for a in live}
        want = max(samples, key=samples.get)
        got = ab.choose(db, s, "tags", key, now=now, kind="results").arm_id
        db.x("DELETE FROM ab_assignments WHERE post_key=?", key)     # keep the posterior fixed
        assert got == want
        picked.add(got)
    assert len(picked) >= 2


def test_situation_stored_for_hooks(env):
    s, db = env
    ab.choose(db, s, "results.hook", "r1-results", "sweep", {"winner": "Claude", "record": "3-0"}, kind="results")
    assert db.one("SELECT situation FROM ab_assignments WHERE slot='results.hook'")["situation"] == "sweep"


# ---------- M3: reward v2

def _confounded(db, shift: int, now: float):
    """96 results posts on 'tags' arms A/B with no arm effect. Evening posts (UTC 20h) get +shift replies, and arm B
    got 3/4 of the evening posts by chance, so a raw comparison sees a fake effect."""
    a, b = [x["arm_id"] for x in ab.arms(db, "tags")[:2]]
    day = (now // DAY - 5) * DAY
    n = 0
    for hb in (0, 1):
        for arm, cnt in ((a, 12 if hb else 36), (b, 36 if hb else 12)):
            for j in range(cnt):
                key = f"c{hb}-{arm}-{j}"
                tweet(db, key, at=day + (20 if hb else 2) * 3600 + n, replies=j % 4 + (shift if hb else 0))
                db.x("INSERT INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at)"
                     " VALUES(?,?,?,?,?,?)", key, "tags", arm, arm, "results", day)
                n += 1
    return a, b


def _delta(db, s, now, a, b):
    obs = ab.observations(db, s, "tags", now)
    za = [o["z"] for o in obs if o["arm_id"] == a]
    zb = [o["z"] for o in obs if o["arm_id"] == b]
    return sum(zb) / len(zb) - sum(za) / len(za)


def test_reward_v2_covariate_shift_does_not_move_delta(env):
    from agentpitbench import ab_reward
    s, db = env
    now = time.time()
    a, b = _confounded(db, 6, now)
    s.ab_reward_version = 1
    d1 = _delta(db, s, now, a, b)
    s.ab_reward_version = 2
    d2 = _delta(db, s, now, a, b)
    assert d1 > 0.5                       # v1 reads the evening shift as an arm effect
    assert abs(d2) < 0.1                  # v2 adjusts it away: no arm effect
    rep = ab_reward.variance_report(db, s, now)
    assert rep["adjustment_applied"] and rep["reduction_pct"] > 50


def test_reward_v2_no_post_adjusts_itself(env):
    from agentpitbench import ab_reward
    s, db = env
    now = time.time()
    _confounded(db, 6, now)
    m1 = ab_reward.Model(db, s, now)
    key = f"c1-{ab.arms(db, 'tags')[1]['arm_id']}-0"
    tid = db.one("SELECT tweet_id FROM tweets WHERE post_key=?", key)["tweet_id"]
    i = next(i for i, r in enumerate(m1.rows) if r["tweet_id"] == tid)
    adj1 = m1.y[i] - m1._adj[tid]
    db.x("UPDATE tweet_metrics SET replies=40 WHERE tweet_id=?", tid)     # change only this post's outcome
    m2 = ab_reward.Model(db, s, now)
    i2 = next(i for i, r in enumerate(m2.rows) if r["tweet_id"] == tid)
    assert m2.y[i2] != m1.y[i] and m1.applied and m2.applied
    assert m2.y[i2] - m2._adj[tid] == pytest.approx(adj1, abs=1e-12)   # its own adjustment didn't move


def test_reward_v2_covariates_are_pre_treatment(env):
    from agentpitbench import ab_reward
    s, db = env
    tid = tweet(db, "r1-results", replies=3, impressions=500)
    raw = ab_reward.Features(db).raw({"tweet_id": tid, "post_key": "r1-results", "kind": "results",
                                      "round_id": None, "posted_at": T0 + 3600, "impressions": 500})
    assert set(raw) == {"kind", "situation", "hour", "followers", "exhibition", "volume"}
    assert not set(raw) & ab_reward.FORBIDDEN


def test_reward_v2_cap_at_p99(env):
    import math
    from agentpitbench import ab_reward
    s, db = env
    now = time.time()
    for i in range(60):
        tweet(db, f"p{i}", at=now - 3 * DAY + i, replies=1000 if i == 0 else i % 3)
    m = ab_reward.Model(db, s, now)
    assert m.caps["results"] < 5000
    i = next(i for i, r in enumerate(m.rows) if r["post_key"] == "p0")
    assert m.y[i] == pytest.approx(math.log1p(m.caps["results"]))
    assert ab_reward.Model(db, s, now, days=1).caps == {}       # < 50 posts of the kind in the window: no cap


def test_reward_version_1_is_default_and_untouched(env, monkeypatch):
    from agentpitbench import ab_reward
    s, db = env
    assert s.ab_reward_version == 1
    now = time.time()
    _confounded(db, 6, now)

    def boom(*a, **k):
        raise AssertionError("reward v2 used under ab_reward_version = 1")
    monkeypatch.setattr(ab_reward, "Model", boom)
    assert ab.posterior(db, s, "tags", now)
    ab.choose(db, s, "tags", "fresh-post", now=now, kind="results")
