"""Share everywhere, part A: the share bar on every page, link previews (og/twitter meta, 1200x630 images
rendered only when their input changes), per-network share text, UTM attribution, the build's acceptance check."""
import io
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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


# ---------- part B: Share image, Share your pick, Share my win ----------

FUTURE = "2099-01-01T00:00:00Z"
OUTCOMES = ["CYBERSHOKE", "Nexus"]


def _open_round(env, thread: str | None = "1900000000000000001", state="awaiting_resolution", end=FUTURE):
    s, db = env
    rid = make_round(db, PICKS, state=state, market_id="m9")
    db.x("UPDATE rounds SET end_date=?, thread_tweet_id=? WHERE round_id=?", end, thread, rid)
    if state == "running":  # agents still deciding: their decision cards exist but are never published
        (s.cards_dir / str(rid)).mkdir(parents=True)
        Image.new("RGB", (1200, 675), (40, 60, 200)).save(s.cards_dir / f"{rid}/decision-claude.png")
    exports.export(s, db)
    return s, rid, site.build(s)


def _pick_links(html: str) -> dict[str, list[tuple[str, str]]]:
    """{outcome: [(net, href)]} from the Share your pick section."""
    sec = re.search(r'<section class="pickshare".*?</section>', html, re.S).group(0)
    out = {}
    for o, body in re.findall(r'<summary class="btn"><span dir="auto">([^<]+)</span></summary>(.*?)</details>', sec, re.S):
        out[unescape(o)] = [(n, unescape(h)) for h, n in re.findall(r'href="([^"]+)" data-net="(\w+)"', body)]
    return out


def test_pick_reply_is_collected_in_every_language(env):
    """The X link replies to the round's opener with the outcome label first, so collect_humans' exact-match
    parser reads back exactly that pick, whatever the page language."""
    from agentpitbench.virality import parse_pick
    s, rid, out = _open_round(env)
    for code in i18n.CODES:
        prefix = "" if code == "en" else f"{code}/"
        html = (out / prefix / f"round/{rid}/index.html").read_text()
        assert len(html.encode()) < 100_000, code
        links = _pick_links(html)
        assert list(links) == OUTCOMES, code
        for o, nets in links.items():
            assert sorted(n for n, _h in nets) == sorted(site.PICK_NETS), code
            x = dict(nets)["x"]
            q = parse_qs(urlparse(x).query)
            assert x.startswith("https://x.com/intent/tweet?") and q["in_reply_to"] == ["1900000000000000001"]
            assert "url" not in q, code
            text = q["text"][0]
            assert text.startswith(o + ": ") and "⁨" not in text and "#AgentpitBench" in text
            assert len(text) <= 280
            assert parse_pick("@AgentpitBench " + text, OUTCOMES) == o, (code, text)
            for net, href in nets:  # other networks: a normal post with the round URL, attributed
                if net != "x":
                    assert "utm_content=share-pick" in unquote(href), (code, net)
    en = (out / f"round/{rid}/index.html").read_text()
    assert "Share your pick" in en and "Reply on X" in en and "picks counted" not in en
    assert "Choisissez une issue" in (out / f"fr/round/{rid}/index.html").read_text()
    assert 'dir="rtl"' in (out / f"ar/round/{rid}/index.html").read_text()


@pytest.mark.parametrize("thread", [None, "dry-r1-reveal"])  # no opener yet, or one posted to the outbox only
def test_pick_without_opener_posts_normally(env, thread):
    s, rid, out = _open_round(env, thread=thread)
    html = (out / f"round/{rid}/index.html").read_text()
    x = parse_qs(urlparse(dict(_pick_links(html)["Nexus"])["x"]).query)
    assert "in_reply_to" not in x and "utm_source=x" in x["url"][0]
    assert x["text"][0].startswith("Nexus: my pick.")
    assert "come back then to reply" in html and "Reply on X" not in html


