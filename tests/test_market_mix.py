"""Market mix (methodology v2.2): classifier, per-day type caps, match boost."""
from __future__ import annotations

import pytest

from agentpitbench.config import Settings
from agentpitbench.market_type import market_type
from agentpitbench.watcher import Watcher
from tests.test_core import FakeAPI, gamma
from tests.test_core import db, s  # noqa: F401  (fixtures)

REAL = [
    ("Predators vs. Canadiens", None, "match"),
    ("Will Aston Villa FC vs. Brentford FC end in a draw?", None, "match"),
    ("Will En Avant Guingamp win on 2026-10-09?", None, "match"),
    ("Will Lille OSC win on 2026-10-17?", "Lille OSC vs. Stade Brestois 29", "match"),
    ("Counter-Strike: fnatic vs K27 (BO3) - 1win Private Club #2: Closed Qualifier Playoffs", None, "match"),
    ("Oilers vs. Kings", "Oilers vs. Kings", "match"),
    ("Bitcoin Up or Down - October 10, 9PM ET", None, "crypto_updown"),
    ("Ethereum Up or Down on October 14?", None, "crypto_updown"),
    ("Solana Up or Down on October 14?", None, "crypto_updown"),
    ("Bitcoin Up or Down - October 13, 12:00PM-4:00PM ET", None, "crypto_updown"),
    ("Will Amazon.com, Inc. (AMZN) hit (LOW) $256 Week of October 12 2026?", None, "price_threshold"),
    ("Will Palantir Technologies Inc. (PLTR) hit (HIGH) $210 Week of October 12 2026?", None, "price_threshold"),
    ("Will Ethereum dip to $2,400 October 5-11?", None, "price_threshold"),
    ("Will XRP dip to $1.30 October 12-18?", None, "price_threshold"),
    ("Will Bitcoin reach $84,000 October 5-11?", None, "price_threshold"),
    ("Will the price of Ethereum be above $2,500 on October 9?", None, "price_threshold"),
    ("Will Trump's approval rating be at least 37.8 on October 16, 2026?", None, "other"),
    ("Will Elon Musk post 115-139 tweets from October 8 to October 10, 2026?", None, "other"),
    ("Will it rain in New Orleans, LA on October 10?", None, "other"),
    ("Will A beat B?", None, "other"),
]


@pytest.mark.parametrize("q,title,want", REAL)
def test_classifier(q, title, want):
    assert market_type(q, title) == want


def test_defaults_neutral():
    d = Settings()
    assert (d.max_crypto_updown_per_day, d.max_price_threshold_per_day, d.match_priority_boost) == (0, 0, 0)


W = "Will Team{} win on 2026-10-17?"
UD = "Bitcoin Up or Down - October {}, 9PM ET"
TH = "Will Bitcoin reach ${},000 October 5-11?"


async def _watch(s, db, extra):
    s.max_category_share = 1.0
    api = FakeAPI([gamma(1)])
    w = Watcher(s, db, api)
    await w.poll()
    api.markets_ += extra
    await w.poll()
    return w


async def test_no_caps_all_picked(s, db):
    w = await _watch(s, db, [gamma(i, UD.format(i)) for i in range(2, 6)])
    assert len(await w.pick(free_slots=9)) == 4


async def test_type_caps_and_skipped_stay_eligible(s, db):
    s.max_crypto_updown_per_day = 2
    s.max_price_threshold_per_day = 1
    ms = [gamma(i, UD.format(i)) for i in range(2, 6)] + [gamma(i, TH.format(i)) for i in range(6, 9)]
    ms.append(gamma(9, W.format(1)))
    w = await _watch(s, db, ms)
    picked = await w.pick(free_slots=9)
    types = [market_type(m["question"]) for m in picked]
    assert types.count("crypto_updown") == 2 and types.count("price_threshold") == 1 and types.count("match") == 1
    assert db.one("SELECT COUNT(*) n FROM seen_markets WHERE eligible=1 AND started=0")["n"] == 4
    for m in picked:
        db.create_round(m)
    assert await w.pick(free_slots=9) == []          # today's caps already used by started rounds


