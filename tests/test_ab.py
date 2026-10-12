"""A/B testing (docs/ab-testing-spec.md section 13): assignment, posterior, validator, fallback, retire."""
import json
import time

import pytest

from agentpitbench import ab, xstats
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.twitter import TAG, fit, reveal_text, results_text

HOOK = {"situations": {"agree": "Unanimous call", "alone": "{score}: {lone} stands apart",
                       "dead_split": "{score}: split down the middle", "nobody_agrees": "{score}: no two alike",
                       "nway": "{score}: a split field"}}


@pytest.fixture
def env(tmp_path):
    s = Settings(data_dir=tmp_path / "var", dry_run=True)
    db = DB(s.db_path)
    ab.seed(db)
    return s, db


def post(db, key, kind="results", at=None, tid=None, **kw):
    db.x("INSERT INTO tweets(post_key,tweet_id,kind,posted_at,reply_to,quote_of) VALUES(?,?,?,?,?,?)",
         key, tid or f"t-{key}", kind, at or time.time(), kw.get("reply_to"), kw.get("quote_of"))
    return tid or f"t-{key}"


def force(db, slot, arm_id):
    """Exploration picks the fewest-assigned arm: give every other live arm of the slot many assignments."""
    for a in ab.arms(db, slot):
        if a["arm_id"] != arm_id:
            for i in range(50):
                db.x("INSERT INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at)"
                     " VALUES(?,?,?,?,?,?)", f"old-{a['arm_id']}-{i}", slot, a["arm_id"], a["arm_id"], "x", 0)


def fake_rewards(monkeypatch, table: dict, base: list | None = None):
    """table: {(tweet_id, bucket): counts}; base: rows xstats.rewards returns (the kind baseline)."""
    monkeypatch.setattr(xstats, "reward_inputs", lambda db, tid, bucket, s=None: table.get((tid, bucket)))
    monkeypatch.setattr(xstats, "rewards", lambda db, bucket, kinds=None, since=None, until=None, s=None: base or [])


# 1. assignment

def test_control_only_and_idempotent(env):
    s, db = env
    c = ab.choose(db, s, "reveal.cta", "r1-reveal", kind="reveal")
    assert c.is_control and c.text == ""
    rows = db.q("SELECT * FROM ab_assignments WHERE slot='reveal.cta'")   # recorded: the control's baseline
    assert [r["arm_id"] for r in rows] == [ab.control(db, "reveal.cta")["arm_id"]]
    t1 = ab.choose(db, s, "tags", "r1-reveal", kind="reveal")
    db.x("UPDATE ab_arms SET status='retired' WHERE slot='tags' AND arm_id<>?", t1.arm_id)   # can't redraw now
    assert ab.choose(db, s, "tags", "r1-reveal", kind="reveal").arm_id == t1.arm_id


def test_exploration_fewest_then_thompson_reproducible(env):
    s, db = env
    s.ab_min_posts = 2
    picks = [ab.choose(db, s, "tags", f"p{i}", kind="results").arm_id for i in range(6)]
    ids = [a["arm_id"] for a in ab.arms(db, "tags")]
    assert picks == ids + ids                                  # round-robin from the lowest id while exploring
    a = ab.choose(db, s, "tags", "p-ts", kind="results").arm_id
    db.x("DELETE FROM ab_assignments WHERE post_key='p-ts'")
    assert ab.choose(db, s, "tags", "p-ts", kind="results").arm_id == a      # seeded by post_key + slot


def test_hook_without_situation_is_control_unrecorded(env):
    s, db = env
    ab.add_arm(db, s, "reveal.hook", HOOK)
    c = ab.choose(db, s, "reveal.hook", "r9-reveal", None, {}, kind="reveal")
    assert c.is_control and not db.q("SELECT 1 FROM ab_assignments WHERE post_key='r9-reveal'")


def test_hook_fills_roles(env):
    s, db = env
    aid, st = ab.add_arm(db, s, "reveal.hook", HOOK)
    assert st == "live"
    force(db, "reveal.hook", aid)
    c = ab.choose(db, s, "reveal.hook", "r1-reveal", "alone", {"score": "3–1", "lone": "Grok", "n": "2"}, kind="reveal")
    assert c.arm_id == aid and c.text == "3–1: Grok stands apart"


# 2. posterior

def test_blend_and_self_engagement(env, monkeypatch):
    s, db = env
    tid = post(db, "r1-results")
    post(db, "r1-press-1", "press", reply_to=tid)
    r = {"replies": 3, "quotes": 1, "likes": 2, "reposts": 0, "bookmarks": 0, "link_clicks": None}
    import math
    assert ab.blend(s, r, *ab._self_counts(db, tid)) == pytest.approx(math.log1p(5 * 2 + 3 * 1 + 2))


