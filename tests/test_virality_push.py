"""Virality push 2026-10-27: the @grok ask quote-post, the Sunday labs standings, the summons promotion line."""
import asyncio
import json
import types
from datetime import datetime, timezone

import pytest

from agentpitbench import bluesky, publish, telegram, threads
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.publish import BenchPublisher
from agentpitbench.twitter import (GROK_ASK_TEMPLATES, SUMMONS_LINES, Poster, grok_ask_pick, grok_ask_text,
                                   labs_week_text, measure, results_text, strip_summons, summons_line, tweet_len)

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc).timestamp()      # a Tuesday
SUNDAY = datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc).timestamp()
AGENTS = [("claude", "Claude"), ("codex", "ChatGPT"), ("agy", "Gemini"), ("grok", "Grok")]


def rnd(rid, question="Will Bitcoin hit 100k?", picks=("Yes", "Yes", "No", "No"), state="awaiting_resolution",
        revealed=NOW - 3600, tweet="9000", **kw):
    return {"round_id": rid, "question": question, "event_title": "", "outcomes": ["Yes", "No"], "state": state,
            "winner": None, "revealed_at": revealed, "thread_tweet_id": tweet, "exhibition": False,
            "resolved_at": None,
            "entries": [{"agent": a, "name": n, "outcome": p, "won": None, "pnl": None}
                        for (a, n), p in zip(AGENTS, picks)], **kw}


# ---------- 1. @grok ask ----------

def test_pick_prefers_tier_then_even_split_and_skips_esports_agree_resolved_and_old():
    esports = rnd(1, "Counter-Strike: Navi vs FaZe (BO3) - ESL Pro League")
    agree = rnd(2, "Will Trump win the election?", picks=("Yes",) * 4)
    done = rnd(3, "Will Bitcoin hit 100k?", state="resolved")
    old = rnd(4, "Will Bitcoin hit 100k?", revealed=NOW - 2 * 86400)
    sealed = rnd(5, "Will Bitcoin hit 100k?", tweet=None)
    mid = rnd(6, "Will the Rams beat the Bills in the NFL?", picks=("Yes", "Yes", "No", "No"))      # tier 2
    lone = rnd(7, "Will Bitcoin hit 100k?", picks=("Yes", "Yes", "Yes", "No"))                    # tier 3, alone
    dead = rnd(8, "Will Ethereum hit 10k?", picks=("Yes", "Yes", "No", "No"), revealed=NOW - 7200)  # tier 3, dead split
    rounds = [esports, agree, done, old, sealed, mid, lone, dead]
    assert grok_ask_pick(rounds, NOW)["round_id"] == 8
    assert grok_ask_pick(rounds, NOW, asked={8})["round_id"] == 7
    assert grok_ask_pick(rounds, NOW, asked={8, 7})["round_id"] == 6
    assert grok_ask_pick([esports, agree, done, old, sealed], NOW) is None


def test_text_has_market_picks_ask_and_varies_daily():
    r = rnd(1)
    texts = [grok_ask_text(r, d) for d in range(len(GROK_ASK_TEMPLATES) * 2)]
    for t in texts:
        assert "Will Bitcoin hit 100k?" in t and "Yes: Claude, ChatGPT · No: Gemini, Grok" in t
        assert "@grok" in t and "who's right?" in t and tweet_len(t) <= 280
    assert all(texts[i] != texts[i + 1] for i in range(len(texts) - 1))          # never the same wording two days running
    assert len(set(texts)) == len(GROK_ASK_TEMPLATES)
    long = grok_ask_text(rnd(2, "Q " * 150), 1)
    assert tweet_len(long) <= 280 and "@grok" in long and "Yes: Claude" in long


@pytest.fixture
def pub(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_ACCESS_TOKEN", "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    s = Settings(data_dir=tmp_path / "var", dry_run=True, grok_ask_enabled=True, labs_week_enabled=True)
    db = DB(s.db_path)
    p = types.SimpleNamespace(s=s, db=db, poster=Poster(s, db))
    p._kind_listed = lambda k: BenchPublisher._kind_listed(p, k)
    return p


def rows(pub):
    return [dict(r) for r in pub.db.q("SELECT post_key, kind, text, quote_of, round_id FROM tweets")]


def test_grok_ask_posts_once_a_day_as_a_quote_of_the_reveal(pub, monkeypatch):
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": [rnd(8, tweet="9008"), rnd(7, tweet="9007")]})
    assert asyncio.run(BenchPublisher.grok_ask(pub, NOW))
    r = rows(pub)
    assert len(r) == 1 and r[0]["kind"] == "grok_ask" and r[0]["quote_of"] == "9008" and r[0]["round_id"] == 8
    assert r[0]["post_key"] == "grok-ask-2026-10-27" and "@grok" in r[0]["text"]
    assert asyncio.run(BenchPublisher.grok_ask(pub, NOW + 3600)) is None            # max 1 a day
    assert len(rows(pub)) == 1
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": [
        rnd(8, tweet="9008", revealed=NOW + 82800), rnd(7, tweet="9007", revealed=NOW + 82800)]})
    asyncio.run(BenchPublisher.grok_ask(pub, NOW + 86400))                           # next day: another round, not 8 again
    r = rows(pub)
    assert len(r) == 2 and {x["round_id"] for x in r} == {8, 7}


