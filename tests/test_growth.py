"""Growth batch (docs/growth-overnight-spec.md): /best-at/ category pages, "Who's on fire" streaks, and the
challenge's "your record vs the AIs" card. Resolved rounds only, so nothing sealed reaches any of them."""
import json
import re
import time

from agentpitbench import exports, i18n, site
from agentpitbench.config import Settings
from agentpitbench.db import DB

AGENTS = ["claude", "codex", "agy", "grok"]
BTC = "Will Bitcoin close above $100k on Friday?"


def rnd(rid, won: dict, q=BTC, state="resolved", t=None, exhibition=False):
    """won: agent -> True (won), False (bet and lost), None (no bet)."""
    return {"round_id": rid, "question": q, "state": state, "exhibition": exhibition,
            "resolved_at": (t if t is not None else rid) if state == "resolved" else None,
            "entries": [{"agent": a, "name": a, "outcome": None if w is None else "Yes", "won": bool(w),
                         "avg_price": None if w is None else 0.5, "pnl": 0 if w is None else (50.0 if w else -100.0)} for a, w in won.items()]}


def test_streak_runs_and_rules():
    rs = [rnd(1, {"claude": True}), rnd(2, {"claude": True}), rnd(3, {"claude": False}), rnd(4, {"claude": True}),
          rnd(5, {"claude": True}), rnd(6, {"claude": True})]
    assert site.streak(rs, "claude") == {"agent": "claude", "cur": 3, "longest": 3}
    # a loss run counts every round not won; a no-bet counts as a loss (the scoreboard's rule)
    rs2 = rs + [rnd(7, {"claude": False}), rnd(8, {"claude": False})]
    assert site.streak(rs2, "claude")["cur"] == -2
    assert site.streak(rs2 + [rnd(9, {"claude": None})], "claude")["cur"] == -3
    assert site.streak(rs + [rnd(7, {"claude": None})], "claude")["cur"] == -1
    assert site.streak(rs + [rnd(7, {"claude": None}), rnd(8, {"claude": True})], "claude")["cur"] == 1
    # unresolved rounds (a running round's sealed pick, a round awaiting resolution) are ignored
    assert site.streak(rs + [rnd(9, {"claude": False}, state="running"),
                             rnd(10, {"claude": False}, state="awaiting_resolution")], "claude")["cur"] == 3
    # resolution order, not round id
    assert site.streak([rnd(1, {"claude": True}, t=5), rnd(2, {"claude": False}, t=1)], "claude")["cur"] == 1
    # the agent page's longest win streak is the same number
    assert site.agent_stats(rs2, "claude")["longest_streak"] == site.streak(rs2, "claude")["longest"] == 3


def test_streaks_hidden_until_a_run_of_two():
    assert site.streaks([rnd(1, {"claude": True, "codex": False})], ["claude", "codex"]) is None
    rows = site.streaks([rnd(1, {"claude": True, "codex": False}), rnd(2, {"claude": True, "codex": False})],
                        ["claude", "codex"])
    assert [(x["agent"], x["cur"]) for x in rows] == [("claude", 2), ("codex", -2)]


def _cats(rounds):
    lb = {"by_category": exports.by_category(rounds, AGENTS)}
    return {c["category"]: c for c in site.category_pages(rounds, lb, AGENTS)}


def test_category_rounds_resolved_and_counted_only():
    rs = [rnd(1, {"claude": True, "codex": False}), rnd(2, {"claude": False, "codex": True}, state="running"),
          rnd(3, {"claude": True, "codex": False}, exhibition=True)]
    c = _cats(rs)["Crypto"]
    assert [r["round_id"] for r in c["rounds"]] == [1] and c["n"] == 1
    assert c["slug"] == "crypto" and _cats(rs)["Other"]["slug"] is None
    # the table is the export's by_category, pivoted, never recomputed
    lb = exports.by_category(rs, AGENTS)
    assert {x["agent"]: x["net_pnl"] for x in c["rows"]} == {a: next(v["net_pnl"] for v in lb[a] if v["category"] == "Crypto")
                                                              for a in ("claude", "codex")}


