"""The market grid (docs/polymarket-layout-spec.md, milestone 1): tokens and dark default, the header search and
its rounds index, the category bar with its /c/<category>/ pages, and the home page's round cards (binary,
multi-outcome, sealed, live with the chance now, resolved), standings strip and film card."""
import asyncio
import json
import re
import time

import pytest

from agentpitbench import exports, site
from agentpitbench.config import Settings
from agentpitbench.db import DB

AGENTS = ["claude", "codex", "agy", "grok"]


def mk(db, question, outcomes, prices, picks, state="awaiting_resolution", winner=None, market_id="m1", event=None):
    m = {"id": market_id, "slug": f"s-{market_id}", "question": question, "outcomes_list": outcomes,
         "prices_list": prices, "endDate": "2026-10-17T20:00:00Z", "conditionId": f"0x{market_id}"}
    rid = db.create_round(m)
    for a in AGENTS:
        run = db.create_run(rid, a, cli_version="1.0", model_reported=a)
        p = picks.get(a)
        if p is None:
            db.record_bet(run, outcome=None, decided_s=None)
            db.finish_run(run, "no_bet", None, None)
            continue
        o, price = p
        db.record_bet(run, outcome=o, token_id="t", rationale="r", confidence=.6, avg_price=price,
                      shares_filled=100 / price, stake_filled=100, decided_s=60)
        db.finish_run(run, "bet", None, None)
    if state == "resolved":
        for e in db.round_entries(rid):
            if e["outcome"]:
                pay = e["shares_filled"] if e["outcome"] == winner else 0
                db.settle_bet(e["run_id"], pay, pay - e["stake_filled"])
        db.set_round(rid, state="resolved", winner=winner, resolved_at=time.time())
    else:
        db.set_round(rid, state=state)
    if event:
        db.set_round(rid, event_title=event)
    return rid


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    s = Settings(data_dir=tmp_path_factory.mktemp("grid") / "var", agents=AGENTS, dry_run=True)
    db = DB(s.db_path)
    ids = {
        "resolved": mk(db, "Will Lille OSC win on 2026-10-17?", ["Yes", "No"], [.62, .38],
                       {"claude": ("Yes", .41), "codex": ("Yes", .40), "agy": ("No", .60), "grok": ("Yes", .62)},
                       state="resolved", winner="Yes", market_id="m1", event="Lille OSC vs. Stade Brestois 29"),
        "multi": mk(db, "Who will win the 2026 Ohio Senate election?", ["Brown", "Husted", "Other"], [.30, .55, .15],
                    {"claude": ("Husted", .55), "codex": ("Brown", .30), "agy": ("Other", .15)}, market_id="m2"),
        "sealed": mk(db, "Will Bitcoin close above $120,000 on October 20?", ["Yes", "No"], [.25, .75],
                     {"claude": ("No", .75), "grok": ("Yes", .25)}, state="running", market_id="m3"),
    }
    exports.export(s, db)
    (s.export_dir / site.LIVE_PRICES_FILE).write_text(json.dumps({"m2": {"prices": [.2, .7, .1], "at": 1}}))
    return s, site.build(s), ids


def home(out, lang="en"):
    return (out / ("" if lang == "en" else lang) / "index.html").read_text()


def cards(html):
    return re.findall(r'<a class="mk[^"]*" href="(?:\.\./)*round/(\d+)/"(.*?)</a>\n', html, re.S)


def test_home_grid_live_first_with_every_card_kind(built):
    s, out, ids = built
    html = home(out)
    order = [int(i) for i, _ in cards(html)]
    assert set(order) == set(ids.values())
    assert order[-1] == ids["resolved"]                       # live rounds first, then the resolved ones
    body = {int(i): b for i, b in cards(html)}
    res = body[ids["resolved"]]
    assert "Lille OSC vs. Stade Brestois 29" in res and "Will Lille OSC win on 2026-10-17?" in res  # match title first
    assert "Resolved <b>Yes</b>" in res and "2 of 4 AIs right" not in res and "3 of 4 AIs right" in res
    assert 'class="pl yes"' in res and 'class="pl no"' in res and "41¢" in res
    multi = body[ids["multi"]]
    assert "Husted" in multi and "70%" in multi and "20%" in multi and "Other</span><span class=\"p\">" not in multi
    assert "Chance now" in multi and "data-live" in multi    # the build's refreshed price, labelled as now
    assert "No bet" in multi                                  # Grok sat it out
    sealed = body[ids["sealed"]]
    assert sealed.count("🔒") >= 5 and 'data-outcome=""' in sealed and "Picks revealed when all four are in" in sealed
    assert 'data-outcome="No"' not in sealed and "75¢" not in sealed      # sealed picks never leak
    assert "25%" in sealed and "at start" in sealed           # no fresh price: the chance at round start


