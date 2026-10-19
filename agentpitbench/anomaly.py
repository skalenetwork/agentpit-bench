"""Anomalies: detect, pause, draft, alert (docs/ai-automation-spec.md §10).

Detectors are pure functions of the DB and the files the bench writes. Code decides everything; the operator
AI only improves the incident draft's wording. A critical anomaly pauses posting and/or new rounds in the same
tick, before any model call; the pause stays until the operator runs `agentpitbench ops resume`.

Values never travel: detail_json carries field names, ids and positions, never a leaked pick, quote or secret.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import alerts
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

ROUNDS_ONLY = {"methodology-drift", "void-burst"}   # critical, but only new rounds pause
SEALED_FIELDS = ("outcome", "quote", "transcript")
# narrow, high-precision secret shapes. Transcripts are published raw, so the broad virality._SECRET (any
# 32+ char run, 0x hashes, UUIDs) would match every on-chain tx hash and token id an agent prints.
SECRET_SHAPES = re.compile(
    r"((?<![\w-])sk-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"(?i:(api[_-]?key|secret|password|passwd|authorization|bearer)\s*[:=]\s*[A-Za-z0-9_\-+/=.]{16,}))")


@dataclass
class Anomaly:
    key: str
    detector: str
    severity: str           # critical / warn
    detail: dict = field(default_factory=dict)


class ResumeError(RuntimeError):
    pass


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- detectors

def embargo_breach(s: Settings, db: DB, now: float) -> list[Anomaly]:
    running = [r["round_id"] for r in db.q("SELECT round_id FROM rounds WHERE state='running'")]
    if not running:
        return []
    out = []
    exported: dict[int, dict] = {}
    for where in (s.export_dir / "rounds.json", s.site_dir / "data" / "rounds.json"):
        try:
            for r in json.loads(where.read_text()):
                if r.get("round_id") in running:
                    exported.setdefault(r["round_id"], {})[str(where.relative_to(s.data_dir))] = r
        except (OSError, ValueError):
            continue
    for rid in running:
        for where, r in exported.get(rid, {}).items():
            fields = sorted({f for e in r.get("entries", []) for f in SEALED_FIELDS if e.get(f)})
            if fields:
                out.append(Anomaly(f"embargo-breach:{rid}", "embargo-breach", "critical",
                                   {"round_id": rid, "fields": fields, "where": where}))
        page = s.site_dir / "round" / str(rid) / "index.html"
        if page.exists() and f"transcripts/{rid}/" in page.read_text(errors="replace"):
            out.append(Anomaly(f"embargo-breach:{rid}", "embargo-breach", "critical",
                               {"round_id": rid, "fields": ["transcript"], "where": f"site/round/{rid}/index.html"}))
        kinds = [t["kind"] for t in db.q("SELECT kind FROM tweets WHERE round_id=? AND kind IN "
                                          "('reveal','decision','split')", rid)]
        if kinds:
            out.append(Anomaly(f"embargo-breach:{rid}", "embargo-breach", "critical",
                               {"round_id": rid, "fields": sorted(set(kinds)), "where": "tweets"}))
    return out


def forecast_leak(s: Settings, db: DB, now: float) -> list[Anomaly]:
    from .forecast import revealable_dates
    ok = revealable_dates(db)
    out = []
    fdir = s.site_dir / "data" / "forecasts"
    if fdir.exists():
        for f in fdir.glob("*.json"):
            if f.stem not in ok:
                out.append(Anomaly(f"forecast-leak:{f.stem}", "forecast-leak", "critical",
                                   {"date": f.stem, "where": "site/data/forecasts"}))
    tdir = s.site_dir / "transcripts" / "sweep"
    if tdir.exists():
        for d in tdir.iterdir():
            if d.is_dir() and d.name not in ok:
                out.append(Anomaly(f"forecast-leak:{d.name}", "forecast-leak", "critical",
                                   {"date": d.name, "where": "site/transcripts/sweep"}))
    return out


def result_edit(s: Settings, db: DB, now: float) -> list[Anomaly]:
    out = []
    for r in db.q("SELECT k, v FROM kv WHERE k LIKE 'settled\\_%' ESCAPE '\\'"):
        run_id = int(r["k"].split("_", 1)[1])
        rec = json.loads(r["v"])
        b = db.one("SELECT payout, pnl FROM bets WHERE run_id=?", run_id)
        if b is None or b["payout"] != rec["payout"] or b["pnl"] != rec["pnl"]:
            out.append(Anomaly(f"result-edit:{run_id}", "result-edit", "critical",
                               {"run_id": run_id, "fields": ["payout", "pnl"]}))
    return out


_scanned: dict[str, float] = {}


def secret_in_transcript(s: Settings, db: DB, now: float) -> list[Anomaly]:
    from .orchestrator import SECRET_PREFIXES
    values = [v for k, v in os.environ.items() if k.startswith(SECRET_PREFIXES) and len(v) >= 12]
    root = s.site_dir / "transcripts"
    if not root.exists():
        return []
    out = []
    for f in root.rglob("*.txt"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        key = str(f)
        if _scanned.get(key) == mt:
            continue
        text = f.read_text(errors="replace")
        hit = None
        for i, line in enumerate(text.splitlines(), 1):
            if SECRET_SHAPES.search(line):
                hit = {"path": str(f.relative_to(s.site_dir)), "pattern": "secret-shape", "line": i}
                break
            if any(v in line for v in values):
                hit = {"path": str(f.relative_to(s.site_dir)), "pattern": "env-secret-value", "line": i}
                break
        if hit:
            out.append(Anomaly(f"secret-in-transcript:{hit['path']}", "secret-in-transcript", "critical", hit))
        else:
            _scanned[key] = mt
    return out


def double_post(s: Settings, db: DB, now: float) -> list[Anomaly]:
    out = []
    # same text for the same round (or both without a round): different rounds legitimately share generic
    # copy ("They all agree. Claude: No, ..." posted for rounds 22 and 24 on 2026-10-11)
    for r in db.q("SELECT a.post_key k1, b.post_key k2 FROM tweets a JOIN tweets b ON a.text=b.text AND "
                  "a.post_key<b.post_key AND a.round_id IS b.round_id WHERE a.tweet_id NOT LIKE 'dry-%' AND b.tweet_id NOT LIKE 'dry-%' AND "
                  "a.posted_at>? AND b.posted_at>? AND ABS(a.posted_at-b.posted_at)<86400",
                  now - 86400 * 2, now - 86400 * 2):
        out.append(Anomaly(f"double-post:{r['k1']}:{r['k2']}", "double-post", "critical",
                           {"post_keys": [r["k1"], r["k2"]], "why": "identical text within 24 h"}))
    n = db.one("SELECT COUNT(*) n FROM tweets WHERE tweet_id NOT LIKE 'dry-%' AND posted_at>? AND "
               "COALESCE(kind,'')!='report'", now - 3600)["n"]
    if n > s.anomaly_posts_per_hour:
        out.append(Anomaly(f"double-post:burst:{datetime.fromtimestamp(now, timezone.utc):%Y-%m-%dT%H}",
                           "double-post", "critical", {"posts_last_hour": n, "limit": s.anomaly_posts_per_hour}))
    return out


def void_burst(s: Settings, db: DB, now: float) -> list[Anomaly]:
    rows = db.q("SELECT round_id FROM rounds WHERE exclusion='infra' AND resolved_at>? ORDER BY round_id",
                now - 6 * 3600)
    if len(rows) >= 2:
        ids = [r["round_id"] for r in rows]
        return [Anomaly(f"void-burst:{ids[-1]}", "void-burst", "critical", {"round_ids": ids})]
    return []


def methodology_drift(s: Settings, db: DB, now: float) -> list[Anomaly]:
    from . import methodology
    try:
        problems = methodology.verify(s)
    except Exception as e:  # an unreadable lock is drift too
        problems = [f"lock unreadable: {type(e).__name__}"]
    if not problems:
        return []
    import hashlib
    h = hashlib.sha256("\n".join(problems).encode()).hexdigest()[:12]
    return [Anomaly(f"methodology-drift:{h}", "methodology-drift", "critical", {"problems": problems})]


def all_no_bet(s: Settings, db: DB, now: float) -> list[Anomaly]:
    out = []
    for r in db.q("SELECT round_id FROM rounds WHERE state IN ('awaiting_resolution','resolved') AND "
                  "COALESCE(exclusion,'')!='infra' AND started_at>?", now - 3 * 86400):
        bets = db.q("SELECT b.outcome FROM runs JOIN bets b USING(run_id) WHERE runs.round_id=?", r["round_id"])
        runs = db.one("SELECT COUNT(*) n FROM runs WHERE round_id=?", r["round_id"])["n"]
        if runs and not any(b["outcome"] for b in bets):
            out.append(Anomaly(f"all-no-bet:{r['round_id']}", "all-no-bet", "warn", {"round_id": r["round_id"]}))
    return out


def agent_no_bet_streak(s: Settings, db: DB, now: float) -> list[Anomaly]:
    out = []
    for a in s.agents:
        rows = db.q("SELECT ro.round_id, b.outcome FROM runs r JOIN rounds ro USING(round_id) LEFT JOIN bets b "
                    "USING(run_id) WHERE r.agent=? AND ro.state IN ('awaiting_resolution','resolved') "
                    "ORDER BY ro.round_id DESC LIMIT 3", a)
        if len(rows) == 3 and not any(r["outcome"] for r in rows):
            out.append(Anomaly(f"agent-no-bet-streak:{a}:{rows[0]['round_id']}", "agent-no-bet-streak", "warn",
                               {"agent": a, "round_ids": [r["round_id"] for r in rows]}))
    return out


def _xstats():
    from . import xstats
    return xstats


def visibility_drop(s: Settings, db: DB, now: float) -> list[Anomaly]:
    d = _xstats().visibility_drop(db, now)
    if not d:
        return []
    return [Anomaly(f"visibility-drop:{d.get('kind')}:{_day(now)}", "visibility-drop", "warn",
                    {k: d[k] for k in ("kind", "posts", "baseline") if k in d})]


def x_read_failures(s: Settings, db: DB, now: float) -> list[Anomaly]:
    n = _xstats().health(db, now).get("consecutive_auth_failures", 0)
    if n >= 3:
        return [Anomaly(f"x-read-failures:{_day(now)}", "x-read-failures", "warn", {"consecutive": n})]
    return []


def x_read_spend(s: Settings, db: DB, now: float) -> list[Anomaly]:
    r = _xstats().reads(db, _day(now), s)
    if r.get("usd", 0) > s.x_read_alert_usd:
        return [Anomaly(f"x-read-spend:{_day(now)}", "x-read-spend", "warn",
                        {"usd": r["usd"], "limit": s.x_read_alert_usd})]
    return []


def deploy_stale(s: Settings, db: DB, now: float) -> list[Anomaly]:
    if s.dry_run:
        return []
    last = db.get("site_deployed_at")
    if last is None:  # first run with this detector: start the clock instead of alerting on history
        db.put("site_deployed_at", now)
        return []
    changed = db.one("SELECT MAX(MAX(started_at), COALESCE(MAX(resolved_at),0)) t FROM rounds")["t"] or 0
    if changed > last and now - max(last, changed) > 2 * 3600:
        return [Anomaly(f"deploy-stale:{int(changed)}", "deploy-stale", "warn",
                        {"last_deploy": last, "rounds_changed_at": changed})]
    return []


def opai_failing(s: Settings, db: DB, now: float) -> list[Anomaly]:
    n = db.one("SELECT COUNT(*) n FROM ai_calls WHERE started_at>? AND status IN "
               "('quota','auth','timeout','invalid','crash')", now - 6 * 3600)["n"]
    if n >= 5:
        return [Anomaly(f"opai-failing:{_day(now)}", "opai-failing", "warn", {"failed_calls_6h": n})]
    return []


def market_name_missing(s: Settings, db: DB, now: float) -> list[Anomaly]:
    """Round posts (reveal, results, motd) must carry the market name in full (docs/longtail-search-spec.md §7).
    Only posts after the kv marker market_name_since (set on the first run after the deploy) count."""
    import html
    from .twitter import market_name
    since = db.get("market_name_since")
    if since is None:
        db.put("market_name_since", now)
        return []
    out = []
    for table in ("tweets", "bsky_posts", "tg_posts", "threads_posts"):
        rows = db.q(f"SELECT p.post_key, p.text, r.question, r.event_title FROM {table} p JOIN rounds r USING(round_id) "
                    "WHERE p.kind IN ('reveal','results','motd') AND p.posted_at>? AND p.posted_at>?",
                    float(since), now - 86400)
        for r in rows:
            name = market_name({"question": r["question"], "event_title": r["event_title"]})
            if name and name not in html.unescape(r["text"] or ""):
                out.append(Anomaly(f"market-name-missing:{table}:{r['post_key']}", "market-name-missing", "warn",
                                   {"post_key": r["post_key"], "table": table}))
    return out


DETECTORS = [embargo_breach, forecast_leak, result_edit, secret_in_transcript, double_post, void_burst,
             methodology_drift, all_no_bet, agent_no_bet_streak, visibility_drop, x_read_failures, x_read_spend,
             deploy_stale, opai_failing, market_name_missing]


def detect(s: Settings, db: DB, now: float | None = None) -> list[Anomaly]:
    now = time.time() if now is None else now
    found: dict[str, Anomaly] = {}
    for fn in DETECTORS:
        try:
            for a in fn(s, db, now):
                found.setdefault(a.key, a)
        except Exception:
            log.exception("anomaly detector %s failed", fn.__name__)
    return list(found.values())


# ---------------------------------------------------------------- pause

def pause(db: DB, *, posting: bool, rounds: bool, reason: str, anomaly_id: int | None = None) -> dict:
    cur = db.get("pause") or {}
    p = {"posting": bool(cur.get("posting")) or posting, "rounds": bool(cur.get("rounds")) or rounds,
         "reason": reason if (posting or rounds) else cur.get("reason"), "since": cur.get("since") or time.time(),
         "anomaly_id": anomaly_id if anomaly_id is not None else cur.get("anomaly_id")}
    db.put("pause", p)
    log.critical("PAUSED (posting=%s, rounds=%s): %s", p["posting"], p["rounds"], reason)
    return p


def resume(db: DB, *, posting: bool, rounds: bool, isatty=None) -> dict:
    """Open anomalies become acknowledged. Resuming new rounds works from anywhere (rounds always keep
    running); resuming posting is operator only, at a terminal."""
    if posting and not (isatty or sys.stdin.isatty)():
        raise ResumeError("resuming posting needs an interactive terminal")
    cur = db.get("pause") or {}
    p = {**cur, "posting": bool(cur.get("posting")) and not posting, "rounds": bool(cur.get("rounds")) and not rounds}
    if not p["posting"] and not p["rounds"]:
        p = {}
    db.put("pause", p)
    db.x("UPDATE anomalies SET state='acknowledged' WHERE state='open'")
    log.warning("resumed by the operator (posting=%s, rounds=%s)", posting, rounds)
    return p


# ---------------------------------------------------------------- raise: pause, draft, alert

TEMPLATES = {
    "embargo-breach": ("Sealed picks exposed early", "A pick, quote or transcript of a round still in progress "
                       "appeared in a public file or post before every agent had finished."),
    "forecast-leak": ("Daily forecasts exposed early", "Sealed daily forecasts or sweep transcripts appeared on the "
                      "site before their markets had resolved."),
    "result-edit": ("Recorded result changed", "A settled bet's recorded payout or profit changed after settlement."),
    "secret-in-transcript": ("Credential pattern in a transcript", "A published transcript contained text shaped "
                             "like a credential."),
    "double-post": ("Duplicate or excess posts", "The bench account posted duplicate text or more posts in an hour "
                    "than the safety limit allows."),
    "void-burst": ("Several rounds voided", "Several rounds were voided by infrastructure failures within six hours."),
    "methodology-drift": ("Methodology files changed", "Files covered by the methodology lock changed without an "
                          "approved methodology version."),
    "visibility-drop": ("Lower reach on X", "Recent posts of one kind reached far fewer people than usual."),
}
FALLBACK_TEMPLATE = ("Operational issue", "An automated check flagged an operational issue.")
ACTIONS = {"both": "paused posting and new rounds", "rounds": "paused new rounds (posting continues)",
           "none": "alert only; nothing paused"}


def drafts_dir(s: Settings) -> Path:
    return s.data_dir / "incident_drafts"


def _write_draft_file(s: Settings, db: DB, draft_id: int) -> str:
    r = dict(db.one("SELECT * FROM incident_drafts WHERE draft_id=?", draft_id))
    p = drafts_dir(s) / f"{draft_id}.json"
    alerts._atomic_write(p, json.dumps(r, indent=1, ensure_ascii=False))
    return str(p)


def raise_(s: Settings, db: DB, a: Anomaly, *, improve: bool = True) -> int | None:
    """Idempotent by key. Returns the new anomaly id, or None if already raised."""
    now = time.time()
    cur = db.conn.execute("INSERT OR IGNORE INTO anomalies(key,detector,severity,detail_json,detected_at)"
                          " VALUES(?,?,?,?,?)", (a.key, a.detector, a.severity, json.dumps(a.detail), now))
    if cur.rowcount == 0:
        return None
    aid = cur.lastrowid
    action = "none"
    if a.severity == "critical":  # first, in this tick, before any model call
        rounds_only = a.detector in ROUNDS_ONLY
        pause(db, posting=not rounds_only, rounds=True, reason=f"{a.detector} (anomaly #{aid})", anomaly_id=aid)
        action = "rounds" if rounds_only else "both"
    draft_path = None
    if a.severity == "critical" or a.detector == "visibility-drop":
        title, what = TEMPLATES.get(a.detector, FALLBACK_TEMPLATE)
        did = db.x("INSERT INTO incident_drafts(anomaly_id,date,title,what,fix) VALUES(?,?,?,?,?)", aid,
                   datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"), title, what,
                   "Under investigation.")
        draft_path = _write_draft_file(s, db, did)
        if improve:
            try:
                improve_draft(s, db, did, a, now)
                draft_path = _write_draft_file(s, db, did)
            except Exception:
                log.exception("incident draft improvement failed; the template draft stands")
    resume_hint = {"both": "agentpitbench ops resume (after checking; resumes posting and new rounds)",
                   "rounds": "agentpitbench ops resume --rounds" + (
                       " (after `agentpitbench method propose --rehash` + approve, or reverting the change)"
                       if a.detector == "methodology-drift" else ""),
                   "none": ""}[action]
    alerts.send(s, db, a.severity, a.detector, TEMPLATES.get(a.detector, FALLBACK_TEMPLATE)[0],
                f"{TEMPLATES.get(a.detector, FALLBACK_TEMPLATE)[1]} Detail: {json.dumps(a.detail)}",
                action=ACTIONS[action], resume=resume_hint, draft_path=draft_path)
    return aid


def improve_draft(s: Settings, db: DB, draft_id: int, a: Anomaly, at: float) -> bool:
    from . import logs, opai
    lines = logs.excerpt(db, at)
    payload = {"detector": a.detector, "detail": a.detail, "log_lines": lines}
    allowed = set(re.findall(r"\d+", json.dumps(a.detail) + "\n".join(lines)))

    def validate(d: dict) -> str | None:
        if len(d["title"]) > 60 or len(d["what"]) > 400 or len(d["fix"]) > 300:
            return "too long"
        for k in ("title", "what", "fix"):
            bad = [n for n in re.findall(r"\d+", d[k]) if n not in allowed]
            if bad:
                return f"ungrounded number in {k}"
        return None

    r = opai.call(s, db, "incident", payload, subject=f"anomaly:{a.key}", validate=validate)
    if not r:
        return False
    db.x("UPDATE incident_drafts SET title=?, what=?, fix=?, output_id=? WHERE draft_id=? AND state='draft'",
         r.data["title"], r.data["what"], r.data["fix"], r.output_id, draft_id)
    return True


def expire_rounds_pause(s: Settings, db: DB, now: float | None = None) -> bool:
    """Rounds never sit paused for long: a rounds pause older than pause_rounds_max_h lifts itself and the
    operator gets an alert. A posting pause stays until the operator resumes it at a terminal."""
    now = time.time() if now is None else now
    cur = db.get("pause") or {}
    if s.pause_rounds_max_h <= 0 or not cur.get("rounds") or now - (cur.get("since") or now) < s.pause_rounds_max_h * 3600:
        return False
    p = {**cur, "rounds": False} if cur.get("posting") else {}
    db.put("pause", p)
    hours = (now - cur["since"]) / 3600
    log.warning("rounds pause expired after %.1f h (%s); new rounds resumed", hours, cur.get("reason"))
    alerts.send(s, db, "warn", "pause-expired", "New rounds resumed automatically",
                f"New rounds were paused for {hours:.1f} h ({cur.get('reason')}); a rounds pause lifts itself "
                f"after {s.pause_rounds_max_h:g} h." + (" Posting stays paused." if p else ""),
                action="new rounds resumed", resume="agentpitbench ops pause --rounds to hold them again")
    return True


def tick(s: Settings, db: DB, now: float | None = None) -> list[Anomaly]:
    """Lift an expired rounds pause, then run every detector; raise the new ones. Returns the anomalies raised
    this tick."""
    expire_rounds_pause(s, db, now)
    new = []
    for a in detect(s, db, now):
        if raise_(s, db, a):
            new.append(a)
    return new


def listing(db: DB, include_closed: bool = False) -> list[dict]:
    where = "" if include_closed else "WHERE state='open'"
    return [dict(r) for r in db.q(f"SELECT * FROM anomalies {where} ORDER BY anomaly_id DESC")]


# ---------------------------------------------------------------- incidents

INCIDENTS_JSON = Path(__file__).resolve().parent / "incidents.json"


def published_incidents(db: DB) -> list[tuple[str, str, str, str]]:
    return [(r["date"], r["title"], r["what"], r["fix"]) for r in
            db.q("SELECT * FROM incident_drafts WHERE state='published' ORDER BY published_at DESC")]


def publish_incident(s: Settings, db: DB, draft_id: int, path: Path | None = None) -> str:
    r = db.one("SELECT * FROM incident_drafts WHERE draft_id=?", draft_id)
    if not r or r["state"] != "draft":
        return f"draft {draft_id} not found or not a draft"
    db.x("UPDATE incident_drafts SET state='published', published_at=? WHERE draft_id=?", time.time(), draft_id)
    if r["output_id"]:
        db.x("UPDATE operator_ai_outputs SET public=1 WHERE output_id=?", r["output_id"])
    path = path or INCIDENTS_JSON
    entries = json.loads(path.read_text("utf-8")) if path.exists() else []
    entries.append({"date": r["date"], "title": r["title"], "what": r["what"], "fix": r["fix"]})
    path.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n")
    from . import methodology
    methodology._fill_or_placeholder([r["title"], r["what"], r["fix"]])
    return (f"published; shows on /incidents/ at the next site build. Commit {path.name} and the i18n tables:\n"
            f"  git add agentpitbench/incidents.json agentpitbench/i18n/*.json && git commit -m 'Incident: {r['title']}'")


def edit_draft(db: DB, draft_id: int) -> None:
    import subprocess
    import tempfile
    r = db.one("SELECT title, what, fix FROM incident_drafts WHERE draft_id=?", draft_id)
    if not r:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(dict(r), f, indent=1, ensure_ascii=False)
    subprocess.run([os.environ.get("EDITOR", "vi"), f.name])
    d = json.loads(Path(f.name).read_text())
    db.x("UPDATE incident_drafts SET title=?, what=?, fix=? WHERE draft_id=?", d["title"], d["what"], d["fix"],
         draft_id)
    Path(f.name).unlink(missing_ok=True)
