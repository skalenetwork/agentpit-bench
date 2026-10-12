"""Cadence policy (docs/ai-automation-spec.md §9, §15.12)."""
import json
import time
from datetime import datetime, timezone

import pytest

from agentpitbench import opai, policy
from agentpitbench.config import Settings
from agentpitbench.db import DB

NOW = datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc).timestamp()
MOST_RESTRICTIVE = {"press_max_replies": 0, "post_replay": False, "post_milestones": False, "post_awards": False,
                    "post_contrarian": False, "race_hour_utc": 22, "optional_posts_per_day": 0}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    s = Settings(data_dir=tmp_path / "var", opai_enabled=True, opai_tasks=["cadence"], cadence_enabled=True)
    return s, DB(s.db_path)


def add_post(db, tid, posted_at, kind="results", imps=100):
    db.x("INSERT INTO tweets(post_key,tweet_id,kind,text,posted_at) VALUES(?,?,?,?,?)", f"k-{tid}", tid, kind, "t",
         posted_at)
    db.x("INSERT INTO tweet_metrics(tweet_id,taken_at,age_s,bucket,impressions) VALUES(?,?,?,?,?)",
         tid, posted_at + 86400, 86400, "24h", imps)


def week_of_data(db, days=8, per_day=3, tag="w"):
    for d in range(days):
        for i in range(per_day):
            add_post(db, f"{tag}{d}-{i}", NOW - (d + 1) * 86400 - i * 600, kind=("results", "replay", "press")[i])


def test_clamp_drops_unknown_and_clamps():
    assert policy.clamp({"press_max_replies": 5, "race_hour_utc": 3, "optional_posts_per_day": -2,
                         "post_replay": "false", "max_reply_reads": 1000, "post_reveal": False,
                         "daily_round_cap": 1}) == {"press_max_replies": 1, "race_hour_utc": 12,
                                                    "optional_posts_per_day": 0, "post_replay": False}


def test_defaults_are_config_values(env):
    s, db = env
    assert policy.current(s, db) == {"press_max_replies": 1, "race_hour_utc": 18, "post_replay": True,
                                     "post_milestones": True, "post_awards": True, "post_contrarian": True,
                                     "optional_posts_per_day": 8}
    assert s.policy(db, "race_hour_utc") == 18
    with pytest.raises(KeyError):
        s.policy(db, "max_reply_reads")


def test_reveal_and_results_never_gated(env):
    s, db = env
    db.put(policy.KV, MOST_RESTRICTIVE)
    for kind in ("reveal", "results", "race", "champion", "model"):
        assert policy.allows(s, db, kind)
    for kind in policy.OPTIONAL_KINDS:
        assert not policy.allows(s, db, kind)


def test_optional_cap_counts_today(env):
    s, db = env
    db.put(policy.KV, {"optional_posts_per_day": 2})
    add_post(db, "a", NOW - 3600, "replay")
    assert policy.allows(s, db, "milestone", NOW)
    add_post(db, "b", NOW - 1800, "press")
    add_post(db, "c", NOW - 1700, "results")       # not optional: not counted
    assert policy.optional_today(db, NOW) == 2
    assert not policy.allows(s, db, "milestone", NOW)
    assert policy.allows(s, db, "results", NOW)


def test_validator_one_knob():
    cur = {"press_max_replies": 1, "post_replay": True}
    v = policy.validator(cur)
    assert v({"policy": {"post_replay": False}, "reason": "replays underperform"}) is None
    assert v({"policy": {"post_replay": False, "press_max_replies": 0}, "reason": "x"}) == "more than one knob changed"
    assert v({"policy": {"max_reply_reads": 10}, "reason": "x"}) == "unknown knob max_reply_reads"


def test_sufficiency(env):
    s, db = env
    assert not policy.sufficiency(db, NOW)["ok"]
    week_of_data(db, days=3)
    assert not policy.sufficiency(db, NOW)["ok"]
    week_of_data(db, days=8, tag="v")
    assert policy.sufficiency(db, NOW)["ok"]


@pytest.fixture
def week(env):
    s, db = env
    week_of_data(db)
    return s, db