def test_leader_threshold_and_tie():
    nine = [rnd(i, {"claude": True, "codex": False}) for i in range(1, 10)]
    assert site.CATEGORY_MIN == 10
    assert _cats(nine)["Crypto"]["leader"] is None and _cats(nine)["Crypto"]["streaks"] is None
    ten = nine + [rnd(10, {"claude": True, "codex": False})]
    c = _cats(ten)["Crypto"]
    assert c["leader"]["agent"] == "claude" and c["n"] == 10 and c["streaks"][0]["cur"] == 10
    tie = [rnd(i, {"claude": i % 2 == 0, "codex": i % 2 == 1}) for i in range(1, 11)]
    assert _cats(tie)["Crypto"]["leader"] is None


def _round(db, q, mid, picks, winner=None, running=False):
    rid = db.create_round({"id": mid, "question": q, "outcomes_list": ["Yes", "No"], "prices_list": [0.5, 0.5],
                           "endDate": "2099-01-01T00:00:00Z"})
    for a, o in picks.items():
        run = db.create_run(rid, a)
        db.record_bet(run, outcome=o, avg_price=0.5, shares_filled=200, stake_filled=100)
        db.finish_run(run, "bet", None, None)
    if running:
        return rid
    for e in db.round_entries(rid):
        payout = e["shares_filled"] if e["outcome"] == winner else 0
        db.settle_bet(e["run_id"], payout, payout - e["stake_filled"])
    db.set_round(rid, state="resolved", winner=winner, resolved_at=time.time() + rid)
    return rid


def test_site_pages(tmp_path):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS)
    db = DB(s.db_path)
    for i in range(10):
        _round(db, f"Will Bitcoin hit ${100 + i}k this week?", f"b{i}", {"claude": "Yes", "codex": "No", "agy": "No", "grok": "No"}, "Yes")
    _round(db, "Will Ethereum flip Zebracornia by Friday?", "sealed", {"claude": "Quokkafish"}, running=True)
    _round(db, "Will the Senate pass the farm bill?", "p1", {"claude": "No", "codex": "Yes"}, "Yes")
    exports.export(s, db)
    out = site.build(s)
    assert (out / "best-at/crypto/index.html").exists() and (out / "best-at/politics/index.html").exists()
    assert not (out / "best-at/other").exists() and not (out / "best-at/sports").exists()
    titles = set()
    for code in i18n.CODES:
        d = out / ("" if code == "en" else f"{code}/")
        for p in ("best-at/index.html", "best-at/crypto/index.html", "best-at/politics/index.html"):
            html = (d / p).read_text()
            assert ('dir="rtl"' in html) == (code in i18n.RTL), (code, p)
            assert len(html.encode()) < 100_000 and "Zebracornia" not in html and "Quokkafish" not in html
            titles.add(re.search(r"<title>(.*?)</title>", html).group(1))
        assert 'href="../../best-at/crypto/"' in (d / "agent/claude/index.html").read_text() or \
            'best-at/crypto/' in (d / "agent/claude/index.html").read_text()
        ch = (d / "challenge/index.html").read_text()
        assert "data-rec" in ch and "toBlob" in ch and len(ch.encode()) < 100_000
    assert len(titles) == 3 * len(i18n.CODES)
    en = (out / "best-at/crypto/index.html").read_text()
    assert "Leader so far: Claude" in en and "over 10 rounds" in en and "Who&#39;s on fire" in en
    pol = (out / "best-at/politics/index.html").read_text()
    assert "Too few rounds to name a leader: 1 so far. We name one from 10." in pol
    idx = (out / "best-at/index.html").read_text()
    assert 'href="crypto/"' in idx or "best-at/crypto/" in idx
    assert "Who&#39;s on fire" in (out / "index.html").read_text() and "best-at/" in (out / "leaderboard/index.html").read_text()
    assert "/best-at/" in (out / "llms.txt").read_text()
    assert "best-at" in site.BRANDED
