"""Site translations: every message id has a translation in all 19 non-English tables, placeholders intact,
every language builds, and the English pages carry the browser-language redirect."""
import json
import re
import string
from pathlib import Path

import pytest

from agentpitbench import exports, i18n, site
from agentpitbench.config import Settings
from agentpitbench.db import DB

PKG = Path(__file__).resolve().parent.parent / "agentpitbench"
CALL = re.compile(r'''\b_h?\(\s*"((?:[^"\\]|\\.)*)"''')


def message_ids() -> set[str]:
    ids = set()
    for f in [*(PKG / "templates/site").glob("*.html"), PKG / "site.py", PKG / "cards.py"]:
        ids |= {m.encode().decode("unicode_escape").encode("latin-1").decode("utf-8")
                for m in CALL.findall(f.read_text())}
    ids |= {name for name, _ in site.CATEGORIES} | {"Other"}
    return ids


def fields(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def test_languages():
    assert len(i18n.LANGS) == 20 and i18n.CODES[0] == "en" and len(set(i18n.CODES)) == 20


@pytest.mark.parametrize("lang", i18n.CODES[1:])
def test_table_complete(lang):
    ids = message_ids()
    table = json.loads((PKG / "i18n" / f"{lang}.json").read_text("utf-8"))
    missing = sorted(ids - table.keys())
    assert not missing, f"{lang} missing {missing}"
    assert not sorted(table.keys() - ids), f"{lang} has stale ids"
    for k, v in table.items():
        assert v.strip(), (lang, k)
        assert fields(v) == fields(k), (lang, k, v)
        assert v.count("<b>") == k.count("<b>"), (lang, k, v)


def test_translator_falls_back():
    t = i18n.Translator("xx")
    assert t.lang == "en" and t("Round {n}", n=3) == "Round 3"
    fr = i18n.Translator("fr")
    assert fr("no such message {x}", x=1) == "no such message 1"
    assert "&lt;i&gt;" in fr.html("<b>{name}</b> was {pct}% sure of <b>{outcome}</b> at {price}. It resolved <b>{winner}</b>.",
                                  name="<i>", pct=1, outcome="a", price="b", winner="c")


def test_site_in_every_language(tmp_path):
    s = Settings(data_dir=tmp_path / "var")
    exports.export(s, DB(s.db_path))
    out = site.build(s)
    for code in i18n.CODES:
        prefix = "" if code == "en" else f"{code}/"
        for p in ("index.html", "leaderboard/index.html", "data/index.html", "agent/grok/index.html"):
            html = (out / prefix / p).read_text()
            assert f'<html lang="{code}"' in html, (code, p)
            assert ('dir="rtl"' in html) == (code in i18n.RTL)
            assert f'hreflang="{code}"' in html and 'hreflang="x-default"' in html
    en = (out / "leaderboard/index.html").read_text()
    assert "navigator.languages" in en and 'href="../fr/leaderboard/"' in en
    fr = (out / "fr/leaderboard/index.html").read_text()
    assert "navigator.languages" not in fr and "Classement" in fr
    assert 'href="../../leaderboard/"' in fr and 'href="../../feed.xml"' in fr  # English page; shared feed
    assert not (out / "fr/widget").exists() and (out / "widget/index.html").exists()
