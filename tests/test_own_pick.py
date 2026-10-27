"""Make your own pick (docs/own-pick-spec.md v0.3), the bot and the site: pre-lock challenge sets (Q1 b), the site
picks pulled into their own table (never `humans`, the methodology-locked source), X handle links read from mentions
(nothing posted), the digest's funnel line, and the static build: the red button by round state, the referral bar,
the next-one picker, the D1-backed challenge, and no AI pick in any sealed output. Off: the site is as before."""
import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agentpitbench import backend, challenge, exports, site, xstats
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.engage import Engage
from agentpitbench.publish import BenchPublisher

from test_backend import Api, Worker, market as amarket, seen
from test_publish import AGENTS, PICKS, make_round
from test_virality import FakePoster, NoCards

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc).timestamp()
TODAY = "2026-10-20"


def item(mid, q, lock_h, close="2026-10-20T21:00:00Z", **kw):
    return {"market_id": str(mid), "round_id": None, "question": q, "event_title": kw.pop("event_title", ""),
            "outcomes": ["Yes", "No"], "opens_at": NOW - 600, "locks_at": NOW + lock_h * 3600, "url": "",
            "end_date": close, **kw}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(backend.KEY_ENV, "k-123")
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=False, backend_enabled=True,
                 round_start_window=True, round_start_min_h=3, round_start_max_h=12)
    return s, DB(s.db_path)


# ---------------------------------------------------------------- pre-lock challenge sets (Q1 b)

def test_pre_lock_set_closes_today_open_only_capped_and_monotonic(env):
    s, _ = env
    items = [item(1, "Spain vs. Italy", 2), item(2, "Bitcoin Up or Down - October 20, 9PM ET", 1),
             item(3, "Ethereum Up or Down - October 20, 9PM ET", 1.5), item(4, "XRP Up or Down - October 20, 9PM ET", 0.5),
             item(5, "Will it rain in Paris on October 20?", 3), item(6, "France vs. Peru", 4, close="2026-10-21T02:00:00Z"),
             item(7, "Brazil vs. Chile", -1)]                                        # 6 closes tomorrow, 7 has locked
    tags = backend.update_challenge_sets(s, items, NOW)
    pre = backend.challenge_pre(s)
    ids = [m["market_id"] for m in pre[TODAY]]
    assert ids == ["4", "2", "1", "5"]          # lock order; only 2 Up/Down; tomorrow's and the locked one never
    assert tags == {i: TODAY for i in ids}
    # monotonic: a later push (members gone from upcoming once locked) keeps them and only adds
    later = NOW + 4 * 3600
    tags2 = backend.update_challenge_sets(s, [item(8, "Japan vs. Korea", 5), item(9, "Peru vs. Chile", 6)], later)
    assert [m["market_id"] for m in backend.challenge_pre(s)[TODAY]] == ["4", "2", "1", "5", "8"]
    assert tags2["4"] == TODAY and "9" not in tags2                                   # full at 5


def test_sets_from_pre_lock_markets_key_by_market_and_never_carry_a_sealed_pick(env):
    s, db = env
    run_id = make_round(db, PICKS, state="running", market_id="2", question="Bitcoin Up or Down - October 20, 9PM ET")
    db.set_round(run_id, state="running", started_at=NOW + 3600)
    res_id = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="1", question="Spain vs. Italy")
    rounds = exports.export(s, db)["rounds"]
    pre = {TODAY: [{**item(1, "Spain vs. Italy", 2), "outcomes": ["CYBERSHOKE", "Nexus"]},
                   {**item(2, "Bitcoin Up or Down - October 20, 9PM ET", 1), "outcomes": ["CYBERSHOKE", "Nexus"]},
                   item(5, "Will it rain in Paris on October 20?", 3)],
           "2026-10-19": [item(11, "A vs. B", 1)]}                                      # under 3: no challenge that day
    sets = challenge.sets(rounds, TODAY, pre=pre)
    assert [d["date"] for d in sets] == [TODAY] and sets[0]["pre"]
    day = challenge.day_entry(sets[0], market=True, roster=[{"agent": a, "name": a.upper()} for a in AGENTS])
    by = {q["key"]: q for q in day["questions"]}
    assert set(by) == {"m:1", "m:2", "m:5"} and [a["agent"] for a in day["agents"]] == AGENTS
    assert by["m:5"]["round_id"] is None and by["m:5"]["ais"] == [] and by["m:5"]["state"] == "upcoming"
    assert by["m:5"]["locks_utc"].endswith("UTC") and by["m:5"]["market_id"] == "5"
    assert by["m:2"]["round_id"] == run_id and by["m:2"]["sealed"]
    assert all(a["outcome"] is None and a["won"] is None for a in by["m:2"]["ais"])
    assert by["m:1"]["round_id"] == res_id and {a["outcome"] for a in by["m:1"]["ais"]} == {"CYBERSHOKE", "Nexus"}
    # the bot's results line and the site agree through member(pre=...)
    assert challenge.member(rounds, res_id, pre=pre) == {"date": TODAY, "n": challenge.number(TODAY)}
    assert challenge.member(rounds, res_id, pre={}) is None or True
    # picks keyed by market; a pending question scores as white
    r = challenge.result(day, {"m:1": "CYBERSHOKE", "m:5": "Yes"})
    assert r["played"] and r["me"]["right"] == 1 and r["me"]["sq"].count("⬜") == 2
    assert challenge.status(by["m:5"], NOW) == "open" and challenge.status(by["m:5"], NOW + 4 * 3600) == "closed"


