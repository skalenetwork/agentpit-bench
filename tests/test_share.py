"""Share everywhere, part A: the share bar on every page, link previews (og/twitter meta, 1200x630 images
rendered only when their input changes), per-network share text, UTM attribution, the build's acceptance check."""
import io
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from agentpitbench import exports, i18n, og, site
from agentpitbench.i18n import Translator

from test_publish import PICKS, env, make_round  # noqa: F401  (env is a fixture)

Image = pytest.importorskip("PIL.Image")  # the optional "media" extra; without it previews are letterboxed by Chromium

PUBLIC = ["index.html", "leaderboard/index.html", "round/1/index.html", "agent/claude/index.html",
          "splits/index.html", "hall-of-shame/index.html", "data/index.html", "press/index.html",
          "methodology/index.html", "independence/index.html", "incidents/index.html",
          "contrarian/index.html", "reports/index.html"]
NETS = ["x", "linkedin", "facebook", "reddit", "whatsapp", "telegram", "bluesky", "email"]


def _built(env, card: bool = False):
    s, db = env
    make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")
    if card:  # a round's share card, as the publisher leaves it
        (s.cards_dir / "1").mkdir(parents=True)
        Image.new("RGB", (1200, 675), (40, 60, 200)).save(s.cards_dir / "1/results.png")
    make_round(db, {"claude": ("Nexus", .4, .6), "grok": ("CYBERSHOKE", .6, .9)}, market_id="m2")
    exports.export(s, db)
    return s, site.build(s)


def _meta(html: str, prop: str) -> str | None:
    m = re.search(rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)"', html)
    return unescape(m.group(1)) if m else None


def test_every_page_previews_and_shares(env):
    s, out = _built(env)
    for code in ("en", "fr", "ar", "ur", "ja"):
        prefix = "" if code == "en" else f"{code}/"
        for p in PUBLIC:
            html = (out / prefix / p).read_text()
            assert len(html.encode()) < 100_000, (code, p)
            for prop in ("og:title", "og:description", "og:image", "og:url", "og:locale", "og:site_name",
                         "twitter:title", "twitter:description", "twitter:image", "twitter:site"):
                assert _meta(html, prop), (code, p, prop)
            assert _meta(html, "og:image:height") == "630" and _meta(html, "og:image:width") == "1200"
            assert _meta(html, "twitter:card") == "summary_large_image"
            assert "?utm" not in _meta(html, "og:url")  # the canonical stays clean; only shared links carry UTM
            assert html.count("data-share-bar") == 1, (code, p)  # exactly one bar: the old buttons are folded in
            assert 'data-net="x"' in html and 'data-net="bluesky"' in html and "data-copy" in html
            assert "Share on X" not in html
    # the default preview is copied at build; every page without its own image points at it
    assert (out / og.DEFAULT).exists()
    assert _meta((out / "index.html").read_text(), "og:image").startswith(f"{s.site_url}/og-default.png?v=")
    # translated, right to left, localised preview
    fr = (out / "fr/leaderboard/index.html").read_text()
    assert "Partager cette page" in fr and _meta(fr, "og:locale") == "fr_FR"
    assert 'property="og:locale:alternate" content="en_US"' in fr
    ar = (out / "ar/methodology/index.html").read_text()
    assert 'dir="rtl"' in ar and "شارك هذه الصفحة" in ar
    # widget and embed are embedded elsewhere: no bar
    assert "data-share-bar" not in (out / "widget/index.html").read_text()


def test_share_links_utm_and_lengths(env):
    s, out = _built(env)
    html = (out / "round/2/index.html").read_text()
    links = {net: href for href, net in re.findall(r'href="([^"]+)" data-net="(\w+)"', html)}
    assert list(links) == NETS
    for net, href in links.items():
        q = parse_qs(urlparse(unescape(href)).query)
        shared = (q.get("url") or q.get("u") or [None])[0] or re.search(r"https://\S+", (q.get("text") or q["body"])[0]).group(0)
        u = parse_qs(urlparse(shared).query)
        assert u == {"utm_source": [net], "utm_medium": ["share"], "utm_campaign": ["site"],
                     "utm_content": ["share-round"]}, net
    copy = re.search(r'href="([^"]+)" data-copy', html).group(1)
    assert "utm_source=copy" in unescape(copy) and "utm_content=share-round" in unescape(copy)
    assert "utm_source=native" in re.search(r'data-url="([^"]+)"', html).group(1)


