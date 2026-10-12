"""Weekly selection review (docs/ai-automation-spec.md §5.3): with selection_auto_apply off it drafts proposals
and never applies them (auto-apply: tests/test_selection_auto.py)."""
import json
from datetime import datetime, timezone

import pytest

from agentpitbench import methodology, opai, selection_review as sr
from agentpitbench.config import Settings
from agentpitbench.db import DB

SUNDAY = datetime(2026, 10, 18, 19, 5, tzinfo=timezone.utc).timestamp()
GOOD = {"changes": {"selection.scoring": "model"}, "rationale": "Model ratings track what gets read.",
        "evidence": "overlap 2/5 on most days", "changelog_text": "Markets are ordered by a model-rated "
        "newsworthiness score (keyword rule as fallback and floor)."}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    s = Settings(data_dir=tmp_path / "var", opai_enabled=True, opai_tasks=["proposal"], selection_review_enabled=True)
    db = DB(s.db_path)
    m = {"id": "m1", "slug": "s", "question": "Will Brazil win the election?", "outcomes_list": ["Yes", "No"],
         "prices_list": [0.62, 0.38], "nw": {"score": 70, "source": "keyword"}}
    rid = db.create_round(m)
    db.set_round(rid, state="void")
    running = db.create_round({**m, "id": "m2", "question": "Sealed one?"})
    assert db.round(running)["state"] == "running"
    return s, db


@pytest.mark.parametrize("data,why", [
    ({"changes": {"selection.scoring": "model", "daily_round_cap": 4}, "rationale": "x", "changelog_text": "x"},
     "exactly one change"),
    ({**GOOD, "changes": {"stake": 200}}, "key stake not allowed"),
    ({**GOOD, "changes": {"selection.rater.prompt_version": 2}}, "key selection.rater.prompt_version not allowed"),
    ({**GOOD, "changes": {"max_favourite_price": 0.99}}, "max_favourite_price=0.99 outside 0.7..0.92"),
    ({**GOOD, "changelog_text": "see https://evil.example"}, "changelog_text rejected: quotes can't contain links"),
    ({**GOOD, "changelog_text": ""}, "changelog_text missing or too long"),
    ({"none": "fine", "changes": {"daily_round_cap": 4}}, "none together with a change"),
])
def test_validate_rejects(data, why):
    assert sr.validate(data) == why


def test_validate_accepts():
    assert sr.validate(GOOD) is None
    assert sr.validate({"none": "selection looks balanced"}) is None
    assert opai.check(GOOD, opai.schema_for("proposal", {})) is None


def test_payload_excludes_running_rounds(env):
    s, db = env
    p = sr.payload(s, db, SUNDAY + 3600)
    assert [r["void"] for r in p["rounds"]] == [True]
    assert p["rounds"][0]["favourite_price"] == 0.62 and p["rounds"][0]["nw_score"] == 70
    assert "Sealed" not in json.dumps(p)
    assert set(p["bounds"]) == set(sr.KEYS) and "shadow_report" in p


async def test_writes_a_proposal_and_never_applies(env, fake_claude):
    s, db = env
    before = methodology.LOCK.read_bytes()
    fake_claude.respond(GOOD)
    out = await sr.due(s, db, SUNDAY)
    assert out["proposal"]
    p = methodology.get_proposal(s, out["proposal"])
    assert p["author"] == "opai" and p["state"] == "open" and p["changes"] == {"selection.scoring": "model"}
    assert "news shadow" in p["evidence"] and p["output_id"]
    assert methodology.LOCK.read_bytes() == before and methodology.load()["version"] == "1.0"
    assert db.get(sr.LAST)["proposal"] == p["id"]
    assert await sr.due(s, db, SUNDAY + 600) is None                  # once a week
    out = await sr.review(s, db, SUNDAY + 7 * 86400)                   # an open opai proposal: no new one
    assert out["summary"].startswith("skipped") and len(fake_claude.calls) == 1


async def test_none_writes_nothing(env, fake_claude):
    s, db = env
    fake_claude.respond({"none": "not enough rounds"})
    out = await sr.review(s, db, SUNDAY)
    assert "proposal" not in out and methodology.list_proposals(s) == []


async def test_off_and_not_sunday(env, fake_claude, tmp_path):
    s, db = env
    assert await sr.due(s, db, SUNDAY - 86400) is None                 # Saturday
    assert await sr.due(s, db, SUNDAY - 3 * 3600) is None              # Sunday 16:05
    s.selection_review_enabled = False
    assert await sr.due(s, db, SUNDAY) is None
    assert fake_claude.calls == []


def test_never_calls_approve():
    import ast
    tree = ast.parse((methodology.PKG / "selection_review.py").read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("approve", "write_english")]
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and "approve" in {a.name for a in n.names}]
