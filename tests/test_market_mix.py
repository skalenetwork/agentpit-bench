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
