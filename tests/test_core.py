"""Core tests: config, market parsing, eligibility, watcher, scoring, leaderboard, exports, a full round."""
from __future__ import annotations

import json
import shutil
import time
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
    assert link.startswith("https://agentpit.dev/start?market=abc&") and "utm_campaign=agentpitbench_r7" in link


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
    assert not basic_eligible(s, gamma(1, outcomePrices='["0.9", "0.1"]'))   # near-certain: dead round
    assert basic_eligible(s, gamma(1, outcomePrices='["0.85", "0.15"]'))


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
    now = time.time()
    old = make_round(db, 2, {"claude": ("No", 0.5, 0.9)})
    Tracker(s, db, None, NullPublisher()).resolve(old, "Yes", 1000)      # a past season
    Tracker(s, db, None, NullPublisher()).resolve(rid, "Yes", now)
    out = exports.export(s, db)
    rounds = json.loads((s.export_dir / "rounds.json").read_text())
    assert rounds == json.loads(json.dumps(out["rounds"]))
    r = next(r for r in rounds if r["round_id"] == rid)
    assert r["split"] is True and r["winner"] == "Yes" and r["market_link"].endswith("agentpitbench_r1")
    assert {e["agent"]: e["won"] for e in r["entries"]} == {"claude": True, "codex": False, "agy": False}
    lb = json.loads((s.export_dir / "leaderboard.json").read_text())
    assert lb["series"]["claude"] == [[now, 100.0]]                        # this season only
    at = {a["agent"]: a for a in lb["all_time"]["agents"]}
    assert at["claude"]["played"] == 2 and at["claude"]["net_pnl"] == 0     # +100 now, -100 in 1970
    csv_lines = (s.export_dir / "bets.csv").read_text().splitlines()
    assert len(csv_lines) == 5 and csv_lines[0].startswith("round_id,") and csv_lines[0].endswith(",quote")


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
    for k in ("X_API_KEY", "X_ACCESS_TOKEN", "X_CLIENT_SECRET", "X_OAUTH2_REFRESH_TOKEN", "X_OAUTH2_ACCESS_TOKEN",
              "XAI_API_KEY", "GH_PAGES_DEPLOY_KEY", "HF_TOKEN", "KAGGLE_KEY", "HUGGINGFACE_TOKEN"):
        monkeypatch.setenv(k, "secret")
    env = orchestrator.agent_env("/sock", "/shim")
    assert not [k for k, v in env.items() if v == "secret"]
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
    monkeypatch.setenv("BENCH_REAL_HOME", str(tmp_path / "realhome"))
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
    assert by["claude"]["stake_filled"] == pytest.approx(100)  # default limit = best ask + 5c walks to 0.65
    assert by["codex"]["outcome"] == "Yes" and by["codex"]["rationale"] == "one"   # second bet refused
    assert by["agy"]["outcome"] is None and by["agy"]["exit_reason"] == "timeout"
    assert by["grok"]["outcome"] is None and by["grok"]["exit_reason"] == "crash"
    assert "restarting" in Path(by["grok"]["transcript_path"]).read_text()           # one restart, then gives up
    assert db.round(rid)["state"] == "awaiting_resolution"
    assert pub.done == [rid] and sorted(pub.bets) == sorted(e["run_id"] for e in by.values())
    transcript = Path(by["claude"]["transcript_path"]).read_text()
    assert "--- prompt ---" in transcript and '"placed": true' in transcript


