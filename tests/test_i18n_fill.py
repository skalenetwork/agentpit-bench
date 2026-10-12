"""`agentpitbench i18n fill` (docs/ai-automation-spec.md §12, §15.13), on a scratch copy of the tables."""
import json
import shutil
from pathlib import Path

import pytest

from agentpitbench import i18n, i18n_fill, opai
from agentpitbench.config import Settings
from agentpitbench.db import DB

PKG = Path(__file__).resolve().parent.parent / "agentpitbench"
NEW = "Hello {name}, you have <b>{n}</b> rounds"


@pytest.fixture
def tables(tmp_path, monkeypatch):
    d = tmp_path / "i18n"
    shutil.copytree(PKG / "i18n", d)
    real = i18n.message_ids
    monkeypatch.setattr(i18n, "message_ids", lambda: real() | {NEW})
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    return d


@pytest.fixture
def s(tmp_path):
    return Settings(data_dir=tmp_path / "var", opai_enabled=True)


def test_check():
    assert i18n_fill.check(NEW, "Hallo {name}, du hast <b>{n}</b> Runden") is None
    assert i18n_fill.check(NEW, "Hallo {nom}, du hast <b>{n}</b> Runden") == "placeholders differ"
    assert i18n_fill.check(NEW, "Hallo {name}, du hast {n} Runden") == "<b> count differs"
    assert i18n_fill.check(NEW, " ") == "empty"


def test_missing_and_fill(tables, s, fake_claude):
    m = i18n_fill.missing(["de", "fr"], d=tables)
    assert m == {"de": [NEW], "fr": [NEW]}
    fake_claude.respond({"translations": {"de": {NEW: "Hallo {name}, du hast <b>{n}</b> Runden", "bogus": "x"},
                                          "fr": {NEW: "Bonjour {nom}"}}})
    left = i18n_fill.fill(s, DB(s.db_path), langs=["de", "fr"], d=tables)
    assert left == {"de": 0, "fr": 1}
    assert len(fake_claude.calls) == 1  # both languages in one call
    de = json.loads((tables / "de.json").read_text())
    assert de[NEW].startswith("Hallo") and "bogus" not in de
    assert list(de) == sorted(de)  # merge keeps sort order
    assert (tables / "de.json").read_text().startswith('{\n"')  # indent 0, as today
    assert NEW not in json.loads((tables / "fr.json").read_text())  # rejected: English fallback at render
    sent = json.loads(fake_claude.calls[0]["stdin"].split('<data id="input">')[1].split("</data>")[0])
    assert set(sent["languages"]) == {"de", "fr"} and sent["languages"]["de"]["ids"] == [NEW]
    assert len(sent["languages"]["de"]["style_samples"]) == i18n_fill.SAMPLES


def test_batches_languages_and_falls_back_per_language(tables, s, fake_claude):
    """A new id in all 39 languages is 4 calls (10 languages each), not 39. A language the answer leaves out
    falls back alone; an explicit id gets its English placeholder there and is queued for retry."""
    langs = [c for c in i18n.CODES if c != "en"]
    calls = i18n_fill.plan({l: [NEW] for l in langs})
    assert [len(c) for c in calls] == [10, 10, 10, len(langs) - 30]
    assert i18n_fill.plan({"de": [str(i) for i in range(400)]}) == [[("de", [str(i) for i in range(150)])],
                                                                    [("de", [str(i) for i in range(150, 300)])],
                                                                    [("de", [str(i) for i in range(300, 400)])]]
    assert [len(c) for c in i18n_fill.plan({l: [str(i) for i in range(100)] for l in langs[:7]})] == [3, 3, 1]
    good = "Tr {name} <b>{n}</b>"
    fake_claude.respond(*[{"translations": {l: {NEW: good} for l, _ in c if l != "ja"}} for c in calls])
    db = DB(s.db_path)
    left = i18n_fill.fill(s, db, ids=[NEW], d=tables)
    assert len(fake_claude.calls) == 4
    assert left["ja"] == 1 and sum(left.values()) == 1
    assert json.loads((tables / "de.json").read_text())[NEW] == good
    assert json.loads((tables / "ja.json").read_text())[NEW] == NEW and db.get("i18n_retry") == [NEW]
    schema = json.loads(fake_claude.calls[0]["argv"][fake_claude.calls[0]["argv"].index("--json-schema") + 1])
    assert set(schema["properties"]["translations"]["properties"]) == {l for l, _ in calls[0]}
    assert db.one("SELECT COUNT(*) n FROM operator_ai_outputs WHERE public=1")["n"] == 4


def test_explicit_ids_get_placeholders_when_opai_is_down(tables, s, fake_claude):
    s.opai_enabled = False
    db = DB(s.db_path)
    left = i18n_fill.fill(s, db, ids=[NEW], d=tables)
    assert set(left) == {c for c in i18n.CODES if c != "en"} and all(v == 1 for v in left.values())
    assert json.loads((tables / "ja.json").read_text())[NEW] == NEW
    assert db.get("i18n_retry") == [NEW]
    assert i18n_fill.missing(["ja"], d=tables, retry={NEW}) == {"ja": [NEW]}  # placeholder still counts
    s.opai_enabled = True
    calls = i18n_fill.plan(i18n_fill.missing(None, {NEW}, tables, {NEW}))
    fake_claude.respond(*[{"translations": {l: {NEW: "Translated {name} <b>{n}</b>"} for l, _ in c}} for c in calls])
    i18n_fill.fill(s, db, d=tables)
    assert json.loads((tables / "ja.json").read_text())[NEW] == "Translated {name} <b>{n}</b>"
    assert db.get("i18n_retry") == []


def test_check_cli_on_repo_tables(tmp_path):
    from agentpitbench import cli
    toml = tmp_path / "bench.toml"
    toml.write_text(f'data_dir = "{tmp_path}/var"\n')
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(toml), "i18n", "fill", "--check"])
    assert e.value.code == 0
