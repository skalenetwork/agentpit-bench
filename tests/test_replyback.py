"""F2 reply-back (docs/virality-2-spec.md): the narrow reply exception, limits, opt-out, kill switch, mirrors."""
import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from agentpitbench import cli, methodology, replyback
from agentpitbench.alerts import alerts_dir
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.twitter import Poster

ME = "999"
NOW = time.time()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


OLD_ACCOUNT = iso(NOW - 400 * 86400)


@pytest.fixture
def s(tmp_path):
    s = Settings(data_dir=tmp_path / "var")
    methodology.apply(s, {})
    s.post_kinds = ["reveal", "results", "reply-back"]
    return s


@pytest.fixture
def db(s):
    db = DB(s.db_path)
    replyback.ensure_schema(db)
    return db


class FakePoster(Poster):
    """A dry-run Poster (posts go to the outbox) whose X reads come from self.inbox."""

    def __init__(self, s, db):
        super().__init__(s, db)
        self.inbox: dict[str, list[dict]] = {}
        self.reads: list[tuple] = []

    @property
    def can_read(self):
        return True

    def my_user_id(self):
        return ME

    def replies(self, conversation_id, max_reads, since_id=None, caller="replies"):
        self.reads.append((conversation_id, since_id, caller))
        out = [r for r in self.inbox.get(conversation_id, [])
               if since_id is None or (len(r["id"]), r["id"]) > (len(since_id), since_id)]
        return [dict(r) for r in out]


def make_round(db, rid=1, state="running", winner=None, kind="reveal", tweet_id="5000", posted=NOW - 600):
    db.x("INSERT INTO rounds(round_id,market_id,question,outcomes,snapshot_json,started_at,state,winner,"
         "thread_tweet_id) VALUES(?,?,?,?,?,?,?,?,?)", rid, f"m{rid}", "Will the Rams beat the Bills?",
         json.dumps(["Rams", "Bills"]), "{}", NOW - 3600, state, winner, tweet_id)
    for i, (agent, pick, price) in enumerate([("claude", "Rams", 0.41), ("codex", "Bills", 0.6),
                                              ("agy", "Rams", 0.42), ("grok", None, None)]):
        run = db.x("INSERT INTO runs(round_id,agent,started_at,exit_reason) VALUES(?,?,?,?)", rid, agent, NOW,
                   "bet" if pick else "timeout")
        won = None if winner is None or pick is None else pick == winner
        db.x("INSERT INTO bets(run_id,outcome,avg_price,stake_filled,pnl,placed_at) VALUES(?,?,?,?,?,?)", run, pick,
             price, 40 if pick else 0, None if won is None else (20 if won else -40), NOW)
    db.x("INSERT INTO tweets(post_key,tweet_id,kind,round_id,text,posted_at) VALUES(?,?,?,?,?,?)",
         f"r{rid}-{kind}", tweet_id, kind, rid, "opening", posted)


_n = [7000]


def reply(text, user="u1", to=ME, age_s=300, account=OLD_ACCOUNT, rid=None):
    _n[0] += 1
    return {"id": rid or str(_n[0]), "author_id": user, "username": f"name{user}", "text": f"@agentpitbench {text}",
            "created_at": iso(NOW - age_s), "in_reply_to_user_id": to, "author_created_at": account}


def answers(db):
    return [dict(r) for r in db.q("SELECT * FROM tweets WHERE kind='reply-back' ORDER BY posted_at, post_key")]


def run(s, db, p, now=NOW):
    return asyncio.run(replyback.poll(s, db, p, now=now))


# ---------------------------------------------------------------- the Poster gate

def test_reply_back_allowed_only_when_listed(s, db):
    p = Poster(s, db)
    assert asyncio.run(p.post("rb-1", "reply-back", "Claude has 40 tokens on Rams.", reply_to="7001"))
    assert asyncio.run(p.post("r1-late", "results", "a reply", reply_to="1")) is None   # still no other replies
    assert asyncio.run(p.post("r1-press-0", "press", "statement", reply_to="1")) is None
    s.post_kinds = ["reveal", "results"]
    assert asyncio.run(p.post("rb-2", "reply-back", "x", reply_to="7002")) is None
    s.post_kinds = []                                                   # empty = every kind, but never reply-back
    assert asyncio.run(p.post("rb-3", "reply-back", "x", reply_to="7003")) is None
    assert [r["post_key"] for r in answers(db)] == ["rb-1"]


def test_reply_back_never_mirrored(s, db):
    s.bluesky_enabled = s.telegram_enabled = True
    p = Poster(s, db)
    seen = []

    async def rec(key, *a, **kw):
        seen.append(key)
    p.bsky.post = rec
    p.tg.post = rec
    asyncio.run(p.post("rb-1", "reply-back", "Thanks!", reply_to="7001"))
    asyncio.run(p.post("r1-reveal", "reveal", "opening"))
    assert "rb-1" not in seen and "r1-reveal" in seen


def test_disabled_without_listing(s, db):
    s.post_kinds = ["reveal", "results"]
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply("Why did Claude pick Rams?")]
    assert run(s, db, p)["answered"] == 0 and not p.reads
    assert cli.reply_back_task(s, db, type("Pub", (), {"poster": p})()) is None
    s.post_kinds.append("reply-back")
    assert cli.reply_back_task(s, db, type("Pub", (), {"poster": p})()) is not None


