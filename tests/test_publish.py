"""Publishing layer in dry-run: cards, outbox tweets, thread order, site build. Four agents per round."""
import json
import time
import xml.etree.ElementTree as ET

import pytest

from agentpitbench import exports, site
from agentpitbench.cards import CardRenderer, results_headline, split_headline
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.publish import BenchPublisher
from agentpitbench.twitter import MAX_LEN, Poster, decision_text, results_text, split_text, tweet_len

AGENTS = ["claude", "codex", "agy", "grok"]
LONG = ("The order book is thin but the favourite has won nine of its last ten series on LAN, and the "
        "underdog is playing with a stand-in, so I am taking the favourite at a fair price here. " * 3)


def make_round(db: DB, picks: dict, state="awaiting_resolution", winner=None, slug="cs2-a-vs-b",
               question="Counter-Strike: CYBERSHOKE Esports vs Nexus (BO3) - CCT Europe Series 4 Group B decider",
               market_id="m1") -> int:
    m = {"id": market_id, "slug": slug, "question": question, "outcomes_list": ["CYBERSHOKE", "Nexus"],
         "prices_list": [0.62, 0.38], "endDate": "2026-10-10T20:00:00Z", "conditionId": "0xabc"}
    rid = db.create_round(m)
    for i, a in enumerate(AGENTS):
        run = db.create_run(rid, a, cli_version="1.0", model_reported=f"{a}-default")
        p = picks.get(a)
        if p is None:
            db.record_bet(run, outcome=None, decided_s=None)
            db.finish_run(run, "timeout", None, None)
            continue
        outcome, price, conf = p
        db.record_bet(run, outcome=outcome, token_id="t", rationale=LONG if a == "claude" else "Nexus looked sharp in qualifiers.",
                      confidence=conf, avg_price=price, shares_filled=100 / price, stake_filled=100,
                      decided_s=60 + 30 * i)
        db.finish_run(run, "bet", None, None)
    if state == "resolved":
        for e in db.round_entries(rid):
            if e["outcome"]:
                payout = e["shares_filled"] if e["outcome"] == winner else 0
                db.settle_bet(e["run_id"], payout, payout - e["stake_filled"])
        db.set_round(rid, state="resolved", winner=winner, resolved_at=time.time())
    else:
        db.set_round(rid, state=state)
    return rid


PICKS = {"claude": ("CYBERSHOKE", 0.63, 0.7), "codex": ("CYBERSHOKE", 0.63, 0.8),
         "agy": ("CYBERSHOKE", 0.64, 0.95), "grok": ("Nexus", 0.21, 0.55)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, deploy_debounce_s=0)
    return s, DB(s.db_path)


def rec(s, db, rid):
    return exports.round_record(s, db, db.round(rid))


def test_split_headlines(env):
    s, db = env
    mk = lambda picks, mid: rec(s, db, make_round(db, picks, market_id=mid))
    assert split_headline(mk(PICKS, "a")) == ("split", "3–1: Grok goes alone")
    two = {"claude": ("A", .5, .5), "codex": ("A", .5, .5), "agy": ("B", .5, .5), "grok": ("B", .5, .5)}
    assert split_headline(mk(two, "b"))[1] == "2–2: dead split"
    three = {"claude": ("A", .5, .5), "codex": ("A", .5, .5), "agy": ("B", .5, .5), "grok": ("C", .5, .5)}
    assert split_headline(mk(three, "c"))[1] == "2–1–1: 3-way split"
    agree = {"claude": ("A", .5, .5), "codex": ("A", .5, .5), "agy": ("A", .5, .5)}  # grok: no bet, not counted
    assert split_headline(mk(agree, "d")) == ("agree", "They all agree")


def test_texts_fit_and_tags(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="Nexus",
                     question="Will " + "very long question " * 20 + "?")
    r = rec(s, db, rid)
    board = exports.export(s, db)["leaderboard"]["agents"]
    texts = [decision_text(s, r, e) for e in r["entries"]]
    kind, h = split_headline(r)
    texts.append(split_text(s, r, kind, h))
    res = results_text(s, r, board, "https://skalenetwork.github.io/agentpit-bench/round/1/")
    texts.append(res)
    for t in texts:
        assert tweet_len(t) <= MAX_LEN, t
    claude = decision_text(s, r, r["entries"][0])
    assert claude.count('"') == 4 and "…\"" in claude  # clipped rationale keeps its closing quote
    assert "@xai" in res and "@AnthropicAI" not in res  # only the winner's vendor
    assert not any("@" in t for t in texts[:-1])  # never tag on decisions or splits
    assert "Grok 1-0" in res and "Claude 0-1" in res
    assert results_headline(r).startswith("Upset! Grok")


