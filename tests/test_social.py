"""Polymarket-style social controls, milestone 1 (docs/polymarket-social-spec.md §3-§4): the ☆ bookmark on round
cards and round pages, the /saved/ page and header ★, all in the browser only; and the share icon beside a round's
title, whose links carry the round's fixed ?s=r<id> so X keeps a fresh card per round."""
import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agentpitbench import i18n, site
from test_grid import built  # noqa: F401  (fixture)


def test_round_cards_have_a_hidden_bookmark_toggle(built):  # noqa: F811
    _, out, ids = built
    for page in ("index.html", "c/live/index.html", "de/index.html", "ar/c/sports/index.html"):
        html = (out / page).read_text()
        shown = [int(i) for i in re.findall(r'<a class="mk[^"]*" href="[^"]*round/(\d+)/"', html)]
        assert shown, page
        for rid in shown:  # outside the card's link (no button inside <a>), hidden until the script shows it
            assert re.search(rf'<div class="mkw"><a class="mk[^"]*" href="[^"]*round/{rid}/".*?</a>\n'
                             rf'<button type="button" class="bm" data-bm="{rid}" aria-pressed="false" hidden>☆</button></div>',
                             html, re.S), (page, rid)


def test_header_saved_link_on_every_page(built):  # noqa: F811
    _, out, _ = built
    for page in ("index.html", "leaderboard/index.html", "fr/round/1/index.html", "ar/saved/index.html"):
        html = (out / page).read_text()
        hdr = html[html.index("<header>"):html.index("</header>")]
        assert re.search(r'<a class="svd" href="(?:\.\./)*(?:\w+/)?saved/" hidden data-saved-link', hdr), page
        assert '<span class="n" hidden></span>' in hdr  # the count badge, hidden at 0
    assert 'aria-current="page"' in (out / "saved/index.html").read_text().split("data-saved-link", 1)[1][:400]


def test_round_page_bookmark_and_share_icon(built):  # noqa: F811
    s, out, ids = built
    rid = ids["resolved"]
    html = (out / f"round/{rid}/index.html").read_text()
    head = html[html.index('<div class="rh">'):]
    head = head[:head.index("</details>")]
    assert f'<button type="button" class="bm" data-bm="{rid}" aria-pressed="false" hidden>☆</button>' in head
    menu = head[head.index("data-share-menu"):]
    copy = unescape(re.search(r'<a href="([^"]+)" data-mcopy>Copy link</a>', menu).group(1))
    assert copy.startswith(f"{s.site_url}/round/{rid}/?s=r{rid}&") and "utm_source=copy" in copy
    x = unescape(re.search(r'<a href="([^"]+)" target="_blank" rel="noopener" data-mx>Post on X</a>', menu).group(1))
    assert x.startswith("https://x.com/intent/tweet?")
    shared = parse_qs(urlparse(x).query)["url"][0]
    assert parse_qs(urlparse(shared).query)["s"] == [f"r{rid}"] and "utm_source=x" in shared
    assert '<button type="button" data-mnative hidden>Share…</button>' in menu  # only where navigator.share exists
    code = unescape(re.search(r'data-membed data-code="([^"]+)" hidden>Embed', menu).group(1))
    assert code.startswith(f'<iframe src="{s.site_url}/embed/round/{rid}/"')
    native = unescape(re.search(r'data-su="([^"]+)"', menu).group(1))
    assert f"/round/{rid}/?s=r{rid}&" in native and "utm_source=native" in native
    # the canonical and og:url stay clean; the share row and bar links carry ?s= too
    assert f'<link rel="canonical" href="{s.site_url}/round/{rid}/">' in html
    assert f'<meta property="og:url" content="{s.site_url}/round/{rid}/">' in html
    bar = html[html.index('id="share"'):]
    assert all(f"s%3Dr{rid}" in h or f"?s=r{rid}" in unescape(h)
               for h in re.findall(r'href="([^"]+)" data-net="\w+"', bar))
    # translated, with the round's own query in every language
    fr = (out / f"fr/round/{rid}/index.html").read_text()
    assert f"{s.site_url}/fr/round/{rid}/?s=r{rid}&amp;utm_source=copy" in fr


def test_share_icon_script_and_no_cookies():
    tpl = Path(site.__file__).resolve().parent / "templates" / "site"
    bar = (tpl / "_share_bar.html").read_text()
    assert 'each("[data-share-menu]"' in bar and "navigator.share" in bar
    base = (tpl / "base.html").read_text()
    assert "slice(0,200)" in base and "K+\"-seen\"" in base  # at most 200 bookmarks; the last look at /saved/
    for f in tpl.glob("*.html"):
        assert "document.cookie" not in f.read_text(), f.name


def test_saved_page_every_language_noindex_and_off_the_sitemap(built):  # noqa: F811
    s, out, _ = built
    for code in i18n.CODES:
        p = "" if code == "en" else f"{code}/"
        html = (out / p / "saved/index.html").read_text()
        assert '<meta name="robots" content="noindex">' in html, code
        assert "<noscript>" in html and "data-saved-empty hidden" in html, code
        assert re.search(r'data-saved-grid data-idx="(\.\./)+data/rounds-index\.json"', html), code
        assert "data-share-bar" not in html and "data-share-top" not in html, code  # nothing to share: it's yours
    en = (out / "saved/index.html").read_text()
    assert "Nothing saved yet. Tap ☆ on any round." in en and "Saved markets need JavaScript." in en
    assert 'localStorage.setItem(K,String(Math.floor(Date.now()/1000)))' in en  # the last look, for the dot
    for f in out.glob("sitemap*.xml"):
        assert "/saved/" not in f.read_text(), f.name
    # "Resolved {winner}" reaches the script escaped, with a placeholder the script fills with escaped text
    win = unescape(re.search(r'data-win="([^"]+)"', en).group(1))
    assert win == "Resolved <b>{w}</b>"


def test_rounds_index_carries_resolution_for_the_dot(built):  # noqa: F811
    _, out, ids = built
    idx = {r["id"]: r for r in json.loads((out / "data/rounds-index.json").read_text())}
    res = idx[ids["resolved"]]
    assert res["w"] == "Yes" and res["rt"] > 1_700_000_000
    assert "w" not in idx[ids["sealed"]] and "rt" not in idx[ids["sealed"]]  # nothing about an open round


def test_privacy_mentions_saved_markets(built):  # noqa: F811
    _, out, _ = built
    assert "the markets you save with ☆" in (out / "privacy/index.html").read_text()
