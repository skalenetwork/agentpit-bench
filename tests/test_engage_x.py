"""engage_x (operator 2026-10-27): likes, shout-outs, follow-backs; scope no-op, idempotency, caps, narrow reply kind."""
import asyncio
import json
import logging
import time
from pathlib import Path

import pytest

from agentpitbench import engage_x, methodology, replyback, twitter
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.twitter import PermanentError, Poster

NOW = time.time()
ALL = {"tweet.write", "media.write", "users.read", "tweet.read", "offline.access", "like.write", "follows.write"}


@pytest.fixture(autouse=True)
def _reset_warnings():
    engage_x._warned.clear()


@pytest.fixture
def s(tmp_path):
    s = Settings(data_dir=tmp_path / "var")
    methodology.apply(s, {})
    s.post_kinds = ["reveal", "results", "shoutout"]
    s.x_like_replies = s.x_shoutouts = s.x_follow_back = True
    return s


@pytest.fixture
def db(s):
    db = DB(s.db_path)
    engage_x.ensure_schema(db)
    replyback.ensure_schema(db)
    return db


class FakePoster(Poster):
    def __init__(self, s, db, scopes=ALL, live=True):
        super().__init__(s, db)
        self._scopes, self._live = set(scopes), live
        self.oauth2.scopes = lambda: self._scopes
        self.likes, self.follows, self.posted = [], [], []
        self.error = None

    @property
    def live(self):
        return self._live

    def my_user_id(self):
        return "999"

    def like(self, tweet_id):
        if self.error:
            raise self.error
        self.likes.append(tweet_id)

    def follow(self, user_id):
        if self.error:
            raise self.error
        self.follows.append(user_id)

    async def _post_live(self, text, image, reply_to, quote_of=None, poll=None):
        self.posted.append((text, reply_to))
        return f"90{len(self.posted)}"


def human(db, rid, uid, pick, tid, at=None):
    db.x("INSERT INTO humans(round_id,user_id,username,pick,tweet_id,created_at) VALUES(?,?,?,?,?,?)",
         rid, uid, f"user{uid}", pick, tid, at or NOW - 100)


def make_round(db, rid=1, ai_picks=("Rams", "Rams", "Rams", "Rams"), winner="Bills"):
    db.x("INSERT INTO rounds(round_id,market_id,question,outcomes,snapshot_json,started_at,state,winner,"
         "results_tweet_id,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?)", rid, f"m{rid}", "Rams v Bills?",
         json.dumps(["Rams", "Bills"]), "{}", NOW - 3600, "resolved", winner, f"res{rid}", NOW - 60)
    for agent, pick in zip(["claude", "codex", "agy", "grok"], ai_picks):
        run = db.x("INSERT INTO runs(round_id,agent,started_at,exit_reason) VALUES(?,?,?,?)", rid, agent, NOW,
                   "bet" if pick else "timeout")
        db.x("INSERT INTO bets(run_id,outcome,stake_filled,placed_at) VALUES(?,?,?,?)", run, pick, 40, NOW)


def go(s, db, p):
    return asyncio.run(engage_x.run(s, db, p, now=NOW))


def test_likes_idempotent_and_skip_dry_and_optout(s, db):
    make_round(db)
    human(db, 1, "u1", "Bills", "1001")
    human(db, 1, "u2", "Rams", "1002")
    human(db, 1, "u3", "Rams", "dry-x")
    human(db, 1, "u4", "Rams", "1004")
    db.x("INSERT INTO reply_optouts(user_id,username,reply_id,at) VALUES('u4','x','1',?)", NOW)
    p = FakePoster(s, db)
    assert go(s, db, p)["likes"] == 2
    assert sorted(p.likes) == ["1001", "1002"]
    assert go(s, db, p)["likes"] == 0 and len(p.likes) == 2


def test_like_daily_cap(s, db):
    s.x_like_daily_cap = 2
    for i in range(5):
        human(db, 1, f"u{i}", "Rams", f"10{i}")
    p = FakePoster(s, db)
    go(s, db, p)
    go(s, db, p)
    assert len(p.likes) == 2


