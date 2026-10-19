"""Anomalies, pause, alerts and incidents (docs/ai-automation-spec.md §10, §15.11). No network: a fake claude."""
import json
import time

import pytest

from agentpitbench import alerts, anomaly, cli, exports, logs, methodology, opai, site
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.twitter import Poster


@pytest.fixture
def s(tmp_path):
    s = Settings(data_dir=tmp_path / "var")
    methodology.apply(s, {})  # as config.load does: otherwise the drift detector sees unlocked settings
    return s


@pytest.fixture
def db(s):
    return DB(s.db_path)


@pytest.fixture(autouse=True)
def _quiet_xstats(monkeypatch):
    from agentpitbench import xstats
    monkeypatch.setattr(xstats, "visibility_drop", lambda db, now=None: None)
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    monkeypatch.setattr(logs, "journal", lambda since, until, unit="agentpitbench", limit=400: [])


def round_(db, mid=1, state="running", picks=("Yes", "No", None), infra=False):
    rid = db.create_round({"id": mid, "question": f"Will thing {mid} happen?", "outcomes_list": ["Yes", "No"],
                           "endDate": "2099-01-01T00:00:00Z"})
    for agent, pick in zip(("claude", "codex", "agy"), picks):
        run = db.create_run(rid, agent)
        db.record_bet(run, outcome=pick, avg_price=0.5 if pick else None, stake_filled=100 if pick else 0,
                      shares_filled=200 if pick else 0, quote="I like it" if pick else None)
        db.finish_run(run, "infra" if infra else ("bet" if pick else "timeout"), None, None)
    db.set_round(rid, state=state)
    return rid


def keys(found):
    return {a.detector for a in found}


def test_logs_clean_keeps_only_resolved_round():
    lines = ["round 5: claude bet Yes @ 0.62", "round 5 started for market 9", "round 4 resolved: Yes",
             "[outbox] r5-reveal: picks", "[outbox] r4-results: done", "grok bet No @ 0.4 (round 4)",
             "token=abcdefghijklmnopqrstuvwxyz0123456789"]
    assert logs.scrub(lines, {5}) == ["round 4 resolved: Yes", "[outbox] r4-results: done",
                                      "token=abcdefghijklmnopqrstuvwxyz0123456789"]


def test_logs_clean_redacts(db, monkeypatch):
    monkeypatch.setenv("X_API_SECRET", "supersecretvalue123")
    rid = round_(db, 7, state="running")
    out = logs.clean([f"round {rid} picked", "key supersecretvalue123 used", "api_key=abcdef0123456789"], db)
    assert out == ["key [redacted] used", "[redacted]"]


def test_embargo_breach_detail_has_no_values(s, db):
    rid = round_(db, state="running")
    s.export_dir.mkdir(parents=True)
    rec = {"round_id": rid, "entries": [{"outcome": "Yes", "quote": "I like it", "transcript": None}]}
    (s.export_dir / "rounds.json").write_text(json.dumps([rec]))
    found = anomaly.detect(s, db)
    a = next(x for x in found if x.detector == "embargo-breach")
    assert a.severity == "critical" and a.detail["fields"] == ["outcome", "quote"]
    assert "Yes" not in json.dumps(a.detail) and "like" not in json.dumps(a.detail)


def test_real_export_of_running_round_is_clean(s, db):
    round_(db, state="running")
    exports.export(s, db)
    assert "embargo-breach" not in keys(anomaly.detect(s, db))


def test_built_site_of_running_round_is_clean(s, db):
    round_(db, state="running")
    exports.export(s, db)
    site.build(s)
    assert "embargo-breach" not in keys(anomaly.detect(s, db))


def test_embargo_breach_by_tweet(s, db):
    rid = round_(db, state="running")
    db.x("INSERT INTO tweets(post_key,tweet_id,kind,round_id,text,posted_at) VALUES('k','dry-k','reveal',?,'t',?)",
         rid, time.time())
    assert "embargo-breach" in keys(anomaly.detect(s, db))


def test_forecast_leak(s, db):
    d = s.site_dir / "data" / "forecasts"
    d.mkdir(parents=True)
    (d / "2099-01-01.json").write_text("{}")
    a = next(x for x in anomaly.detect(s, db) if x.detector == "forecast-leak")
    assert a.detail == {"date": "2099-01-01", "where": "site/data/forecasts"}


