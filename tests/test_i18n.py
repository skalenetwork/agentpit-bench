"""Site translations: every message id has a translation in all 39 non-English tables, placeholders intact,
every language builds, and the English pages carry the browser-language redirect."""
import json
import re
import shutil
import string
from pathlib import Path

import pytest

from agentpitbench import exports, i18n, og, site
from agentpitbench.config import Settings
from agentpitbench.db import DB

PKG = Path(__file__).resolve().parent.parent / "agentpitbench"
CALL = re.compile(i18n._CALL)
message_ids = i18n.message_ids  # moved into i18n.py (also reads methodology.json and incidents.json)


def fields(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def test_languages():
    assert len(i18n.LANGS) == 40 and i18n.CODES[0] == "en" and len(set(i18n.CODES)) == 40


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
    assert 'fil:"tl"' in en  # browsers say fil (Filipino) for the Tagalog pages
    fr = (out / "fr/leaderboard/index.html").read_text()
    assert "navigator.languages" not in fr and "Classement" in fr
    assert 'href="../../leaderboard/"' in fr and 'href="../../feed.xml"' in fr  # English page; shared feed
    assert not (out / "fr/widget").exists() and (out / "widget/index.html").exists()


@pytest.mark.skipif(not shutil.which("fc-list"), reason="no fontconfig to ask")
def test_fonts_for_every_language():
    """Every language's branded preview titles and native name can be drawn by an installed font."""
    for code in i18n.CODES:
        tr = i18n.Translator(code)
        texts = [tr(m) for m in site.BRANDED.values()] + [i18n.NAMES[code]]
        assert not og.missing_glyphs(texts), code


def test_preview_without_font_keeps_default(tmp_path, monkeypatch):
    """A branded card whose title no installed font can draw is not rendered (no empty boxes): the page keeps the
    default image, and an older image of it is removed."""
    monkeypatch.setattr(og, "ENABLED", True)
    monkeypatch.setattr(og, "_covered", {})
    monkeypatch.setattr(og, "_fc_has", lambda cps: all(c < 0x1000 for c in cps))  # no Myanmar font here
    monkeypatch.setattr(og.shutil, "which", lambda name: "/usr/bin/" + name)
    rendered = []

    async def fake_render(self, jobs):
        rendered.extend(j.out.name for j in jobs)
    monkeypatch.setattr(og.Previews, "_render", fake_render)
    pv = og.Previews(Settings(data_dir=tmp_path / "var"))
    old = pv.s.cards_dir / "pages" / "methodology-my-og.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"stale")
    my = pv.page("methodology", "နည်းစနစ်", "my", False)
    pv.page("methodology", "Méthodologie", "fr", False)
    pv.run()
    assert rendered == ["methodology-fr-og.jpg"]
    assert pv.url(my) == pv.default_url and not old.exists()
    assert og.missing_glyphs(["Méthodologie · 95%"]) == set() and og.missing_glyphs(["နည်း"]) == {"န", "ည", "်", "း"}