async def test_cards_render(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="Nexus")
    r = rec(s, db, rid)
    board = exports.export(s, db)["leaderboard"]["agents"]
    cr = CardRenderer(s)
    try:
        paths = [await cr.decision(r, e, board) for e in r["entries"]]
        paths += [await cr.split(r, board), await cr.results(r, board)]
    finally:
        await cr.close()
    for p in paths:
        head = p.read_bytes()[:24]
        assert head[:8] == b"\x89PNG\r\n\x1a\n"
        assert (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) == (1200, 675)


async def test_publisher_thread_and_idempotency(env):
    s, db = env
    rid = make_round(db, {**PICKS, "agy": None})  # Gemini timed out
    pub = BenchPublisher(s, db)
    try:
        runs = {e["agent"]: e["run_id"] for e in db.round_entries(rid)}
        for a in AGENTS:
            await pub.on_bet(rid, runs[a])
        await pub.on_bets_done(rid)
        for e in db.round_entries(rid):
            e_run = e["run_id"]
            if e["agent"] == "claude":
                db.settle_bet(e_run, 0, -100)
        db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())
        await pub.on_resolved(rid)
        await pub.on_resolved(rid)  # retry must not double-post
        first = db.round(rid)["thread_tweet_id"]
        assert first == f"dry-r{rid}-decision-claude"
        out = sorted(s.outbox_dir.glob("*.json"))
        assert len(out) == 6  # 4 decisions + split + results
        tweets = {json.loads(p.read_text())["post_key"]: json.loads(p.read_text()) for p in out}
        assert tweets[f"r{rid}-decision-claude"]["reply_to"] is None
        for k, t in tweets.items():
            assert t["length"] <= MAX_LEN
            if k != f"r{rid}-decision-claude":
                assert t["reply_to"] == first
        assert "timed out" in tweets[f"r{rid}-decision-agy"]["text"]
        assert db.round(rid)["results_tweet_id"] == f"dry-r{rid}-results"
        assert all(e["tweet_id"] for e in db.round_entries(rid))
        assert db.one("SELECT COUNT(*) c FROM tweets")["c"] == 6
    finally:
        await pub.close()
    assert (s.site_dir / "index.html").exists()  # close() flushed the pending site build


def test_site_build(env):
    s, db = env
    make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")
    live = make_round(db, {"claude": ("Nexus", .4, .6), "grok": ("CYBERSHOKE", .6, .9)}, market_id="m2")
    for e in db.round_entries(1):
        t = s.transcripts_dir / "1" / f"{e['agent']}.txt"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("transcript")
        db.x("UPDATE runs SET transcript_path=? WHERE run_id=?", str(t), e["run_id"])
    exports.export(s, db)
    out = site.build(s)
    pages = ["index.html", "leaderboard/index.html", "round/1/index.html", f"round/{live}/index.html",
             *(f"agent/{a}/index.html" for a in AGENTS), "data/index.html", "splits/index.html",
             "hall-of-shame/index.html", "widget/index.html"]
    for p in pages:
        f = out / p
        assert f.exists(), p
        assert f.stat().st_size < 100_000, p
    home = (out / "index.html").read_text()
    assert "Claude vs Codex vs Gemini vs Grok" in home and "data-live" in home
    assert 'class="team dkb"' in home  # Grok's near-black colour gets a light ring in dark mode
    r1 = (out / "round/1/index.html").read_text()
    assert "Bet against the AIs" in r1 and "utm_source" in r1 and "transcripts/1/claude.txt" in r1
    assert (out / "transcripts/1/grok.txt").exists()
    shame = (out / "hall-of-shame/index.html").read_text()
    assert "95% sure" in shame  # Gemini's most confident wrong call leads
    badge = (out / "badge.svg").read_text()
    ET.fromstring(badge)
    assert "Grok 1-0" in badge and "Claude 0-1" in badge
    ET.fromstring((out / "feed.xml").read_text())
    assert json.loads((out / "feed.json").read_text())["items"]
    for name in ("rounds.json", "leaderboard.json", "bets.csv"):
        assert (out / "data" / name).exists()


def test_deploy_skipped_in_dry_run(env):
    s, db = env
    exports.export(s, db)
    site.build(s)
    assert site.deploy(s) is False
    assert not (s.data_dir / "gh-pages").exists()


def test_poster_idempotent(env):
    s, db = env
    p = Poster(s, db)
    import asyncio
    a = asyncio.run(p.post("k1", "x", "hello"))
    b = asyncio.run(p.post("k1", "x", "hello again"))
    assert a == b == "dry-k1"
    assert len(list(s.outbox_dir.glob("*.json"))) == 1
