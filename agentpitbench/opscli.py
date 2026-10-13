"""Operator commands for the operator AI, the methodology lock, pauses, anomalies, alerts and incidents
(docs/ai-automation-spec.md §14). Kept apart from cli.py; cli.main hands these subcommands over."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from .config import Settings
from .db import DB

COMMANDS = {"opai", "method", "alerts", "ops", "incidents", "humans", "summaries", "translate", "news", "i18n"}


def _ts(t) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "-"


def add_parsers(sub) -> None:
    o = sub.add_parser("opai", help="operator AI: budget/status, or one call with real input")
    osub = o.add_subparsers(dest="opai_cmd", required=True)
    osub.add_parser("status", help="budget used per task, backoff, breaker, last 20 calls")
    t = osub.add_parser("try", help="one call with real input; prints the output, publishes nothing")
    t.add_argument("task")
    t.add_argument("--subject", required=True, help="e.g. round:26")
    t.add_argument("--config-dir", help="CLAUDE_CONFIG_DIR override (default: opai_config_dir)")
    t.add_argument("--langs", help="translate: comma-separated subset of site languages")

    m = sub.add_parser("method", help="the methodology lock: verify, proposals, approval")
    msub = m.add_subparsers(dest="method_cmd", required=True)
    msub.add_parser("verify")
    msub.add_parser("list")
    sh = msub.add_parser("show")
    sh.add_argument("id")
    pr = msub.add_parser("propose")
    pr.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    pr.add_argument("--rehash", action="store_true", help="re-baseline hashed files (prompt.txt, AGENT_MODELS, ...)")
    pr.add_argument("--text", required=True, help="the public changelog line")
    ap = msub.add_parser("approve")
    ap.add_argument("id")
    ap.add_argument("--major", action="store_true")
    rj = msub.add_parser("reject")
    rj.add_argument("id")
    rv = msub.add_parser("revert", help="undo a generated change (e.g. an automatic selection change) as a new "
                                        "version")
    rv.add_argument("version")

    a = sub.add_parser("alerts", help="recent operator alerts (log + var/alerts/), or acknowledge one")
    a.add_argument("--all", action="store_true", help="include acknowledged alerts")
    a.add_argument("-n", type=int, default=20)
    asub = a.add_subparsers(dest="alerts_cmd")
    ak = asub.add_parser("ack")
    ak.add_argument("id", type=int)

    op = sub.add_parser("ops", help="pause state, anomalies; pause / resume posting and new rounds")
    opsub = op.add_subparsers(dest="ops_cmd", required=True)
    opsub.add_parser("status")
    pa = opsub.add_parser("pause")
    pa.add_argument("--posting", action="store_true")
    pa.add_argument("--rounds", action="store_true")
    pa.add_argument("--reason", required=True)
    re_ = opsub.add_parser("resume")
    re_.add_argument("--posting", action="store_true")
    re_.add_argument("--rounds", action="store_true")
    an = opsub.add_parser("anomalies")
    an.add_argument("--all", action="store_true")
    opsub.add_parser("check", help="run the anomaly detectors once now")
    opsub.add_parser("backfill-event-titles", help="match titles for open rounds (longtail search spec §2)")
    opsub.add_parser("snapshot", help="write the ops agent's scrubbed 24 h snapshot (no AI) and print its path")
    ni = opsub.add_parser("nightly", help="the nightly ops agent: clone, agent in its sandbox, gate, PR or patch")
    ni.add_argument("--force", action="store_true", help="run even if it already ran today (still needs "
                    "ops_agent_enabled and opai available)")
    it = opsub.add_parser("install-timer", help="copy the agentpitbench-ops service + timer unit files into "
                          "~/.config/systemd/user/ (never enables them)")
    it.add_argument("--dir", help="target directory (default ~/.config/systemd/user)")
    opsub.add_parser("cadence", help="the cadence policy in force and the latest decision (§9)")

    inc = sub.add_parser("incidents", help="incident drafts: list, show, publish, discard")
    isub = inc.add_subparsers(dest="inc_cmd", required=True)
    isub.add_parser("list")
    for name in ("show", "discard"):
        x = isub.add_parser(name)
        x.add_argument("id", type=int)
    ip = isub.add_parser("publish")
    ip.add_argument("id", type=int)
    ip.add_argument("--edit", action="store_true", help="open $EDITOR on the draft first")

    h = sub.add_parser("humans", help="human picks: rerun the model pass for a round (only adds picks)")
    hsub = h.add_subparsers(dest="humans_cmd", required=True)
    hr = hsub.add_parser("reparse")
    hr.add_argument("round_id", type=int)

    sm = sub.add_parser("summaries", help="per-round agent summaries")
    smsub = sm.add_subparsers(dest="sum_cmd", required=True)
    sw = smsub.add_parser("write")
    sw.add_argument("round_id", type=int)

    tr = sub.add_parser("translate", help="market question/outcome translations for the site")
    trsub = tr.add_subparsers(dest="tr_cmd", required=True)
    tro = trsub.add_parser("round")
    tro.add_argument("round_id", type=int)
    trb = trsub.add_parser("backfill")
    trb.add_argument("--limit", type=int, default=5)
    trsub.add_parser("status")

    nw = sub.add_parser("news", help="newsworthiness rating (shadow mode until methodology approval)")
    nsub = nw.add_subparsers(dest="news_cmd", required=True)
    ns = nsub.add_parser("shadow")
    ns.add_argument("--days", type=int, default=7)
    nr = nsub.add_parser("rate")
    nr.add_argument("--limit", type=int, default=40)

    i = sub.add_parser("i18n", help="UI translations")
    isb = i.add_subparsers(dest="i18n_cmd", required=True)
    f = isb.add_parser("fill")
    f.add_argument("--check", action="store_true", help="exit 1 if any id is missing")
    f.add_argument("--lang")


def dispatch(s: Settings, a: argparse.Namespace) -> None:
    db = DB(s.db_path)
    getattr(sys.modules[__name__], f"_{a.cmd}")(s, db, a)


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


# ---------------------------------------------------------------- opai

def _opai(s: Settings, db: DB, a) -> None:
    from . import opai
    if a.opai_cmd == "status":
        st = opai.status(s, db)
        for c in st["last_calls"]:
            c["started_at"] = _ts(c["started_at"])
        _print(st)
        return
    from . import tasks
    out = tasks.try_task(s, db, a.task, a.subject, config_dir=a.config_dir,
                         langs=a.langs.split(",") if a.langs else None)
    _print(out)


# ---------------------------------------------------------------- method

def _method(s: Settings, db: DB, a) -> None:
    from . import methodology as m
    try:
        if a.method_cmd == "verify":
            problems = m.verify(s)
            print("methodology v%s: %s" % (m.load()["version"], "clean" if not problems else "DRIFT"))
            for p in problems:
                print(" -", p)
            sys.exit(1 if problems else 0)
        elif a.method_cmd == "list":
            for p in m.list_proposals(s):
                print(f"{p['id']}  {p.get('state', 'open'):9}  {p['author']:9}  {json.dumps(p['changes'])}  "
                      f"{p['changelog_text'][:60]}")
        elif a.method_cmd == "show":
            _print(m.get_proposal(s, a.id))
        elif a.method_cmd == "propose":
            changes = dict(kv.split("=", 1) for kv in a.set)
            p = m.propose(s, changes, a.text, "operator", rehash=a.rehash)
            print(f"proposal {p['id']} written to {m.proposals_dir(s) / (p['id'] + '.json')}")
        elif a.method_cmd == "approve":
            new = m.approve(s, a.id, "operator", major=a.major)
            print(f"methodology.json is now v{new['version']}. Commit and deploy it:\n"
                  f"  git add agentpitbench/methodology.json agentpitbench/i18n/*.json\n"
                  f"  git commit -m 'Methodology v{new['version']}: {new['changelog'][-1]['text'][:60]}'\n"
                  "The service picks it up on the next deploy (approval never restarts it).")
        elif a.method_cmd == "reject":
            m.reject(s, a.id)
            print("rejected")
        elif a.method_cmd == "revert":
            new = m.revert(s, a.version)
            e = new["changelog"][-1]
            print(f"methodology.json is now v{new['version']} ({e['change']['key']} back to {e['change']['new']}); "
                  f"evidence: {m.change_url(s, new['version'])}\n"
                  f"  git add agentpitbench/methodology.json\n"
                  f"  git commit -m 'Methodology v{new['version']}: revert v{a.version}'\n"
                  "Then deploy, or restart the service if this is the live checkout. Until the service reloads the "
                  "lock, the methodology-drift detector pauses new rounds (by design); after the restart, clear "
                  "the pause with `agentpitbench ops resume --rounds`.")
    except m.MethodologyError as e:
        sys.exit(f"refused: {e}")


# ---------------------------------------------------------------- alerts

def _alerts(s: Settings, db: DB, a) -> None:
    from . import alerts
    if a.alerts_cmd == "ack":
        sys.exit(0 if alerts.ack(db, a.id) else f"no alert {a.id}")
    for r in alerts.recent(db, a.n, unacked=not a.all):
        print(f"#{r['alert_id']} {_ts(r['at'])} [{r['severity']}] {r['kind'] or '-'}: {r['subject']}"
              f"{'  (acked)' if r['acked_at'] else ''}\n    {r['body']}\n    action: {r['action'] or '-'}"
              f"  resume: {r['resume'] or '-'}  draft: {r['draft_path'] or '-'}")


# ---------------------------------------------------------------- ops

def _ops(s: Settings, db: DB, a) -> None:
    from . import anomaly, northstar, opai
    if a.ops_cmd == "status":
        st = opai.status(s, db)
        _print({"pause": db.get("pause") or {}, "posting_paused_flag": s.posting_paused,
                "open_anomalies": anomaly.listing(db, include_closed=False),
                "opai": {k: st[k] for k in ("enabled", "calls_today", "budget", "backoff_until",
                                            "contestant_breaker")},
                "northstar": northstar.status(db)})
    elif a.ops_cmd == "pause":
        if not (a.posting or a.rounds):
            a.posting = a.rounds = True
        anomaly.pause(db, posting=a.posting, rounds=a.rounds, reason=a.reason)
        print("paused:", db.get("pause"))
    elif a.ops_cmd == "resume":
        try:
            anomaly.resume(db, posting=a.posting or not a.rounds, rounds=a.rounds or not a.posting)
        except anomaly.ResumeError as e:
            sys.exit(f"refused: {e}")
        print("pause now:", db.get("pause"))
    elif a.ops_cmd == "anomalies":
        for r in anomaly.listing(db, include_closed=a.all):
            print(f"#{r['anomaly_id']} {_ts(r['detected_at'])} [{r['severity']}] {r['detector']} {r['state']}  "
                  f"{r['detail_json']}")
    elif a.ops_cmd == "backfill-event-titles":
        import asyncio
        from .agentpit import Agentpit
        from .orchestrator import event_title

        async def run():
            api = Agentpit(s.api_url)
            try:
                for r in db.q("SELECT round_id, condition_id FROM rounds WHERE event_title IS NULL AND "
                              "state NOT IN ('resolved','void')"):
                    title = await event_title(api, r["condition_id"])
                    print(r["round_id"], title or "-")
                    if title:
                        db.set_round(r["round_id"], event_title=title)
            finally:
                await api.close()
        asyncio.run(run())
    elif a.ops_cmd == "check":
        _print([x.__dict__ for x in anomaly.tick(s, db)])
    elif a.ops_cmd == "snapshot":
        from . import ops
        print(ops.snapshot(s, db, ops.CACHE / opai.utc_day()))
    elif a.ops_cmd == "nightly":
        from . import ops
        out = ops.nightly(s, db, force=a.force)
        _print(out)
        sys.exit(1 if out.get("status") == "error" else 0)
    elif a.ops_cmd == "install-timer":
        from pathlib import Path
        from . import ops
        for p in ops.install_timer(Path(a.dir).expanduser() if a.dir else None):
            print("wrote", p)
        print("Not enabled. To enable (operator):\n  systemctl --user daemon-reload\n"
              "  systemctl --user enable --now agentpitbench-ops.timer\n"
              "  loginctl enable-linger $USER   # so the timer runs while logged out\n"
              "The run is a no-op until ops_agent_enabled = true in bench.toml.")
    elif a.ops_cmd == "cadence":
        from . import policy
        _print({"mode": policy.mode(s, db), "policy": policy.current(s, db), "data": policy.sufficiency(db),
                "last": db.get(policy.LAST)})


# ---------------------------------------------------------------- incidents

def _incidents(s: Settings, db: DB, a) -> None:
    from . import anomaly
    if a.inc_cmd == "list":
        for r in db.q("SELECT draft_id, date, state, title, output_id FROM incident_drafts ORDER BY draft_id DESC"):
            print(f"#{r['draft_id']} {r['date']} {r['state']:9} {'model' if r['output_id'] else 'template'}  "
                  f"{r['title']}")
    elif a.inc_cmd == "show":
        r = db.one("SELECT * FROM incident_drafts WHERE draft_id=?", a.id)
        _print(dict(r) if r else None)
    elif a.inc_cmd == "discard":
        db.x("UPDATE incident_drafts SET state='discarded' WHERE draft_id=? AND state='draft'", a.id)
    elif a.inc_cmd == "publish":
        if a.edit:
            anomaly.edit_draft(db, a.id)
        print(anomaly.publish_incident(s, db, a.id))


# ---------------------------------------------------------------- humans / summaries / translate / news / i18n

def _humans(s: Settings, db: DB, a) -> None:
    import asyncio
    from . import picks
    print(asyncio.run(picks.model_pass(s, db, a.round_id, force=True)))


def _summaries(s: Settings, db: DB, a) -> None:
    import asyncio
    from . import summaries
    print(asyncio.run(summaries.write(s, db, a.round_id, rewrite=True)))


def _translate(s: Settings, db: DB, a) -> None:
    from . import market_i18n as mi
    if a.tr_cmd == "round":
        import asyncio
        _print(asyncio.run(mi.translate_round(s, db, a.round_id)))
    elif a.tr_cmd == "backfill":
        _print(mi.tick(s, db, limit=a.limit))
    else:
        _print(mi.coverage(db))


def _news(s: Settings, db: DB, a) -> None:
    from . import rating
    if a.news_cmd == "shadow":
        print(rating.shadow_report(db, a.days))
    else:
        import asyncio

        async def go():
            from .agentpit import Agentpit
            api = Agentpit(s.api_url)
            try:
                ids = [r["market_id"] for r in db.q(
                    "SELECT market_id FROM seen_markets sm WHERE eligible=1 AND started=0 AND NOT EXISTS "
                    "(SELECT 1 FROM market_ratings mr WHERE mr.market_id=sm.market_id) ORDER BY first_seen DESC "
                    "LIMIT ?", a.limit)]
                rating.observe([m for m in [await api.market(i) for i in ids] if m])
            finally:
                await api.close()
            return await rating.rate_pending(s, db, limit=a.limit, force_gap=False)
        print(asyncio.run(go()))


def _i18n(s: Settings, db: DB, a) -> None:
    from . import i18n_fill
    if a.check:
        missing = i18n_fill.missing(retry=set(db.get("i18n_retry") or []))
        for lang, ids in missing.items():
            if ids:
                print(f"{lang}: {len(ids)} missing")
        sys.exit(1 if any(missing.values()) else 0)
    _print(i18n_fill.fill(s, db, langs=[a.lang] if a.lang else None))