async def test_boost_orders_match_first(s, db):
    ms = [gamma(2, "Will it rain in Paris on October 20?", volume="900000"), gamma(3, W.format(3))]
    w = await _watch(s, db, ms)
    assert [m["id"] for m in await w.pick(free_slots=1)] == ["2"]      # boost 0: volume wins
    s.match_priority_boost = 30
    db.x("UPDATE seen_markets SET started=0")
    assert [m["id"] for m in await w.pick(free_slots=1)] == ["3"]


async def test_boost_does_not_lift_over_far_higher_tier(s, db):
    s.match_priority_boost = 10                       # less than a keyword tier gap (25 points)
    ms = [gamma(2, "Will Trump win the election?"), gamma(3, W.format(3))]
    w = await _watch(s, db, ms)
    assert [m["id"] for m in await w.pick(free_slots=1)] == ["2"]


async def test_boost_respects_category_cap(s, db):
    s.match_priority_boost = 30
    ms = [gamma(2, "Will Team2 vs. Team3 end in a draw?"), gamma(3, UD.format(3), volume="5")]
    w = await _watch(s, db, ms)
    s.max_category_share = 0.4
    for i in range(3):                                # a week of Sports rounds already
        db.create_round(gamma(50 + i, "Will A vs. B end in a draw?"))
    picked = await w.pick(free_slots=1)
    assert [m["id"] for m in picked] == ["3"]         # over-cap match is held; the other category runs


async def test_boost_respects_per_event_cap(s, db):
    s.match_priority_boost = 30
    s.max_rounds_per_event_per_day = 1
    ms = [gamma(2, "Will Team win on 2026-10-17?"), gamma(3, "Will Team win on 2026-10-18?")]
    w = await _watch(s, db, ms)
    picked = await w.pick(free_slots=5)
    assert len([m for m in picked if "Team win" in m["question"]]) == 1


async def test_start_window_still_respected(s, db):
    from agentpitbench import watcher
    s.round_start_window = True
    s.match_priority_boost = 30
    s.max_crypto_updown_per_day = 2
    w = await _watch(s, db, [gamma(2, W.format(2)), gamma(3, UD.format(3))])
    orig = watcher.start_ok
    watcher.start_ok = lambda q, *a, **k: "Up or Down" in q
    try:
        picked = await w.pick(free_slots=5, now=None)
    finally:
        watcher.start_ok = orig
    assert all("Up or Down" in m["question"] for m in picked)


# ---------- marquee markets (docs/marquee-proposal.md): esports cap, marquee boost by league code ----------

CS = "Counter-Strike: Team{0} vs Team{1} (BO3) - PGL Masters Bucharest Group Stage"


def test_marquee_defaults_neutral():
    d = Settings()
    assert (d.marquee_priority_boost, d.marquee_leagues, d.max_esports_per_day) == (0, [], 0)


@pytest.mark.parametrize("slug,want", [("ucl-lin2-liv-2026-10-14-draw", "ucl"), ("epl-sun-lee-2026-10-25-draw", "epl"),
                                       ("btc-updown-4h-1791907200", "btc"), ("NBA-x", "nba"), (None, ""), ("", "")])
def test_league(slug, want):
    from agentpitbench.market_type import league
    assert league(slug) == want


async def test_esports_cap_and_skipped_stay_eligible(s, db):
    ms = [gamma(i, CS.format(i, i + 100)) for i in range(2, 5)] + [gamma(9, W.format(9))]
    w = await _watch(s, db, ms)
    assert len(await w.pick(free_slots=9)) == 4                      # cap 0: no limit
    db.x("UPDATE seen_markets SET started=0")
    s.max_esports_per_day = 1
    picked = await w.pick(free_slots=9)
    assert [m["question"].startswith("Counter-Strike") for m in picked].count(True) == 1 and len(picked) == 2
    assert db.one("SELECT COUNT(*) n FROM seen_markets WHERE eligible=1 AND started=0")["n"] == 2
    for m in picked:
        db.create_round(m)
    assert await w.pick(free_slots=9) == []                          # today's esports round already started


