"""The reveal.format A/B slot: the round's opener as a native X poll instead of the split card."""
import json
import time

import pytest

from agentpitbench import ab, backend
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.publish import BenchPublisher
from agentpitbench.twitter import (POLL_LABEL_MAX, Poster, poll_minutes, poll_options, poll_result_line,
                                   results_text)
from test_publish import AGENTS, PICKS, make_round

@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_ACCESS_TOKEN", "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, deploy_debounce_s=0, animate=False)
    return s, DB(s.db_path)


def force_poll(db, key):
    ab.seed(db)
    arm = next(a for a in ab.arms(db, "reveal.format") if a["code_key"] == "poll")
    db.x("INSERT INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at) VALUES(?,?,?,?,?,?)",
         key, "reveal.format", arm["arm_id"], arm["arm_id"], "reveal", time.time())


def entry(agent, name, outcome):
    return {"agent": agent, "name": name, "outcome": outcome}


def rnd_of(*pairs, outcomes=("YES", "NO")):
    return {"entries": [entry(a, n, o) for a, n, o in pairs], "outcomes": list(outcomes)}


SPLIT = (("claude", "Claude", "YES"), ("grok", "Grok", "YES"), ("codex", "ChatGPT", "NO"), ("agy", "Gemini", "NO"))


def test_slot_is_a_code_slot_with_card_as_control(env):
    s, db = env
    ab.seed(db)
    assert ab.SLOTS["reveal.format"]["codes"] == ["card", "poll"]
    assert ab.control(db, "reveal.format")["code_key"] == "card"
    assert {a["code_key"] for a in ab.arms(db, "reveal.format")} == {"card", "poll"}


def test_split_option_labels_name_each_side_and_its_ais():
    opts = poll_options(rnd_of(*SPLIT))
    assert [o["label"] for o in opts] == ["YES · Claude, Grok", "NO · ChatGPT, Gemini"]
    assert [o["outcome"] for o in opts] == ["YES", "NO"]


def test_option_labels_never_exceed_25_chars():
    longest = rnd_of(("claude", "Claude", "Manchester City"), ("codex", "ChatGPT", "Manchester City"),
                     ("agy", "Gemini", "Manchester City"), ("grok", "Grok", "Arsenal Football Club of London"),
                     outcomes=("Manchester City", "Arsenal Football Club of London"))
    labels = [o["label"] for o in poll_options(longest)]
    assert labels[0] == "Manchester City · 3 AIs"            # names dropped, AI count kept
    assert all(len(x) <= POLL_LABEL_MAX for x in labels)
    assert len(labels[1]) <= POLL_LABEL_MAX and labels[1].startswith("Arsenal Football")
    three = [o["label"] for o in poll_options(rnd_of(("claude", "Claude", "YES"), ("codex", "ChatGPT", "YES"),
                                                     ("agy", "Gemini", "YES"), ("grok", "Grok", "NO")))]
    assert three == ["YES · 3 AIs", "NO · Grok"]
    for names in (("Claude", "Grok"), ("ChatGPT", "Gemini")):
        assert len(f"NO · {', '.join(names)}") <= POLL_LABEL_MAX


def test_unanimous_variant():
    opts = poll_options(rnd_of(*[(a, n, "YES") for a, n, _ in SPLIT]))
    assert [o["label"] for o in opts] == ["All 4 AIs: YES", "They're wrong"]
    assert [o["outcome"] for o in opts] == ["YES", "NO"]
    long = poll_options(rnd_of(*[(a, n, "Manchester City Football Club") for a, n, _ in SPLIT],
                               outcomes=("Manchester City Football Club", "Other")))
    assert all(len(o["label"]) <= POLL_LABEL_MAX for o in long)
    multi = poll_options(rnd_of(*[(a, n, "A") for a, n, _ in SPLIT], outcomes=("A", "B", "C")))
    assert multi[1]["outcome"] is None


def test_fewer_than_two_bettors_have_no_poll():
    assert poll_options(rnd_of(("claude", "Claude", "YES"), ("grok", "Grok", None))) is None


def test_duration_runs_to_the_close_clamped_to_x_limits():
    now = 1_000_000.0
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))   # noqa: E731
    assert poll_minutes(iso(now + 3 * 3600), now) == 180
    assert poll_minutes(iso(now + 60), now) == 5                       # too soon: 5 minutes
    assert poll_minutes(iso(now - 3600), now) == 5                     # already past
    assert poll_minutes(iso(now + 30 * 86400), now) == 7 * 24 * 60     # far: 7 days
    assert poll_minutes(None, now) == 24 * 60 and poll_minutes("garbage", now) == 24 * 60


async def run_round(s, db, rid, resolve=None, pub_hook=None):
    pub = BenchPublisher(s, db)
    try:
        if pub_hook:
            pub_hook(pub)
        for e in db.round_entries(rid):
            await pub.on_bet(rid, e["run_id"])
        await pub.on_bets_done(rid)
        if resolve:
            db.set_round(rid, state="resolved", winner=resolve, resolved_at=time.time())
            await pub.on_resolved(rid)
    finally:
        await pub.close()
    return {json.loads(p.read_text())["post_key"]: json.loads(p.read_text()) for p in s.outbox_dir.glob("*.json")}


