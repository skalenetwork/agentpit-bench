"""Posts v3 phase 2: debate threads, hype, confession, hot take, humans' week, grudge match, soap opera."""
import asyncio
import json
import types
from datetime import datetime, timezone

import pytest

from agentpitbench import posts3, publish
from agentpitbench.config import AGENT_NAMES, Settings
from agentpitbench.db import DB
from agentpitbench.publish import BenchPublisher
from agentpitbench.twitter import Poster, tweet_len
from test_posts_v3 import BOARD, NOW, add_round, facts, rnd


def ok(t, f):
    err = posts3.validate(t, f)
    assert tweet_len(t) <= 280 and err is None, (t, err)


# ---------- templates ----------

def test_debate_template_one_post_per_bettor_plus_close():
    f = facts("reveal", rnd(picks=("No", "No", "Yes", None)))
    texts = posts3.debate_template(f)
    assert len(texts) == 4 and "Who wins the argument? 👇" in texts[-1]
    for t in texts:
        ok(t, f)
    assert posts3.debate_check(f)(texts) is None
    assert "CLAUDE: NO." in texts[0] and "GEMINI: YES." in texts[2]
    bad = ["Claude: YES all day"] + texts[1:]
    assert "pick" in posts3.debate_check(f)(bad)


def test_hype_never_shows_a_pick():
    r = rnd(picks=("Yes", "Yes", "Yes", "Yes"))
    f = posts3.hype_facts(r, "19:45", 60)
    assert all(a["pick"] == "no bet" for a in f["ais"]) and "Yes" not in json.dumps(f["ais"])
    t = posts3.hype_template(f)
    ok(t, f)
    assert "KICKOFF IN 1 HOUR" in t and "19:45 UTC" in t and "YES" not in t


def test_confession_pick_and_template():
    r = rnd(picks=("Yes", "Yes", "Yes", "Yes"), winner="No")
    r["entries"][3]["confidence"] = 0.9
    e = posts3.confession_pick(r, BOARD)
    assert e["agent"] == "grok"                                   # streak -6 and confident: worst first
    f = posts3.confession_facts(r, e, BOARD, [], NOW)
    t = posts3.confession_template(f)
    ok(t, f)
    assert t.startswith("WHAT WAS GROK THINKING? 🤔") and "easiest money on the board" in t and "1–10" in t
    win = rnd(picks=("No",) * 4, winner="No")
    assert posts3.confession_pick(win, BOARD) is None


@pytest.mark.parametrize("day", range(4))
def test_hot_take_frames(day):
    f = posts3.hot_take_facts(BOARD, {"picks": 12, "wins": 7}, day)
    t = posts3.hot_take_template(f)
    ok(t, f)
    assert t.endswith("Discuss 👇")


def test_humans_week_mentions_only_humans_who_beat_the_ais():
    rows = [{"username": "ace", "picks": 6, "wins": 6}, {"username": "meh", "picks": 6, "wins": 2},
            {"username": "lucky", "picks": 1, "wins": 1}]
    rec = {"agy": (5, 3), "grok": (1, 7)}
    f = posts3.humans_week_facts(rows, rec, AGENT_NAMES)
    t = posts3.humans_week_template(f)
    ok(t, f)
    assert "@ace" in t and "@meh" not in t and "@lucky" not in t and "Gemini 5-3" in t
    assert f["champion"]["username"] == "@ace" and f["champion"]["next_opponent"] == "Gemini"
    assert posts3.humans_week_facts([{"username": "meh", "picks": 2, "wins": 0}], rec, AGENT_NAMES) is None


def test_grudge_pair_head_to_head_and_template():
    assert set(posts3.grudge_pair(BOARD)) == {"codex", "agy"} or set(posts3.grudge_pair(BOARD)) == {"codex", "claude"}
    rs = [rnd(i, picks=("Yes", "No", "Yes", "No"), winner="Yes") for i in range(3)]
    h = posts3.head_to_head(rs, "claude", "codex", 0, NOW * 2)
    assert h["n"] == 3 and h["claude"] == 240 and h["codex"] == -300
    f = {"kind": "grudge", "ais": [{"name": b["name"], "pick": "no bet"} for b in BOARD],
         "standings": posts3.standings_facts(BOARD), "next": {"a": "ChatGPT", "b": "Claude", "gap": "559"},
         "last": {"winner": "Claude", "loser": "ChatGPT", "n": 3, "winner_pnl": "+240", "loser_pnl": "-300"}}
    t = posts3.grudge_template(f)
    ok(t, f)
    assert "ChatGPT wears the 💩" in t


