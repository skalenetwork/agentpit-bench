"""Credibility layer: forecast sweep, Brier scoring and statistics, sealing, duels, contrarian calls,
significance, exclusions from the neutral tables, and infra-failure voiding."""
import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from agentpitbench import exports, forecast, orchestrator
from agentpitbench.config import Settings
from agentpitbench.db import DB


def iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(mid, q="Will BTC close above 90k?", prices=(0.6, 0.4), outcomes=("Yes", "No"), days=2):
    return {"id": str(mid), "question": q, "slug": f"m{mid}", "outcomes_list": list(outcomes),
            "prices_list": list(prices), "token_ids": [f"t{mid}a", f"t{mid}b"], "endDate": iso(days),
            "active": True, "acceptingOrders": True, "closed": False, "volume": "0", "liquidity": "0"}


class FakeAPI:
    def __init__(self, markets, winners=None):
        self.ms, self.winners = markets, winners or {}

    async def markets(self, limit=1000, offset=0):
        return self.ms[offset:offset + limit]

    async def market(self, mid):
        m = next((x for x in self.ms if str(x["id"]) == str(mid)), None)
        if m and str(mid) in self.winners:
            return {**m, "winner": self.winners[str(mid)], "closed": True}
        return m


@pytest.fixture
def env(tmp_path):
    s = Settings(data_dir=tmp_path / "var", sandbox=False, sweep_markets=3)
    return s, DB(s.db_path)


# ---------- parsing and scoring ----------

def test_parse_takes_last_object_clamps_and_normalises():
    ms = [market(1), market(2, outcomes=("A", "B", "C"), prices=(0.5, 0.3, 0.2))]
    out = ('thinking... {"forecasts": [{"id": "1", "probabilities": {"Yes": 0.2}}]}\n'
           'final: ```json\n{"forecasts": [{"id": "1", "probabilities": {"yes": 1.0}},'
           ' {"id": "2", "probabilities": {"A": 50, "B": 30, "C": 20}}, {"id": "9", "probabilities": {"X": 1}}]}\n```')
    got = forecast.parse("codex", out, ms)
    assert set(got) == {"1", "2"}
    assert got["1"]["Yes"] == pytest.approx(0.99) and got["1"]["No"] == pytest.approx(0.01)  # completed + clamped
    assert sum(got["2"].values()) == pytest.approx(1) and got["2"]["A"] == pytest.approx(0.5)
    assert forecast.parse("codex", "no json here", ms) == {}


def test_parse_reads_claude_and_grok_stream_formats():
    ms = [market(1)]
    answer = '{"forecasts": [{"id": "1", "probabilities": {"Yes": 0.7, "No": 0.3}}]}'
    claude = json.dumps({"type": "result", "result": "Here you go\n" + answer})
    grok = "\n".join(json.dumps({"type": "text", "data": answer[i:i + 9]}) for i in range(0, len(answer), 9))
    assert forecast.parse("claude", claude, ms)["1"]["Yes"] == pytest.approx(0.7)
    assert forecast.parse("grok", grok, ms)["1"]["No"] == pytest.approx(0.3)


