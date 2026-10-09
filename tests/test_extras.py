"""Category cap, records by category, and the post-match press conference. Dry-run, no network."""
import json
import sys
import time

import pytest

from agentpitbench import exports, press, virality as v
from agentpitbench.watcher import Watcher
from test_core import FakeAPI, gamma
from test_virality import AGENTS, FakePoster, NoCards, env, make_round  # noqa: F401  (env is a fixture)


# ---------- category cap ----------

def test_category():
    assert v.category("Counter-Strike: A vs B (BO3)") == "Esports"
    assert v.category("Will Bitcoin close above $100k?") == "Crypto"
    assert v.category("Who wins the presidential election?") == "Politics"
    assert v.category("Will it snow in Paris?") == "Other"


def test_category_ok():
    assert v.category_ok("Esports", [], 0.4)                              # first round of the week is always fine
    assert not v.category_ok("Esports", ["Esports"], 0.4)                 # 2 of 2 = 100%
    assert v.category_ok("Crypto", ["Esports"], 0.4)
    week = ["Esports", "Crypto", "Politics", "Other"]
    assert v.category_ok("Esports", week, 0.4)                            # 2 of 5 = 40%
    assert not v.category_ok("Esports", week + ["Esports"], 0.4)          # 3 of 6 = 50%


async def test_pick_respects_cap_but_never_starves(env):
    s, db = env
    s.daily_round_cap = 10
    esports = [gamma(i, question=f"Counter-Strike: Team {i} vs Rival (BO3)") for i in (101, 102, 103)]
    api = FakeAPI([gamma(99)])
    w = Watcher(s, db, api)
    await w.poll()                                                         # first start: backlog only
    api.markets_ += esports + [gamma(104, question="Will Bitcoin close above $100k?")]
    await w.poll()
    picked = await w.pick(free_slots=3)
    cats = [v.category(m["question"]) for m in picked]
    assert cats.count("Esports") == 1 and "Crypto" in cats                 # one esports, then the cap holds
    for m in picked:
        db.create_round(m)
    assert [v.category(m["question"]) for m in await w.pick(free_slots=3)] == ["Esports"]  # only over-cap left


def test_by_category_export(env):
    s, db = env
    make_round(db, {"claude": ("CYBERSHOKE", 0.5, 0.7), "grok": ("Nexus", 0.5, 0.6)}, winner="Nexus")
    make_round(db, {"claude": ("CYBERSHOKE", 0.5, 0.7)}, winner="CYBERSHOKE", mid="m2",
               q="Will Bitcoin close above $100k?")
    bc = exports.export(s, db)["leaderboard"]["by_category"]
    claude = {c["category"]: c for c in bc["claude"]}
    assert claude["Esports"]["wins"] == 0 and claude["Esports"]["net_pnl"] == -100
    assert claude["Crypto"]["wins"] == 1 and claude["Crypto"]["played"] == 1
    assert {c["category"] for c in bc["grok"]} == {"Esports", "Crypto"}


# ---------- press conference ----------

def test_extract_formats():
    claude = "\n".join(json.dumps(d) for d in [{"type": "system"}, {"type": "result", "result": "We'll be back."}])
    assert press.extract("claude", claude) == "We'll be back."
    grok = "\n".join(json.dumps(d) for d in [{"type": "text", "data": "The market "}, {"type": "text", "data": "was wrong."},
                                             {"type": "end"}])
    assert press.extract("grok", grok) == "The market was wrong."
    assert press.extract("codex", "header\ncodex\nNo regrets.\ntokens used\n1,924\n\"No regrets.\"\n") == "No regrets."
    assert press.extract("agy", "Lesson learned.\n") == "Lesson learned."
    assert press.extract("agy", "") is None