def test_share_text_clipped_per_network():
    _ = Translator("en")
    long_q = "Counter-Strike: fnatic vs K27 (BO3) - " + "very long tournament name " * 12 + "winner?"
    r = {"state": "awaiting_resolution", "question": long_q, "sealed": False,
         "entries": [{"name": n, "outcome": "fnatic" if i % 2 else "K27"} for i, n in enumerate(["Claude", "ChatGPT", "Gemini", "Grok"])]}
    t = site.round_share(r, _, "Claude vs ChatGPT vs Gemini vs Grok")
    assert t.full.startswith("Counter-Strike") and t.full.endswith("Who's right?")
    url = "https://agentpitbench.org/round/7/"
    bar = site.share_bar(t, "Round 7", url, "round", _)
    q = {net: parse_qs(urlparse(h).query) for net, _l, h in bar["links"]}
    x = q["x"]["text"][0]
    assert len(x) + 1 + 23 <= 280 and "Claude K27, ChatGPT fnatic, Gemini K27, Grok fnatic. Who's right?" in x
    assert x.startswith("Counter-Strike") and "…" in x  # the question is clipped first, the picks survive
    bsky = q["bluesky"]["text"][0]
    assert len(bsky) <= 300 and bsky.endswith("utm_content=share-round")
    assert len(q["telegram"]["text"][0]) <= 200 and len(q["reddit"]["title"][0]) <= 300
    wa = q["whatsapp"]["text"][0]
    assert len(wa.rsplit(" ", 1)[0]) <= 200 and "Who's right?" in wa
    # sealed: picks are secret until every AI is in
    sealed = site.round_share({**r, "state": "running", "sealed": True}, _, "Claude vs Grok")
    assert "placing their bets" in sealed.full and "fnatic" not in sealed.tail
    # resolved: the results tweet's own hook, so the two never drift
    from agentpitbench.twitter import results_hook
    done = {**r, "state": "resolved", "winner": "K27", "entries": [{**e, "won": e["outcome"] == "K27"} for e in r["entries"]]}
    assert site.round_share(done, _, "x").full == results_hook(done)
    # right to left: the clipped flex keeps its isolates around it
    ar = site.round_share(r, Translator("ar"), "x")
    assert "⁨" in ar.head and "⁩" in ar.tail


def test_check_pages_fails_without_preview(tmp_path):
    (tmp_path / "ok").mkdir()
    good = ('<meta property="og:title" content="t"><meta property="og:description" content="d">'
            '<meta property="og:image" content="i"><section data-share-bar></section>')
    (tmp_path / "ok/index.html").write_text(good)
    (tmp_path / "widget").mkdir()
    (tmp_path / "widget/index.html").write_text("<html></html>")  # exempt
    site.check_pages(tmp_path)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad/index.html").write_text(good.replace("og:description", "x"))
    with pytest.raises(RuntimeError, match="bad/index.html"):
        site.check_pages(tmp_path)


def test_letterbox_keeps_the_whole_card():
    card = Image.new("RGB", (1200, 675), (255, 0, 0))
    buf = io.BytesIO()
    card.save(buf, "PNG")
    out = Image.open(io.BytesIO(og.letterbox_bytes(buf.getvalue())))
    assert out.size == (1200, 630)
    assert out.getpixel((10, 315)) == og.BG and out.getpixel((1190, 315)) == og.BG  # side bars
    assert out.getpixel((45, 2)) == (255, 0, 0) and out.getpixel((1155, 627)) == (255, 0, 0)  # never cropped


async def test_previews_render_once_per_input(env, monkeypatch):
    """Real Chromium: the standings, agent and branded cards come out at 1200x630 and are only re-rendered
    when their input changes; the ?v= key follows the input."""
    monkeypatch.setattr(og, "ENABLED", True)
    monkeypatch.setattr(site, "BRANDED", {"methodology": "Methodology"})
    monkeypatch.setattr(site, "CODES", ["en", "ar"])
    s, out = _built(env, card=True)
    cards = s.cards_dir
    for p in ("standings.png", "agents/claude.png"):
        assert Image.open(cards / p).size == (1200, 675), p
    for p in ("standings-og.png", "agents/claude-og.png", "pages/methodology-en-og.png", "pages/methodology-ar-og.png",
              "1/results-og.png"):
        assert Image.open(cards / p).size == (1200, 630), p
    lb = (out / "leaderboard/index.html").read_text()
    img = _meta(lb, "og:image")
    assert re.fullmatch(rf"{s.site_url}/cards/standings-og\.png\?v=[0-9a-f]{{12}}", img)
    assert (out / "cards/standings-og.png").exists()
    assert "pages/methodology-ar-og.png" in _meta((out / "ar/methodology/index.html").read_text(), "og:image")
    assert "1/results-og.png" in _meta((out / "round/1/index.html").read_text(), "og:image")
    # unchanged input: nothing is rendered again
    mtime = (cards / "standings-og.png").stat().st_mtime_ns
    calls = []
    monkeypatch.setattr(og.Previews, "_render", lambda self, jobs: calls.append(jobs))
    site.build(s)
    assert not calls and (cards / "standings-og.png").stat().st_mtime_ns == mtime
    assert _meta((s.site_dir / "leaderboard/index.html").read_text(), "og:image") == img