def test_decay_and_1h_replaced_by_24h(env, monkeypatch):
    s, db = env
    s.ab_min_posts = 0
    now = time.time()
    c = ab.choose(db, s, "tags", "r1-results", kind="results")
    tid = post(db, "r1-results", at=now - 14 * 86400)
    counts = {"replies": 1, "quotes": 0, "likes": 0, "reposts": 0, "bookmarks": 0, "link_clicks": 0}
    table = {(tid, "1h"): counts}
    fake_rewards(monkeypatch, table)
    o = ab.observations(db, s, "tags", now)
    assert len(o) == 1 and o[0]["w"] == pytest.approx(0.5 * 0.5, rel=1e-3)     # 1 h weight, halved at 14 d
    table[(tid, "24h")] = counts
    o = ab.observations(db, s, "tags", now)
    assert len(o) == 1 and o[0]["w"] == pytest.approx(0.5, rel=1e-3)           # replaced, not added
    assert o[0]["arm_id"] == c.arm_id


def test_void_and_boosted_excluded(env, monkeypatch):
    s, db = env
    ab.choose(db, s, "tags", "r1-results", kind="results")
    ab.choose(db, s, "tags", "r2-results", kind="results")
    t1, t2 = post(db, "r1-results"), post(db, "r2-results")
    counts = {"replies": 1}
    fake_rewards(monkeypatch, {(t1, "24h"): counts})      # t2: reward_inputs None (boosted / deleted / stale)
    assert len(ab.observations(db, s, "tags")) == 1
    db.x("UPDATE ab_assignments SET excluded='void' WHERE post_key='r1-results'")
    assert ab.observations(db, s, "tags") == []


def test_posterior_math_and_p_best(env, monkeypatch):
    s, db = env
    s.ab_min_posts = 0
    tags = ab.arms(db, "tags")
    now = time.time()
    table = {}
    for i in range(30):
        key = f"r{i}-results"
        arm = tags[i % 3]
        db.x("INSERT INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at) VALUES(?,?,?,?,?,?)",
             key, "tags", arm["arm_id"], arm["arm_id"], "results", now)
        tid = post(db, key, at=now)
        table[(tid, "24h")] = {"replies": 10 if arm["code_key"] == " #AI" else 0}
    fake_rewards(monkeypatch, table)
    p = {r["code_key"]: r for r in ab.posterior(db, s, "tags", now)}
    assert p[" #AI"]["p_best"] > 0.9 and p[" #AI"]["n"] == 10
    assert p[" #AI"]["sd"] == pytest.approx(1 / (10 + 2) ** 0.5, rel=1e-3)


# 3. validator

@pytest.mark.parametrize("text,why", [
    ("Won 3 times", "digits"), ("Claude is on fire", "names"), ("Anthropic's day", "names"),
    ("Ask @someone", "@ or #"), ("Big day #AI", "@ or #"), ("See www.example.com", "link"),
    ("What a stupid call", "disparaging"), ("This proves it", "claim"), ("The best call yet", "claim"),
    ("x" * 61, "too long"), ("Call it \"now\"", "quotes"), ("Wait for it…", "ellipsis"),
])
def test_validator_rejects(text, why):
    errs = ab.validate("reveal.cta", {"text": text})
    assert errs and any(why in e for e in errs), errs


def test_validator_hooks():
    assert ab.validate("reveal.hook", HOOK) == []
    bad = json.loads(json.dumps(HOOK))
    del bad["situations"]["nway"]
    assert any("incomplete" in e for e in ab.validate("reveal.hook", bad))
    bad = json.loads(json.dumps(HOOK))
    bad["situations"]["alone"] = "{score}: {lone} and {lone}"
    assert any("exactly once" in e for e in ab.validate("reveal.hook", bad))
    bad["situations"]["alone"] = "{score}: {who} alone"
    assert any("unknown placeholder" in e for e in ab.validate("reveal.hook", bad))
    bad["situations"]["alone"] = "{score: alone"
    assert any("parse" in e for e in ab.validate("reveal.hook", bad))
    assert ab.validate("press.head", {"text": "From the losing bench:"}) == []
    assert ab.validate("press.head", {"text": "Statements from the losers"})  # > 22 and DISPARAGE
    assert ab.validate("tags", {"text": "x"})                                 # code slot: never generated


def test_slur_filter():
    from agentpitbench import virality
    word = next(iter(virality._BLOCK))
    assert ab.validate("reveal.cta", {"text": f"well {word}"})


def rnd(outcomes=("CYBERSHOKE", "Nexus"), picks=None):
    names = ["Claude", "ChatGPT", "Gemini", "Grok"]
    picks = picks or [outcomes[0], outcomes[0], outcomes[0], outcomes[1]]
    return {"round_id": 1, "state": "resolved", "winner": outcomes[1], "question": "Q?", "outcomes": list(outcomes),
            "entries": [{"agent": a, "name": n, "outcome": p, "won": p == outcomes[1]}
                        for a, n, p in zip(["claude", "codex", "agy", "grok"], names, picks)]}