async def test_press_conference_stores_and_replies(env):
    s, db = env
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7), "codex": ("CYBERSHOKE", 0.62, 0.8),
                          "agy": ("Nexus", 0.4, 0.6), "grok": None}, winner="Nexus")
    asked = []

    async def runner(s_, agent, prompt):
        asked.append((agent, prompt))
        return {"claude": json.dumps({"type": "result", "result": "Variance is a cruel teacher."}),
                "codex": "Check out https://spam.example now"}[agent]   # a link: dropped by the filter

    rnd = next(r for r in exports.export(s, db)["rounds"] if r["round_id"] == rid)
    out = await press.collect(s, db, rnd, runner=runner)
    assert sorted(a for a, _ in asked) == ["claude", "codex"]             # losers only, never no-bets or winners
    assert 'resolved Nexus' in asked[0][1] and "CYBERSHOKE at 62¢" in asked[0][1]
    assert out == [{"agent": "claude", "name": "Claude", "statement": "Variance is a cruel teacher."}]
    assert await press.collect(s, db, rnd, runner=runner) == out and len(asked) == 2   # never asked twice
    entries = {e["agent"]: e for e in exports.export(s, db)["rounds"][0]["entries"]}
    assert entries["claude"]["statement"] == "Variance is a cruel teacher." and entries["codex"]["statement"] == ""
    texts = press.texts(out)
    assert texts == ["Post-match statements:\nClaude: “Variance is a cruel teacher.”"]
    assert "http" not in texts[0]


def test_press_texts_split():
    # round 1 live: four long statements, the old two-reply cap silently dropped Grok's
    sts = [{"name": n, "statement": "x" * 190} for n in ("Claude", "Codex", "Gemini", "Grok")]
    t = press.texts(sts)
    assert 1 < len(t) <= 3 and all(len(x) <= 280 for x in t) and t[0].startswith("Post-match statements:")
    assert all(any(f"{n}:" in x for x in t) for n in ("Claude", "Codex", "Gemini", "Grok"))


async def test_press_runs_real_cli_shape(env, tmp_path, monkeypatch):
    """run_cli: clean home, hard timeout, stdout captured."""
    from agentpitbench import orchestrator
    s, _ = env
    script = tmp_path / "fake"
    script.write_text("#!/bin/sh\necho \"home=$HOME\"\necho 'Next round is ours.'\n")   # program dir is read-only
    script.chmod(0o755)
    monkeypatch.setenv("BENCH_REAL_HOME", str(tmp_path / "real"))
    monkeypatch.setitem(orchestrator.CLIS, "agy", orchestrator.AgentCLI("agy", [str(script), "{prompt}"], ["true"]))
    out = await press.run_cli(s, "agy", "statement please")
    assert press.extract("agy", out) == "Next round is ours."
    assert "home=" in out and "apbh-agy" in out                             # ran in a throwaway home
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setitem(orchestrator.CLIS, "agy", orchestrator.AgentCLI("agy", [str(slow)], ["true"]))
    s.press_timeout_s = 1
    t0 = time.time()
    assert await press.run_cli(s, "agy", "x") == "" and time.time() - t0 < 10


async def test_tracker_calls_press_after_results(env, monkeypatch):
    from agentpitbench.publish import BenchPublisher
    from agentpitbench.tracker import Tracker
    s, db = env
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7), "agy": ("Nexus", 0.4, 0.6)})
    pub = BenchPublisher(s, db)
    pub.cards, pub.poster = NoCards(), FakePoster()
    pub.engage.cards, pub.engage.poster = pub.cards, pub.poster

    async def runner(s_, agent, prompt):  # claude's real output format is stream-json
        return json.dumps({"type": "result", "result": "Tough beat."}) if agent == "claude" else "Tough beat."
    monkeypatch.setattr(press, "run_cli", runner)
    api = FakeAPI([gamma(1)])
    api.markets_ = [dict(gamma(1), id="m1", winner="Nexus", closed=True, active=False, resolvedAt=time.time())]
    try:
        assert await Tracker(s, db, api, pub).check() == [rid]
    finally:
        await pub.close()
    kinds = [p["kind"] for p in pub.poster.posts]
    assert "results" in kinds and "press" in kinds, (kinds, db.round_entries(rid))
    assert kinds.index("press") > kinds.index("results")
    press_post = next(p for p in pub.poster.posts if p["kind"] == "press")
    assert press_post["reply_to"] == next(p["id"] for p in pub.poster.posts if p["kind"] == "results")
    assert "Claude: “Tough beat.”" in press_post["text"]


