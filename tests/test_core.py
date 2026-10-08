"""Core tests: config, market parsing, eligibility, watcher, scoring, leaderboard, exports, a full round."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentpitbench import config, exports, orchestrator
from agentpitbench.agentpit import best_ask, parse_market
from agentpitbench.db import DB, leaderboard
from agentpitbench.orchestrator import AgentCLI, NullPublisher, Round, build_prompt, simulate_fill
from agentpitbench.tracker import Tracker, score
from agentpitbench.watcher import Watcher, basic_eligible


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def gamma(mid: int, question: str = "Will A beat B?", days: float = 2, **kw) -> dict:
    m = {
        "id": str(mid), "conditionId": f"0xc{mid}", "slug": f"m-{mid}", "question": question,
        "description": "", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.6", "0.4"]',
        "clobTokenIds": f'["t{mid}y", "t{mid}n"]', "active": True, "closed": False, "acceptingOrders": True,
        "endDate": iso(datetime.now(timezone.utc) + timedelta(days=days)), "volume": "0", "liquidity": "0",
        "winner": None,
    }
    m.update(kw)
    return parse_market(m)


BOOK = {"asks": [{"price": "0.62", "size": "100"}, {"price": "0.65", "size": "1000"}],
        "bids": [{"price": "0.60", "size": "50"}]}


class FakeAPI:
    def __init__(self, markets: list[dict], book: dict = BOOK):
        self.markets_ = markets
        self.book_ = book

    async def markets(self, limit=1000, offset=0):
        ms = sorted(self.markets_, key=lambda m: -int(m["id"]))
        return ms[offset:offset + limit]

    async def market(self, mid):
        return next((m for m in self.markets_ if str(m["id"]) == str(mid)), None)

    async def book(self, token_id):
        return self.book_

    async def prices_history(self, cid, interval="1d"):
        return {"history": []}

    async def close(self):
        pass


@pytest.fixture
def s(tmp_path):
    return config.Settings(data_dir=tmp_path / "var", excluded_keywords=["death", "war "])


@pytest.fixture
def db(s):
    return DB(s.db_path)


# config / parsing
def test_load_secrets_env_wins(tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    f.write_text("# comment\nAGENTPIT_KEY_TESTA=fromfile\nAGENTPIT_KEY_TESTB=fromfile\n")
    monkeypatch.setenv("AGENTPIT_KEY_TESTB", "fromenv")
    monkeypatch.delenv("AGENTPIT_KEY_TESTA", raising=False)
    config.load_secrets(f)
    assert config.Settings().agentpit_key("testa") == "fromfile"
    assert config.Settings().agentpit_key("testb") == "fromenv"


def test_market_link_has_utm():
    link = config.Settings().market_link("abc", "r7")
    assert link.startswith("https://agentpit.dev/market/abc?") and "utm_campaign=agentpitbench_r7" in link


def test_parse_market_and_best_ask():
    m = gamma(5)
    assert m["outcomes_list"] == ["Yes", "No"] and m["prices_list"] == [0.6, 0.4]
    assert m["token_ids"] == ["t5y", "t5n"]
    assert best_ask(BOOK) == 0.62 and best_ask({"asks": []}) is None


def test_simulate_fill_walks_book_within_limit():
    f = simulate_fill(BOOK, 0.65, 100)
    assert f["cost"] == pytest.approx(100)
    assert f["shares"] == pytest.approx(100 + (100 - 62) / 0.65)
    f = simulate_fill(BOOK, 0.62, 100)  # only the first level is within the limit
    assert f["shares"] == pytest.approx(100) and f["cost"] == pytest.approx(62)
    assert simulate_fill(BOOK, 0.5, 100)["shares"] == 0


# eligibility / watcher
def test_basic_eligible(s):
    assert basic_eligible(s, gamma(1))
    assert not basic_eligible(s, gamma(1, days=10))            # closes too late
    assert not basic_eligible(s, gamma(1, days=0.01))          # closes too soon
    assert not basic_eligible(s, gamma(1, question="Will the war end?"))
    assert not basic_eligible(s, gamma(1, closed=True))
    assert not basic_eligible(s, gamma(1, clobTokenIds='["only-one"]'))


async def test_watcher_skips_backlog_then_picks_new(s, db):
    api = FakeAPI([gamma(1), gamma(2)])
    w = Watcher(s, db, api)
    assert await w.poll() == []                     # first start: backlog recorded, no rounds
    api.markets_ += [gamma(3), gamma(4, question="death toll above 10?")]
    new = await w.poll()
    assert [m["id"] for m in new] == ["3"]
    assert await w.poll() == []                     # not reported twice
    picked = await w.pick(free_slots=3)
    assert [m["id"] for m in picked] == ["3"]
    assert await w.pick(free_slots=3) == []         # started markets are not picked again


async def test_watcher_respects_daily_cap(s, db):
    s.daily_round_cap = 1
    api = FakeAPI([gamma(1)])
    w = Watcher(s, db, api)
    await w.poll()
    api.markets_ += [gamma(2), gamma(3)]
    await w.poll()
    db.create_round(gamma(99))                      # one round already today
    assert await w.pick(free_slots=3) == []


async def test_watcher_drops_markets_without_sellers(s, db):
    api = FakeAPI([gamma(1)], book={"asks": [], "bids": []})
    w = Watcher(s, db, api)
    await w.poll()
    api.markets_.append(gamma(2))
    await w.poll()
    assert await w.pick(3) == []
    assert db.one("SELECT eligible FROM seen_markets WHERE market_id='2'")["eligible"] == 0


# scoring / tracker / leaderboard
def make_round(db: DB, mid: int, picks: dict[str, tuple | None]) -> int:
    rid = db.create_round(gamma(mid))
    for agent, pick in picks.items():
        run = db.create_run(rid, agent)
        if pick is None:
            db.record_bet(run, outcome=None)
        else:
            outcome, price, conf = pick
            db.record_bet(run, outcome=outcome, avg_price=price, shares_filled=100 / price,
                          stake_filled=100, confidence=conf, decided_s=30)
        db.finish_run(run, "bet" if pick else "timeout", None, None)
    db.set_round(rid, state="awaiting_resolution")
    return rid


def test_score():
    entries = [{"run_id": 1, "outcome": "Yes", "shares_filled": 160, "stake_filled": 100},
               {"run_id": 2, "outcome": "No", "shares_filled": 250, "stake_filled": 100},
               {"run_id": 3, "outcome": None, "shares_filled": 0, "stake_filled": 0}]
    assert score(entries, "Yes") == [(1, 160, 60), (2, 0, -100), (3, 0, 0)]


async def test_tracker_resolves_settles_once_and_publishes(s, db):
    rid = make_round(db, 1, {"claude": ("Yes", 0.5, 0.8), "codex": ("No", 0.5, 0.6), "agy": None})
    api = FakeAPI([gamma(1, winner="Yes", closed=True, active=False, resolvedAt=1000.0)])

    class Pub(NullPublisher):
        resolved = []
        async def on_resolved(self, round_id):
            self.resolved.append(round_id)

    pub = Pub()
    assert await Tracker(s, db, api, pub).check() == [rid]
    assert pub.resolved == [rid]
    by = {e["agent"]: e for e in db.round_entries(rid)}
    assert by["claude"]["pnl"] == pytest.approx(100) and by["codex"]["pnl"] == pytest.approx(-100)
    assert by["agy"]["pnl"] == 0
    db.settle_bet(by["claude"]["run_id"], 0, -999)  # a recorded result is never overwritten
    assert db.round_entries(rid)[0]["pnl"] == pytest.approx(100)
    assert await Tracker(s, db, api, pub).check() == []


async def test_tracker_voids_long_closed_market(s, db):
    rid = make_round(db, 1, {"claude": ("Yes", 0.5, 0.8)})
    db.set_round(rid, end_date=iso(datetime.now(timezone.utc) - timedelta(days=s.void_after_days + 1)))
    api = FakeAPI([gamma(1, closed=True, active=False)])
    assert await Tracker(s, db, api, NullPublisher()).check() == [rid]
    assert db.round(rid)["state"] == "void"


def test_tracker_recover_marks_unfinished_runs_crashed(s, db):
    rid = db.create_round(gamma(1))
    run = db.create_run(rid, "claude")
    Tracker(s, db, None, NullPublisher()).recover()
    e = db.round_entries(rid)[0]
    assert e["exit_reason"] == "crash" and e["outcome"] is None and e["placed_at"] is not None
    assert db.round(rid)["state"] == "awaiting_resolution"


def test_leaderboard(s, db):
    t = Tracker(s, db, None, NullPublisher())
    r1 = make_round(db, 1, {"claude": ("Yes", 0.5, 0.9), "codex": ("No", 0.5, 0.7), "agy": None})
    t.resolve(r1, "Yes", 1)
    r2 = make_round(db, 2, {"claude": ("Yes", 0.25, 0.6), "codex": ("Yes", 0.25, 0.8), "agy": ("No", 0.8, 0.5)})
    t.resolve(r2, "Yes", 2)
    board = {r["agent"]: r for r in leaderboard(db, s.agents)}
    c = board["claude"]
    assert (c["played"], c["wins"], c["losses"], c["streak"], c["rank"]) == (2, 2, 0, 2, 1)
    assert c["net_pnl"] == pytest.approx(400) and c["roi"] == pytest.approx(2)
    assert c["brier"] == pytest.approx(((0.9 - 1) ** 2 + (0.6 - 1) ** 2) / 2, abs=1e-4)
    assert board["codex"]["streak"] == 1 and board["codex"]["wins"] == 1
    assert board["agy"]["no_bets"] == 1 and board["agy"]["streak"] == -2 and board["agy"]["rank"] == 4
    assert board["grok"]["played"] == 0 and board["grok"]["rank"] == 3


def test_export_files(s, db):
    rid = make_round(db, 1, {"claude": ("Yes", 0.5, 0.9), "codex": ("No", 0.5, 0.7), "agy": None})
    Tracker(s, db, None, NullPublisher()).resolve(rid, "Yes", 1000)
    out = exports.export(s, db)
    rounds = json.loads((s.export_dir / "rounds.json").read_text())
    assert rounds == json.loads(json.dumps(out["rounds"]))
    r = rounds[0]
    assert r["split"] is True and r["winner"] == "Yes" and r["market_link"].endswith("agentpitbench_r1")
    assert {e["agent"]: e["won"] for e in r["entries"]} == {"claude": True, "codex": False, "agy": False}
    lb = json.loads((s.export_dir / "leaderboard.json").read_text())
    assert lb["series"]["claude"] == [[1000, 100.0]]
    csv_lines = (s.export_dir / "bets.csv").read_text().splitlines()
    assert len(csv_lines) == 4 and csv_lines[0].startswith("round_id,")


# prompt / agent sandbox
def test_build_prompt_with_and_without_memory():
    m = gamma(1, question="Will {x} happen?")
    p = build_prompt(m, [])
    assert "Will {x} happen?" in p and "Yes 0.600, No 0.400" in p and "first round" in p
    mem = [{"question": "Q1", "outcome": "Yes", "winner": "Yes", "pnl": 50.0},
           {"question": "Q2", "outcome": None, "winner": "No", "pnl": None}]
    p = build_prompt(m, mem)
    assert "1-1, net P&L +50.0" in p and "you picked no bet" in p


def test_agent_env_strips_secrets(monkeypatch):
    monkeypatch.setenv("AGENTPIT_KEY_CLAUDE", "secret")
    monkeypatch.setenv("X_API_KEY", "secret")
    env = orchestrator.agent_env("/sock", "/shim")
    assert "AGENTPIT_KEY_CLAUDE" not in env and "X_API_KEY" not in env
    assert env["BENCH_SOCKET"] == "/sock" and env["PATH"].startswith("/shim")


# full round with fake agent CLIs that call `bench`
FAKE_AGENT = """#!/bin/sh
bench market > /dev/null || exit 3
case "$1" in
  bet)   bench bet --outcome "$2" --confidence 0.7 --rationale "fake agent $2"; echo '"model": "fake-1"';;
  twice) bench bet --outcome Yes --confidence 0.7 --rationale one; bench bet --outcome No --confidence 0.5 --rationale two && exit 9;;
  hang)  sleep 30;;
  crash) exit 1;;
