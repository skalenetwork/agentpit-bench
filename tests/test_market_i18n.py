"""Translated market questions and outcome labels (operator addition, 2026-10-12)."""
import json

import pytest

from agentpitbench import exports, i18n, market_i18n as mi, opai, site
from agentpitbench.config import Settings
from agentpitbench.db import DB

Q = "Will Amazon.com, Inc. (AMZN) hit (LOW) $256 Week of October 12 2026?"
KO = "Amazon.com, Inc. (AMZN)이 2026년 10월 12일 주에 (LOW) $256에 도달할까요?"


@pytest.mark.parametrize("src,out,lang,ok", [
    (Q, KO, "ko", True),
    (Q, "Amazon.com, Inc. (AMZN)이 2026년 11월 12일 주에 (LOW) $256에 도달할까요?", "ko", False),  # wrong month
    (Q, "Amazon.com, Inc. (AMZN)이 2026년 10월 12일 주에 (LOW) $265에 도달할까요?", "ko", False),  # amount
    (Q, "아마존 (AMZN)이 2026년 10월 12일 주에 (LOW) $256에 도달할까요?", "ko", False),                # name changed
    (Q, "Amazon.com, Inc. (AMZN)이 2026년 10월 12일 주에 $256에 도달할까요?", "ko", False),              # ticker dropped
    (Q, "Amazon.com, Inc. (AMZN) কি (LOW) $২৫৬ ছুঁবে, অক্টোবর ১২ ২০২৬ সপ্তাহে?", "bn", True),          # native digits
    ("Will Levante UD vs. Sevilla FC end in a draw?", "¿Terminará en empate el Levante UD vs. Sevilla FC?", "es", True),
    ("Will Levante UD vs. Sevilla FC end in a draw?", "¿Terminará en empate el Levante vs. Sevilla?", "es", False),
    ("Yes", "Sí", "es", True),
    ("Yes", "", "es", False),
    ("Bitcoin above 45.5% dominance?", "¿Dominancia de Bitcoin por encima del %45.5?", "tr", True),
    ("Bitcoin above 45.5% dominance?", "¿Dominancia de Bitcoin por encima del 46%?", "es", False),
    ("Over {n} goals?", "Más de {m} goles?", "es", False),
    ("Will it rain?", "Will it rain? " * 10, "es", False),
])
def test_check(src, out, lang, ok):
    assert (mi.check(src, out, lang) is None) is ok, mi.check(src, out, lang)


def test_proper_nouns_and_tickers():
    assert mi.proper_nouns(Q) == ["Amazon.com", "Inc"]
    assert mi.tickers(Q) == ["AMZN", "LOW"]
    assert mi.proper_nouns("Will Levante UD vs. Sevilla FC end in a draw?") == ["Levante", "Sevilla"]


@pytest.fixture
def s(tmp_path):
    return Settings(data_dir=tmp_path / "var", opai_enabled=True)


@pytest.fixture
def db(s):
    return DB(s.db_path)


@pytest.fixture(autouse=True)
def _outside_sweep(monkeypatch):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))


def make_round(db, mid=1, q=Q, outcomes=("Yes", "No")):
    rid = db.create_round({"id": mid, "question": q, "outcomes_list": list(outcomes), "prices_list": [0.4, 0.6],
                           "endDate": "2026-10-17T00:00:00Z"})
    db.set_round(rid, state="awaiting_resolution")
    return rid


def answer(langs, texts, override=None):
    table = {Q: KO, "Yes": "예", "No": "아니요"}
    out = {l: [table.get(t, t) for t in texts] for l in langs}
    out.update(override or {})
    return out