def test_result_edit(s, db):
    rid = round_(db, state="awaiting_resolution")
    run = db.q("SELECT run_id FROM runs WHERE round_id=?", rid)[0]["run_id"]
    db.settle_bet(run, 200.0, 100.0)
    assert "result-edit" not in keys(anomaly.detect(s, db))
    db.settle_bet(run, 0.0, -100.0)  # a second settle is a no-op
    assert "result-edit" not in keys(anomaly.detect(s, db))
    db.x("UPDATE bets SET pnl=150 WHERE run_id=?", run)
    assert "result-edit" in keys(anomaly.detect(s, db))


def test_secret_in_transcript_detail_has_no_secret(s, db, monkeypatch):
    anomaly._scanned.clear()
    d = s.site_dir / "transcripts" / "3"
    d.mkdir(parents=True)
    (d / "claude.txt").write_text("thinking\nfound sk-ant-abcdefghijklmnopqrstuvwxyz012345 in env\n")
    (d / "codex.txt").write_text("tx 0x" + "ab" * 32 + " token 1234567890123456789012345678901234567890\n"
                                 "the elon-musk-of-tweets-market is open\n")
    found = [x for x in anomaly.detect(s, db) if x.detector == "secret-in-transcript"]
    assert len(found) == 1 and found[0].detail == {"path": "transcripts/3/claude.txt", "pattern": "secret-shape",
                                                   "line": 2}
    monkeypatch.setenv("AGENTPIT_KEY_GROK", "plainvaluewithoutshape99")
    (d / "grok.txt").write_text("my key is plainvaluewithoutshape99\n")
    found = [x for x in anomaly.detect(s, db) if x.detector == "secret-in-transcript"]
    assert {f.detail["pattern"] for f in found} == {"secret-shape", "env-secret-value"}
    assert "plainvalue" not in json.dumps([f.detail for f in found])


def test_double_post(s, db):
    now = time.time()
    for k, tid in (("a", "111"), ("b", "222"), ("c", "dry-c")):
        db.x("INSERT INTO tweets(post_key,tweet_id,kind,text,posted_at) VALUES(?,?,'results','same',?)", k, tid, now)
    found = [x for x in anomaly.detect(s, db) if x.detector == "double-post"]
    assert len(found) == 1 and found[0].detail["post_keys"] == ["a", "b"]
    db.x("DELETE FROM tweets")
    for k, rid in (("r22-split", 22), ("r24-split", 24)):  # generic copy shared by two rounds is fine
        db.x("INSERT INTO tweets(post_key,tweet_id,kind,round_id,text,posted_at) VALUES(?,?,'agree',?,'same',?)",
             k, k, rid, now)
    assert "double-post" not in keys(anomaly.detect(s, db))


def test_post_burst_excludes_reports(s, db):
    s.anomaly_posts_per_hour = 3
    now = time.time()
    for i in range(6):
        db.x("INSERT INTO tweets(post_key,tweet_id,kind,text,posted_at) VALUES(?,?,'report',?,?)", f"r{i}", str(i),
             f"t{i}", now)
    assert "double-post" not in keys(anomaly.detect(s, db))
    for i in range(4):
        db.x("INSERT INTO tweets(post_key,tweet_id,kind,text,posted_at) VALUES(?,?,'race',?,?)", f"x{i}",
             str(10 + i), f"u{i}", now)
    assert "double-post" in keys(anomaly.detect(s, db))


def test_void_burst_pauses_rounds_only(s, db):
    for mid in (1, 2):
        rid = round_(db, mid, state="void", infra=True)
        db.set_round(rid, exclusion="infra", resolved_at=time.time())
    a = next(x for x in anomaly.detect(s, db) if x.detector == "void-burst")
    anomaly.raise_(s, db, a)
    assert opai.paused(s, db, "rounds") and not opai.paused(s, db, "posting")


def test_methodology_drift_pauses_rounds_only(s, db, monkeypatch):
    monkeypatch.setattr(methodology, "verify", lambda s: ["hash drift: prompt.txt"])
    a = next(x for x in anomaly.detect(s, db) if x.detector == "methodology-drift")
    anomaly.raise_(s, db, a)
    assert opai.paused(s, db, "rounds") and not opai.paused(s, db, "posting")
    assert "rehash" in db.one("SELECT resume FROM alerts")["resume"]


def test_no_bet_warnings(s, db):
    for mid in (1, 2, 3):
        round_(db, mid, state="awaiting_resolution", picks=("Yes", None, None))
    found = keys(anomaly.detect(s, db))
    assert "agent-no-bet-streak" in found and "all-no-bet" not in found
    round_(db, 4, state="awaiting_resolution", picks=(None, None, None))
    assert "all-no-bet" in keys(anomaly.detect(s, db))


