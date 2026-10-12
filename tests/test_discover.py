"""Outputs for AI assistants and agents (docs/ai-referral-spec.md): robots.txt, sitemaps, llms.txt, /facts/,
JSON-LD, the static /api/v1/, and the pick embargo across all of them."""
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape

import pytest

from agentpitbench import discover, exports, forecast, i18n, site
from agentpitbench.config import Settings
from agentpitbench.db import DB
from test_publish import PICKS, make_round

AGENTS = ["claude", "codex", "agy", "grok"]
SECRET = "SENTINEL-RATIONALE-7c1f"   # a running round's rationale: must appear in no output
LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def add_sweep(db: DB, date: str, scored: bool, n: int = 6) -> None:
    """n two-outcome markets swept on `date`; Claude sharp, the others noisy; resolved and scored if `scored`."""
    for i in range(n):
        mid = f"{date}-m{i}"
        win = "Yes" if i % 2 else "No"
        db.x("INSERT INTO sweep_markets(market_id, sweep_date, question, outcomes, prices, end_date, winner, state)"
             " VALUES(?,?,?,?,?,?,?,?)", mid, date, f"Market {i}?", '["Yes","No"]', "[0.5,0.5]", date + "T20:00:00Z",
             win if scored else None, "resolved" if scored else "open")
        for j, a in enumerate(AGENTS):
            p_yes = (0.9 if win == "Yes" else 0.1) if a == "claude" else 0.3 + 0.1 * j
            probs = {"Yes": p_yes, "No": round(1 - p_yes, 4)}
            db.x("INSERT INTO forecasts(sweep_date, agent, market_id, probs, model_reported, brier) VALUES(?,?,?,?,?,?)",
                 date, a, mid, json.dumps(probs), f"{a}-1",
                 forecast.brier(probs, win) if scored else None)
    for a in AGENTS:
        db.x("INSERT INTO sweep_runs(sweep_date, agent, status, n_forecasts) VALUES(?,?,?,?)", date, a, "ok", n)


@pytest.fixture
def built(tmp_path):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True)
    db = DB(s.db_path)
    make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")
    void = make_round(db, PICKS, state="void", market_id="m2")
    db.set_round(void, exhibition=1, exclusion="infra", resolved_at=time.time())
    running = make_round(db, {a: ("Nexus", .4, .77) for a in AGENTS}, state="running", market_id="m3")
    db.x("UPDATE bets SET rationale=?, quote=? WHERE run_id IN (SELECT run_id FROM runs WHERE round_id=?)",
         SECRET, SECRET, running)
    for rid in (1, running):  # a stray decision card must not leak a running round's pick
        (s.cards_dir / str(rid)).mkdir(parents=True, exist_ok=True)
        (s.cards_dir / str(rid) / "decision-claude.png").write_bytes(b"png")
    add_sweep(db, "2026-10-10", scored=True)
    add_sweep(db, "2026-10-11", scored=False)
    forecast.seal(s, db, "2026-10-10")
    forecast.seal(s, db, "2026-10-11")
    from agentpitbench import methodology
    methodology.changes_dir(s).mkdir(parents=True)
    (methodology.changes_dir(s) / "1.1.json").write_text('{"version": "1.1"}')
    exports.export(s, db)
    return s, db, site.build(s), {"void": void, "running": running}


def test_methodology_change_evidence_published(built):
    """Every generated changelog line links /methodology/changes/<version>.json: the build must publish it."""
    s, _, out, _ = built
    assert json.loads((out / "methodology" / "changes" / "1.1.json").read_text()) == {"version": "1.1"}
    assert (out / "methodology" / "index.html").exists()


def test_robots_allows_everything(built):
    s, _, out, _ = built
    robots = (out / "robots.txt").read_text()
    assert "Disallow" not in robots  # no legitimate reason for one: the site is fully public
    assert "User-agent: *\nAllow: /" in robots and f"Sitemap: {s.site_url}/sitemap.xml" in robots


def test_sitemap_every_page_every_language(built):
    s, _, out, ids = built
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    index = ET.fromstring((out / "sitemap.xml").read_text())
    files = [e.text.rsplit("/", 1)[1] for e in index.findall("sm:sitemap/sm:loc", ns)]
    assert files == [f"sitemap-{c}.xml" for c in i18n.CODES]
    locs = set()
    for f in files:
        for u in ET.fromstring((out / f).read_text()).findall("sm:url", ns):
            loc = u.find("sm:loc", ns).text
            locs.add(loc)
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", u.find("sm:lastmod", ns).text)
            alts = u.findall("x:link", ns)
            assert {a.get("hreflang") for a in alts} == {*i18n.CODES, "x-default"}
            assert loc in {a.get("href") for a in alts}
    for rel in ("", "leaderboard/", "facts/", "methodology/", "round/1/", "agent/grok/"):
        for c in i18n.CODES:
            assert f"{s.site_url}/{'' if c == 'en' else c + '/'}{rel}" in locs, (c, rel)
    assert not any("/widget/" in x or "/embed/" in x for x in locs)
    assert f"{s.site_url}/round/{ids['void']}/" not in locs  # hidden void rounds have no page