async def test_translate_round_one_call_all_languages(s, db, fake_claude):
    langs = mi.site_langs()
    assert langs == [c for c in i18n.CODES if c != "en"]  # whatever the site has at runtime
    rid = make_round(db)
    bad = ["Amazon (AMZN) 2026 10 12 $999", "Ja", "Nein"]
    fake_claude.respond(answer(langs, [Q, "Yes", "No"], {"de": bad}))
    rep = await mi.translate_round(s, db, rid)
    assert len(fake_claude.calls) == 1
    sent = json.loads(fake_claude.calls[0]["stdin"].split('<data id="input">')[1].split("</data>")[0])
    assert sent == {"texts": [Q, "Yes", "No"], "langs": langs}
    schema = json.loads(fake_claude.calls[0]["argv"][fake_claude.calls[0]["argv"].index("--json-schema") + 1])
    assert set(schema["required"]) == set(langs)
    assert rep["stored"] == 3 * (len(langs) - 1) + 2 and rep["rejected"] == 1  # de question rejected
    t = mi.table(db)
    assert t["ko"][Q] == KO and Q not in t["de"] and t["de"]["Yes"] == "Ja"
    row = db.one("SELECT * FROM market_i18n WHERE lang='ko' AND source_text=?", Q)
    assert row["model"] == "claude-sonnet-fake" and row["prompt_version"] == "translate.v1" and row["output_id"]
    out = db.one("SELECT * FROM operator_ai_outputs")
    assert out["purpose"] == "translate" and out["public"] == 1 and "de:number 256 missing" in out["reject_reason"]
    # second market: Yes/No are already done; only the new question and the one German gap are asked for
    rid2 = make_round(db, 2, q="Will Levante UD vs. Sevilla FC end in a draw?")
    fake_claude.respond(answer(langs, ["Will Levante UD vs. Sevilla FC end in a draw?"]))
    p = mi.payload_for(db, mi.round_texts(db.round(rid2)))
    assert p["texts"] == ["Will Levante UD vs. Sevilla FC end in a draw?"]


async def test_tick_newest_first_with_tries_and_budget(s, db, fake_claude):
    langs = mi.site_langs()
    a, b = make_round(db, 1), make_round(db, 2, q="Will Levante UD vs. Sevilla FC end in a draw?")
    assert mi.due(db) == [b, a]
    fake_claude.mode = "badjson"
    await mi.atick(s, db)
    await mi.atick(s, db)
    assert mi.due(db) == [a]  # b used its two tries; it stays English
    s.opai_task_budget = {**s.opai_task_budget, "translate": 2}
    fake_claude.mode = "ok"
    fake_claude.respond(answer(langs, [Q, "Yes", "No"]))
    assert await mi.atick(s, db) == []  # budget spent: no call, no row
    assert len(fake_claude.calls) == 2


def test_site_shows_translation_with_english_source(s, db):
    rid = make_round(db)
    for lang, text in (("ko", KO), ("ko", "예"), ("de", "Ja")):
        src = Q if text == KO else "Yes"
        mi.store(db, src, lang, text, "m", 1, 1)
    exports.export(s, db)
    out = site.build(s)
    ko = (out / f"ko/round/{rid}/index.html").read_text()
    assert f'<span class="mt" title="{Q.replace("&", "&amp;")}">{KO}</span>' in ko.replace("&#39;", "'")
    assert "<title>" in ko and KO.split(" ")[0] in ko.split("<title>")[1].split("</title>")[0]
    assert 'data-outcomes=\'["Yes", "No"]\'' in ko  # the live panel keeps the English labels
    assert '"name": "Will Amazon.com' in ko or Q in ko.split('application/ld+json')[1]  # JSON-LD stays English
    en = (out / f"round/{rid}/index.html").read_text()
    assert KO not in en and 'class="mt"' not in en
    fr = (out / f"fr/round/{rid}/index.html").read_text()
    assert 'class="mt"' not in fr and "Will Amazon.com" in fr  # missing translation: English
    idx = (out / "ko/index.html").read_text()
    assert KO in idx
    api = json.loads((out / "data/rounds.json").read_text())
    assert api[0]["question"] == Q


def test_opai_try_translate_writes_nothing_public(tmp_path, fake_claude, capsys):
    from agentpitbench import cli
    toml = tmp_path / "bench.toml"
    toml.write_text(f'data_dir = "{tmp_path}/v"\n')
    db = DB(tmp_path / "v" / "bench.sqlite")
    rid = make_round(db)
    fake_claude.respond(answer(["ko", "de"], [Q, "Yes", "No"]))
    cli.main(["--config", str(toml), "opai", "try", "translate", "--subject", f"round:{rid}", "--langs", "ko,de",
              "--config-dir", str(tmp_path / "cfg")])
    out = json.loads(capsys.readouterr().out)
    assert out["output"]["ko"][0] == KO and out["verdicts"]["ko"][Q] is None
    assert db.one("SELECT COUNT(*) n FROM market_i18n")["n"] == 0
    assert db.one("SELECT public FROM operator_ai_outputs")["public"] == 0
    assert fake_claude.calls[0]["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