def test_x_and_opai_warnings(s, db, monkeypatch):
    from agentpitbench import xstats
    monkeypatch.setattr(xstats, "visibility_drop", lambda db, now=None: {"kind": "results", "posts": 6, "baseline": 900})
    monkeypatch.setattr(xstats, "health", lambda db, now=None: {"consecutive_auth_failures": 3})
    monkeypatch.setattr(xstats, "reads", lambda db, day=None, s=None: {"usd": 9.0})
    for _ in range(5):
        db.x("INSERT INTO ai_calls(task,day,started_at,status) VALUES('rate','d',?, 'crash')", time.time())
    found = keys(anomaly.detect(s, db))
    assert {"visibility-drop", "x-read-failures", "x-read-spend", "opai-failing"} <= found
    a = next(x for x in anomaly.detect(s, db) if x.detector == "visibility-drop")
    anomaly.raise_(s, db, a, improve=False)
    assert not opai.paused(s, db, "posting")
    assert db.one("SELECT COUNT(*) n FROM incident_drafts")["n"] == 1  # warn, but drafted


def test_deploy_stale(s, db):
    s.dry_run = False
    assert "deploy-stale" not in keys(anomaly.detect(s, db)) and db.get("site_deployed_at")  # clock starts
    rid = round_(db, state="awaiting_resolution")
    db.set_round(rid, started_at=time.time() - 4 * 3600)
    db.put("site_deployed_at", time.time() - 5 * 3600)
    assert "deploy-stale" in keys(anomaly.detect(s, db))
    db.put("site_deployed_at", time.time() - 3 * 3600)
    assert "deploy-stale" not in keys(anomaly.detect(s, db))


def test_critical_pauses_before_model_call_drafts_and_alerts(s, db, fake_claude):
    s.opai_enabled = True
    fake_claude.respond({"title": "Picks exposed early", "what": "Round 1 data appeared early.",
                         "fix": "Under investigation."})
    fake_claude.on_call(f"""
import sqlite3, json
c = sqlite3.connect({str(s.db_path)!r})
v = c.execute("SELECT v FROM kv WHERE k='pause'").fetchone()
result = {{"pause_at_call": json.loads(v[0]) if v else None}}
""")
    rid = round_(db, state="running")
    s.export_dir.mkdir(parents=True)
    (s.export_dir / "rounds.json").write_text(json.dumps([{"round_id": rid, "entries": [{"outcome": "Yes"}]}]))
    new = anomaly.tick(s, db)
    assert [a.detector for a in new] == ["embargo-breach"]
    assert fake_claude.calls[0]["pause_at_call"]["posting"] is True
    assert "Yes" not in fake_claude.calls[0]["stdin"].split("<data")[1]
    d = db.one("SELECT * FROM incident_drafts")
    assert d["title"] == "Picks exposed early" and d["output_id"] and d["state"] == "draft"
    files = list((s.data_dir / "alerts").glob("*.json"))
    assert len(files) == 1 and not list((s.data_dir / "alerts").glob(".tmp-*.part"))
    al = json.loads(files[0].read_text())
    assert al["severity"] == "critical" and al["kind"] == "embargo-breach"
    assert al["action_taken"] == "paused posting and new rounds" and "ops resume" in al["how_to_resume"]
    assert al["incident_draft"].endswith(f"{d['draft_id']}.json") and al["ts"] and al["what_happened"]
    assert anomaly.tick(s, db) == []  # raised once per key
    assert alerts.ack(db, al["id"]) and json.loads(files[0].read_text())["acked"] is True
    assert alerts.recent(db, unacked=True) == []


def test_incident_draft_rejects_ungrounded_numbers(s, db, fake_claude):
    s.opai_enabled = True
    fake_claude.respond({"title": "12 posts", "what": "x", "fix": "Under investigation."})
    a = anomaly.Anomaly("forecast-leak:2099-01-01", "forecast-leak", "critical", {"date": "2099-01-01"})
    anomaly.raise_(s, db, a)
    d = db.one("SELECT * FROM incident_drafts")
    assert d["output_id"] is None and d["title"] == anomaly.TEMPLATES["forecast-leak"][0]