async def test_esports_cap_counts_alongside_type_caps(s, db):
    s.max_esports_per_day = 2
    w = await _watch(s, db, [gamma(i, CS.format(i, i + 100)) for i in range(2, 6)])
    assert w.type_counts_today() == {}
    for m in await w.pick(free_slots=9):
        db.create_round(m)
    assert w.type_counts_today() == {"match": 2, "esports": 2}


async def test_marquee_boost_orders_marquee_first(s, db):
    s.match_priority_boost = 30
    ms = [gamma(2, "Will Team2 vs. Team3 end in a draw?", slug="tur-x-y-2026-10-26-draw", volume="900000"),
          gamma(3, "Will Team4 vs. Team5 end in a draw?", slug="epl-a-b-2026-10-26-draw")]
    w = await _watch(s, db, ms)
    assert [m["id"] for m in await w.pick(free_slots=1)] == ["2"]    # no marquee leagues: volume wins
    db.x("UPDATE seen_markets SET started=0")
    s.marquee_priority_boost = 40                                    # boost alone, no leagues: still neutral
    assert [m["id"] for m in await w.pick(free_slots=1)] == ["2"]
    db.x("UPDATE seen_markets SET started=0")
    s.marquee_leagues = ["EPL", "ucl"]
    assert [m["id"] for m in await w.pick(free_slots=1)] == ["3"]


async def test_marquee_crypto_beats_ordinary_match(s, db):
    s.match_priority_boost = 30
    s.marquee_priority_boost = 40
    s.marquee_leagues = ["btc"]
    ms = [gamma(2, W.format(2), slug="tur-x-y-2026-10-26", volume="900000"),
          gamma(3, UD.format(3), slug="btc-updown-4h-1791907200")]
    w = await _watch(s, db, ms)
    assert [m["id"] for m in await w.pick(free_slots=1)] == ["3"]    # 85 + 40 over 85 + 30


async def test_marquee_boost_in_model_mode(s, db):
    s.selection_scoring = "model"
    s.marquee_priority_boost = 40
    s.marquee_leagues = ["ucl"]
    ms = [gamma(2, W.format(2), slug="arg-x-y-2026-10-26"), gamma(3, W.format(3), slug="ucl-x-y-2026-10-26")]
    w = await _watch(s, db, ms)
    for mid, score in (("2", 60), ("3", 30)):
        db.x("INSERT INTO market_ratings(market_id,score,keyword_tier,rated_at) VALUES(?,?,3,0)", mid, score)
    assert [m["id"] for m in await w.pick(free_slots=1)] == ["3"]    # 30 + 40 over 60


# ---------- one round per match (one_round_per_fixture) ----------

@pytest.mark.parametrize("slug,want", [
    ("lal-get-ray-2026-10-19-draw", "lal-get-ray-2026-10-19"), ("lal-get-ray-2026-10-19-get", "lal-get-ray-2026-10-19"),
    ("nba-bos-det-2026-10-20", "nba-bos-det-2026-10-20"), ("cs2-tl1-aur1-2026-10-27-game1", "cs2-tl1-aur1-2026-10-27"),
    ("btc-updown-4h-1791907200", None), ("bitcoin-up-or-down-on-october-14", None), (None, None), ("", None)])
def test_fixture(slug, want):
    from agentpitbench.market_type import fixture
    assert fixture(slug) == want