def test_grok_ask_gates(pub, monkeypatch):
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": [rnd(8)]})
    early = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc).timestamp()
    assert asyncio.run(BenchPublisher.grok_ask(pub, early)) is None                  # before grok_ask_hour_utc
    pub.s.grok_ask_enabled = False
    assert asyncio.run(BenchPublisher.grok_ask(pub, NOW)) is None
    pub.s.grok_ask_enabled = True
    pub.s.post_kinds = ["reveal", "results"]                                          # kind not listed: no post, no state
    assert asyncio.run(BenchPublisher.grok_ask(pub, NOW)) is None
    pub.s.post_kinds = ["reveal", "grok_ask"]
    assert asyncio.run(BenchPublisher.grok_ask(pub, NOW))
    assert not Settings().grok_ask_enabled                                            # off by default


def test_grok_ask_never_posts_without_a_public_reveal(pub, monkeypatch):
    monkeypatch.setattr(publish.exports, "export",
                        lambda s, db: {"rounds": [rnd(8, tweet=None), rnd(9, revealed=None)]})
    assert asyncio.run(BenchPublisher.grok_ask(pub, NOW)) is None and rows(pub) == []


def test_grok_ask_not_mirrored():
    assert "grok_ask" not in bluesky.MIRROR_KINDS and "grok_ask" not in telegram.MIRROR_KINDS


# ---------- 2. labs week ----------

def week_round(rid, pnl, won, resolved):
    return {"round_id": rid, "state": "resolved", "exhibition": False, "resolved_at": resolved, "entries": [
        {"agent": a, "name": n, "outcome": "Yes", "won": won[i], "pnl": pnl[i]} for i, (a, n) in enumerate(AGENTS)]}


def test_labs_week_text_tags_all_four_labs():
    t = labs_week_text([{"agent": a, "name": n, "pnl": p, "wins": w, "losses": l}
                        for (a, n), p, w, l in zip(AGENTS, (120.4, -35.0, 40.0, -90.2), (5, 2, 3, 1), (2, 4, 3, 5))])
    assert t.startswith("This week: Claude +120 (5-2) · Gemini +40 (3-3) · ChatGPT -35 (2-4) · Grok -90 (1-5).")
    for h in ("@AnthropicAI", "@OpenAI", "@GoogleDeepMind", "@xai", "#AgentpitBench"):
        assert h in t
    assert tweet_len(t) <= 280


def test_labs_week_posts_on_sunday_once_per_week(pub, monkeypatch):
    start = datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp()          # Monday of that ISO week
    rs = [week_round(1, (50, -20, 10, -40), (True, False, True, False), start + 3600),
          week_round(2, (70, -15, 30, -50), (True, False, True, False), start + 7200),
          week_round(3, (999, 999, 999, 999), (True,) * 4, start - 86400)]    # last week: ignored
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": rs})
    assert asyncio.run(BenchPublisher.labs_week(pub, NOW)) is None             # not Sunday
    assert asyncio.run(BenchPublisher.labs_week(pub, SUNDAY - 3 * 3600)) is None   # 14:00, before labs_week_hour_utc
    assert asyncio.run(BenchPublisher.labs_week(pub, SUNDAY))
    r = rows(pub)
    assert len(r) == 1 and r[0]["kind"] == "labs_week" and r[0]["post_key"] == "labs-week-2026-W44"
    assert "Claude +120 (2-0)" in r[0]["text"] and "Grok -90 (0-2)" in r[0]["text"] and "@xai" in r[0]["text"]
    assert asyncio.run(BenchPublisher.labs_week(pub, SUNDAY + 3600)) is None
    assert len(rows(pub)) == 1


def test_labs_week_hour_does_not_collide_with_race_or_episode():
    s = Settings()
    assert s.labs_week_hour_utc < s.race_hour_utc <= s.week_episode_hour_utc
    assert s.labs_week_hour_utc < s.week_episode_prep_hour_utc + 3
    assert not s.labs_week_enabled


def test_labs_week_nothing_without_resolved_rounds_and_when_off(pub, monkeypatch):
    monkeypatch.setattr(publish.exports, "export", lambda s, db: {"rounds": []})
    assert asyncio.run(BenchPublisher.labs_week(pub, SUNDAY)) is None
    pub.s.labs_week_enabled = False
    assert asyncio.run(BenchPublisher.labs_week(pub, SUNDAY)) is None


