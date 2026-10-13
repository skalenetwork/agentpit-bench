"""Long-tail search (docs/longtail-search-spec.md): every round post carries the full market name."""
import asyncio
import html
import time

from agentpitbench import ab, anomaly
from agentpitbench.orchestrator import event_info
from agentpitbench.twitter import (MAX_LEN, market_name, measure, results_text, reveal_text, summon_reply_text,
                                   tweet_len)

LILLE = {"question": "Will Lille OSC win on 2026-10-17?", "event_title": "Lille OSC vs. Stade Brestois 29"}
ESPORTS_104 = ("Rainbow Six Siege: Black Dragons e-Sports vs FURIA Esports (BO3) - South America League Stage 2 "
               "Playoffs")


def _rnd(question, outcomes=("Yes", "No"), picks=None, event_title=None, state="awaiting_resolution", winner=None):
    names = {"claude": "Claude", "codex": "ChatGPT", "agy": "Gemini", "grok": "Grok"}
    picks = picks or {"claude": outcomes[1], "codex": outcomes[0], "agy": outcomes[1], "grok": outcomes[1]}
    return {"round_id": 1, "question": question, "event_title": event_title, "outcomes": list(outcomes),
            "state": state, "winner": winner,
            "entries": [{"agent": a, "name": n, "outcome": picks.get(a), "won": picks.get(a) == winner,
                         "stake_filled": 100, "avg_price": 0.5} for a, n in names.items()]}


def test_market_name():
    assert market_name(LILLE) == "Lille OSC vs. Stade Brestois 29 · Will Lille OSC win on 2026-10-17?"
    draw = {"question": "Will Atalanta BC vs. Venezia FC end in a draw?", "event_title": "Atalanta BC vs. Venezia FC"}
    assert market_name(draw) == draw["question"]                      # already names both sides
    cs = {"question": "Counter-Strike: Liquid vs BBL - Map 1 Winner",
          "event_title": "Counter-Strike: Liquid vs BBL (BO3) - Stake Pulse Beat III Group B"}
    assert market_name(cs) == cs["question"]
    assert market_name({"question": "Will  AMZN\nhit $252?", "event_title": None}) == "Will AMZN hit $252?"
    assert market_name({"question": "Will Lille OSC win on 2026-10-17?"}) == "Will Lille OSC win on 2026-10-17?"


def test_reveal_keeps_the_name_and_gives_way_in_order():
    r = _rnd(**LILLE)
    t = reveal_text(r, "3–1: ChatGPT goes alone", "Think you know better?", " #AI")
    assert t.split("\n")[0] == market_name(r) and tweet_len(t) <= MAX_LEN
    assert "Claude: No · ChatGPT: Yes" in t and "Think you know better?" in t and t.endswith("#AgentpitBench #AI")
    long = _rnd(ESPORTS_104, outcomes=("Black Dragons e-Sports", "FURIA Esports"),
                picks={"claude": "FURIA Esports", "codex": "Black Dragons e-Sports", "agy": "FURIA Esports",
                       "grok": "FURIA Esports"})
    t, clipped = measure(lambda: reveal_text(long, "3–1: ChatGPT goes alone", "Think you know better?", " #AI"))
    assert t.split("\n")[0] == ESPORTS_104 and tweet_len(t) <= MAX_LEN
    assert "Think you know better?" not in t and "#AI" not in t   # cta, then tags went first
    assert "FURIA Esports: Claude, Gemini, Grok" in t              # grouped picks hold every bettor
    assert clipped > 0                                             # dropped parts count as clipped (A/B)


def test_results_drop_crowd_then_season_never_the_name():
    board = [{"name": n, "wins": 3, "losses": 2} for n in ("Claude", "ChatGPT", "Gemini", "Grok")]
    crowd = {"played": 5, "wins": 2, "losses": 3}
    url = "https://agentpitbench.org/round/1/?utm_source=x&utm_medium=social&utm_campaign=results&utm_content=fun"
    r = _rnd(**LILLE, state="resolved", winner="No")
    t = results_text(None, r, board, url, crowd)
    assert t.split("\n")[0] == market_name(r) and tweet_len(t) <= MAX_LEN and t.endswith(url)
    assert "Season:" in t and "@xai" in t
    long = _rnd(ESPORTS_104, outcomes=("A", "B"), picks={"claude": "A", "codex": "A", "agy": "A", "grok": "B"},
                state="resolved", winner="A")
    t = results_text(None, long, board, url, crowd)
    assert t.startswith(ESPORTS_104 + "\n") and "| Crowd" not in t and tweet_len(t) <= MAX_LEN
    void = dict(_rnd(**LILLE), state="void")
    assert f"Voided: {market_name(void)}. No result this round." in results_text(None, void, board, url)


def test_cta_that_does_not_fit_counts_as_clipped_so_the_control_posts():
    board = [{"name": n, "wins": 3, "losses": 2} for n in ("Claude", "ChatGPT", "Gemini", "Grok")]
    url = "https://agentpitbench.org/round/1/?utm_source=x"
    r = _rnd(**LILLE, state="resolved", winner="No")
    control, c_clip = measure(lambda: results_text(None, r, board, url, {"played": 5, "wins": 2, "losses": 3}))
    variant, v_clip = measure(lambda: results_text(None, r, board, url, {"played": 5, "wins": 2, "losses": 3},
                                                   cta="See the full round breakdown on the site"))
    assert ab.check_rendered("results", variant, control, v_clip, c_clip, market_name(r))
    assert "market name missing" in ab.check_rendered("results", "no name", control, 0, 0, market_name(r))