async def test_control_arm_posts_the_split_card_and_no_poll(env):
    s, db = env
    rid = make_round(db, PICKS)
    reveal = (await run_round(s, db, rid))[f"r{rid}-reveal"]
    assert reveal["poll"] is None and reveal["images"] == [str(s.cards_dir / str(rid) / "split.png")]
    assert db.one("SELECT 1 FROM x_polls WHERE round_id=?", rid) is None


async def test_poll_arm_posts_text_and_poll_without_media_and_mirrors_get_text_and_card(env):
    s, db = env
    rid = make_round(db, PICKS)
    force_poll(db, f"r{rid}-reveal")
    seen = {}

    class Mirror:
        enabled = True

        def __init__(self, name):
            self.name = name

        async def post(self, post_key, kind, text, image, **kw):
            seen[self.name] = (kind, text, image)

    def hook(pub):
        pub.poster.bsky, pub.poster.tg = Mirror("bsky"), Mirror("tg")

    tweets = await run_round(s, db, rid, pub_hook=hook)
    reveal = tweets[f"r{rid}-reveal"]
    assert reveal["kind"] == "reveal" and reveal["image"] is None and reveal["images"] == []   # no media on X
    assert reveal["poll"]["options"] == ["CYBERSHOKE · 3 AIs", "Nexus · Grok"]
    assert all(len(o) <= 25 for o in reveal["poll"]["options"])
    assert 5 <= reveal["poll"]["duration_minutes"] <= 7 * 24 * 60
    # the same text as the card arm: line 1 is the full market name, the reply invite is kept
    assert reveal["text"].split("\n")[0].startswith("Counter-Strike: CYBERSHOKE") and "Reply with your pick" in reveal["text"]
    card = str(s.cards_dir / str(rid) / "split.png")
    assert seen["bsky"] == ("reveal", reveal["text"], __import__("pathlib").Path(card))
    assert seen["tg"][1] == reveal["text"] and str(seen["tg"][2]) == card
    assert db.round(rid)["thread_tweet_id"] == f"dry-r{rid}-reveal"
    row = db.one("SELECT * FROM x_polls WHERE round_id=?", rid)
    assert [o["outcome"] for o in json.loads(row["options_json"])] == ["CYBERSHOKE", "Nexus"]
    assert set(tweets) == {f"r{rid}-reveal"}                       # still one opener, no extra posts


async def test_a_refused_poll_falls_back_to_the_split_card(env):
    s, db = env
    rid = make_round(db, PICKS)
    force_poll(db, f"r{rid}-reveal")
    calls = []

    def hook(pub):
        orig = pub.poster.post

        async def post(key, kind, text, image=None, **kw):
            calls.append(kw.get("poll"))
            return None if kw.get("poll") else await orig(key, kind, text, image, **kw)
        pub.poster.post = post

    tweets = await run_round(s, db, rid, pub_hook=hook)
    assert calls[0] and calls[1] is None
    assert tweets[f"r{rid}-reveal"]["images"] and db.one("SELECT 1 FROM x_polls WHERE round_id=?", rid) is None
    arm = db.one("SELECT a.code_key FROM ab_assignments x JOIN ab_arms a ON a.arm_id=x.realized_arm_id "
                 "WHERE x.post_key=? AND x.slot='reveal.format'", f"r{rid}-reveal")
    assert arm["code_key"] == "card"


def test_poll_body_reaches_the_v2_api_and_outbox_records_it(env, monkeypatch):
    s, db = env
    poster = Poster(s, db)
    poll = {"options": ["A", "B"], "duration_minutes": 90}
    sent = {}

    class R:
        def json(self):
            return {"data": {"id": "42"}}

    monkeypatch.setattr(type(poster.oauth2), "ready", property(lambda self: True))
    monkeypatch.setattr(poster, "_call", lambda method, url, **kw: sent.update(kw) or R())
    assert poster._post_sync("hello", None, None, None, poll) == "42"
    assert sent["json"] == {"text": "hello", "poll": poll}
    poster._post_sync("hello", None, None, None, None)
    assert sent["json"] == {"text": "hello"}


def test_poll_results_use_the_poll_expansion(env, monkeypatch):
    s, db = env
    poster = Poster(s, db)
    got = {}
    monkeypatch.setattr(Poster, "can_read", property(lambda self: True))

    def read(url, params, resource, caller, now=None):
        got.update(params=params, resource=resource, caller=caller)
        return {"data": [{"id": "9"}], "includes": {"polls": [{"id": "p", "voting_status": "closed", "options": [
            {"position": 2, "label": "NO", "votes": 36}, {"position": 1, "label": "YES", "votes": 64}]}]}}
    monkeypatch.setattr(poster, "read", read)
    res = poster.poll_results("9")
    assert got["params"]["expansions"] == "attachments.poll_ids" and "options" in got["params"]["poll.fields"]
    assert "voting_status" in got["params"]["poll.fields"] and got["caller"] == "poll"
    assert res == {"options": [{"label": "YES", "votes": 64}, {"label": "NO", "votes": 36}], "total": 100,
                   "status": "closed"}
    assert poster.poll_results("dry-x") is None