def test_brier_is_classic_for_two_outcomes():
    assert forecast.brier({"Yes": 0.8, "No": 0.2}, "Yes") == pytest.approx(0.04)
    assert forecast.brier({"Yes": 0.8, "No": 0.2}, "No") == pytest.approx(0.64)
    assert forecast.brier({"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}, "A") == pytest.approx(1 / 3)


def test_pick_markets_mixes_categories_and_skips_swept(env):
    s, _ = env
    ms = [market(i, q=f"Will Bitcoin hit {i}k?") for i in range(1, 6)] + \
         [market(10 + i, q=f"Counter-Strike: A vs B game {i}") for i in range(3)] + [market(99, prices=(0.97, 0.03))]
    got = forecast.pick_markets(s, ms, already={"1"}, n=4)
    ids = [m["id"] for m in got]
    assert "1" not in ids and "99" not in ids and len(ids) == 4
    assert any(i.startswith("1") and len(i) == 2 for i in ids)        # an esports market made it in


# ---------- the sweep end to end ----------

def fake_runner(answers):
    async def run(s, agent, prompt, tpath, cli=None):
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tpath.write_text("--- output ---\n" + answers[agent])
        return answers[agent], f"{agent}-model", False
    return run


def ans(p1, p2):
    return json.dumps({"forecasts": [{"id": "1", "probabilities": {"Yes": p1, "No": 1 - p1}},
                                     {"id": "2", "probabilities": {"Yes": p2, "No": 1 - p2}}]})


async def test_sweep_score_seal_and_metrics(env):
    s, db = env
    s.agents = ["claude", "codex", "grok"]
    api = FakeAPI([market(1, prices=(0.6, 0.4)), market(2, q="Will ETH rally?", prices=(0.3, 0.7))])
    answers = {"claude": ans(0.9, 0.1), "codex": "You've hit your usage limit. Try again in 4 hours.", "grok": "garbage"}
    status = await forecast.sweep(s, db, api, "2026-10-10", runner=fake_runner(answers))
    assert status == {"claude": "ok", "codex": "infra", "grok": "failed"}
    assert db.one("SELECT COUNT(*) c FROM forecasts")["c"] == 2
    sha = forecast.seal(s, db, "2026-10-10")
    f = s.export_dir / "forecasts" / "2026-10-10.json"
    assert hashlib.sha256(f.read_bytes()).hexdigest() == sha and forecast.seal(s, db, "2026-10-10") == sha
    assert json.loads((s.export_dir / "sealed.json").read_text())[0]["sha256"] == sha
    # resolution: both markets end in the past and resolve Yes / No
    db.x("UPDATE sweep_markets SET end_date=?", iso(-1))
    api.winners = {"1": "Yes", "2": "No"}
    assert await forecast.score_due(s, db, api) == 2
    m = forecast.metrics(db, s.agents, b=200)
    c = next(a for a in m["agents"] if a["agent"] == "claude")
    assert c["n"] == 2 and c["brier"] == pytest.approx(0.01) and c["rank"] == 1 and c["coverage"] == 1
    assert c["ci"][0] <= c["brier"] <= c["ci"][1] and c["vs_crowd"]["mean_diff"] < 0
    codex = next(a for a in m["agents"] if a["agent"] == "codex")
    assert codex["n"] == 0 and codex["offered"] == 0                 # infra day: not held against it
    grok = next(a for a in m["agents"] if a["agent"] == "grok")
    assert grok["offered"] == 2 and grok["coverage"] == 0            # its own failure: counts as no forecast
    assert m["crowd"]["brier"] == pytest.approx((0.16 + 0.09) / 2)
    again = await forecast.sweep(s, db, api, "2026-10-10", runner=fake_runner(answers))
    assert again["claude"] == "done"                                 # idempotent per agent per day


def test_sign_test_and_significance():
    assert forecast._sign_test([-1] * 10) < 0.01 and forecast._sign_test([1, -1]) == 1.0
    m = {"agents": [{"agent": "claude", "n": 40, "vs_crowd": {"verdict": "better", "sign_test_p": 0.01}}]}
    assert forecast.significance([], m, ["claude"])["label"] == "n=40 · significant vs the Crowd (p<0.05)"
    m["agents"][0]["vs_crowd"]["verdict"] = "inconclusive"
    assert forecast.significance([], m, ["claude"])["label"] == "n=40 · not yet significant"


def test_contrarian_calls(env):
    s, db = env
    db.x("INSERT INTO sweep_markets(market_id,sweep_date,question,outcomes,prices,end_date,state,winner)"
         " VALUES('1','2026-10-10','Q?','[\"Yes\",\"No\"]','[0.3,0.7]',?, 'resolved','Yes')", iso(-1))
    db.x("INSERT INTO forecasts(sweep_date,agent,market_id,probs) VALUES('2026-10-10','claude','1','{\"Yes\":0.8,\"No\":0.2}')")
    db.x("INSERT INTO forecasts(sweep_date,agent,market_id,probs) VALUES('2026-10-10','codex','1','{\"Yes\":0.45,\"No\":0.55}')")
    calls = forecast.contrarian(db, ["claude", "codex"])
    assert len(calls) == 1 and calls[0]["agent"] == "claude" and calls[0]["outcome"] == "Yes"
    assert calls[0]["agent_p"] == 0.8 and calls[0]["market_p"] == 0.3 and calls[0]["right"] is True


async def test_launch_day_duel(env, monkeypatch):
    s, db = env
    api = FakeAPI([market(1), market(2, q="Will ETH rally?")])
    await forecast.sweep(s, db, api, forecast.today(), runner=fake_runner({a: ans(0.6, 0.4) for a in s.agents}))
    db.put("agent_models", {**{a: m[0] for a, m in forecast.AGENT_MODELS.items()}, "claude": "claude-old-1"})
    seen = []

    async def runner(s_, agent, prompt, tpath, cli=None):
        seen.append(cli.argv)
        old = "claude-old-1" in cli.argv
        return ans(0.5 if old else 0.9, 0.5 if old else 0.1), None, False

    done = await forecast.duels_due(s, db, api, runner=runner)
    assert done and done[0]["old"] == 2 and done[0]["new"] == 2
    assert any("claude-old-1" in a for a in seen) and any(forecast.AGENT_MODELS["claude"][0] in a for a in seen)
    d = forecast.duels(db)[0]
    assert d["status"] == "run" and d["result"] is None               # waits for resolution
    db.x("UPDATE sweep_markets SET end_date=?", iso(-1))
    api.winners = {"1": "Yes", "2": "No"}
    await forecast.score_due(s, db, api)
    r = forecast.duels(db)[0]["result"]
    assert r["n"] == 2 and r["new"] < r["old"]
    m = forecast.metrics(db, s.agents, b=100)
    assert all(not a["agent"].startswith("duel") for a in m["agents"])  # never in the season tables


# ---------- exclusions ----------

def test_preseason_and_high_stakes_rounds_are_excluded(env):
    s, db = env
    old = db.create_round(market(1))
    db.x("UPDATE rounds SET started_at=? WHERE round_id=?", s.season_start_ts - 3600, old)
    hs = db.create_round({**market(2), "high_stakes": True, "bench_stake": 500})
    normal = db.create_round(market(3))
    rounds = {r["round_id"]: r for r in exports.export(s, db)["rounds"]}
    assert rounds[old]["exclusion"] == "pre-season" and rounds[old]["exhibition"]
    assert rounds[hs]["exclusion"] == "high-stakes" and rounds[hs]["exhibition"]
    assert rounds[normal]["exclusion"] is None and not rounds[normal]["exhibition"]
    assert json.loads((s.export_dir / "metrics.json").read_text())["methodology_version"] == s.methodology_version


# ---------- infra failures ----------

def test_infra_failure_patterns():
    f = orchestrator.infra_failure
    assert f("bwrap: setting up uid map: Permission denied") == "sandbox"
    assert f('{"type":"error","message":"Not signed in. To authenticate..."}') == "auth"
    assert f("You've hit your usage limit. Try again in 3 hours.") == "quota"
    assert f("I bet on the Hawks: deep roster, home ice.") is None


async def test_round_with_infra_failure_is_voided_and_posts_nothing(tmp_path, monkeypatch):
    from tests.test_core import FakeAPI as RoundAPI, gamma
    s = Settings(data_dir=tmp_path / "var", decision_limit_s=4, restart_min_remaining_s=1)
    db = DB(s.db_path)
    monkeypatch.setenv("BENCH_REAL_HOME", str(tmp_path / "realhome"))
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.write_text('#!/bin/sh\nbench bet --outcome Yes --confidence 0.7 --rationale ok\n')
    bad.write_text("#!/bin/sh\necho \"Error: You've hit your usage limit. Try again in 4 hours.\"\nexit 1\n")
    for p in (good, bad):
        p.chmod(0o755)
    monkeypatch.setattr(orchestrator, "CLIS", {
        "claude": orchestrator.AgentCLI("claude", [str(good)], ["true"]),
        "codex": orchestrator.AgentCLI("codex", [str(bad)], ["true"]),
    })

    class Pub(orchestrator.NullPublisher):
        def __init__(self):
            self.bets, self.done, self.voided = [], [], []
        async def on_bet(self, rid, run_id):
            self.bets.append(run_id)
        async def on_bets_done(self, rid):
            self.done.append(rid)
        async def void_round(self, rid):
            self.voided.append(rid)

    pub = Pub()
    rid = await orchestrator.Round(s, db, RoundAPI([gamma(1)]), gamma(1), pub, agents=["claude", "codex"]).run()
    r = db.round(rid)
    assert r["state"] == "void" and r["exclusion"] == "infra" and r["exhibition"] == 1
    assert pub.voided == [rid] and pub.done == []                      # no split card, cleanup requested
    codex = next(e for e in db.round_entries(rid) if e["agent"] == "codex")
    assert codex["exit_reason"] == "infra" and codex["run_id"] not in pub.bets   # no forfeit post


async def test_publisher_void_round_deletes_posts(tmp_path):
    from agentpitbench.publish import BenchPublisher
    s = Settings(data_dir=tmp_path / "var", dry_run=True)
    db = DB(s.db_path)
    rid = db.create_round(market(1))
    pub = BenchPublisher(s, db)
    try:
        tid = await pub.poster.post(f"r{rid}-decision-claude", "decision", "hello", round_id=rid)
        assert (s.outbox_dir / f"r{rid}-decision-claude.json").exists() and tid.startswith("dry-")
        await pub.void_round(rid)
        assert not (s.outbox_dir / f"r{rid}-decision-claude.json").exists()
        assert db.one("SELECT COUNT(*) c FROM tweets WHERE round_id=?", rid)["c"] == 0
    finally:
        await pub.close()


async def test_sandbox_self_check_reports_failure(monkeypatch):
    monkeypatch.setattr(orchestrator.shutil, "which", lambda name: None)
    assert await orchestrator.sandbox_ok() is False


def test_void_rounds_free_their_daily_slot(env):
    from agentpitbench.watcher import Watcher
    s, db = env
    w = Watcher(s, db, None)
    a, b = db.create_round(market(1)), db.create_round(market(2))
    assert w.rounds_today() == 2
    db.set_round(a, state="void")
    assert w.rounds_today() == 1


def test_musk_tweet_brackets_are_excluded():
    from agentpitbench import config
    from agentpitbench.watcher import basic_eligible
    s = config.load("bench.toml")
    assert not basic_eligible(s, market(1, q="Will Elon Musk post 115-139 tweets from October 8 to October 10, 2026?"))
    assert basic_eligible(s, market(2, q="Will BTC close above 90k?"))


def test_report_thread_and_personal_draft():
    from agentpitbench.engage import personal_draft, report_thread
    from agentpitbench.twitter import tweet_len
    rep = {"week": "2026-W41", "markets": 40, "rounds": 5, "crowd_brier": 0.25,
           "forecast": [{"agent": a, "name": a.title(), "n": 40, "brier": b} for a, b in
                        (("claude", 0.11), ("codex", 0.19), ("agy", 0.21), ("grok", 0.27))],
           "betting": [{"agent": a, "name": a.title(), "wins": 3, "losses": 2, "profit": 41.5} for a in ("claude", "codex", "agy", "grok")],
           "contrarian": {"total": 7, "right": 4}}
    posts = report_thread(rep, "https://agentpitbench.org/reports/2026-W41/")
    assert len(posts) == 3 and all(tweet_len(p) <= 280 for p in posts)
    assert "http" not in posts[0] + posts[1] and "agentpitbench.org/reports/2026-W41/" in posts[2]
    draft = personal_draft(rep, "https://agentpitbench.org/reports/2026-W41/")
    assert "Claude" in draft and "Beat the market" in draft and draft.endswith("\n")