def test_last_resort_clips_a_huge_name_but_still_posts():
    q = "Will " + "an extraordinarily long market question " * 7 + "?"
    t = reveal_text(_rnd(q), "2–2: dead split")
    assert tweet_len(t) <= MAX_LEN and t.startswith("Will an extraordinarily")


def test_summon_reply_carries_the_full_name():
    t = summon_reply_text(market_name(LILLE), "https://x.com/agentpitbench/status/1")
    assert market_name(LILLE) in t and "…" not in t


def test_event_info_matches_by_condition_and_keeps_sports_titles_only():
    class Api:
        def __init__(self, ev):
            self.ev = ev

        async def event_for(self, cid):
            if isinstance(self.ev, Exception):
                raise self.ev
            return self.ev if cid == "0xabc" else None
    sports = {"title": "Lille OSC vs. Stade Brestois 29", "category": "Sports",
              "description": "scheduled for October 17, 2026 at 1:00 PM ET"}
    title, start, _ = asyncio.run(event_info(Api(sports), "0xabc"))
    assert title == "Lille OSC vs. Stade Brestois 29" and start
    biz = {"title": "What will Amazon.com, Inc. (AMZN) hit Week of October 12 2026?", "category": "Business"}
    assert asyncio.run(event_info(Api(biz), "0xabc"))[0] is None
    assert asyncio.run(event_info(Api(sports), "0xother")) == (None, None, None)
    assert asyncio.run(event_info(Api(TimeoutError("slow")), "0xabc")) == (None, None, None)


def test_detector_flags_posts_without_the_name(tmp_path):
    from agentpitbench.config import Settings
    from agentpitbench.db import DB
    s = Settings(data_dir=tmp_path / "var")
    db = DB(s.db_path)
    rid = db.create_round({"id": "1", "question": LILLE["question"], "outcomes_list": ["Yes", "No"]})
    db.set_round(rid, event_title=LILLE["event_title"])
    now = time.time()
    assert anomaly.market_name_missing(s, db, now - 100) == []           # first run sets the marker
    db.x("INSERT INTO tweets(post_key,kind,round_id,text,tweet_id,posted_at) VALUES(?,?,?,?,?,?)",
         "old", "results", rid, "Resolved: No.", "1", now - 200)          # before the marker: ignored
    db.x("INSERT INTO tweets(post_key,kind,round_id,text,tweet_id,posted_at) VALUES(?,?,?,?,?,?)",
         "r1-results", "results", rid, "Resolved: No. #AgentpitBench", "2", now - 10)
    db.x("INSERT INTO tg_posts(post_key,kind,round_id,text,posted_at) VALUES(?,?,?,?,?)",
         "r1-reveal", "reveal", rid, html.escape(market_name(LILLE)) + "\nPicks", now - 10)
    found = anomaly.market_name_missing(s, db, now)
    assert [a.detail["post_key"] for a in found] == ["r1-results"] and found[0].severity == "warn"


def test_bluesky_and_film_posts_keep_the_name():
    from agentpitbench import bluesky, motd
    from agentpitbench.config import Settings
    board = [{"name": n, "wins": 3, "losses": 2} for n in ("Claude", "ChatGPT", "Gemini", "Grok")]
    url = "https://agentpitbench.org/round/1/?utm_source=x&utm_medium=social&utm_campaign=results&utm_content=fun"
    long = _rnd(ESPORTS_104, outcomes=("A", "B"), picks={"claude": "A", "codex": "A", "agy": "A", "grok": "B"},
                state="resolved", winner="A")
    x = results_text(None, long, board, url, {"played": 5, "wins": 2, "losses": 3})
    b, _links = bluesky.adapt(x, Settings())
    b = bluesky.fit300(b)
    assert len(b) <= 300 and b.startswith(ESPORTS_104 + "\n")
    s = Settings(bluesky_vendor_handles={"grok": "x" * 200 + ".bsky.social", "claude": "y" * 200 + ".bsky.social"})
    clipped = bluesky.fit300(bluesky.adapt(x, s)[0])
    assert len(clipped) <= 300 and clipped.startswith(ESPORTS_104)      # the end gives way, the name is line 1
    rec = {"hook": LILLE["question"], "post_hook": LILLE["question"], "question": LILLE["question"],
           "market_name": market_name(LILLE)}
    assert motd.post_text(rec) == f"{market_name(LILLE)}\nMatch of the Day #AgentpitBench"   # hook not repeated
    rec["post_hook"] = "3 AIs said No. ChatGPT said Yes."
    assert motd.post_text(rec).startswith(market_name(LILLE) + "\n3 AIs said No.")


def test_round_page_title_leads_with_the_match():
    from agentpitbench import site
    from agentpitbench.i18n import Translator
    _ = Translator("en")
    r = {**_rnd(**LILLE), "market_name": market_name(LILLE)}
    t = site.round_title(r, _, "Claude, ChatGPT, Gemini, Grok")
    assert t.startswith("Lille OSC vs. Stade Brestois 29 · Will Lille OSC")   # the title cap clips the question, not the match
    assert t.endswith(": AI predictions from Claude, ChatGPT, Gemini, Grok")
    assert site.round_description(r, _).startswith("Lille OSC vs. Stade Brestois 29. ")
    plain = {**_rnd("Will it rain in New Orleans, LA on October 10?")}
    assert site.round_title(plain, _, "x").startswith("Will it rain")