async def test_off_does_nothing(tmp_path, fake_claude):
    s = Settings(data_dir=tmp_path / "var", opai_enabled=True, opai_tasks=["cadence"])
    db = DB(s.db_path)
    assert await policy.due(s, db, NOW) is None
    assert fake_claude.calls == [] and db.get(policy.LAST) is None


async def test_shadow_without_a_week_of_data(env, fake_claude):
    s, db = env
    out = await policy.due(s, db, NOW)
    assert out["mode"] == "shadow" and fake_claude.calls == []
    assert db.get(policy.LAST)["mode"] == "shadow"
    assert json.loads((s.export_dir / "cadence.json").read_text())["mode"] == "shadow"
    assert await policy.due(s, db, NOW + 60) is None          # once a day
    assert db.get(policy.KV) is None


async def test_recommend_does_not_apply(week, fake_claude):
    s, db = week
    fake_claude.respond({"policy": {**policy.current(s, db), "post_replay": False}, "reason": "replays get few views"})
    out = await policy.decide(s, db, NOW)
    assert out["mode"] == "recommend" and out["recommendation"] == {"post_replay": False}
    assert len(fake_claude.calls) == 1
    assert db.get(policy.KV) is None and policy.get(s, db, "post_replay") is True
    assert db.one("SELECT COUNT(*) n FROM policy_changes")["n"] == 0


async def test_apply_one_knob_clamped_once_a_day(week, fake_claude):
    s, db = week
    s.cadence_apply = True
    fake_claude.respond({"policy": {"race_hour_utc": 30}, "reason": "evening posts travel further"})
    out = await policy.decide(s, db, NOW)
    assert out["applied"] == {"race_hour_utc": 22}
    assert s.policy(db, "race_hour_utc") == 22
    assert db.one("SELECT COUNT(*) n FROM policy_changes")["n"] == 1
    s.opai_task_budget = {**s.opai_task_budget, "cadence": 5}
    fake_claude.respond({"policy": {"post_awards": False}, "reason": "x"})
    out = await policy.decide(s, db, NOW + 60)
    assert "applied" not in out and "already changed today" in out["summary"]
    assert s.policy(db, "post_awards") is True


async def test_two_knobs_rejected(week, fake_claude):
    s, db = week
    s.cadence_apply = True
    fake_claude.respond({"policy": {"post_awards": False, "post_replay": False}, "reason": "x"})
    out = await policy.decide(s, db, NOW)
    assert "applied" not in out and db.get(policy.KV) is None


async def test_payload_inputs(week):
    s, db = week
    p = policy.payload(s, db, NOW)
    assert {k["kind"] for k in p["by_kind_24h"]} == {"results", "replay", "press"}
    assert p["baselines"]["results"]["24h"] == 100
    assert p["impressions_curves"]["replay"] and "never" in p


# ---------------------------------------------------------------- readers in the posting path

async def test_press_off_and_reveal_results_still_post(tmp_path, monkeypatch):
    """Every knob at its most restrictive: the round's reveal and results still go out; no press reply."""
    from tests.test_publish import PICKS, make_round
    from agentpitbench.publish import BenchPublisher
    for k in ("X_API_KEY", "X_ACCESS_TOKEN", "X_CLIENT_ID", "X_OAUTH2_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    s = Settings(data_dir=tmp_path / "var", agents=["claude", "codex", "agy", "grok"], dry_run=True,
                 deploy_debounce_s=0, animate=False)
    db = DB(s.db_path)
    db.put(policy.KV, MOST_RESTRICTIVE)
    rid = make_round(db, {**PICKS, "agy": None})
    called = []

    async def collect(*a, **k):
        called.append(1)
        return [{"name": "Claude", "statement": "Close one."}]
    monkeypatch.setattr("agentpitbench.press.collect", collect)
    pub = BenchPublisher(s, db)
    try:
        for e in db.round_entries(rid):
            await pub.on_bet(rid, e["run_id"])
        await pub.on_bets_done(rid)
        db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())
        await pub.on_resolved(rid)
        await pub.press_conference(rid)
    finally:
        await pub.close()
    keys = {json.loads(p.read_text())["post_key"] for p in s.outbox_dir.glob("*.json")}
    assert {f"r{rid}-reveal", f"r{rid}-results"} <= keys
    assert f"r{rid}-press-1" not in keys and not called