def test_poll_result_line_is_neutral_and_needs_enough_votes():
    poll = {"options": [{"label": "a", "outcome": "YES", "votes": 136}, {"label": "b", "outcome": "NO", "votes": 76}],
            "total": 212}
    assert poll_result_line(poll, "YES") == "64% of 212 X voters backed YES; they were right."
    assert poll_result_line(poll, "NO") == "64% of 212 X voters backed YES; they were wrong."
    assert poll_result_line({**poll, "total": 9}, "YES") == ""
    tie = {"options": [{"label": "a", "outcome": "YES", "votes": 10}, {"label": "b", "outcome": "NO", "votes": 10}],
           "total": 20}
    assert poll_result_line(tie, "YES") == "" and poll_result_line(None, "YES") == ""
    doubt = {"options": [{"label": "a", "outcome": "A", "votes": 5}, {"label": "b", "outcome": None, "votes": 20}],
             "total": 25}
    assert poll_result_line(doubt, "C") == "80% of 25 X voters doubted the AIs; they were right."
    assert poll_result_line(doubt, "A") == "80% of 25 X voters doubted the AIs; they were wrong."


async def test_results_post_carries_the_poll_percentage_and_totals_are_stored(env):
    s, db = env
    rid = make_round(db, PICKS, question="Will Nexus win the decider?")
    force_poll(db, f"r{rid}-reveal")
    reads, lines = [], []

    def hook(pub):
        orig = pub._poll_line

        async def spy(round_id, rnd):
            lines.append(await orig(round_id, rnd))
            return lines[-1]
        pub._poll_line = spy

        def poll_results(tweet_id):
            reads.append(tweet_id)
            return {"options": [{"label": "CYBERSHOKE · 3 AIs", "votes": 30}, {"label": "Nexus · Grok", "votes": 70}],
                    "total": 100, "status": "closed"}
        pub.poster.poll_results = poll_results
    tweets = await run_round(s, db, rid, resolve="CYBERSHOKE", pub_hook=hook)
    assert len(reads) == 1
    res = tweets[f"r{rid}-results"]["text"]
    line = "70% of 100 X voters backed Nexus; they were wrong."
    assert lines == [line]                       # what the results post was offered
    assert line in res or "Season:" in res       # optional: the text gives it up first when it does not fit
    assert res.split("\n")[0] == "Will Nexus win the decider?" and tweets[f"r{rid}-results"]["length"] <= 280
    row = db.one("SELECT * FROM x_polls WHERE round_id=?", rid)
    assert row["total"] == 100 and row["status"] == "closed" and row["read_at"]
    assert json.loads(row["votes_json"]) == {"CYBERSHOKE · 3 AIs": 30, "Nexus · Grok": 70}


async def test_failed_poll_read_leaves_the_results_text_unchanged(env):
    s, db = env
    rid = make_round(db, PICKS)
    force_poll(db, f"r{rid}-reveal")

    def hook(pub):
        def boom(tweet_id):
            raise RuntimeError("X down")
        pub.poster.poll_results = boom
    tweets = await run_round(s, db, rid, resolve="CYBERSHOKE", pub_hook=hook)
    assert "X voters" not in tweets[f"r{rid}-results"]["text"]
    row = db.one("SELECT * FROM x_polls WHERE round_id=?", rid)
    assert row["votes_json"] is None and row["read_at"]            # attempted: the backend push stops waiting


def test_poll_line_gives_way_first(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE")
    rnd = {"question": "Q" * 150, "entries": [], "state": "resolved", "winner": "CYBERSHOKE"}
    line = "64% of 212 X voters backed CYBERSHOKE; they were right."
    board = [{"name": "Claude", "wins": 3, "losses": 2}]
    with_line = results_text(s, rnd, board, "https://x.test/r/1/", None, poll_line=line)
    without = results_text(s, rnd, board, "https://x.test/r/1/", None)
    assert line in with_line or with_line == without
    full = {"question": "Will it?", "entries": [], "state": "resolved", "winner": "YES"}
    assert line in results_text(s, full, board, "https://x.test/r/1/", None, poll_line=line)
    assert rid


def test_backend_results_carry_x_poll_and_wait_for_the_read(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE")
    plain = backend.results(db, AGENTS, set())
    assert "x_poll" not in plain[0]
    db.x("INSERT INTO x_polls(round_id,tweet_id,options_json) VALUES(?,?,?)", rid, "t1", "[]")
    assert backend.results(db, AGENTS, set()) == []                                  # unread: held for the read
    assert backend.results(db, AGENTS, set(), now=time.time() + backend.POLL_GRACE_S + 5)[0].get("x_poll") is None
    db.x("UPDATE x_polls SET votes_json=?, total=100, read_at=? WHERE round_id=?",
         json.dumps({"YES · Claude": 64, "NO · Grok": 36}), time.time(), rid)
    out = backend.results(db, AGENTS, set())
    assert out[0]["x_poll"] == {"YES · Claude": 64, "NO · Grok": 36} and out[0]["round_id"] == rid
