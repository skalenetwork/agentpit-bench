"""Per-round agent summaries (docs/ai-automation-spec.md §7, §15.5, §15.9)."""
import inspect
import json
from pathlib import Path

import pytest

from agentpitbench import exports, opai, site, summaries
from agentpitbench.config import Settings
from agentpitbench.db import DB


@pytest.fixture
def s(tmp_path):
    return Settings(data_dir=tmp_path / "var", opai_enabled=True)


@pytest.fixture
def db(s):
    return DB(s.db_path)


@pytest.fixture(autouse=True)
def _outside_sweep(monkeypatch):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))


def make_round(s, db, state="awaiting_resolution"):
    rid = db.create_round({"id": 5, "question": "Will Barcelona beat Sevilla?", "outcomes_list": ["Yes", "No"],
                           "prices_list": [0.62, 0.38], "endDate": "2099-01-01T00:00:00Z"})
    for agent, pick in (("claude", ("Yes", 0.62, 0.7)), ("codex", ("No", 0.38, 0.55)), ("agy", None)):
        run = db.create_run(rid, agent)
        tdir = s.transcripts_dir / str(rid)
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{agent}.txt").write_text("--- output ---\nBarcelona have won 4 of their last 5 home games.\n"
                                           "The price of 62 cents looks fair against injuries.\n")
        if pick:
            db.record_bet(run, outcome=pick[0], avg_price=pick[1], confidence=pick[2], stake_filled=100,
                          shares_filled=100 / pick[1], decided_s=40)
        else:
            db.record_bet(run, outcome=None)
        db.finish_run(run, "bet" if pick else "timeout", str(tdir / f"{agent}.txt"), None)
    db.set_round(rid, state=state)
    return rid


GOOD = {"agents": {
    "claude": {"line1": "Leaned on Barcelona winning 4 of their last 5 home games.",
               "line2": "Took Yes at 62 cents, calling the price fair."},
    "codex": {"line1": "Doubted Barcelona's defence after recent injuries.",
              "line2": "Bought the cheaper side at 38 cents for the payout."},
}}


async def test_sealed_round_raises_before_reading(s, db, monkeypatch):
    rid = make_round(s, db, state="running")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read")))
    with pytest.raises(opai.SealedError):
        await summaries.write(s, db, rid)


def test_payload_builder_never_touches_the_sweep():
    src = inspect.getsource(summaries.build_payload) + inspect.getsource(summaries._transcript_lines)
    for word in ("forecast", "sweep"):
        assert word not in src


async def test_write_validates_per_agent(s, db, fake_claude):
    rid = make_round(s, db)
    bad = json.loads(json.dumps(GOOD))
    bad["agents"]["codex"]["line2"] = "Bought No at 41 cents."  # 41 is not in the record or transcript
    fake_claude.respond(bad)
    assert await summaries.write(s, db, rid) == 1
    rows = {r["agent"]: dict(r) for r in db.q("SELECT * FROM round_summaries")}
    assert set(rows) == {"claude"} and rows["claude"]["output_id"]
    out = db.one("SELECT * FROM operator_ai_outputs")
    assert out["public"] == 1 and "codex: ungrounded number" in out["reject_reason"]
    sent = fake_claude.calls[0]["stdin"]
    assert '"agy"' not in sent  # no-bet agents aren't summarised


@pytest.mark.parametrize("line,why", [
    ("Took Yes at 62 cents but No looked close.", "names another outcome"),
    ("See https://example.com for the stats.", "quotes can't contain links"),
    ("Agreed with @someone about the price.", "quotes can't @mention accounts"),
    ("A genius read of the 62 cent price.", "banned word"),
    ("x" * 111, "too long"),
    ("Paid 99 cents.", "ungrounded number"),
])
def test_check_line(line, why):
    assert summaries.check_line(line, "Yes", ["Yes", "No"], {"62"}) == why


async def test_page_and_export(s, db, fake_claude):
    rid = make_round(s, db)
    fake_claude.respond(GOOD)
    assert await summaries.write(s, db, rid) == 2
    assert await summaries.write(s, db, rid) == 0  # written once
    rec = exports.round_record(s, db, db.round(rid))
    assert next(e for e in rec["entries"] if e["agent"] == "claude")["summary"][0].startswith("Leaned on")
    exports.export(s, db)
    html = (site.build(s) / f"round/{rid}/index.html").read_text()
    assert "Leaned on Barcelona" in html and "Full transcript" in html
    assert summaries.due(db) == []


async def test_opai_down_then_catch_up(s, db, fake_claude):
    rid = make_round(s, db)
    s.opai_enabled = False
    assert await summaries.write(s, db, rid) == -1 and not fake_claude.calls
    assert summaries.due(db) == [rid]
    s.opai_enabled = True
    fake_claude.respond(GOOD)
    await summaries.atick(s, db)
    assert summaries.due(db) == []