def test_standings_strip_and_hero(built):
    _, out, _ = built
    html = home(out)
    assert "Which AI is smartest with money?" in html
    strip = re.search(r'<div class="stand".*?</div>', html, re.S).group(0)
    assert strip.count('class="chip') == 5 and 'href="leaderboard/"' in strip   # four AIs + the full table
    assert strip.index("Grok") < strip.index("Gemini")         # ranked by profit
    main = html[html.index("<main"):html.index("</main>")]
    assert "Brier" not in main and 'href="vs/' not in main     # no Brier or head-to-head (simple-scoring spec)


def test_category_pages_and_bar(built):
    _, out, ids = built
    bar = re.search(r'<nav class="cats".*?</nav>', home(out), re.S).group(0)
    assert re.findall(r'href="(c/[a-z]+/)"', bar) == ["c/live/", "c/sports/", "c/crypto/", "c/politics/"]
    assert 'href="" aria-current="page"' in bar               # All = the home page
    sports = (out / "c/sports/index.html").read_text()
    assert [int(i) for i, _ in cards(sports)] == [ids["resolved"]]
    live = (out / "c/live/index.html").read_text()
    assert {int(i) for i, _ in cards(live)} == {ids["multi"], ids["sealed"]}
    assert '<a href="../../c/live/" aria-current="page">' in live
    assert (out / "de/c/crypto/index.html").exists() and 'dir="rtl"' in (out / "ar/c/live/index.html").read_text()
    assert not (out / "c/economy").exists()                   # no round, no tab and no page
    assert "/c/politics/" in (out / "sitemap-en.xml").read_text()


def test_rounds_index_for_search(built):
    _, out, ids = built
    idx = json.loads((out / "data/rounds-index.json").read_text())
    assert {r["id"] for r in idx} == set(ids.values())
    assert all(set(r) == ({"id", "q", "ev", "cat", "state", "w", "rt"} if r["state"] == "resolved"
                          else {"id", "q", "ev", "cat", "state"}) for r in idx)
    lille = next(r for r in idx if r["id"] == ids["resolved"])
    assert lille["ev"] == "Lille OSC vs. Stade Brestois 29" and lille["cat"] == "Sports"
    assert next(r for r in idx if r["id"] == ids["sealed"])["state"] == "live"
    html = home(out)
    assert '<form class="srch" role="search" hidden data-idx="data/rounds-index.json"' in html


def test_dark_by_default_and_no_polymarket_branding(built):
    _, out, _ = built
    for page in ("index.html", "c/live/index.html", "ar/index.html", "de/c/sports/index.html"):
        html = (out / page).read_text()
        assert re.search(r'<html lang="[a-z]+"( dir="rtl")? data-theme="dark">', html), page
        assert 'aria-label="Theme: Dark"' in html or page.startswith(("ar/", "de/")), page
        foot = html[html.index("<footer>"):]
        assert html.count("Polymarket") == foot.count("Polymarket") == 1, page   # only the footer disclaimer
    assert "Not affiliated with Polymarket" in home(out)


def test_refresh_live_prices_writes_the_chance_now(tmp_path):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True)
    db = DB(s.db_path)
    mk(db, "Will it rain?", ["Yes", "No"], [.5, .5], {"claude": ("Yes", .5)}, market_id="11")
    mk(db, "Will it snow?", ["Yes", "No"], [.5, .5], {"claude": ("Yes", .5)}, market_id="12")
    mk(db, "Old one?", ["Yes", "No"], [.5, .5], {"claude": ("Yes", .5)}, state="resolved", winner="Yes", market_id="13")
    exports.export(s, db)
    asked = []

    class Api:
        async def market(self, mid):
            asked.append(mid)
            if mid == "12":
                raise RuntimeError("down")
            return {"prices_list": [.8, .2]}

    assert asyncio.run(site.refresh_live_prices(s, Api())) == 1
    assert sorted(asked) == ["11", "12"]                       # resolved rounds are not refreshed
    assert site.live_prices(s) == {"11": [.8, .2]}
    out = site.build(s)
    body = {int(i): b for i, b in cards(home(out))}
    assert "80%" in body[1] and "now" in body[1] and "50%" in body[2] and "at start" in body[2]
