"""Search engine optimisation (docs/seo-spec.md): titles and descriptions, the uniqueness check, head-to-head pages
with the n >= 20 rule, the 404 page, breadcrumbs, verification tags and IndexNow (no network)."""
import json
import re
from html import unescape

import pytest

from agentpitbench import exports, i18n, site
from agentpitbench.config import Settings
from agentpitbench.db import DB
from test_discover import AGENTS, LD, add_sweep
from test_publish import PICKS, make_round

TITLE = re.compile(r"<title>(.*?)</title>", re.S)
DESC = re.compile(r'<meta name="description" content="([^"]*)"')


def head(out, path):
    html = (out / path).read_text()
    return unescape(TITLE.search(html).group(1)), unescape(DESC.search(html).group(1))


@pytest.fixture
def built(tmp_path):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, google_site_verification="g-token-123",
                 bing_site_verification="BING0123")
    db = DB(s.db_path)
    make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")
    make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m2")  # same question as round 1
    running = make_round(db, {a: ("Nexus", .4, .77) for a in AGENTS}, state="running", market_id="m3",
                         question="Will Team Alpha win the grand final?")
    for d in ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"):
        add_sweep(db, d, scored=True)  # 24 resolved markets forecast by every agent
    # Grok misses two sweep days: 12 shared markets, below the n >= 20 bar for its pairs
    db.x("DELETE FROM forecasts WHERE agent='grok' AND sweep_date IN ('2026-10-06','2026-10-07')")
    exports.export(s, db)
    return s, db, site.build(s), running


def test_titles_and_descriptions(built):
    s, _, out, running = built
    t, d = head(out, "leaderboard/index.html")
    assert t.startswith("AI forecasting leaderboard: Claude, ChatGPT, Gemini, Grok")
    assert re.search(r"Claude leads on forecast accuracy after 24 markets \(Brier [\d.]+, 95% CI [\d.]+–[\d.]+\)\. "
                     r"Updated \d{4}-\d{2}-\d{2}\.", d)
    t, _ = head(out, "agent/codex/index.html")
    assert t == "ChatGPT (GPT-6 Astra) forecasting record · AgentpitBench"
    t, d = head(out, "index.html")
    assert t == "AgentpitBench: Claude vs ChatGPT vs Gemini vs Grok on real prediction markets"
    t, d = head(out, "round/1/index.html")
    assert t.endswith(": which AI got it right? · round 1") and "Counter-Strike" in t
    assert d.startswith("Resolved Nexus. Right: Grok. Wrong: Claude, ChatGPT, Gemini.") and d.endswith(" · round 1")
    assert head(out, "round/2/index.html")[0].endswith("· round 2")  # same question: titles stay distinct
    t, d = head(out, f"round/{running}/index.html")
    assert t.startswith("Will Team Alpha win the grand final?: AI predictions from Claude")
    assert "Nexus" not in t + d and "placing their bets" in d  # the sealed picks never reach a snippet
    fr_t, fr_d = head(out, "fr/leaderboard/index.html")
    assert fr_t.startswith("Classement des prévisions d'IA") and "après 24 marchés" in fr_d
    html = (out / f"round/{running}/index.html").read_text()
    assert 'alt="Share card' not in html
    # every fixed page type has its own title and description in every language (else the build would have failed)
    for code in i18n.CODES:
        p = "" if code == "en" else f"{code}/"
        heads = [head(out, p + f) for f in ("index.html", "leaderboard/index.html", "data/index.html",
                                             "press/index.html", "methodology/index.html", "independence/index.html")]
        assert len({h[0] for h in heads}) == len(heads) and len({h[1] for h in heads}) == len(heads), code


def test_seo_check_fails_only_on_fixed_pages():
    site.check_seo([("en", "round/1/", "Q", "D"), ("en", "round/2/", "Q", "D"), ("en", "", "Home", "a")])
    with pytest.raises(RuntimeError, match="duplicate description"):
        site.check_seo([("en", "press/", "Press", "same"), ("en", "data/", "Data", "same")])
    with pytest.raises(RuntimeError, match="empty title"):
        site.check_seo([("de", "facts/", "", "x")])