def test_paused_post_records_nothing(s, db):
    p = Poster(s, db)
    anomaly.pause(db, posting=True, rounds=False, reason="drill")
    import asyncio
    assert asyncio.run(p.post("r1-reveal", "reveal", "hello")) is None
    assert db.one("SELECT COUNT(*) n FROM tweets")["n"] == 0
    assert not s.outbox_dir.exists() or not list(s.outbox_dir.iterdir())


def test_static_flag_also_records_nothing(s, db):
    import asyncio
    s.posting_paused = True
    assert asyncio.run(Poster(s, db).post("k", "race", "hi")) is None
    assert db.one("SELECT COUNT(*) n FROM tweets")["n"] == 0


def test_rounds_pause_and_resume(s, db):
    assert not cli.rounds_paused(s, db)
    anomaly.pause(db, posting=True, rounds=True, reason="drill")
    assert cli.rounds_paused(s, db)
    db.x("INSERT INTO anomalies(key,detector,severity,detail_json,detected_at) VALUES('k','d','critical','{}',1)")
    with pytest.raises(anomaly.ResumeError):
        anomaly.resume(db, posting=True, rounds=True, isatty=lambda: False)
    assert cli.rounds_paused(s, db)
    anomaly.resume(db, posting=True, rounds=False, isatty=lambda: True)
    assert cli.rounds_paused(s, db) and not opai.paused(s, db, "posting")
    anomaly.resume(db, posting=False, rounds=True, isatty=lambda: False)  # rounds: no terminal needed
    assert not cli.rounds_paused(s, db)
    assert db.one("SELECT state FROM anomalies")["state"] == "acknowledged"


def test_published_incident_renders(s, db, tmp_path, monkeypatch):
    monkeypatch.setattr(methodology, "_fill_or_placeholder", lambda ids: {})
    did = db.x("INSERT INTO incident_drafts(date,title,what,fix) VALUES('2026-10-12','Drill incident',"
               "'A drill happened.','Under investigation.')")
    inc = tmp_path / "incidents.json"
    inc.write_text("[]")
    anomaly.publish_incident(s, db, did, path=inc)
    assert json.loads(inc.read_text())[0]["title"] == "Drill incident"
    exports.export(s, db)
    out = site.build(s)
    html = (out / "incidents/index.html").read_text()
    assert "Drill incident" in html and html.index("Drill incident") < html.index("Rounds 2")
    assert "Drill incident" in (out / "de/incidents/index.html").read_text()  # English fallback


def test_only_opening_and_closing_posts_go_out(s, db):
    """Operator 2026-10-13: two posts per market (reveal + results), no replies and no other kinds."""
    import asyncio
    s.post_kinds = ["reveal", "results"]
    p = Poster(s, db)
    assert asyncio.run(p.post("r1-reveal", "reveal", "opening"))
    assert asyncio.run(p.post("r1-results", "results", "closing", quote_of="1"))
    assert asyncio.run(p.post("r1-press-0", "press", "statement", reply_to="1")) is None
    assert asyncio.run(p.post("r1-late", "results", "a reply", reply_to="1")) is None   # a reply even of an allowed kind
    assert asyncio.run(p.post("awards-1", "awards", "weekly")) is None
    assert [r["kind"] for r in db.q("SELECT kind FROM tweets ORDER BY posted_at")] == ["reveal", "results"]


def test_rounds_pause_expires_with_an_alert(s, db):
    anomaly.pause(db, posting=False, rounds=True, reason="methodology switch")
    since = db.get("pause")["since"]
    assert not anomaly.expire_rounds_pause(s, db, since + 5 * 3600)
    assert db.get("pause")["rounds"]
    assert anomaly.expire_rounds_pause(s, db, since + 6 * 3600 + 1)
    assert db.get("pause") == {}
    assert db.one("SELECT 1 FROM alerts WHERE kind='pause-expired'")


def test_posting_pause_outlives_the_rounds_pause(s, db):
    anomaly.pause(db, posting=True, rounds=True, reason="embargo-breach (anomaly #1)")
    since = db.get("pause")["since"]
    assert anomaly.expire_rounds_pause(s, db, since + 7 * 3600)
    p = db.get("pause")
    assert p["posting"] and not p["rounds"]


def test_rounds_pause_expiry_off(s, db):
    s.pause_rounds_max_h = 0
    anomaly.pause(db, posting=False, rounds=True, reason="x")
    assert not anomaly.expire_rounds_pause(s, db, time.time() + 30 * 86400)
    assert db.get("pause")["rounds"]