def test_labs_week_mirrors_with_lab_names():
    assert "labs_week" in bluesky.MIRROR_KINDS and "labs_week" in telegram.MIRROR_KINDS
    t = labs_week_text([{"agent": a, "name": n, "pnl": 1.0, "wins": 1, "losses": 0} for a, n in AGENTS])
    out, _ = bluesky.adapt(t, Settings())
    assert "@AnthropicAI" not in out and "Anthropic" in out and "xAI" in out
    assert "@xai" not in telegram.adapt(t, Settings(), "labs_week", None)[0]


# ---------- 3. summons line ----------

def resolved_round(name_len=3, q="Spain vs. Italy"):
    return {"state": "resolved", "winner": "Yes", "question": q, "outcomes": ["Yes", "No"], "entries": [
        {"agent": a, "name": a.title() * name_len, "outcome": "Yes" if i % 2 else "No", "won": bool(i % 2)}
        for i, a in enumerate(["claude", "codex", "agy", "grok"])]}


BOARD = [{"name": a * 4, "wins": 10, "losses": 9} for a in ("claude", "codex", "agy", "grok")]
URL = "https://agentpitbench.org/round/123/?utm_source=x&utm_medium=social&utm_campaign=results&utm_content=fun"
CHAL = "Can you beat them? → https://agentpitbench.org/challenge/2026-10-20/?utm_source=x"
CROWD = {"played": 3, "wins": 2, "losses": 1}


def test_summons_line_rotation_and_rate():
    assert summons_line(3, 0) == "" and summons_line(5, 3) == ""
    hits = [rid for rid in range(1, 31) if summons_line(rid, 3)]
    assert hits == list(range(3, 31, 3))                                       # 1 in 3
    assert {summons_line(rid, 3) for rid in hits} == set(SUMMONS_LINES)         # rotates through all the lines
    assert summons_line(9, 3) == summons_line(9, 3)                             # deterministic


def test_summons_line_in_results_text_and_drops_first():
    s = Settings()
    line = SUMMONS_LINES[0]
    short = [{"name": a, "wins": 1, "losses": 0} for a in ("C", "G", "A", "X")]
    plain = results_text(s, resolved_round(1), short, URL, None)
    withl = results_text(s, resolved_round(1), short, URL, None, summons_line=line)
    assert line in withl and withl.endswith(URL) and withl.replace(line + "\n", "") == plain
    both = results_text(s, resolved_round(1), short, URL, None, challenge_line=CHAL, summons_line=line)
    assert both.index(line) < both.index(CHAL) and both.endswith(URL)
    long = resolved_round(name_len=8)
    base = results_text(s, long, BOARD, URL, CROWD)
    assert results_text(s, long, BOARD, URL, CROWD, summons_line=line) == base           # gave way, nothing else did
    assert results_text(s, long, BOARD, URL, CROWD, challenge_line=CHAL, summons_line=line) == base
    # the summons line goes before the challenge line
    mid = results_text(s, resolved_round(4), BOARD, URL, None, challenge_line=CHAL, summons_line=line)
    full = results_text(s, resolved_round(4), BOARD, URL, None, challenge_line=CHAL)
    if line not in mid:
        assert CHAL in mid or mid == results_text(s, resolved_round(4), BOARD, URL, None)
    assert tweet_len(mid) <= 280 and tweet_len(full) <= 280


def test_summons_line_is_never_blamed_on_a_variant():
    rnd0 = resolved_round(name_len=9, q="Q " * 60)
    _, c0 = measure(lambda: results_text(Settings(), rnd0, BOARD, URL, CROWD))
    _, c1 = measure(lambda: results_text(Settings(), rnd0, BOARD, URL, CROWD, challenge_line=CHAL,
                                         summons_line=SUMMONS_LINES[1]))
    assert c1 == c0
    _, c2 = measure(lambda: results_text(Settings(), rnd0, BOARD, URL, CROWD, summons_line=SUMMONS_LINES[1]))
    assert c2 == c0


def test_summons_line_stripped_on_mirrors():
    t = "Spain vs. Italy\nResolved.\n" + SUMMONS_LINES[0] + "\nhttps://agentpitbench.org/round/1/?utm_source=x"
    assert SUMMONS_LINES[0] not in strip_summons(t)
    assert SUMMONS_LINES[0] not in bluesky.adapt(t, Settings())[0]
    assert SUMMONS_LINES[0] not in telegram.adapt(t, Settings(), "results", 1)[0]
    assert SUMMONS_LINES[0] not in threads.build_text(Settings(), t, "results", 1)


def test_bench_toml_turns_the_push_on():
    from agentpitbench.config import load as load_settings
    s = load_settings("bench.toml")
    assert s.grok_ask_enabled and s.labs_week_enabled and s.summons_line_every == 3
    assert "grok_ask" in s.post_kinds and "labs_week" in s.post_kinds
    assert Settings().summons_line_every == 0