def test_head_to_head_pages(built):
    s, _, out, _ = built
    vs = sorted(p.name for p in (out / "vs").iterdir())
    assert "claude-vs-chatgpt" in vs and "chatgpt-vs-gemini" in vs and "claude-vs-crowd" in vs
    assert not [v for v in vs if "grok" in v]  # n = 12 < 20: no thin pages
    t, d = head(out, "vs/claude-vs-chatgpt/index.html")
    assert t == "Claude vs ChatGPT: who forecasts better? · AgentpitBench"
    assert d.startswith("Claude vs ChatGPT on the same 24 markets: Brier ")
    html = (out / "vs/claude-vs-chatgpt/index.html").read_text()
    assert "<h1>" in html and "Claude vs ChatGPT: who forecasts better?</h1>" in html
    assert "95% CI" in html and "24 markets both forecast" in html and "data as of" in html
    assert f'<link rel="canonical" href="{s.site_url}/vs/claude-vs-chatgpt/">' in html
    graph = json.loads(LD.search(html).group(1))["@graph"]
    assert {"Dataset", "BreadcrumbList", "Organization"} <= {n["@type"] for n in graph}
    crumbs = next(n for n in graph if n["@type"] == "BreadcrumbList")["itemListElement"]
    assert [c["name"] for c in crumbs] == ["Home", "Leaderboard", "Claude vs ChatGPT: who forecasts better?"]
    assert (out / "de/vs/claude-vs-chatgpt/index.html").exists()
    assert 'href="../../vs/claude-vs-chatgpt/"' in (out / "agent/claude/index.html").read_text()
    assert 'href="../vs/claude-vs-chatgpt/"' in (out / "leaderboard/index.html").read_text()
    sitemap = (out / "sitemap-en.xml").read_text()
    assert f"{s.site_url}/vs/claude-vs-chatgpt/" in sitemap and "/vs/claude-vs-grok/" not in sitemap
    pairs = json.loads((s.export_dir / "leaderboard.json").read_text())["pairs"]
    cc = next(p for p in pairs if (p["a"], p["b"]) == ("claude", "codex"))
    assert cc["n"] == 24 and cc["a_brier"] < cc["b_brier"] and cc["ci"][0] <= cc["mean_diff"] <= cc["ci"][1]
    assert cc["verdict"] == "a better"  # Claude is sharp in the fixture: the CI excludes zero


def test_breadcrumbs_point_at_real_pages(built):
    s, _, out, _ = built
    for path in ("round/1/index.html", "agent/codex/index.html", "fr/round/1/index.html"):
        graph = json.loads(LD.search((out / path).read_text()).group(1))["@graph"]
        crumbs = next(n for n in graph if n["@type"] == "BreadcrumbList")["itemListElement"]
        for c in crumbs:
            rel = c["item"].removeprefix(s.site_url + "/")
            assert (out / rel / "index.html").exists(), c["item"]


def test_round_internal_links(built):
    _, _, out, _ = built
    html = (out / "round/2/index.html").read_text()
    assert 'href="../../round/1/" rel="prev"' in html and 'href="../../round/3/" rel="next"' in html
    assert "More markets in Esports" in html


def test_not_found_page(built):
    s, _, out, _ = built
    html = (out / "404.html").read_text()
    assert '<meta name="robots" content="noindex">' in html and "data-share-bar" not in html
    assert f'href="{s.site_url}/leaderboard/"' in html and f'href="{s.site_url}/vs/claude-vs-chatgpt/"' in html
    assert '"fr": {' in html and "Page introuvable" in html  # its own language check, every language inline
    assert "404" not in (out / "sitemap-en.xml").read_text()


def test_verification(built):
    s, _, out, _ = built
    html = (out / "index.html").read_text()
    assert '<meta name="google-site-verification" content="g-token-123">' in html
    assert '<meta name="msvalidate.01" content="BING0123">' in html
    assert "<user>BING0123</user>" in (out / "BingSiteAuth.xml").read_text()


def test_verification_file_and_nothing_when_empty(tmp_path):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, google_site_verification="google0a1b2c3d.html")
    exports.export(s, DB(s.db_path))
    out = site.build(s)
    assert (out / "google0a1b2c3d.html").read_text() == "google-site-verification: google0a1b2c3d.html"
    html = (out / "index.html").read_text()
    assert "google-site-verification" not in html and "msvalidate" not in html
    assert not (out / "BingSiteAuth.xml").exists()
    assert not list(out.glob("*.txt")) or {p.name for p in out.glob("*.txt")} <= {"robots.txt", "llms.txt",
                                                                                  "llms-full.txt"}


def test_indexnow(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS)
    assert site.indexnow_key(s) is None  # off by default: no key, no file, no ping
    s.indexnow_enabled = True
    key = site.indexnow_key(s)
    assert re.fullmatch(r"[0-9a-f]{32}", key) and site.indexnow_key(s) == key  # generated once, then stable
    db = DB(s.db_path)
    make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")
    exports.export(s, db)
    out = site.build(s)
    assert (out / f"{key}.txt").read_text() == key
    rels = site.indexnow_changes(s)
    assert rels == ["", "leaderboard/", "round/1/"] and site.indexnow_changes(s) == []  # nothing new since
    body = site.indexnow_payload(s, key, rels)
    assert body == {"host": "agentpitbench.org", "key": key, "keyLocation": f"https://agentpitbench.org/{key}.txt",
                    "urlList": ["https://agentpitbench.org/", "https://agentpitbench.org/leaderboard/",
                                "https://agentpitbench.org/round/1/"]}
    assert len(site.indexnow_payload(s, key, [f"round/{i}/" for i in range(300)])["urlList"]) == 100
    sent = []

    def fail(req, timeout):
        sent.append(json.loads(req.data))
        raise OSError("offline")
    monkeypatch.setattr(site.urllib.request, "urlopen", fail)
    assert site.indexnow_ping(s, rels) is False and len(sent) == 1  # logged, never retried
