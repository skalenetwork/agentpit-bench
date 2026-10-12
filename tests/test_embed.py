"""Embeds (docs/embed-spec.md): per-round and leaderboard iframes in every language, sealed picks never shown,
themes/sizes, auto-height loader, oEmbed files and discovery links, the share bar's Embed button, the docs page."""
import json
import re
from html import unescape

from agentpitbench import exports, i18n, site

from test_publish import PICKS, env, make_round  # noqa: F401  (env is a fixture)

SEALED = {"claude": ("Nexus", .4, .6), "codex": ("CYBERSHOKE", .63, .8), "agy": ("Nexus", .41, .7),
          "grok": ("CYBERSHOKE", .6, .9)}


def _built(env):
    s, db = env
    make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")                      # round 1
    make_round(db, SEALED, state="running", market_id="m2", question="Will the sealed thing happen?")  # round 2
    exports.export(s, db)
    return s, site.build(s)


def test_embeds_exist_in_every_language_small_and_private(env):
    s, out = _built(env)
    for code in i18n.CODES:
        prefix = "" if code == "en" else f"{code}/"
        for p in ("embed/round/1/index.html", "embed/round/2/index.html", "embed/leaderboard/index.html"):
            html = (out / prefix / p).read_text()
            assert len(html.encode()) < 30_000, (code, p, len(html.encode()))
            assert f'<html lang="{code}"' in html and ('dir="rtl"' in html) == (code in i18n.RTL), (code, p)
            assert "cloudflareinsights" not in html and "document.cookie" not in html and "localStorage" not in html
            assert 'name="robots" content="noindex"' in html and "data-share-bar" not in html
            assert "agentpitbench:height" in html and 'data-theme' in html and '"compact"' in html
            # credibility line: methodology version, n, a link back with utm_source=embed
            assert s.methodology_version in html and "utm_source=embed" in html and "utm_medium=iframe" in html
            assert 'type="application/json+oembed"' in html


def test_sealed_round_embed_never_shows_a_pick(env):
    s, out = _built(env)
    for code in ("en", "ar", "de"):
        prefix = "" if code == "en" else f"{code}/"
        html = (out / prefix / "embed/round/2/index.html").read_text()
        assert "data-sealed" in html and "🔒" in html
        assert "Will the sealed thing happen?" in html
        # no agent's outcome, price or share count: the only outcome strings are the market's own list
        rows = re.findall(r'<tr data-outcome="([^"]*)" data-shares="([^"]*)" data-stake="([^"]*)"', html)
        assert len(rows) == 4 and all(r == ("", "0", "0") for r in rows), rows
        assert "Nexus" not in html.split("data-outcomes=")[1].split("</table>")[0].split("'", 2)[2]
        assert "data-poll" in html and "api/v1/rounds/2.json" in html  # flips to the picks after the reveal
    en = (out / "embed/round/2/index.html").read_text()
    assert "Picks sealed" in en
    resolved = (out / "embed/round/1/index.html").read_text()
    assert "data-sealed" not in resolved and "CYBERSHOKE" in resolved and 'data-poll=""' in resolved
    assert "Resolved <b>" in resolved


def test_oembed_files_and_discovery(env):
    s, out = _built(env)
    for rel, kind in (("oembed/round/2.json", "round"), ("oembed/leaderboard.json", "leaderboard"),
                      ("fr/oembed/round/1.json", "round")):
        o = json.loads((out / rel).read_text())
        assert o["version"] == "1.0" and o["type"] == "rich" and o["provider_name"] == "AgentpitBench"
        assert o["width"] == site.EMBED_W and o["height"] == site.EMBED_H[kind]
        assert "<iframe" in o["html"] and f'{s.site_url}/embed.js' in o["html"]
    assert 'src="https://agentpitbench.org/fr/embed/round/1/"' in json.loads((out / "fr/oembed/round/1.json").read_text())["html"]
    for p in ("round/1/index.html", "leaderboard/index.html", "ar/round/2/index.html"):
        html = (out / p).read_text()
        assert html.count('type="application/json+oembed"') == 1, p
        assert 'data-embed>' in html and "data-embed-code" in html, p   # the share bar's Embed button
    for p in ("index.html", "methodology/index.html", "agent/claude/index.html"):
        html = (out / p).read_text()
        assert "json+oembed" not in html and "<textarea" not in html, p
    code = unescape(re.search(r"<textarea[^>]*data-embed-code[^>]*>(.*?)</textarea>",
                              (out / "round/2/index.html").read_text(), re.S).group(1))
    assert code.startswith('<iframe src="https://agentpitbench.org/embed/round/2/"') and "embed.js" in code


def test_loader_and_docs_page(env):
    s, out = _built(env)
    js = (out / "embed.js").read_text()
    assert "agentpitbench:height" in js and "e.origin !== origin" in js and "cookie" not in js.replace("No cookies", "")
    docs = (out / "embed/index.html").read_text()
    assert "<iframe" in docs and "embed/round/2/" in docs and "embed/leaderboard/" in docs  # the open round first
    assert 'location.replace("leaderboard/"' in docs  # an old /embed/ iframe keeps showing the leaderboard
    assert "data-share-bar" in docs and (out / "ja/embed/index.html").exists()
    assert "embed/leaderboard/" in (out / "press/index.html").read_text()
