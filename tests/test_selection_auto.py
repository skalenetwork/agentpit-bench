"""Selection review auto-apply (operator decision 2026-10-12; docs/ai-automation-spec.md §5.3): the same lock
writer as `method approve`, bounds re-checked, one change per 7 days, a generated and translated changelog
line, published evidence, an info alert, `method revert`, rounds stamped with their version, and a quiet
methodology-drift detector."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import copy_lock_v10

from agentpitbench import anomaly, i18n, i18n_fill, methodology as m, opai, selection_review as sr
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.i18n import Translator

PKG = Path(__file__).resolve().parent.parent / "agentpitbench"
SUNDAY = datetime(2026, 10, 18, 19, 5, tzinfo=timezone.utc).timestamp()
WEEK = 7 * 86400
GOOD = {"changes": {"max_favourite_price": 0.8}, "rationale": "Heavy favourites made dull rounds.",
        "evidence": "most voids had a 0.84 favourite", "changelog_text": "Favourites are capped at 80c."}


@pytest.fixture
def env(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    shutil.copy(PKG / "prompt.txt", pkg / "prompt.txt")
    shutil.copytree(PKG / "prompts", pkg / "prompts")
    shutil.copytree(PKG / "i18n", pkg / "i18n")
    copy_lock_v10(pkg / "methodology.json")
    monkeypatch.setattr(m, "PKG", pkg)
    monkeypatch.setattr(m, "LOCK", pkg / "methodology.json")
    monkeypatch.setattr(m, "I18N_DIR", pkg / "i18n")
    monkeypatch.setattr(m, "_fill_or_placeholder", lambda ids: pytest.fail("no i18n fill expected"))
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    s = Settings(data_dir=tmp_path / "var", opai_enabled=True, opai_tasks=["proposal"],
                 selection_review_enabled=True, selection_auto_apply=True)
    s.opai_task_budget = {**s.opai_task_budget, "proposal": 5}   # ai_calls count by wall-clock day
    m.apply(s, {})
    return s, DB(s.db_path), pkg


def alerts(s):
    return [json.loads(f.read_text()) for f in sorted((s.data_dir / "alerts").glob("*.json"))]


def test_off_by_default_on_in_bench_toml():
    from agentpitbench import config
    assert Settings().selection_auto_apply is False
    assert config.load(PKG.parent / "bench.toml").selection_auto_apply is True


async def test_happy_path(env, fake_claude):
    s, db, pkg = env
    old_round = db.create_round({"id": "m0", "question": "Before?", "outcomes_list": ["Yes", "No"]},
                                s.methodology_version)
    fake_claude.respond(GOOD)
    out = await sr.review(s, db, SUNDAY)
    assert out["applied"] == "1.1", out
    lock = m.load()
    assert lock["version"] == "1.1" and lock["params"]["max_favourite_price"] == 0.8
    # the running settings follow at once: new rounds use the new rule and record the new version
    assert s.max_favourite_price == 0.8 and s.methodology_version == "1.1"
    new_round = db.create_round({"id": "m1", "question": "After?", "outcomes_list": ["Yes", "No"]},
                                s.methodology_version)
    assert db.round(old_round)["methodology_version"] == "1.0"
    assert db.round(new_round)["methodology_version"] == "1.1"
    # the changelog line is generated from the change, never the model's text
    e = lock["changelog"][-1]
    assert e["text"] == m.AUTO_TEXT and e["approved_by"] == m.AUTO_BY and e["params_sha"] == m.params_sha(lock)
    assert e["values"] == {"param": "max_favourite_price", "old": "0.85", "new": "0.8",
                           "link": "https://agentpitbench.org/methodology/changes/1.1.json"}
    assert "80c" not in json.dumps(e)
    assert m.changelog()[0] == (e["date"], m.AUTO_TEXT, e["values"], e["version"])
    # the evidence is published, the model's text included
    ev = json.loads((m.changes_dir(s) / "1.1.json").read_text())
    assert ev["change"] == {"key": "max_favourite_price", "old": 0.85, "new": 0.8}
    assert ev["proposal"]["rationale"] == GOOD["rationale"] and "news shadow" in ev["proposal"]["evidence"]
    assert ev["proposal"]["changelog_text"] == GOOD["changelog_text"] and ev["previous_version"] == "1.0"
    p = m.get_proposal(s, out["proposal"])
    assert p["state"] == "applied" and p["approved_version"] == "1.1"
    a = alerts(s)
    assert [(x["severity"], x["kind"]) for x in a] == [("info", "selection-auto-apply")]
    assert "method revert 1.1" in a[0]["how_to_resume"] and "git add agentpitbench/methodology.json" in a[0][
        "how_to_resume"]


async def test_drift_detector_quiet_after_auto_apply(env, fake_claude):
    s, db, pkg = env
    before = (pkg / "methodology.json").read_text()
    assert anomaly.methodology_drift(s, db, SUNDAY) == []
    fake_claude.respond(GOOD)
    assert (await sr.review(s, db, SUNDAY))["applied"] == "1.1"
    assert m.verify(s) == [] and anomaly.methodology_drift(s, db, SUNDAY) == []
    # a fresh process (restart) agrees
    s2 = Settings(data_dir=s.data_dir)
    m.apply(s2, {})
    assert m.verify(s2) == []
    # but a deploy that drops the uncommitted lock is caught
    (pkg / "methodology.json").write_text(before)
    m.apply(s2, {})
    assert any("older than generated change v1.1" in p for p in m.verify(s2))
    assert anomaly.methodology_drift(s2, db, SUNDAY)


async def test_bounds_rejected(env, fake_claude):
    s, db, pkg = env
    before = (pkg / "methodology.json").read_bytes()
    fake_claude.respond({**GOOD, "changes": {"max_favourite_price": 0.99}})
    out = await sr.review(s, db, SUNDAY)
    assert "proposal" not in out and (pkg / "methodology.json").read_bytes() == before
    # defense in depth: a proposal file edited out of bounds after validation is still refused
    p = m.propose(s, {"max_favourite_price": 0.8}, "x", "opai")
    f = m.proposals_dir(s) / f"{p['id']}.json"
    f.write_text(json.dumps({**p, "changes": {"max_favourite_price": 0.99}}))
    with pytest.raises(m.MethodologyError, match="outside"):
        m.auto_apply(s, p["id"], now=SUNDAY)
    for bad in ({"selection.rater.prompt_version": 1}, {"daily_round_cap": 6, "max_days_to_close": 5}):
        f.write_text(json.dumps({**p, "changes": bad}))
        with pytest.raises(m.MethodologyError):
            m.auto_apply(s, p["id"], now=SUNDAY)
    f.write_text(json.dumps({**p, "author": "operator"}))
    with pytest.raises(m.MethodologyError, match="not an open opai proposal"):
        m.auto_apply(s, p["id"], now=SUNDAY)
    f.write_text(json.dumps(p))
    s.selection_auto_apply = False
    with pytest.raises(m.MethodologyError, match="off"):
        m.auto_apply(s, p["id"], now=SUNDAY)
    assert (pkg / "methodology.json").read_bytes() == before and not list(m.changes_dir(s).glob("*.json"))


async def test_rate_limit(env, fake_claude):
    s, db, pkg = env
    fake_claude.respond(GOOD)
    assert (await sr.review(s, db, SUNDAY))["applied"] == "1.1"
    fake_claude.respond({**GOOD, "changes": {"daily_round_cap": 6}})
    out = await sr.review(s, db, SUNDAY + 6 * 86400)
    assert out.get("applied", "missing") is None and "rate limit" in out["summary"], out
    p = m.get_proposal(s, out["proposal"])
    assert p["state"] == "rejected" and "rate limit" in p["reject_reason"]   # doesn't block next week
    assert m.load()["version"] == "1.1" and s.daily_round_cap == 5
    assert [x["title"] for x in alerts(s)][-1].endswith("not applied")
    # a week later by the calendar (the review may tick a few minutes earlier than last Sunday's)
    out = await sr.review(s, db, SUNDAY + WEEK - 300)
    assert out["applied"] == "1.2" and s.daily_round_cap == 6 and m.verify(s) == []


async def test_revert(env, fake_claude):
    s, db, pkg = env
    fake_claude.respond(GOOD)
    await sr.review(s, db, SUNDAY)
    with pytest.raises(m.MethodologyError, match="terminal"):
        m.revert(s, "1.1", isatty=lambda: False)
    with pytest.raises(m.MethodologyError, match="not confirmed"):
        m.revert(s, "1.1", isatty=lambda: True, ask=lambda _: "yes")
    with pytest.raises(m.MethodologyError, match="not a generated"):
        m.revert(s, "1.0", isatty=lambda: True, ask=lambda _: "revert v1.0")
    new = m.revert(s, "1.1", isatty=lambda: True, ask=lambda _: "revert v1.1", now=SUNDAY + 3600)
    assert new["version"] == "1.2" and new["params"]["max_favourite_price"] == 0.85
    e = new["changelog"][-1]
    assert e["text"] == m.REVERT_TEXT and e["approved_by"] == "operator"
    assert e["values"] == {"param": "max_favourite_price", "old": "0.8", "new": "0.85", "v": "1.1",
                           "link": "https://agentpitbench.org/methodology/changes/1.2.json"}
    assert json.loads((m.changes_dir(s) / "1.2.json").read_text())["reverts"] == "1.1"
    assert s.max_favourite_price == 0.85 and m.verify(s) == []
    with pytest.raises(m.MethodologyError, match="changed again"):   # once only
        m.revert(s, "1.1", isatty=lambda: True, ask=lambda _: "revert v1.1")
    # a revert doesn't count against the weekly limit (it isn't an automatic change)
    assert m.last_auto(new)["version"] == "1.1"


def test_changelog_templates_translated():
    ids = i18n.message_ids()
    for tpl in (m.AUTO_TEXT, m.REVERT_TEXT):
        assert tpl in ids
        for f in sorted((PKG / "i18n").glob("*.json")):
            t = json.loads(f.read_text("utf-8"))
            assert tpl in t and t[tpl] != tpl and i18n_fill.check(tpl, t[tpl]) is None, (f.stem, tpl)
    values = {"param": "max_favourite_price", "old": "0.85", "new": "0.8",
              "link": "https://agentpitbench.org/methodology/changes/1.1.json"}
    de = Translator("de")(m.AUTO_TEXT, **values)
    assert de.startswith("Marktauswahl: max_favourite_price 0.85 → 0.8") and values["link"] in de
    assert Translator("en")(m.AUTO_TEXT, **values).startswith("Market selection: max_favourite_price 0.85 → 0.8")
    ja = Translator("ja")(m.REVERT_TEXT, **values, v="1.1")
    assert "v1.1" in ja and "0.85" in ja


def test_only_the_review_auto_applies_and_only_the_cli_reverts():
    import ast
    for f in sorted(PKG.glob("*.py")):
        if f.name == "methodology.py":
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Attribute) and n.attr == "auto_apply":
                assert f.name == "selection_review.py", f.name
            if isinstance(n, ast.Attribute) and n.attr == "revert":
                assert f.name == "opscli.py", f.name