def test_check_rendered_invariants():
    r = rnd()
    ctl = reveal_text(r, "3–1: Grok goes alone")
    assert ab.check_rendered("reveal", ctl, ctl) == []
    assert ab.check_rendered("reveal", ctl.replace("Reply with your pick", "Reply"), ctl)
    assert ab.check_rendered("milestone", "x https://e.com " + TAG, "x " + TAG)          # a link added
    s = Settings()
    res = results_text(s, r, [], "https://agentpitbench.org/round/1/")
    assert ab.check_rendered("results", res.replace("https://agentpitbench.org/round/1/", ""), res)  # URL dropped
    assert ab.check_rendered("results", res.replace("/round/1/", "/round/2/"), res)                 # URL changed


# 4. fallback

def _choices(db, s, key, **texts):
    out = []
    for slot, t in texts.items():
        c = ab.choose(db, s, slot, key, "alone" if slot.endswith(".hook") else None, {}, kind="reveal")
        out.append(ab.Choice(slot, c.arm_id, c.key, t, False))
    return out


def test_long_labels_variant_falls_back_to_control(env):
    s, db = env
    aid, _ = ab.add_arm(db, s, "reveal.cta", {"text": "Think you can beat them all today"})
    force(db, "reveal.cta", aid)
    long = ("Yes, over three and a half goals in total", "No, three goals or fewer in total")
    r = rnd(long)
    c = ab.choose(db, s, "reveal.cta", "r1-reveal", kind="reveal")
    assert c.arm_id == aid
    text = ab.finish(db, "r1-reveal", [c], lambda v: reveal_text(r, "2–2: dead split", v.get("reveal.cta", "")),
                     "reveal")
    assert text == reveal_text(r, "2–2: dead split")                 # the control posts
    a = db.one("SELECT * FROM ab_assignments WHERE post_key='r1-reveal' AND slot='reveal.cta'")
    assert a["arm_id"] == aid and a["realized_arm_id"] == ab.control(db, "reveal.cta")["arm_id"]


def test_drop_order_tags_then_cta_hook_survives(env):
    s, db = env
    for slot, t in (("reveal.hook", HOOK), ("reveal.cta", {"text": "c" * 20})):
        ab.add_arm(db, s, slot, t)
    key = "r1-reveal"
    ch = _choices(db, s, key, **{"reveal.hook": "H" * 20, "reveal.cta": "c" * 20, "tags": " #AI"})

    def render(v):
        cta = f"\n{v['reveal.cta']}" if v.get("reveal.cta") else ""
        return fit(v.get("reveal.hook", "H") + "\n" + "p" * 200,
                   cta + "\nReply with your pick: a / b " + TAG + v.get("tags", ""))
    text = ab.finish(db, key, ch, render, "reveal")
    assert text == render({"reveal.hook": "H" * 20})
    real = {r["slot"]: r for r in db.q("SELECT * FROM ab_assignments WHERE post_key=?", key)}
    assert real["reveal.hook"]["realized_arm_id"] == real["reveal.hook"]["arm_id"]
    for sl in ("tags", "reveal.cta"):
        assert real[sl]["realized_arm_id"] == ab.control(db, sl)["arm_id"]


def test_generic_cta_inserted_before_trailing_tag(env):
    s, db = env
    aid, _ = ab.add_arm(db, s, "generic.cta", {"text": "Follow along"})
    force(db, "generic.cta", aid)
    assert ab.generic_text(db, s, "race-W1", "race", f"The week. {TAG}") == f"The week.\nFollow along {TAG}"
    url = "https://agentpitbench.org/reports/W1/"
    assert ab.generic_text(db, s, "report-W1-3", "report", f"Read it: {url}") == f"Read it: {url}"   # no trailing TAG
    assert ab.generic_text(db, s, "r1-reveal", "reveal", f"x {TAG}") == f"x {TAG}"                  # not generic


# 6. retire

def test_retire_refuses_control_and_promotes_queued(env):
    s, db = env
    ids = [ab.add_arm(db, s, "reveal.cta", {"text": t}) for t in ("Your call", "Pick a side", "Who has it")]
    assert [st for _, st in ids] == ["live", "live", "queued"]
    with pytest.raises(ValueError):
        ab.retire(db, ab.control(db, "reveal.cta")["arm_id"])
    assert ab.retire(db, ids[0][0], "meh") == ids[2][0]
    assert {a["arm_id"] for a in ab.arms(db, "reveal.cta")} == {ab.control(db, "reveal.cta")["arm_id"], ids[1][0], ids[2][0]}


def test_seed_idempotent(env):
    s, db = env
    n = db.one("SELECT COUNT(*) c FROM ab_arms")["c"]
    ab.seed(db)
    assert db.one("SELECT COUNT(*) c FROM ab_arms")["c"] == n
    assert {a["code_key"] for a in ab.arms(db, "tags")} == {"", " #AI", " #PredictionMarkets"}