FX = [("Will Getafe CF win on 2026-10-19?", "lal-get-ray-2026-10-19-get"),
      ("Will Getafe CF vs. Rayo Vallecano de Madrid end in a draw?", "lal-get-ray-2026-10-19-draw"),
      ("Will Rayo Vallecano de Madrid win on 2026-10-19?", "lal-get-ray-2026-10-19-ray"),
      ("Will Tottenham Hotspur FC win on 2026-10-19?", "epl-tot-cov-2026-10-19-tot")]


def test_fixture_rule_default_off():
    assert Settings().one_round_per_fixture is False


async def test_off_picks_several_markets_of_one_match(s, db):
    w = await _watch(s, db, [gamma(i, q, slug=sl) for i, (q, sl) in enumerate(FX, 2)])
    assert len(await w.pick(free_slots=9)) == 4


async def test_one_round_per_fixture(s, db):
    s.one_round_per_fixture = True
    w = await _watch(s, db, [gamma(i, q, slug=sl) for i, (q, sl) in enumerate(FX, 2)])
    picked = await w.pick(free_slots=9)
    assert sorted(m["slug"].rsplit("-", 1)[0] for m in picked) == ["epl-tot-cov-2026-10-19", "lal-get-ray-2026-10-19"]
    for m in picked:
        db.create_round(m)
    assert await w.pick(free_slots=9) == []                          # the match already has its round
    assert db.one("SELECT COUNT(*) n FROM seen_markets WHERE eligible=1 AND started=0")["n"] == 2


async def test_fixture_rule_ignores_void_rounds_and_dateless_slugs(s, db):
    s.one_round_per_fixture = True
    ms = [gamma(2, FX[0][0], slug=FX[0][1]), gamma(3, UD.format(3), slug="btc-updown-4h-1"),
          gamma(4, UD.format(4), slug="btc-updown-4h-2")]
    w = await _watch(s, db, ms)
    rid = db.create_round(gamma(50, FX[1][0], slug=FX[1][1]))
    db.set_round(rid, state="void")
    assert {m["id"] for m in await w.pick(free_slots=9)} == {"2", "3", "4"}


# ---------- evening slots (evening_reserved_rounds) ----------

def _at(hour: int) -> float:
    from agentpitbench.watcher import _day0
    return _day0() + hour * 3600 + 120


def test_evening_defaults_neutral():
    d = Settings()
    assert (d.evening_reserved_rounds, d.evening_start_hour_utc) == (0, 15)


async def test_cap_now(s, db):
    w = await _watch(s, db, [])
    s.daily_round_cap = 10
    assert (w.cap_now(_at(3)), w.cap_now(_at(16))) == (10, 10)      # off by default
    s.evening_reserved_rounds = 3
    assert (w.cap_now(_at(3)), w.cap_now(_at(14)), w.cap_now(_at(15)), w.cap_now(_at(23))) == (7, 7, 10, 10)
    s.evening_reserved_rounds = 12
    assert w.cap_now(_at(3)) == 0


async def test_evening_slots_held_back_then_usable(s, db):
    s.daily_round_cap = 4
    s.evening_reserved_rounds = 2
    w = await _watch(s, db, [gamma(i, W.format(i)) for i in range(2, 8)])
    morning = await w.pick(free_slots=9, now=_at(9))
    assert len(morning) == 2                                          # 4 - 2 reserved
    for m in morning:
        db.create_round(m)
    assert await w.pick(free_slots=9, now=_at(12)) == []
    assert len(await w.pick(free_slots=9, now=_at(15))) == 2         # any market from 15 UTC on


async def test_evening_reserve_with_batch_allowance(s, db):
    s.round_start_window = True
    s.daily_round_cap = 10
    s.evening_reserved_rounds = 3
    w = await _watch(s, db, [])
    for i in range(7):
        db.create_round(gamma(100 + i, W.format(100 + i)))
    assert w.batch_allowance(_at(10)) == 0                            # the morning share is used
    assert w.batch_allowance(_at(15)) == 1                            # 3 left over 9 batches