def test_missing_scopes_noop_with_one_warning(s, db, caplog):
    human(db, 1, "u1", "Bills", "1001")
    p = FakePoster(s, db, scopes=ALL - {"like.write", "follows.write"})
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert go(s, db, p)["likes"] == 0
    assert p.likes == [] and p.follows == []
    warns = [r for r in caplog.records if "lacks" in r.getMessage()]
    assert len(warns) == 2   # one per scope, once each, not per tick
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_403_stops_with_one_warning_and_no_crash(s, db, caplog):
    for i in range(3):
        human(db, 1, f"u{i}", "Rams", f"10{i}")
    p = FakePoster(s, db)
    p.error = PermanentError("forbidden", 403)
    with caplog.at_level(logging.WARNING):
        go(s, db, p)
        go(s, db, p)
    assert len([r for r in caplog.records if "refused like" in r.getMessage()]) == 1
    p.error = None
    assert go(s, db, p)["likes"] == 3   # not recorded as done: works once the scope exists


def test_404_recorded_not_retried(s, db):
    human(db, 1, "u1", "Rams", "1001")
    p = FakePoster(s, db)
    p.error = PermanentError("gone", 404)
    go(s, db, p)
    p.error = None
    go(s, db, p)
    assert p.likes == []


def test_not_live_does_nothing(s, db):
    human(db, 1, "u1", "Rams", "1001")
    p = FakePoster(s, db, live=False)
    assert go(s, db, p)["likes"] == 0 and p.likes == []


def test_follow_back_needs_two_pick_replies_and_cap(s, db):
    s.x_follow_daily_cap = 2
    for rid in (1, 2):
        make_round(db, rid)
    for u in ("a", "b", "c"):
        human(db, 1, u, "Rams", f"1{u}1")
        human(db, 2, u, "Rams", f"2{u}1")
    human(db, 1, "once", "Rams", "1once")
    human(db, 1, "999", "Rams", "1me")
    human(db, 2, "999", "Rams", "2me")
    p = FakePoster(s, db)
    assert go(s, db, p)["follows"] == 2
    assert "once" not in p.follows and "999" not in p.follows
    go(s, db, p)
    assert len(p.follows) == 2 and len(set(p.follows)) == 2   # the cap holds across ticks; nobody twice


def test_shoutout_when_human_beats_all_four(s, db):
    make_round(db)
    human(db, 1, "u2", "Bills", "1002", at=NOW - 50)
    human(db, 1, "u1", "Bills", "1001", at=NOW - 80)   # earliest wins the shout-out
    human(db, 1, "u3", "Rams", "1003")
    p = FakePoster(s, db)
    assert go(s, db, p)["shoutouts"] == 1
    assert len(p.posted) == 1
    text, reply_to = p.posted[0]
    assert "@useru1" in text and reply_to == "res1" and len(text) <= 280
    assert go(s, db, p)["shoutouts"] == 0 and len(p.posted) == 1
    assert db.one("SELECT kind FROM tweets WHERE post_key='shoutout-1'")["kind"] == "shoutout"


@pytest.mark.parametrize("picks", [("Rams", "Rams", "Bills", "Rams"), ("Rams", "Rams", "Rams", None)])
def test_no_shoutout_when_an_ai_got_it_or_abstained(s, db, picks):
    make_round(db, ai_picks=picks)
    human(db, 1, "u1", "Bills", "1001")
    p = FakePoster(s, db)
    assert go(s, db, p)["shoutouts"] == 0 and p.posted == []


def test_shoutout_knobs_and_kind_listing(s, db):
    make_round(db)
    human(db, 1, "u1", "Bills", "1001")
    s.x_shoutouts = False
    assert go(s, db, FakePoster(s, db))["shoutouts"] == 0
    s.x_shoutouts = True
    s.post_kinds = ["reveal", "results"]
    assert go(s, db, FakePoster(s, db))["shoutouts"] == 0


def test_poster_allows_shoutout_reply_only_when_listed(s, db):
    p = Poster(s, db)
    assert asyncio.run(p.post("so-1", "shoutout", "hi", reply_to="1"))
    assert asyncio.run(p.post("x-1", "press", "hi", reply_to="1")) is None
    s.post_kinds = []
    assert asyncio.run(p.post("so-2", "shoutout", "hi", reply_to="1")) is None


def test_scopes_requested_by_login(s, db):
    assert twitter.ENGAGE_SCOPES == {"like.write", "follows.write"}
    tok = twitter.OAuth2Token(Path(s.data_dir) / "t.json", "cid", "sec", "r")
    url = tok.authorize_url("http://localhost/cb")
    assert "like.write" in url and "follows.write" in url
