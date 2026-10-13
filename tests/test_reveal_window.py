"""Virality-2 F1: the reveal goes out in the event's live window (T-3h .. T-15min). Held, never dropped; late markets
post at once; a held reveal survives a restart and always precedes the results post."""
import json
import time
from datetime import datetime, timezone

import pytest

from agentpitbench import reveal_window as w
from agentpitbench.db import DB
from agentpitbench.publish import BenchPublisher

from test_publish import AGENTS, PICKS, env, make_round  # noqa: F401  (env is a fixture)


def utc(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("desc,ref,want", [
    # NFL: 8:15PM EDT = 00:15 UTC the next day
    ("In the upcoming NFL football game between Bills and Rams, scheduled for October 12 at 8:15PM ET:",
     "2026-10-13T00:15:00", "2026-10-13T00:15:00"),
    # esports with a year: 2:00 AM EDT = 06:00 UTC (endDate 16:30Z would be wrong)
    ("initially scheduled for October 12, 2026 at 2:00 AM ET", "2026-10-12T16:30:00", "2026-10-12T06:00:00"),
    ("scheduled for October 9, 2026 at 11:00 AM ET", "2026-10-09T21:00:00", "2026-10-09T15:00:00"),
    # winter time: 7:00PM EST = 00:00 UTC
    ("scheduled for December 3 at 7:00PM ET", "2026-12-04T00:00:00", "2026-12-04T00:00:00"),
    # year boundary: a January game listed in late December
    ("scheduled for January 2 at 1:00PM ET", "2026-12-30T00:00:00", "2027-01-02T18:00:00"),
])
def test_kickoff_parsed_from_the_event_description(desc, ref, want):
    assert w.parse_kickoff(desc, utc(ref)) == utc(want)


def test_kickoff_fallbacks():
    assert w.parse_kickoff("The market resolves to Yes if it rains.", time.time()) is None
    # football with a date only: the market ends at kickoff (Aston Villa-Brentford, 3pm UK)
    assert w.kickoff("Will Aston Villa FC vs. Brentford FC end in a draw?", None, "2026-10-10T14:00:00Z",
                     "scheduled for October 10, 2026\nIf the game ends in a draw") == utc("2026-10-10T14:00:00")
    # 'win on <date>' without a usable end: 12:00 UTC that day
    assert w.kickoff("Will Lille OSC win on 2026-10-17?", None, None) == utc("2026-10-17T12:00:00")
    # Up or Down ... ET: the end is the moment
    assert w.kickoff("Bitcoin Up or Down - October 9, 8PM ET", None, "2026-10-10T01:00:00Z") == utc("2026-10-10T01:00:00")
    assert w.kickoff("Will Amazon.com, Inc. (AMZN) hit (LOW) $252 Week of October 12 2026?", None,
                     "2026-10-16T20:00:00Z") is None


def test_due_times():
    q, end = "Will Atalanta BC vs. Venezia FC end in a draw?", "2026-10-12T16:30:00Z"
    desc = "scheduled for October 12, 2026\nIf the game ends in a draw"
    # decided at 00:25 UTC: held until 3 h before the 16:30 kickoff
    assert w.due_at(q, None, end, utc("2026-10-12T00:25:00"), desc) == utc("2026-10-12T13:30:00")
    # inside the window: now
    assert w.due_at(q, None, end, utc("2026-10-12T14:00:00"), desc) == utc("2026-10-12T14:00:00")
    # late (inside the last 15 min, or after kickoff): now, never dropped
    assert w.due_at(q, None, end, utc("2026-10-12T16:20:00"), desc) == utc("2026-10-12T16:20:00")
    assert w.due_at(q, None, end, utc("2026-10-12T17:00:00"), desc) == utc("2026-10-12T17:00:00")
    # no kickoff (stock threshold): the weekday window 13:30-20:00 UTC
    amzn, aend = "Will Amazon.com, Inc. (AMZN) hit (LOW) $252 Week of October 12 2026?", "2026-10-16T20:00:00Z"
    assert w.due_at(amzn, None, aend, utc("2026-10-12T01:26:00")) == utc("2026-10-12T13:30:00")   # Monday night
    assert w.due_at(amzn, None, aend, utc("2026-10-12T15:00:00")) == utc("2026-10-12T15:00:00")   # in the window
    assert w.due_at(amzn, None, aend, utc("2026-10-10T09:00:00")) == utc("2026-10-12T13:30:00")   # Saturday -> Monday
    # never later than 15 min before the market ends
    rain = "Will it rain in New Orleans, LA on October 10?"
    assert w.due_at(rain, None, "2026-10-11T18:00:00Z", utc("2026-10-10T01:21:00")) == utc("2026-10-11T17:45:00")


def _outbox(s):
    return {json.loads(p.read_text())["post_key"]: json.loads(p.read_text()) for p in s.outbox_dir.glob("*.json")}


async def test_reveal_held_then_posted_and_survives_a_restart(env):
    s, db = env
    s.reveal_window = True
    rid = make_round(db, PICKS)                          # ends 2026-10-10 20:00Z, no event: a weekday-window market
    db.set_round(rid, event_start=time.time() + 10 * 3600)   # kickoff in 10 h: the window opens in 7 h
    pub = BenchPublisher(s, db)
    try:
        db.x("UPDATE rounds SET end_date=? WHERE round_id=?",
             datetime.fromtimestamp(time.time() + 12 * 3600, timezone.utc).isoformat(), rid)
        await pub.on_bets_done(rid)
        assert f"r{rid}-reveal" not in _outbox(s)           # held, not posted
        due = db.round(rid)["reveal_due_at"]
        assert abs(due - (time.time() + 7 * 3600)) < 120
        assert await pub.post_due_reveals() == []           # not yet
    finally:
        await pub.close()
    pub2 = BenchPublisher(s, DB(s.db_path))                 # a restart: the hold is in the DB
    try:
        assert await pub2.post_due_reveals(now=due + 1) == [rid]
        assert f"r{rid}-reveal" in _outbox(s) and pub2.db.round(rid)["reveal_due_at"] is None
        assert await pub2.post_due_reveals(now=due + 120) == []   # posted once
    finally:
        await pub2.close()


async def test_held_reveal_goes_out_before_results(env):
    s, db = env
    s.reveal_window = True
    rid = make_round(db, PICKS)
    db.set_round(rid, event_start=time.time() + 10 * 3600)
    db.x("UPDATE rounds SET end_date=? WHERE round_id=?",
         datetime.fromtimestamp(time.time() + 12 * 3600, timezone.utc).isoformat(), rid)
    pub = BenchPublisher(s, db)
    try:
        await pub.on_bets_done(rid)
        assert f"r{rid}-reveal" not in _outbox(s)
        db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())   # resolved while held
        await pub.on_resolved(rid)
    finally:
        await pub.close()
    out = _outbox(s)
    assert f"r{rid}-reveal" in out and f"r{rid}-results" in out
    assert out[f"r{rid}-results"]["quote_of"] == f"dry-r{rid}-reveal"   # results quote the reveal, as always


async def test_window_off_or_late_posts_at_once(env):
    s, db = env
    assert s.reveal_window is False                        # code default: today's behaviour
    rid = make_round(db, PICKS)
    db.set_round(rid, event_start=time.time() + 10 * 3600)
    pub = BenchPublisher(s, db)
    try:
        await pub.on_bets_done(rid)
        assert f"r{rid}-reveal" in _outbox(s)
        s.reveal_window = True
        late = make_round(db, PICKS, market_id="m2", slug="s2")
        db.set_round(late, event_start=time.time() - 600)  # kickoff 10 min ago: post now
        await pub.on_bets_done(late)
        assert f"r{late}-reveal" in _outbox(s) and db.round(late)["reveal_due_at"] is None
    finally:
        await pub.close()