esac
"""


async def test_round_end_to_end(s, db, tmp_path, monkeypatch):
    script = tmp_path / "fake-agent"
    script.write_text(FAKE_AGENT)
    script.chmod(0o755)
    monkeypatch.setattr(orchestrator, "CLIS", {
        "claude": AgentCLI("claude", [str(script), "bet", "Yes"], ["true"], [r'"model"\s*:\s*"([^"]+)"']),
        "codex": AgentCLI("codex", [str(script), "twice"], ["true"]),
        "agy": AgentCLI("agy", [str(script), "hang"], ["true"]),
        "grok": AgentCLI("grok", [str(script), "crash"], ["true"]),
    })
    s.decision_limit_s = 4
    s.restart_min_remaining_s = 1

    class Pub(NullPublisher):
        bets, done = [], []
        async def on_bet(self, round_id, run_id):
            self.bets.append(run_id)
        async def on_bets_done(self, round_id):
            self.done.append(round_id)

    pub = Pub()
    rid = await Round(s, db, FakeAPI([gamma(1)]), gamma(1), pub).run()
    by = {e["agent"]: e for e in db.round_entries(rid)}
    assert by["claude"]["outcome"] == "Yes" and by["claude"]["exit_reason"] == "bet"
    assert by["claude"]["model_reported"] == "fake-1"
    assert by["claude"]["stake_filled"] == pytest.approx(62)   # FAK at best ask: partial fill per spec
    assert by["codex"]["outcome"] == "Yes" and by["codex"]["rationale"] == "one"   # second bet refused
    assert by["agy"]["outcome"] is None and by["agy"]["exit_reason"] == "timeout"
    assert by["grok"]["outcome"] is None and by["grok"]["exit_reason"] == "crash"
    assert "restarting" in Path(by["grok"]["transcript_path"]).read_text()           # one restart, then gives up
    assert db.round(rid)["state"] == "awaiting_resolution"
    assert pub.done == [rid] and sorted(pub.bets) == sorted(e["run_id"] for e in by.values())
    transcript = Path(by["claude"]["transcript_path"]).read_text()
    assert "--- prompt ---" in transcript and '"placed": true' in transcript
