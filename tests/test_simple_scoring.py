"""Simple scoring (docs/simple-scoring-spec.md): the Brier competitions and Head to head are gone from every public
surface, the betting record is the only headline, and the switch is a public methodology change (2.0) with its
evidence file. The lock is a scratch copy rewound to 1.0 (conftest.copy_lock_v10), so nothing here depends on the
live lock's version."""
import json
import re
import shutil
from pathlib import Path

import pytest
from conftest import copy_lock_v10

from agentpitbench import exports, forecast, i18n, methodology as m, og, site
from agentpitbench.config import Settings
from agentpitbench.db import DB
from test_discover import AGENTS, add_sweep
from test_publish import PICKS, make_round

PKG = Path(__file__).resolve().parent.parent / "agentpitbench"
TEXT = ("Headline metric: the betting record (wins, losses, profit) replaces forecast accuracy (Brier score), which "
        "was too hard to read. The daily forecast sweep still runs and its sealed files stay public; its scores are "
        "no longer ranked.")
LANGS = ["en", "ar", "zh"]   # English, one RTL and one CJK language: translated strings are swept too
BANNED = re.compile(r"Brier|Head to head|head-to-head|href=\"[^\"]*/vs/", re.I)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A scratch lock at 1.0 and scratch i18n tables; the repo's lock and tables are never written."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    shutil.copytree(PKG / "i18n", pkg / "i18n")
    copy_lock_v10(pkg / "methodology.json")
    monkeypatch.setattr(m, "LOCK", pkg / "methodology.json")
    monkeypatch.setattr(m, "I18N_DIR", pkg / "i18n")
    return pkg


def world(tmp_path) -> tuple[Settings, DB]:
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True)
    m.apply(s, {})
    db = DB(s.db_path)
    make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")        # Grok alone right
    make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m2")
    for d in ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"):
        add_sweep(db, d, scored=True)   # Claude is the sharp forecaster: the forecast leader
        forecast.seal(s, db, d)
    return s, db


def switch(s, tree, answer=None, **kw):
    p = m.propose(s, {}, TEXT, "operator", rehash=True)
    return m.approve(s, p["id"], major=True, snapshot=True, isatty=lambda: True,
                     ask=lambda _: answer or "approve v2.0", fill=lambda ids: m.write_english(ids, tree / "i18n"), **kw)


# ---------- the methodology change ----------

def test_switch_is_a_major_version_with_its_evidence(tmp_path, tree):
    s, db = world(tmp_path)
    new = switch(s, tree)
    assert new["version"] == "2.0" and s.methodology_version == "2.0"
    e = new["changelog"][-1]
    assert e["text"] == TEXT and e["version"] == "2.0" and e["params_sha"] == m.params_sha(new)
    assert m.verify(s) == []   # the drift detector stays quiet: lock, settings and evidence agree
    ev = json.loads((m.changes_dir(s) / "2.0.json").read_text())
    assert ev["kind"] == exports.SNAPSHOT_KIND and ev["version"] == "2.0" and ev["previous_version"] == "1.0"
    fa, br = ev["forecast_accuracy"], ev["betting_record"]
    assert [a["agent"] for a in fa["agents"]][0] == "claude" and fa["agents"][0]["brier"] is not None
    assert fa["crowd"]["brier"] is not None and {"ci", "n", "vs_crowd"} <= set(fa["agents"][0])
    assert {"wins", "losses", "profit", "rank"} <= set(br["agents"][0]) and ev["markets_resolved"] == 24
    assert ev["forecast_leader"] == "Claude" and ev["betting_leader"] == next(
        b["name"] for b in br["agents"] if b["rank"] == 1)
    assert ev["summary"] == (f"At the switch, the forecast leader was Claude; the betting leader was "
                             f"{ev['betting_leader']}.")
    for f in (tree / "i18n").glob("*.json"):   # the 2.0 line is in every site language
        assert TEXT in json.loads(f.read_text("utf-8")), f.name


def test_failed_commit_leaves_no_evidence(tmp_path, tree, monkeypatch):
    s, _ = world(tmp_path)

    def boom(*a, **kw):
        raise m.MethodologyError("disk full")
    monkeypatch.setattr(m, "_commit", boom)
    with pytest.raises(m.MethodologyError):
        switch(s, tree)
    assert not (m.changes_dir(s) / "2.0.json").exists() and m.load()["version"] == "1.0"


def test_unconfirmed_switch_writes_nothing(tmp_path, tree):
    s, _ = world(tmp_path)
    with pytest.raises(m.MethodologyError):
        switch(s, tree, answer="yes")
    assert not m.changes_dir(s).exists() or not list(m.changes_dir(s).glob("*.json"))


# ---------- the site after the switch ----------

@pytest.fixture
def built(tmp_path, tree):
    s, db = world(tmp_path)
    switch(s, tree)
    data = exports.export(s, db)
    return s, data, site.build(s)


def _sweepable(out: Path, rel: str) -> str:
    """A page's text minus what may still say Brier (§7): on /methodology/ the changelog and the research
    section; JSON-LD carries the changelog's dateModified only, never its text, but is skipped to be safe."""
    html = (out / rel).read_text()
    html = re.sub(r'<script type="application/ld\+json">.*?</script>', "", html, flags=re.S)
    if rel.endswith("methodology/index.html"):
        html = re.sub(r'<ul id="changelog">.*?</ul>', "", html, flags=re.S)
        html = re.sub(r'<section id="research">.*?</section>', "", html, flags=re.S)
    return html


