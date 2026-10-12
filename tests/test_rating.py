"""Newsworthiness rating in shadow mode (docs/ai-automation-spec.md §5, §15.7)."""
import json

import pytest

from agentpitbench import exports, opai, rating, site
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.virality import market_priority


@pytest.fixture
def s(tmp_path):
    return Settings(data_dir=tmp_path / "var", opai_enabled=True, opai_tasks=["rate"])


@pytest.fixture
def db(s):
    return DB(s.db_path)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    rating._pending.clear()
    rating._tries.clear()


MARKETS = [
    {"id": 1, "question": "Will the Fed cut rates in October?", "volume": 10},
    {"id": 2, "question": "Counter-Strike: Team A vs Team B (BO3)?", "volume": 99},
    {"id": 3, "question": "Will Levante UD vs. Sevilla FC end in a draw?", "volume": 5},
    {"id": 4, "question": "Will it rain in Paris tomorrow?", "volume": 1},
]


def test_keyword_mode_is_todays_order(s, db):
    for mid, score in ((1, 10), (2, 95), (3, 90), (4, 99)):
        db.x("INSERT INTO market_ratings(market_id,score,keyword_tier,rated_at) VALUES(?,?,0,0)", str(mid), score)
    assert s.selection_scoring == "keyword"
    a = sorted(MARKETS, key=lambda m: rating.priority(m, db, s), reverse=True)
    assert a == sorted(MARKETS, key=market_priority, reverse=True)


def test_model_mode_with_floor_and_fallback(s, db):
    s.selection_scoring = "model"
    for mid, score in ((1, 40), (2, 80), (4, 70)):
        db.x("INSERT INTO market_ratings(market_id,score,keyword_tier,rated_at) VALUES(?,?,0,0)", str(mid), score)
    order = [m["id"] for m in sorted(MARKETS, key=lambda m: rating.priority(m, db, s), reverse=True)]
    # 4 (model 70), 1 (model 40), 3 (no rating: keyword tier 1 = 35), 2 (esports 80 floored to 10)
    assert order == [4, 1, 3, 2]
    assert rating.nw_of(MARKETS[1], db, s) == {"score": 10, "source": "keyword"}


async def test_rate_once_validated_and_published(s, db, fake_claude):
    fake_claude.respond({"ratings": [
        {"market_id": "1", "score": 88, "reason": "Central bank decision with global market impact"},
        {"market_id": "2", "score": 15, "reason": "Esports match"},
        {"market_id": "99", "score": 50, "reason": "not asked"},
    ]})
    rating.observe(MARKETS[:3])
    assert await rating.rate_pending(s, db, force_gap=False) == -1  # unknown id: the whole batch is rejected
    fake_claude.respond({"ratings": [
        {"market_id": "1", "score": 88, "reason": "Central bank decision with global market impact"},
        {"market_id": "2", "score": 15, "reason": "see https://x.com"},  # reason fails clean_quote
    ]})
    assert await rating.rate_pending(s, db, force_gap=False) == 1
    sent = json.loads(fake_claude.calls[-1]["stdin"].split('<data id="input">')[1].split("</data>")[0])
    assert {m["market_id"] for m in sent["markets"]} == {"1", "2", "3"}
    assert "volume" not in json.dumps(sent) and "price" not in json.dumps(sent)
    assert rating.rating_for(db, 1) == 88
    fake_claude.respond({"ratings": [{"market_id": "1", "score": 5, "reason": "x"}]})
    await rating.rate_pending(s, db, force_gap=False)
    assert rating.rating_for(db, 1) == 88  # never re-rated
    assert db.one("SELECT score FROM market_ratings WHERE market_id='2'")["score"] is None  # 3 tries: keyword
    rows = rating.export_rows(db)
    r1 = next(r for r in rows if r["market_id"] == "1")
    assert r1["model"] == "claude-sonnet-fake" and r1["prompt_version"] == "rate.v1" and r1["source_used"] == "keyword"
    assert db.one("SELECT public FROM operator_ai_outputs WHERE accepted=1 ORDER BY output_id LIMIT 1")["public"] == 1


async def test_gap_between_calls(s, db, fake_claude):
    fake_claude.respond({"ratings": []})
    rating.observe(MARKETS[:1])
    await rating.rate_pending(s, db)
    rating.observe(MARKETS[1:2])
    assert await rating.rate_pending(s, db) == -1 and len(fake_claude.calls) == 1


def test_round_records_and_page(s, db):
    db.x("INSERT INTO market_ratings(market_id,score,reason,keyword_tier,rated_at,question) VALUES('7',72,'r',2,?,'q')",
         __import__("time").time())
    rid = db.create_round({"id": 7, "question": "Will the Fed cut rates?", "outcomes_list": ["Yes", "No"],
                           "nw": {"score": 60, "source": "keyword"}})
    db.set_round(rid, state="awaiting_resolution")
    rec = exports.round_record(s, db, db.round(rid))
    assert rec["newsworthiness"] == {"score": 60, "source": "keyword", "model_score": 72}
    exports.export(s, db)
    out = site.build(s)
    assert "Newsworthiness 72/100" in (out / f"round/{rid}/index.html").read_text()
    assert json.loads((out / "data/newsworthiness.json").read_text())[0]["score"] == 72


def test_shadow_report(db):
    import time
    for mid, q, score, tier in (("1", "Fed decision", 90, 3), ("2", "CS match", 80, 0), ("3", "Rain", 20, 1),
                                ("4", "Election", 95, 3)):
        db.x("INSERT INTO market_ratings(market_id,score,reason,keyword_tier,rated_at,question) VALUES(?,?,?,?,?,?)",
             mid, score, "why", tier, time.time(), q)
    rep = rating.shadow_report(db, 7, per_day=2)
    assert "overlap" in rep and "Spearman" in rep and "Largest disagreements" in rep
