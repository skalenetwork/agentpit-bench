"""Dark / light switch (docs/theme-switch-spec.md): the reader's choice is applied before the first paint, every
dark rule of the site can be forced either way, the header button is translated, and nothing leaks into URLs
or the embeds."""
import re

import pytest

from agentpitbench import exports, site
from agentpitbench.config import Settings
from agentpitbench.db import DB

RTL = "ar"  # one right-to-left language page


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    s = Settings(data_dir=tmp_path_factory.mktemp("theme") / "var")
    exports.export(s, DB(s.db_path))
    return site.build(s)


def pages(out):
    return {"en": (out / "index.html").read_text(), RTL: (out / RTL / "index.html").read_text(),
            "404": (out / "404.html").read_text()}


def _media_blocks(css: str) -> list[str]:
    """The bodies of every @media (prefers-color-scheme:dark){...} block (brace-matched)."""
    found = []
    for m in re.finditer(r"@media \(prefers-color-scheme:dark\)\{", css):
        i, depth = m.end(), 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        found.append(css[m.end():i - 1])
    return found


def _rules(body: str) -> list[tuple[str, str]]:
    return [(sel.strip(), decl.strip()) for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body)]


def test_theme_script_runs_before_the_styles(out):
    for name, html in pages(out).items():
        assert html.index('localStorage.getItem("apb-theme")') < html.index("<style>"), name


def test_every_dark_rule_can_be_forced(out):
    for name, html in pages(out).items():
        css = "".join(re.findall(r"<style>(.*?)</style>", html, re.S))
        blocks = _media_blocks(css)
        assert blocks, name
        for body in blocks:
            for sel, decl in _rules(body):
                parts = [p.strip() for p in sel.split(",")]
                assert all(p.startswith(":root:not([data-theme=light])") for p in parts), (name, sel)
                forced = ",".join(p.replace(":root:not([data-theme=light])", ":root[data-theme=dark]", 1) for p in parts)
                assert any(f.replace(" ", "") == forced.replace(" ", "") and d == decl for f, d in _rules(css)), (name, forced)


def test_header_button_is_present_and_translated(out):
    btn = re.compile(r'<button type="button" class="thm" hidden data-auto="([^"]+)" data-light="([^"]+)" '
                     r'data-dark="([^"]+)" aria-label="([^"]+)"')
    en = btn.findall((out / "index.html").read_text())
    de = btn.findall((out / "de" / "index.html").read_text())
    assert len(en) == 1 and len(de) == 1
    assert en[0] == ("Theme: Auto", "Theme: Light", "Theme: Dark", "Theme: Auto")
    assert de[0][:3] != en[0][:3] and de[0][3] == de[0][0]
    assert 'class="thm"' not in (out / "404.html").read_text()  # 404 gets the theme, not the button


def test_theme_never_reaches_urls_or_embeds(out):
    assert "theme" not in (out / "sitemap.xml").read_text()
    html = (out / "index.html").read_text()
    assert not re.search(r'(?:canonical|hreflang="[^"]+") href="[^"]*theme', html)
    embeds = [p for p in out.glob("embed/*/**/*.html") if "<header>" not in p.read_text()]  # /embed/ itself is docs
    assert embeds
    for embed in embeds:
        text = embed.read_text()
        assert 'class="thm"' not in text and "apb-theme" not in text and "data-theme" in text, embed