def test_pick_never_reveals_sealed_picks(env):
    """While the agents are still deciding the visitor can share a pick, and nothing leaks theirs."""
    s, rid, out = _open_round(env, thread=None, state="running")
    for code in ("en", "ar"):
        prefix = "" if code == "en" else f"{code}/"
        html = (out / prefix / f"round/{rid}/index.html").read_text()
        links = _pick_links(html)
        assert set(links) == set(OUTCOMES)
        assert "decision-claude" not in html and "data-share-image data-text" not in html  # no unpublished card
        assert "cards/" not in _meta(html, "og:image")
        for o, nets in links.items():
            other = OUTCOMES[1 - OUTCOMES.index(o)]
            for _n, href in nets:
                text = unquote(href)
                assert other not in text.split(o, 1)[0] and "Grok" not in text  # own pick first, no agent's
    assert not (out / "cards" / str(rid)).exists()


def test_pick_hidden_after_close(env):
    s, rid, out = _open_round(env, end="2020-01-01T00:00:00Z")
    assert 'class="pickshare"' not in (out / f"round/{rid}/index.html").read_text()  # replies after the close don't count


def test_pick_cap_note(env, monkeypatch):
    from agentpitbench import xstats
    monkeypatch.setattr(xstats, "reply_cap_hit", lambda db, rid, cap=None: True)
    s, rid, out = _open_round(env)
    assert "First 100 picks counted." in (out / f"round/{rid}/index.html").read_text()
    assert "100" in (out / f"ja/round/{rid}/index.html").read_text().split('class="pickshare"', 1)[1]


def test_reply_cap_on_a_fresh_db(env):
    s, rid, out = _open_round(env)
    assert site._reply_caps(s, [{"round_id": rid, "thread_tweet_id": "1900000000000000001"}]) == {}


def test_share_image_on_round(env):
    s, out = _built(env, card=True)
    html = (out / "round/1/index.html").read_text()
    a = re.search(r'<a class="btn" href="([^"]+)" download="([^"]+)" data-share-image data-text="([^"]*)" '
                  r'data-url="([^"]+)"', html)
    assert a and a.group(1) == "../../cards/1/results.png" and a.group(2) == "agentpitbench-round-1.png"
    assert "utm_medium=share-image" in unescape(a.group(4)) and "Save image" in html  # works with JavaScript off
    assert "navigator.canShare" in html and (out / "cards/1/results.png").exists()
    ar = (out / "ar/round/1/index.html").read_text()
    assert "حفظ الصورة" in ar and 'href="../../../cards/1/results.png"' in ar
    # no browser here, so no standings card: the leaderboard simply has no Share image
    assert "data-share-image data-text" not in (out / "leaderboard/index.html").read_text()


def test_share_image_link():
    img = site.share_image("cards/agents/claude.png", "agent-claude", "Claude on AgentpitBench",
                           "https://x.org/agent/claude/", "agent")
    assert img["src"] == "cards/agents/claude.png" and img["name"] == "agentpitbench-agent-claude.png"
    assert "utm_medium=share-image" in img["url"] and "utm_content=share-agent" in img["url"]
    assert site.share_image(None, "x", "t", "u", "agent") is None


def test_share_my_win(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")
    card = s.cards_dir / "humans" / str(rid) / "ann.png"
    card.parent.mkdir(parents=True)
    Image.new("RGB", (1200, 675)).save(card)
    db.x("INSERT INTO humans(round_id,user_id,username,pick,tweet_id,created_at,won,card_path) VALUES(?,?,?,?,?,?,?,?)",
         rid, "u1", "ann", "Nexus", "t1", 1.0, 1, str(card))
    exports.export(s, db)
    html = (site.build(s) / f"round/{rid}/index.html").read_text()
    assert "Share my win" in html and "agentpitbench-win-ann.png" in html and 'class="pickshare"' not in html
    x = unescape(re.search(r'href="(https://x.com/intent/tweet\?[^"]+)" target="_blank" rel="noopener">X<', html).group(1))
    text = parse_qs(urlparse(x).query)["text"][0]
    assert text.startswith("I beat ") and "Claude" in text and "Grok" not in text  # Grok picked Nexus and won