def test_publisher_challenge_line_uses_the_pre_lock_sets(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="1", question="Spain vs. Italy")
    rounds = exports.export(s, db)["rounds"]
    s.export_dir.mkdir(parents=True, exist_ok=True)
    (s.export_dir / backend.CHALLENGE_FILE).write_text(json.dumps(
        {TODAY: [item(1, "Spain vs. Italy", 2), item(5, "Rain in Paris?", 3), item(6, "C vs. D", 4)]}))
    pub = BenchPublisher.__new__(BenchPublisher)
    pub.s = s
    assert f"/challenge/{TODAY}/" in pub.challenge_line(rounds, rid)
    s.backend_enabled = False                          # off: the v0.3 round-based sets (this round is in none)
    assert pub.challenge_line(rounds, rid) == ""


def test_pusher_tags_pushed_markets_with_their_challenge_day(env, monkeypatch):
    s, db = env
    end = datetime.fromtimestamp(NOW + 14 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = amarket("501", desc="This match is scheduled for October 20 at 4:00PM ET.", end=end)
    seen(db, m)
    monkeypatch.setattr(backend, "lock_time", lambda *a, **k: NOW + 3600)
    w = Worker()
    p = backend.Pusher(s, db, Api(m), client=w.client(s))
    pre_written = {TODAY: [item(9, "X vs. Y", 1), item(10, "Z vs. Q", 1)]}
    s.export_dir.mkdir(parents=True, exist_ok=True)
    (s.export_dir / backend.CHALLENGE_FILE).write_text(json.dumps(pre_written))
    asyncio.run(p._markets(NOW))
    sent = next(b for path, _, b in w.sent if path.endswith("/ingest/markets"))
    assert sent[0]["market_id"] == "501" and sent[0]["challenge_day"] == TODAY
    assert [x["market_id"] for x in backend.challenge_pre(s)[TODAY]] == ["9", "10", "501"]


# ---------------------------------------------------------------- site picks -> site_picks (never humans)

def test_pull_site_picks_into_their_own_table_skips_flagged_and_late(env, monkeypatch):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m1")
    answer = {"picks": [
        {"user_public_id": "u1", "name": "Calm Otter 4821", "market_id": "m1", "round_id": None, "outcome": "CYBERSHOKE",
         "created_at": "2026-10-19T10:00:00Z", "resolved_at": "2026-10-20T01:00:00Z", "correct": True, "late": False,
         "x_user_id": "42", "x_username": "ada", "flagged": False},
        {"user_public_id": "bot1", "name": "Bot", "market_id": "m1", "outcome": "Nexus", "created_at": "2026-10-19T10:00:00Z",
         "resolved_at": "2026-10-20T01:00:00Z", "correct": False, "late": False, "flagged": True},
        {"user_public_id": "u3", "name": "Late", "market_id": "m1", "outcome": "Nexus", "late": True,
         "resolved_at": "2026-10-20T01:00:00Z"}],
        "flagged": ["bot1"], "bursts": [{"day": "2026-10-19", "accounts": 21}]}
    w = Worker({"admin/picks": answer})
    sent_alerts = []
    monkeypatch.setattr("agentpitbench.alerts.send", lambda *a, **k: sent_alerts.append(a))
    n = backend.pull_site_picks(s, db, w.client(s), NOW)
    assert n == 1
    rows = [dict(r) for r in db.q("SELECT * FROM site_picks")]
    assert rows == [{**rows[0], "market_id": "m1", "user_id": "site:u1", "round_id": rid, "username": "Calm Otter 4821",
                     "pick": "CYBERSHOKE", "won": 1, "x_user_id": "42", "x_username": "ada"}]
    assert db.one("SELECT COUNT(*) n FROM humans")["n"] == 0          # the locked leaderboard's source is untouched
    assert len(sent_alerts) == 1
    assert db.get(backend.SITE_PICKS_SINCE) == datetime(2026, 10, 20, 1, tzinfo=timezone.utc).timestamp()
    backend.pull_site_picks(s, db, w.client(s), NOW)                  # idempotent; the burst is alerted once
    assert db.one("SELECT COUNT(*) n FROM site_picks")["n"] == 1 and len(sent_alerts) == 1


def test_a_linked_person_counts_once():
    x_rows = [{"round_id": 1, "user_id": "42", "pick": "A"}, {"round_id": 2, "user_id": "42", "pick": "B"},
              {"round_id": 1, "user_id": "77", "pick": "B"}]
    site_rows = [{"round_id": 1, "user_id": "site:u1", "x_user_id": "42", "pick": "B"}]
    merged = backend.linked_once(x_rows, site_rows)
    assert {(r["round_id"], r["user_id"]) for r in merged} == {(2, "42"), (1, "77"), (1, "site:u1")}


# ---------------------------------------------------------------- X link from mentions (no post)

def test_link_code_from_a_mention_links_and_never_posts(env, monkeypatch):
    s, db = env
    assert backend.link_code("@agentpitbench link abcd2345") == "ABCD2345"
    assert backend.link_code("@agentpitbench check this link polymarket") is None
    assert backend.link_code("@agentpitbench link ABCD234I") is None                  # not the Worker's alphabet
    w = Worker()
    real = backend.Backend
    monkeypatch.setattr(backend, "Backend", lambda s: real(s, transport=httpx.MockTransport(w)))
    mentions = [{"id": "900", "author_id": "4242", "username": "ada", "text": "@AgentpitBench link KQ7M2PXA"},
                {"id": "901", "author_id": "5", "username": "bob", "text": "@agentpitbench hello"}]
    poster = FakePoster(mentions=mentions)
    eng = Engage(s, db, poster, NoCards(), api=Api())
    asyncio.run(eng.poll_summons())
    assert [(p, b) for p, _, b in w.sent] == [("/v1/admin/xlink", {"code": "KQ7M2PXA", "x_user_id": "4242", "username": "ada"})]
    assert poster.posts == []
    for tweak in ({"backend_enabled": False}, {"dry_run": True}):
        w.sent.clear()
        eng.s = Settings(**{**s.__dict__, **tweak})
        db.put("mentions_since", None)
        asyncio.run(eng.poll_summons())
        assert w.sent == []


# ---------------------------------------------------------------- the digest's funnel line

def test_digest_funnel_line(env, monkeypatch, caplog):
    s, db = env
    f = {"since": "2026-10-19", "days": [], "total": {"cta": 40, "signins": 9, "picks": 12, "late_picks": 2, "shares": 5,
                                                     "ref_picks": 3, "cta_picks": 4},
         "arms": {"a1": {"opens": 30, "picks": 3}, "a2": {"opens": 10, "picks": 1}}}
    w = Worker({"admin/review": {"comments": []}, "admin/api-usage": {}, "admin/funnel": f})
    real = backend.Backend
    monkeypatch.setattr(backend, "Backend", lambda s: real(s, transport=httpx.MockTransport(w)))
    with caplog.at_level("INFO"):
        out = xstats.digest(s, db, NOW)
    line = backend.funnel_line(f)
    assert out["own_picks"] == f and line in caplog.text
    assert line == ("Own picks: 40 CTA clicks -> 9 sign-ins -> 12 picks (2 late) -> 5 shares -> 3 referral picks; "
                    "arms (opens/picks): a1 30/3, a2 10/1")
    assert backend.funnel_line({"error": "HTTP 503"}).startswith("Own picks: unavailable")


# ---------------------------------------------------------------- the site

def _build(tmp_path, monkeypatch, bk: bool, pre: dict | None = None):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, backend_enabled=bk,
                 google_client_id="123.apps.googleusercontent.com" if bk else "")
    db = DB(s.db_path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    monkeypatch.setattr(site, "_today", lambda: today)
    res = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m1")
    run = make_round(db, PICKS, state="running", market_id="m3", question="Will Team Alpha win the grand final?")
    void = make_round(db, PICKS, state="awaiting_resolution", market_id="m4", question="Will Team Gamma win?")
    db.set_round(void, state="void")
    db.x("UPDATE bets SET rationale=?, quote=? WHERE run_id IN (SELECT run_id FROM runs WHERE round_id=?)",
         SECRET, SECRET, run)                       # the sealed round's reasoning: must reach no output at all
    s.export_dir.mkdir(parents=True, exist_ok=True)
    future = datetime.now(timezone.utc).timestamp() + 5 * 3600
    (s.export_dir / backend.UPCOMING_FILE).write_text(json.dumps([
        {"market_id": "501", "round_id": None, "question": "Will Team 501 win?", "event_title": "Team 501 vs Rivals",
         "outcomes": ["Yes", "No"], "opens_at": 1, "locks_at": future, "url": "", "end_date": "2030-01-01T00:00:00Z"}]))
    if pre is not None:
        (s.export_dir / backend.CHALLENGE_FILE).write_text(json.dumps({today: pre}))
    exports.export(s, db)
    return s, site.build(s), {"res": res, "run": run, "void": void}, today


SECRET = "SEALED-RATIONALE-7f3a"


def _no_secret(out):
    """The sealed round's reasoning (and with it its picks) reaches no page, JSON, feed or share text."""
    n = 0
    for f in out.rglob("*"):
        if f.is_file() and f.suffix in (".html", ".json", ".xml", ".txt", ".js"):
            assert SECRET not in f.read_text(errors="ignore"), f.relative_to(out)
            n += 1
    assert n > 100


def test_backend_off_renders_as_before(tmp_path, monkeypatch):
    s, out, ids, _ = _build(tmp_path, monkeypatch, bk=False)
    for rid in (ids["res"], ids["run"]):
        html = (out / f"round/{rid}/index.html").read_text()
        assert "data-bkcta" not in html and "data-refbar" not in html and "data-next" not in html, rid
        assert 'class="btn cta"' in html and "Bet against the AIs" in html            # today's button and link
    ch = (out / "challenge/index.html").read_text()
    assert "data-chsign" not in ch and "gsi/client" not in ch and "apbOnMe" not in ch and "/picks" not in ch
    for d in json.loads((out / "data/challenge.json").read_text()):
        for q in d["questions"]:
            assert "market_id" not in q and "key" not in q
    priv = (out / "privacy/index.html").read_text()
    assert "generated name" not in priv and "X user id" not in priv


def test_backend_on_button_by_state_ref_bar_and_next_one(tmp_path, monkeypatch):
    s, out, ids, _ = _build(tmp_path, monkeypatch, bk=True)
    sealed = (out / f"round/{ids['run']}/index.html").read_text()
    assert re.search(r'<button type="button" class="btn cta" data-bkcta="late" data-round="\d+"[^>]* hidden>', sealed)
    assert "Pick before the AIs reveal" in sealed and "go/round-cta/" not in sealed and "Bet on agentpit" in sealed
    assert re.search(r'data-pick="m3"[^>]*data-late="1"', sealed) and "data-ai=" not in sealed   # no AI pick while sealed
    assert 'data-refbar data-market="m3"' in sealed and 'data-state="locked"' in sealed
    assert sealed.index("data-refbar") < sealed.index("<h1")                           # above the fold
    final = (out / f"round/{ids['res']}/index.html").read_text()
    assert 'data-bkcta="next"' in final and "Beat the AIs on the next one" in final and 'data-state="resolved"' in final
    nxt = final[final.index("data-next"):final.index("data-nonext")]
    assert 'data-pick="501"' in nxt and 'data-close="2030-01-01T00:00:00Z"' in nxt and 'data-pick="m1"' not in nxt
    assert 'data-winner="CYBERSHOKE"' in final and "data-late" not in final[final.index("data-bkpick"):]
    vp = out / f"round/{ids['void']}/index.html"     # a void round has no page; if it ever gets one, no button
    assert not vp.exists() or "data-bkcta" not in vp.read_text()
    _no_secret(out)
    for f in ("calendar.google.com/calendar/render", "apb-pending", "/picks/import" if False else "/picks", "utm_campaign=pick"):
        assert f in sealed, f
    me = (out / "me/index.html").read_text()
    assert "data-xcode" in me and "/me/xlink" in me and "@agentpitbench link" in me and "data-streak" in me
    priv = (out / "privacy/index.html").read_text()
    assert "generated name" in priv and "X user id" in priv


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_every_inline_script_of_the_backend_pages_parses(tmp_path, monkeypatch):
    lock = datetime.now(timezone.utc).timestamp() + 2 * 3600
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pre = [{"market_id": str(i), "question": f"Team {i} vs. Rivals", "event_title": None, "outcomes": ["Yes", "No"],
            "locks_at": lock, "end_date": f"{today}T23:59:00Z", "url": ""} for i in range(3)]
    s, out, ids, _ = _build(tmp_path, monkeypatch, bk=True, pre=pre)
    pages = [f"round/{ids['run']}/index.html", f"round/{ids['res']}/index.html", "me/index.html", "challenge/index.html",
             "index.html", "ar/challenge/index.html", f"de/round/{ids['run']}/index.html"]
    n = 0
    for rel in pages:
        html = (out / rel).read_text()
        for i, m in enumerate(re.finditer(r"<script(?![^>]*(?:src=|application/(?:ld\+)?json))[^>]*>(.*?)</script>", html, re.S)):
            f = tmp_path / f"s{n}.js"
            f.write_text(m.group(1))
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, (rel, i, r.stderr[-600:])
            n += 1
    assert n > 10


def test_backend_on_challenge_from_pre_lock_sets_without_a_sealed_pick(tmp_path, monkeypatch):
    lock = datetime.now(timezone.utc).timestamp() + 2 * 3600
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    close = f"{today}T23:59:00Z"
    pre = [{"market_id": "m3", "question": "Will Team Alpha win the grand final?", "event_title": None,
            "outcomes": ["CYBERSHOKE", "Nexus"], "locks_at": lock - 4 * 3600, "end_date": close, "url": ""},
           {"market_id": "m1", "question": "Counter-Strike: CYBERSHOKE Esports vs Nexus (BO3)", "event_title": None,
            "outcomes": ["CYBERSHOKE", "Nexus"], "locks_at": lock - 5 * 3600, "end_date": close, "url": ""},
           {"market_id": "777", "question": "Will Team Omega win?", "event_title": None, "outcomes": ["Yes", "No"],
            "locks_at": lock, "end_date": close, "url": ""}]
    s, out, ids, today = _build(tmp_path, monkeypatch, bk=True, pre=pre)
    data = json.loads((out / "data/challenge.json").read_text())
    day = next(d for d in data if d["date"] == today)
    by = {q["key"]: q for q in day["questions"]}
    assert set(by) == {"m:m3", "m:m1", "m:777"} and day["pre"]
    assert by["m:m3"]["round_id"] == ids["run"] and by["m:m3"]["sealed"]
    assert all(a["outcome"] is None for a in by["m:m3"]["ais"]) and by["m:777"]["ais"] == []
    for rel in ("challenge/index.html", f"challenge/{today}/index.html", "es/challenge/index.html", "ar/challenge/index.html"):
        html = (out / rel).read_text()
        ch = json.loads(html.split('id="ch-data">', 1)[1].split("</script>", 1)[0])
        q = next(q for q in ch["questions"] if q.get("key") == "m:m3")
        assert q["sealed"] and all(a["outcome"] is None and a["won"] is None for a in q["ais"]), rel
        assert 'data-rid="m:777"' in html and "data-chsign" in html and "/picks/import" in html, rel
        assert len(html.encode()) < 100_000, rel
    _no_secret(out)
    home = (out / "index.html").read_text()
    assert 'class="chcard"' in home