def test_facts_page_in_every_language_matches_the_leaderboard(built):
    s, _, out, _ = built
    lb = json.loads((out / "data/leaderboard.json").read_text())
    leader = lb["forecast"]["agents"][0]
    brier = "%.3f" % leader["brier"]
    lo, hi = ("%.3f" % v for v in leader["ci"])
    assert brier in (out / "leaderboard/index.html").read_text()
    for code in i18n.CODES:
        prefix = "" if code == "en" else f"{code}/"
        f = out / prefix / "facts/index.html"
        html = f.read_text()
        assert f.stat().st_size < 100_000 and f'<html lang="{code}"' in html
        assert brier in html and lo in html and hi in html and f"n={leader['n']}" in html
    en = (out / "facts/index.html").read_text()
    assert "@misc{agentpitbench" in en and "Last updated" in en
    assert "not yet significant" in en and "forecasts better than the Crowd with 95% confidence" in en
    assert "<h1>Fakten</h1>" in (out / "de/facts/index.html").read_text()

    fj = json.loads((out / "facts.json").read_text())
    assert fj == json.loads((out / "api/v1/facts.json").read_text())
    d = fj["data"]
    assert d["forecast"]["leader"]["agent"] == leader["agent"]
    assert [(a["agent"], a["brier"], a["ci"], a["n"]) for a in d["forecast"]["agents"]] == \
        [(a["agent"], a["brier"], a["ci"], a["n"]) for a in lb["forecast"]["agents"]]
    assert [(b["agent"], b["wins"], b["losses"], b["net_pnl"]) for b in d["betting"]["agents"]] == \
        [(b["agent"], b["wins"], b["losses"], b["net_pnl"]) for b in lb["agents"]]
    assert "⁨" not in json.dumps(fj)  # no RTL isolates leak into data
    assert d["statements"][0]["facts"][0].startswith("As of ")


