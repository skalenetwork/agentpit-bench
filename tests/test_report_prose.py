"""Weekly report prose (docs/ai-automation-spec.md §8, §15.10): the betting record only since the Brier
competitions were removed (docs/simple-scoring-spec.md §5)."""
import pytest

from agentpitbench import opai, report_prose as rp
from agentpitbench.config import Settings
from agentpitbench.db import DB

REP = {"week": "2026-W42", "rounds": 9, "crowd": {"wins": 5, "losses": 4},
       "betting": [{"agent": "claude", "name": "Claude", "wins": 6, "losses": 3, "profit": 120.5},
                   {"agent": "grok", "name": "Grok", "wins": 4, "losses": 5, "profit": -30.0}],
       "contrarian": {"total": 4, "right": 1}}
GOOD = {"headline": "{name.claude} edges {name.grok} in the money race",
        "paragraphs": ["{name.claude} made the most profit, {profit.claude} over {rounds} rounds.",
                       "In betting, {name.claude} went {wins.claude}-{losses.claude}; the Crowd {crowd.wins}-{crowd.losses}."]}


@pytest.mark.parametrize("change,why", [
    ({"headline": "Grok wins 30 markets"}, "digit outside a placeholder"),
    ({"headline": "{brier.claude} for Claude"}, "unknown placeholder brier.claude"),
    ({"headline": "{crowd_brier} for the Crowd"}, "unknown placeholder crowd_brier"),
    ({"headline": "A significant week"}, "banned word"),
    ({"paragraphs": ["{name.grok} was the best bettor.", "x"]}, "superlative for an agent other than the betting leader"),
    ({"paragraphs": ["Grok was the top AI.", "x"]}, "superlative for an agent other than the betting leader"),
    ({"paragraphs": ["only one"]}, "paragraph count"),
])
def test_validate_rejects(change, why):
    assert rp.validate({**GOOD, **change}, REP) == why


def test_superlative_allowed_for_the_betting_leader():
    assert rp.betting_leader(REP) == "claude"
    assert rp.validate({**GOOD, "paragraphs": ["{name.claude} was the best bettor.", "x"]}, REP) is None


def test_validate_and_fill():
    assert rp.validate(GOOD, REP) is None
    f = rp.fill(GOOD, REP)
    assert f["headline"] == "Claude edges Grok in the money race"
    assert f["paragraphs"][0] == "Claude made the most profit, +120 over 9 rounds."
    assert f["paragraphs"][1] == "In betting, Claude went 6-3; the Crowd 5-4."
    assert not any(k.startswith(("brier", "crowd_brier", "forecasts", "markets")) for k in rp.flat(REP))


async def test_write_and_attach(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    s = Settings(data_dir=tmp_path / "var", opai_enabled=True, opai_tasks=["report"])
    db = DB(s.db_path)
    fake_claude.respond(GOOD)
    assert await rp.write(s, db, REP)
    reps = [dict(REP)]
    rp.attach(db, reps)
    assert reps[0]["prose"]["headline"] == "Claude edges Grok in the money race"
    assert not await rp.write(s, db, REP)  # once per week