# ---------- site: press kit, embed, round extras ----------

def rich_site(s, db):
    """A resolved round with every extra, an open High Stakes round, and an exhibition round."""
    done = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7, 60, "Nexus is cooked."), "codex": ("CYBERSHOKE", 0.62, 0.8),
                           "agy": ("Nexus", 0.4, 0.6), "grok": ("Nexus", 0.4, 0.9)}, winner="Nexus")
    db.x("UPDATE bets SET statement='Variance is a cruel teacher.' WHERE run_id=1")
    db.put("wallet_claude", "0x85B0328F9c3DF06d47000a57FE3FAE8A1626856a")
    db.x("UPDATE bets SET tx_hashes=? WHERE run_id=1", json.dumps(["0xabc123def4567890"]))
    db.x("INSERT INTO humans(round_id, user_id, username, pick, won) VALUES(?,?,?,?,?)", done, "u1", "alice", "Nexus", 1)
    db.x("INSERT INTO humans(round_id, user_id, username, pick, won) VALUES(?,?,?,?,?)", done, "u2", "bob", "CYBERSHOKE", 0)
    hs = make_round(db, {"claude": ("Nexus", 0.4, 0.6)}, mid="m2", q="Will Bitcoin close above $100k?")
    snap = json.loads(db.round(hs)["snapshot_json"])
    db.set_round(hs, snapshot_json=json.dumps({**snap, "high_stakes": True, "bench_stake": 500}))
    ex = make_round(db, {"grok": ("Nexus", 0.4, 0.6)}, mid="m3", q="Who wins the presidential election?")
    db.set_round(ex, exhibition=1, exclusion="summon")
    return done, hs, ex


def test_site_extras(env):
    from agentpitbench import i18n, site
    s, db = env
    done, hs, ex = rich_site(s, db)
    exports.export(s, db)
    out = site.build(s)
    rp = (out / f"round/{done}/index.html").read_text()
    for needle in ("Nexus is cooked.", "Variance is a cruel teacher.", "Verified on-chain", "0x85B0", "tx 0xabc123de",
                   "Humans playing along", "2 followers replied with a pick.", "1 got it right.", "@alice", 'href="#m-claude"'):
        assert needle in rp, needle
    assert "Tail Claude" not in rp                                    # resolved: no tail/fade
    live = (out / f"round/{hs}/index.html").read_text()
    assert "High Stakes" in live and "Tail Claude" in live and "Fade Claude" in live
    assert "Not counted in the statistics: summoned by a follower" in (out / f"round/{ex}/index.html").read_text()
    lb = (out / "leaderboard/index.html").read_text()
    for needle in ("All time", "By model", "@alice"):
        assert needle in lb, needle
    assert "Hit rate by category" in (out / "agent/grok/index.html").read_text()
    press_page = (out / "press/index.html").read_text()
    assert "Cite this" in press_page and "@misc{agentpitbench" in press_page and "/embed/" in press_page
    assert (out / "es/press/index.html").exists() and "Kit de prensa" in (out / "es/press/index.html").read_text()
    for a in [*s.agents, "crowd"]:
        assert (out / f"press/mascots/{a}.svg").read_text().startswith("<svg")
    assert (out / "embed/index.html").exists() and (out / "widget/agentpit-widget.js").exists()
    assert "Open challenge" not in (out / "data/index.html").read_text()
    for lang in i18n.CODES:
        for p in (out / (i18n.Translator(lang).prefix() or ".")).glob("**/index.html"):
            assert p.stat().st_size < 100_000, p