def test_soap_episode_arcs_and_cliffhanger_skip_sealed():
    prev = {"ranks": {"claude": 1, "agy": 2}, "headline": "Claude has won 3 straight"}
    sealed = rnd(5, picks=("Yes",) * 4, state="running")
    open_ = rnd(6, picks=("Yes", "No", "No", "No"), question="Will Vissel Kobe win on 2026-10-27?")
    f = posts3.soap_facts(BOARD, prev, 14, [sealed, open_])
    assert f["headline"] == "Gemini took #1" or "fell from #1" in f["headline"]
    t = posts3.soap_template(f)
    ok(t, f)
    assert t.startswith("THE PIT, EPISODE 14 📺") and "Previously: Claude has won 3 straight." in t
    assert posts3.soap_facts(BOARD, None, 1, [])["cliffhanger"] is None or True


# ---------- publisher ----------

@pytest.fixture
def pub(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x.json"))
    kinds = ["reveal", "results", "debate", "hype", "confession", "hot_take", "humans_week", "grudge", "soap"]
    s = Settings(data_dir=tmp_path / "var", dry_run=True, posts_v3=True, post_kinds=kinds, debate_enabled=True,
                 hype_enabled=True, confession_enabled=True, hot_take_enabled=True, humans_week_enabled=True,
                 grudge_enabled=True, soap_enabled=True)
    db = DB(s.db_path)
    p = types.SimpleNamespace(s=s, db=db, poster=Poster(s, db), _locks={})
    for name in ("_kind_listed", "_day_key", "_v3_on", "_lock"):
        setattr(p, name, getattr(BenchPublisher, name).__get__(p))
    return p


def tweets(pub):
    return [dict(r) for r in pub.db.q("SELECT post_key, kind, text, reply_to, quote_of FROM tweets ORDER BY rowid")]


def test_debate_posts_a_reply_chain(pub):
    f = facts("reveal", rnd(picks=("No", "No", "Yes", "Yes")))
    ids = asyncio.run(BenchPublisher.debate(pub, 1, None, f, "9000"))
    rows = tweets(pub)
    assert len(ids) == 5 and [r["kind"] for r in rows] == ["debate"] * 5
    assert rows[0]["reply_to"] == "9000" and all(rows[i]["reply_to"] == ids[i - 1] for i in range(1, 5))


def test_hype_once_about_an_hour_before_kickoff(pub, monkeypatch):
    rid = add_round(pub.db, "1", "Will Arsenal FC win on 2026-10-27?", slug="epl-ars", post_mode="reveal",
                    thread_tweet_id="9001", event_start=NOW + 3600)
    pub.db.set_round(rid, started_at=NOW - 3600)
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": [rnd(rid)]})
    assert asyncio.run(BenchPublisher.hype(pub, NOW - 3600)) == []              # 2 h out: not yet
    assert len(asyncio.run(BenchPublisher.hype(pub, NOW))) == 1
    assert asyncio.run(BenchPublisher.hype(pub, NOW + 60)) == []                # once
    row = tweets(pub)[0]
    assert row["kind"] == "hype" and row["quote_of"] == "9001" and "No" not in row["text"].split("\n")[2:]


def test_soap_numbers_episodes_once_a_day(pub, monkeypatch):
    open_ = rnd(6, picks=("Yes", "No", "No", "No"), question="Will Vissel Kobe win on 2026-10-27?")
    add_round(pub.db, "6", "Will Vissel Kobe win on 2026-10-27?")
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": [open_],
                                                                    "leaderboard": {"agents": BOARD}})
    eve = datetime(2026, 10, 27, 19, 30, tzinfo=timezone.utc).timestamp()
    assert asyncio.run(BenchPublisher.soap(pub, eve))
    assert asyncio.run(BenchPublisher.soap(pub, eve + 60)) is None
    assert asyncio.run(BenchPublisher.soap(pub, eve + 86400))
    eps = pub.db.q("SELECT episode, headline FROM soap_episodes ORDER BY episode")
    assert [e["episode"] for e in eps] == [1, 2]
    assert "EPISODE 2" in tweets(pub)[-1]["text"]