def test_no_brier_or_head_to_head_on_any_page(built):
    s, _, out = built
    seen = 0
    for code in LANGS:
        prefix = "" if code == "en" else f"{code}/"
        for f in (out / prefix).rglob("*.html") if prefix else out.glob("**/*.html"):
            rel = f.relative_to(out).as_posix()
            if code == "en" and re.match(r"[a-z]{2}/", rel):
                continue   # other languages' folders
            if rel.startswith(("cards/", "data/", "transcripts/", "api/")) or re.search(r"(^|/)vs/", rel):
                continue   # raw data and the retired pages' redirect stubs (§7)
            text = _sweepable(out, rel)
            hit = BANNED.search(text)
            assert not hit, (rel, hit.group(0))
            seen += 1
    assert seen > 30
    # the methodology page: headline renamed, research section present, the switch linked and explained
    meth = (out / "methodology/index.html").read_text()
    assert "Headline metric: betting record" in meth and "Headline metric: forecast accuracy" not in meth
    assert "Daily forecast sweep (research data)" in meth and "Brier score" in meth  # the field definition
    assert 'href="../methodology/changes/2.0.json"' in meth
    ev = json.loads((out / "methodology/changes/2.0.json").read_text())
    assert (f"At the switch, the forecast leader was Claude; the betting leader was {ev['betting_leader']}."
            in meth)
    assert "Methodology v2.0" in meth


def test_leaderboard_leads_with_the_betting_record(built):
    s, data, out = built
    for code in LANGS:
        _ = i18n.Translator(code)
        html = (out / ("" if code == "en" else f"{code}/") / "leaderboard/index.html").read_text()
        first_h2 = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.S).group(1)
        assert first_h2 == str(_("Betting record")), code
        assert "Calibration" not in html and "Launch-day duels" not in html
    en = (out / "leaderboard/index.html").read_text()
    n = data["leaderboard"]["rounds_counted"]
    assert n == 2 and f"{n} rounds counted this season. That is a small sample, so the order can still change." in en
    idx = (out / "index.html").read_text()
    assert "Forecast accuracy" not in idx and "th2h" not in idx
    for p in ("splits/index.html", "agent/claude/index.html", "reports/index.html", "films/index.html", "404.html"):
        assert "Head to head" not in (out / p).read_text(), p


def test_api_and_ai_files(built):
    s, data, out = built
    api = out / "api/v1"
    for f in [api / "leaderboard.json", *(api / "agents").glob("*.json")]:
        d = json.loads(f.read_text())["data"]
        assert "forecast" not in d and "vs_crowd" not in d, f.name
    spec = json.loads((api / "openapi.json").read_text())
    assert "ForecastRow" not in spec["components"]["schemas"]
    assert json.loads((api / "forecasts.json").read_text())["data"]["sweeps"]   # the sealed files stay listed
    lb = json.loads((out / "data/leaderboard.json").read_text())
    assert not {"forecast", "pairs", "significance", "duels"} & set(lb) and "brier" not in lb["agents"][0]
    metrics = json.loads((out / "data/metrics.json").read_text())
    assert not {"forecast_accuracy", "significance", "launch_day_duels"} & set(metrics)
    assert metrics["sealed_forecasts"] and metrics["methodology_version"] == "2.0"
    leader = next(b for b in lb["agents"] if b["rank"] == 1)
    for name in ("llms.txt", "facts.json"):
        text = (out / name).read_text()
        assert "Brier" not in text and "/vs/" not in text and leader["name"] in text, name
    reports = json.loads((out / "data/reports.json").read_text())
    assert reports and all("forecast" not in r and "crowd_brier" not in r for r in reports)


# ---------- cards and films ----------

def test_standings_card_is_the_betting_order(built):
    s, data, _ = built
    lb = data["leaderboard"]
    html = og.standings_html(s, lb)
    assert "Forecast accuracy" not in html and "95% CI" not in html and "Brier" not in html
    assert "Win rate" in html and f"{lb['rounds_counted']} rounds · season {lb['season']}" in html
    names = re.findall(r'<span class="nm"[^>]*>([^<]+)</span>', html)
    betting = [b["name"] for b in sorted(lb["agents"], key=lambda b: b["rank"])]
    assert names[:len(betting)] == betting
    agent = og.agent_html(s, lb, "grok")
    assert "Forecast accuracy" not in agent and "Win rate" in agent and "Brier" not in agent


def test_split_card_has_no_head_to_head(built):
    from agentpitbench import cards
    s, data, _ = built
    cr = cards.CardRenderer(s)
    r = data["rounds"][-1]
    html = cards.env.get_template("split.html").render(**cr._ctx(r, data["leaderboard"]["agents"]), kind="split",
                                                       headline="Split decision")
    assert "Head to head" not in html and "Grudge" not in html


def test_posting_code_has_no_brier_wording():
    """§5: the posting paths say nothing about Brier, calibration or head to head."""
    for name in ("engage.py", "publish.py", "twitter.py", "bluesky.py", "telegram.py", "week.py", "motd.py",
                 "report_prose.py", "twists.py"):
        src = (PKG / name).read_text()
        for word in ("Brier", "calibrat", "head to head", "head-to-head", "/vs/", "Forecast accuracy"):
            assert word.lower() not in src.lower(), (name, word)


def test_duels_are_off_by_default():
    assert Settings().duels_enabled is False
    assert not hasattr(forecast, "duels") and hasattr(forecast, "queue_duel")   # rows stay; no display