# ---------------------------------------------------------------- templates and limits

def test_answers_a_question_with_the_agents_pick(s, db):
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply("Why did Claude pick Rams?")]
    out = run(s, db, p)
    assert out["answered"] == 1
    a = answers(db)[0]
    assert a["text"] == "Claude has 40 tokens on Rams at 41¢. Results here when it resolves."
    assert a["reply_to"] == p.inbox["5000"][0]["id"] and a["round_id"] == 1
    assert p.reads[0][2] == "reply-back"


def test_results_post_and_no_template_means_no_answer(s, db):
    p = FakePoster(s, db)
    make_round(db, state="resolved", winner="Bills", kind="results", tweet_id="6000")
    p.inbox["6000"] = [reply("How did Gemini do?", user="a"), reply("interesting market", user="b")]
    run(s, db, p)
    (a,) = answers(db)
    assert a["text"] == "Gemini put 40 tokens on Rams at 42¢ and lost (-40 tokens). It resolved Bills."


def test_one_answer_per_person_per_round(s, db):
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply("Why Claude?"), reply("And why ChatGPT?")]
    run(s, db, p)
    assert len(answers(db)) == 1
    p.inbox["5000"].append(reply("What about Gemini?"))
    run(s, db, p)
    assert len(answers(db)) == 1


def test_daily_cap(s, db):
    s.reply_back_daily_cap = 3
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply(f"Why did {n} pick that?", user=f"u{n}{i}")
                       for n in ("Claude", "ChatGPT", "Gemini") for i in range(2)]   # 6 distinct texts
    run(s, db, p)
    assert len(answers(db)) == 3


def test_never_repeats_identical_text(s, db):
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply("Why Claude?", user=f"u{i}") for i in range(3)]
    run(s, db, p)
    texts = [a["text"] for a in answers(db)]
    assert len(texts) == 2 and len(set(texts)) == 2   # two variants, then silence rather than a repeat


def test_stop_is_honoured_for_good(s, db):
    p = FakePoster(s, db)
    make_round(db)
    make_round(db, rid=2, tweet_id="5001")
    p.inbox["5000"] = [reply("stop replying to me", user="x")]
    out = run(s, db, p)
    assert out["optouts"] == 1 and not answers(db)
    p.inbox["5001"] = [reply("Why did Claude go Rams?", user="x")]
    run(s, db, p)
    assert not answers(db)


def test_skips_young_accounts_abuse_spam_exclusions_and_stale(s, db):
    s.reply_back_exclude = ["@nameu5"]
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply("Why Claude?", user="u1", account=iso(NOW - 3 * 86400)),
                       reply("Why Claude, you idiot?", user="u2"),
                       reply("Why Claude? DM me for signals https://x.y", user="u3"),
                       reply("Why Claude?", user="u4", age_s=5 * 3600),
                       reply("Why Claude?", user="u5"),
                       reply("Why Claude?", user="u6", to="123"),        # replying to someone else in the thread
                       reply("Why Claude?", user=ME)]
    run(s, db, p)
    assert not answers(db)


def test_since_id_and_window(s, db):
    p = FakePoster(s, db)
    make_round(db)
    make_round(db, rid=2, tweet_id="5001", posted=NOW - 49 * 3600)   # outside the 48 h window
    p.inbox["5000"] = [reply("Why Claude?")]
    run(s, db, p)
    run(s, db, p)
    assert [r[:2] for r in p.reads] == [("5000", None), ("5000", p.inbox["5000"][0]["id"])]


def test_pick_and_praise_templates(s, db):
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply("Bills", user="a"), reply("love this", user="b"), reply("no way, wrong", user="c")]
    run(s, db, p)
    got = [a["text"] for a in answers(db)]
    assert "Got your pick: Bills. Same side as ChatGPT. Results here when it resolves." in got
    assert any(t.startswith("Thanks!") for t in got)
    assert "Noted. 2 of 4 AIs are on Rams; results here when it resolves." in got


# ---------------------------------------------------------------- kill switch

def test_kill_switch_on_complaint(s, db):
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply("Reported this AI bot", user="z"), reply("Why Claude?", user="y")]
    out = run(s, db, p)
    assert out["killed"] and replyback.killed(db) and not answers(db)
    files = list(alerts_dir(s).glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["kind"] == "reply-back"
    p.inbox["5000"] = [reply("Why Claude?", user="w")]
    assert run(s, db, p)["answered"] == 0 and not answers(db)


def test_manual_flag_and_resume(s, db, capsys):
    p = FakePoster(s, db)
    make_round(db)
    replyback.kill(s, db, "operator review", manual=True)
    assert list(alerts_dir(s).glob("*.json"))
    p.inbox["5000"] = [reply("Why Claude?")]
    run(s, db, p)
    assert not answers(db) and not p.reads
    replyback.resume(db)
    run(s, db, p)
    assert len(answers(db)) == 1


def test_read_budget_stops_polling(s, db):
    s.reply_back_read_budget_usd = 0.0
    p = FakePoster(s, db)
    make_round(db)
    p.inbox["5000"] = [reply("Why Claude?")]
    run(s, db, p)
    assert not p.reads