# clean agent homes: only the login goes in, refreshed logins come back, nothing else does
async def test_clean_home_syncs_rotated_login_back(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / ".codex").mkdir(parents=True)
    (real / ".codex/auth.json").write_text('{"refresh": "r0"}')
    (real / ".codex/config.toml").write_text("operator config")
    (real / ".claude").mkdir()
    (real / ".claude/.credentials.json").write_text('{"t": 1}')
    (real / ".claude/CLAUDE.md").write_text("operator instructions")
    (real / ".claude.json").write_text('{"oauthAccount": {"a": 1}, "mcpServers": {"x": {}}, "projects": {}}')
    monkeypatch.setenv("BENCH_REAL_HOME", str(real))
    from agentpitbench.orchestrator import CleanHome
    ch = CleanHome("codex")
    home = ch.setup()
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file()) == [".codex/auth.json"]
    (home / ".codex/auth.json").write_text('{"refresh": "r1"}')          # the CLI rotated its token
    await ch.sync()
    assert (real / ".codex/auth.json").read_text() == '{"refresh": "r1"}'
    assert oct((real / ".codex/auth.json").stat().st_mode & 0o777) == "0o600"
    ch.cleanup()
    assert not home.exists()
    cl = CleanHome("claude")
    h2 = cl.setup()
    assert json.loads((h2 / ".claude.json").read_text()) == {"oauthAccount": {"a": 1}}   # no MCP, no projects
    assert not (h2 / ".claude/CLAUDE.md").exists()
    (h2 / ".claude.json").write_text('{"junk": 1}')
    await cl.sync()
    assert "mcpServers" in (real / ".claude.json").read_text()        # .claude.json is never synced back
    env = orchestrator.agent_env("/s", "/x", h2)
    assert env["HOME"] == str(h2) and env["XDG_CONFIG_HOME"].startswith(str(h2)) and "CODEX_HOME" not in env
    cl.cleanup()


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap not installed")
async def test_sandbox_hides_operator_files(tmp_path, monkeypatch):
    real = tmp_path / "realhome"
    (real / ".config/agentpitbench").mkdir(parents=True)
    (real / ".config/agentpitbench/secrets.env").write_text("AGENTPIT_KEY_CLAUDE=topsecret")
    monkeypatch.setenv("BENCH_REAL_HOME", str(real))
    probe = tmp_path / "bin" / "probe"
    probe.parent.mkdir()
    repo_file = Path(orchestrator.__file__).resolve()
    probe.write_text(f"""#!/bin/sh
cat {real}/.config/agentpitbench/secrets.env 2>&1
cat {repo_file} >/dev/null 2>&1 && echo REPO-VISIBLE
ls /home | head -1
cat "$HOME/.ssh/id_rsa" 2>/dev/null
echo home=$HOME
bench --help >/dev/null && echo BENCH-OK
""")
    probe.chmod(0o755)
    from agentpitbench.orchestrator import CleanHome, agent_env, sandboxed, write_shim
    import asyncio, tempfile
    ch = CleanHome("grok")
    home = ch.setup()
    work, ctl = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    write_shim(ctl)
    p = await asyncio.create_subprocess_exec(*sandboxed([str(probe)], home, work, ctl), env=agent_env("/x", str(ctl), home),
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out = (await p.communicate())[0].decode()
    ch.cleanup()
    assert "topsecret" not in out and "No such file" in out
    assert "REPO-VISIBLE" not in out and "BENCH-OK" in out and f"home={home}" in out


def test_home_model_reads_agy_log(tmp_path):
    from agentpitbench.orchestrator import home_model
    d = tmp_path / ".gemini/antigravity-cli/log"
    d.mkdir(parents=True)
    (d / "cli-20261008_180456.log").write_text(
        'I1008 model_config_manager.go:327] Propagating selected model override to backend: label="Gemini 3.8 Flash (High)"\n')
    assert home_model("agy", tmp_path) == "Gemini 3.8 Flash (High)" and home_model("codex", tmp_path) is None


async def test_live_order_sized_by_walking_the_book(s, db, monkeypatch):
    """Sizing at the limit price under-spent (93 of 100) in the live canary; size by the book instead."""
    from agentpitbench import agentpit
    seen = {}

    async def fake_buy(self, token_id, price, stake, coid, size=None):
        seen.update(price=price, size=size)
        return {"orderID": "o1", "fill": {"shares": size, "cost": 100.0, "avg_price": 100.0 / size, "tx_hashes": []}}

    monkeypatch.setattr(agentpit.Agentpit, "buy_fak", fake_buy)
    s.dry_run = False
    r = Round(s, db, FakeAPI([gamma(1)]), gamma(1), NullPublisher(), agents=["claude"])
    r.round_id = db.create_round(gamma(1))
    r.run_ids["claude"] = db.create_run(r.round_id, "claude")
    import asyncio, time
    r.bet_done["claude"] = asyncio.Event()
    r.launched_at["claude"] = time.time()
    await r._place("claude", {"outcome": "Yes", "rationale": "x", "confidence": 0.6})
    assert seen["price"] == pytest.approx(0.67)                       # best ask 0.62 + 5c slippage
    assert seen["size"] == pytest.approx(100 + 38 / 0.65)              # 100 @ .62, rest @ .65 = 100 tokens