def test_llms_txt(built):
    s, _, out, _ = built
    lb = json.loads((out / "data/leaderboard.json").read_text())
    stamp = datetime.fromtimestamp(lb["updated_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    short, full = (out / "llms.txt").read_text(), (out / "llms-full.txt").read_text()
    assert short.startswith("# AgentpitBench\n\n> ")
    for text in (short, full):
        assert f"Data as of {stamp}, methodology v{s.methodology_version}." in text
        assert "also operates agentpit" in text
        for link in ("/facts/", "/methodology/", "/independence/", "/leaderboard/", "/data/metrics.json",
                     "/api/v1/openapi.json"):
            assert s.site_url + link in text, link
        assert "%.3f" % lb["forecast"]["agents"][0]["brier"] in text
    assert full.startswith(short.rstrip())
    assert "Changelog" in full and "@misc{agentpitbench" in full and "Sandbox" in full
    assert "You are taking part" not in full  # the agents' prompts stay on the page, not in text aimed at models
    assert len(full) < 100_000


def test_json_ld(built):
    s, _, out, ids = built

    def ld(path):
        blocks = LD.findall((out / path).read_text())
        assert len(blocks) == 1, path
        return json.loads(blocks[0])

    def types(doc):
        return {n["@type"] for n in doc["@graph"]}
    for p in ("index.html", "leaderboard/index.html", "facts/index.html", "fr/index.html"):
        doc = ld(p)
        assert {"Dataset", "Organization"} <= types(doc)
        ds = next(n for n in doc["@graph"] if n["@type"] == "Dataset")
        assert ds["license"] and ds["dateModified"] and ds["temporalCoverage"] and ds["variableMeasured"]
        assert any(d["contentUrl"].endswith("/data/metrics.json") for d in ds["distribution"])
    org = next(n for n in ld("index.html")["@graph"] if n["@type"] == "Organization")
    assert org["name"] == "SKALE Labs" and any("x.com" in u for u in org["sameAs"])
    meth = ld("methodology/index.html")
    art = next(n for n in meth["@graph"] if n["@type"] == "TechArticle")
    assert art["version"] == s.methodology_version and art["dateModified"]
    assert types(ld("round/1/index.html")) == {"Organization", "Event", "ItemList", "BreadcrumbList"}
    assert types(ld(f"round/{ids['running']}/index.html")) == {"Organization", "Event", "BreadcrumbList"}  # no picks while running


def test_api(built):
    s, _, out, ids = built
    api = out / "api/v1"
    files = list(api.rglob("*.json"))
    for f in files:
        if f.name == "openapi.json":
            continue
        doc = json.loads(f.read_text())
        for k in ("as_of", "methodology_version", "source_url", "citation"):
            assert doc[k], (f, k)
        assert doc["source_url"].startswith(s.site_url) and doc["source_url"] in doc["citation"]
    spec = json.loads((api / "openapi.json").read_text())
    assert spec["openapi"].startswith("3.") and "/rounds/{n}.json" in spec["paths"]
    rounds = json.loads((api / "rounds.json").read_text())["data"]["rounds"]
    by = {r["round_id"]: r for r in rounds}
    assert s.site_hide_void and by[ids["void"]]["state"] == "void"  # void rounds stay in the API, flagged
    assert by[ids["void"]]["exclusion"] == "infra" and by[ids["void"]]["counts"] is False
    assert by[ids["void"]]["url"] is None and by[1]["url"] == f"{s.site_url}/round/1/"
    assert by[1]["counts"] is True and json.loads((api / "rounds/1.json").read_text())["data"]["winner"] == "Nexus"
    agent = json.loads((api / "agents/grok.json").read_text())
    assert agent["data"]["forecast"]["brier"] is not None and agent["source_url"] == f"{s.site_url}/agent/grok/"
    lb = json.loads((api / "leaderboard.json").read_text())["data"]
    assert lb["forecast"]["agents"][0]["agent"] == "claude"
    idx = json.loads((api / "forecasts.json").read_text())["data"]["sweeps"]
    pub = {x["date"]: x["published"] for x in idx}
    assert pub == {"2026-10-10": True, "2026-10-11": False}  # a day with open markets stays sealed
    assert (api / "forecasts/2026-10-10.json").exists() and not (api / "forecasts/2026-10-11.json").exists()
    assert json.loads((api / "forecasts/2026-10-10.json").read_text())["data"]["sha256_matches"] is True
    meth = json.loads((api / "methodology.json").read_text())["data"]
    assert meth["version"] == s.methodology_version and meth["changelog"] and meth["summary"]


def test_independence_discloses_promotion(built):
    _, _, out, _ = built
    html = unescape((out / "independence/index.html").read_text())
    assert "operator's platform promoting the operator's benchmark" in html and "no instructions to models" in html


def test_no_output_contains_a_running_rounds_pick(built):
    """§6: during a running round no output (exports, site, API, llms.txt, facts) contains any agent's pick."""
    s, db, out, ids = built
    rid = ids["running"]
    assert db.round(rid)["state"] == "running"
    for root in (out, s.export_dir):
        for f in root.rglob("*"):
            if f.is_file() and f.suffix in (".html", ".json", ".txt", ".csv", ".xml"):
                assert SECRET not in f.read_text(errors="replace"), f
    rnd = json.loads((out / f"api/v1/rounds/{rid}.json").read_text())["data"]
    assert rnd["sealed"] is True and rnd["split"] is False
    for e in rnd["entries"]:
        assert e["sealed"] is True and all(e[k] is None for k in discover.PICK_FIELDS), e
    summary = next(r for r in json.loads((out / "api/v1/rounds.json").read_text())["data"]["rounds"]
                   if r["round_id"] == rid)
    assert all(p["outcome"] is None for p in summary["picks"])
    for a in AGENTS:
        hist = json.loads((out / f"api/v1/agents/{a}.json").read_text())["data"]["rounds"]
        assert all(h["outcome"] is None for h in hist if h["round_id"] == rid)
    assert (out / "cards/1/decision-claude.png").exists() and not (out / f"cards/{rid}").exists()
    for page in (f"round/{rid}/index.html", "index.html"):  # the live panel: sealed rows, no outcome
        html = (out / page).read_text()
        assert 'data-outcome="Nexus"' not in html and 'data-outcome=""' in html and "🔒" in html, page
    exp = next(r for r in json.loads((s.export_dir / "rounds.json").read_text()) if r["round_id"] == rid)
    assert all(e["outcome"] is None and e["confidence"] is None for e in exp["entries"])


def test_api_seals_even_if_an_export_did_not(tmp_path):
    """Belt and braces: a running round's picks are stripped by the API itself, not only by the export."""
    r = {"round_id": 9, "question": "Q?", "state": "running", "entries": [
        {"agent": "claude", "name": "Claude", "outcome": "Yes", "rationale": "secret", "confidence": 0.9,
         "transcript": "transcripts/9/claude.txt"}], "split": True}
    full = discover.api_round(r, "https://x.test", None)
    assert full["sealed"] and not full["split"]
    assert all(full["entries"][0][k] is None for k in discover.PICK_FIELDS)
    assert "Yes" not in json.dumps(discover.round_ld(Settings(data_dir=tmp_path), r, "https://x.test/round/9/"))


def test_citation_cff_and_readme_abstract():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    cff = (root / "CITATION.cff").read_text()
    assert cff.startswith("cff-version: 1.2.0") and "type: dataset" in cff
    assert f'version: "methodology-{Settings().methodology_version}"' in cff  # bump with the methodology
    readme = (root / "README.md").read_text()
    assert "## Abstract" in readme and "CITATION.cff" in readme and "/facts/" in readme
