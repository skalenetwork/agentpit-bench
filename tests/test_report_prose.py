"""Weekly report prose (docs/ai-automation-spec.md §8, §15.10)."""
import pytest

from agentpitbench import opai, report_prose as rp
from agentpitbench.config import Settings
from agentpitbench.db import DB

REP = {"week": "2026-W42", "rounds": 9, "markets": 30, "crowd_brier": 0.21,
       "forecast": [{"agent": "grok", "name": "Grok", "n": 30, "brier": 0.183},
                    {"agent": "claude", "name": "Claude", "n": 30, "brier": 0.19}],
       "betting": [{"agent": "claude", "name": "Claude", "wins": 6, "losses": 3, "profit": 120.5}],
       "contrarian": {"total": 4, "right": 1}}
GOOD = {"headline": "{name.grok} edges {name.claude} on forecasts",
        "paragraphs": ["{name.grok} was the most accurate forecaster at {brier.grok} across {markets} markets.",
                       "In betting, {name.claude} went {wins.claude}-{losses.claude} for {profit.claude}."]}


@pytest.mark.parametrize("change,why", [
    ({"headline": "Grok wins 30 markets"}, "digit outside a placeholder"),
    ({"headline": "{brier.codex} for ChatGPT"}, "unknown placeholder brier.codex"),
    ({"headline": "A significant week"}, "banned word"),
    ({"paragraphs": ["{name.claude} was the best forecaster.", "x"]}, "superlative for an agent without the lowest Brier"),
    ({"paragraphs": ["only one"]}, "paragraph count"),
])
def test_validate_rejects(change, why):
    assert rp.validate({**GOOD, **change}, REP) == why


def test_validate_and_fill():
    assert rp.validate(GOOD, REP) is None
    f = rp.fill(GOOD, REP)
    assert f["headline"] == "Grok edges Claude on forecasts"
    assert f["paragraphs"][0] == "Grok was the most accurate forecaster at 0.183 across 30 markets."
    assert f["paragraphs"][1] == "In betting, Claude went 6-3 for +120."


async def test_write_and_attach(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    s = Settings(data_dir=tmp_path / "var", opai_enabled=True, opai_tasks=["report"])
    db = DB(s.db_path)
    fake_claude.respond(GOOD)
    assert await rp.write(s, db, REP)
    reps = [dict(REP)]
    rp.attach(db, reps)
    assert reps[0]["prose"]["headline"] == "Grok edges Claude on forecasts"
    assert not await rp.write(s, db, REP)  # once per week
