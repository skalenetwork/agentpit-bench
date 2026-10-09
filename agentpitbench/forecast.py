"""Daily forecast sweep: the benchmark's headline metric.

Once a day each agent gets one sandboxed run (frontier settings, web research allowed) with the same list of
open markets and returns a probability for every outcome. When a market resolves, each forecast is scored with
the Brier score, BS = ½ · Σ_k (p_k − y_k)², which for a two-outcome market equals the classic (p − y)² on one
side and runs from 0 (perfect) to 1. The Crowd's forecast is the market price at sweep time, so "beats the
Crowd" means better calibrated than the market itself.

Bets stay the show; this is the statistic: hundreds of forecasts per agent a month, reported with a bootstrap
95% confidence interval and a paired test against the Crowd, so a ranking can be told apart from luck.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .agentpit import Agentpit
from .config import AGENT_MODELS, AGENT_NAMES, Settings
from .db import DB
from .orchestrator import (CLIS, AgentCLI, CleanHome, Round, agent_env, home_model, infra_failure, sandbox_ok,
                           sandboxed, write_shim)
from .virality import category
from .watcher import basic_eligible, priority

log = logging.getLogger(__name__)

PROMPT = """You are taking part in AgentpitBench's daily forecast sweep, a calibration benchmark run alongside three other AI agents.
For each prediction market below, give your probability for every outcome. When the markets resolve you are scored with the Brier score (lower is better), and compared with the market price at the time of this sweep.
You may research online. You have {minutes} minutes in total for all {n} markets: budget your time and answer every market.

Markets (id | question | outcomes with current market price | closes):
{lines}

Finish with ONLY one JSON object, nothing after it:
{{"forecasts": [{{"id": "<market id>", "probabilities": {{"<outcome>": <probability 0-1>, ...}}}}, ...]}}
Give every outcome of every market a probability; each market's probabilities should sum to 1.
"""
CLAMP = (0.01, 0.99)


def today(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), timezone.utc).strftime("%Y-%m-%d")


# ---------- picking ----------

def pick_markets(s: Settings, markets: list[dict], already: set[str], n: int | None = None) -> list[dict]:
    """n eligible open markets not swept before, mixed across categories: round-robin over categories, each
    in priority order (newsworthy first), so no single category fills the sweep."""
    n = n or s.sweep_markets
    soon = datetime.now(timezone.utc) + timedelta(minutes=30)
    pool: dict[str, list[dict]] = {}
    for m in sorted(markets, key=priority, reverse=True):
        if str(m["id"]) in already or not basic_eligible(s, m):
            continue
        try:
            if datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")) <= soon:
                continue
        except (KeyError, AttributeError, ValueError):
            continue
        pool.setdefault(category(m["question"]), []).append(m)
    out: list[dict] = []
    while len(out) < n and any(pool.values()):
        for cat in sorted(pool, key=lambda c: -len(pool[c])):
            if pool[cat] and len(out) < n:
                out.append(pool[cat].pop(0))
    return out


def build_prompt(s: Settings, markets: list[dict]) -> str:
    lines = []
    for m in markets:
        px = ", ".join(f"{o} {p:.3f}" for o, p in zip(m["outcomes_list"], m["prices_list"]))
        lines.append(f"{m['id']} | {m['question']} | {px} | {m.get('endDate')}")
    return PROMPT.format(minutes=round(s.sweep_timeout_s / 60), n=len(markets), lines="\n".join(lines))


# ---------- parsing ----------

def final_text(agent: str, output: str) -> str:
    """The agent's answer text, out of each CLI's output format (stream JSON for claude/grok, plain otherwise)."""
    if agent in ("claude", "grok"):
        parts, result = [], None
        for line in output.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("type") == "result" and isinstance(d.get("result"), str):
                result = d["result"]
            elif d.get("type") == "text" and isinstance(d.get("data"), str):
                parts.append(d["data"])
        if result is not None:
            return result
        return "".join(parts) if parts else output  # no stream events at all: read the raw output
    return output


def parse(agent: str, output: str, markets: list[dict]) -> dict[str, dict[str, float]]:
    """{market_id: {outcome: p}} from the LAST valid {"forecasts": [...]} object in the answer. Probabilities
    are matched to outcome labels case-insensitively, completed for two-outcome markets, clamped to
    [0.01, 0.99] and normalised. Markets left out are 'no forecast'."""
    text = final_text(agent, output)
    dec, found = json.JSONDecoder(), None
    for m in re.finditer(r'\{\s*"forecasts"\s*:', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("forecasts"), list):
            found = obj["forecasts"]
    if not found:
        return {}
    by_id = {str(m["id"]): m["outcomes_list"] for m in markets}
    out: dict[str, dict[str, float]] = {}
    for f in found:
        if not isinstance(f, dict) or str(f.get("id")) not in by_id or not isinstance(f.get("probabilities"), dict):
            continue
        labels = by_id[str(f["id"])]
        given: dict[str, float] = {}
        for k, v in f["probabilities"].items():
            label = next((l for l in labels if l.lower() == str(k).strip().lower()), None)
            try:
                p = float(v)
            except (TypeError, ValueError):
                continue
            if label is not None and math.isfinite(p):
                given[label] = p / 100 if 1 < p <= 100 else p
        if len(labels) == 2 and len(given) == 1:
            (k, p), = given.items()
            given[next(l for l in labels if l != k)] = 1 - p
        if len(given) != len(labels):
            continue
        clamped = {k: min(CLAMP[1], max(CLAMP[0], v)) for k, v in given.items()}
        total = sum(clamped.values())
        out[str(f["id"])] = {k: round(v / total, 6) for k, v in clamped.items()}
    return out


def brier(probs: dict[str, float], winner: str) -> float:
    """½ · Σ (p − y)²: 0 is perfect, 1 is maximally wrong; equals (p − y)² for a two-outcome market."""
    return round(0.5 * sum((p - (1.0 if k == winner else 0.0)) ** 2 for k, p in probs.items()), 6)


def crowd_probs(outcomes: list[str], prices: list[float]) -> dict[str, float]:
    clamped = [min(CLAMP[1], max(CLAMP[0], p)) for p in prices]
    total = sum(clamped) or 1
    return {o: p / total for o, p in zip(outcomes, clamped)}


# ---------- running ----------

def cli_with_model(agent: str, model_id: str) -> AgentCLI:
    """The agent's CLI with a different model id (launch-day duels run the previous flagship too)."""
    cli = CLIS[agent]
    current = AGENT_MODELS[agent][0]
    return AgentCLI(cli.name, [model_id if a == current else a for a in cli.argv], cli.version_argv, cli.model_patterns)


async def run_once(s: Settings, agent: str, prompt: str, tpath: Path,
                   cli: AgentCLI | None = None) -> tuple[str, str | None, bool]:
    """One sandboxed clean-home run with the frontier settings. Returns (output, model, timed_out); the output
    is also kept as the public transcript."""
    cli = cli or CLIS[agent]
    home_box = CleanHome(agent)
    home = home_box.setup()
    work = Path(tempfile.mkdtemp(prefix=f"apb-sweep-{agent}-"))
    ctl = Path(tempfile.mkdtemp(prefix="apbs-"))
    write_shim(ctl)  # no bench socket: the sweep only needs a final answer
    tpath.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    try:
        argv = cli.command(prompt)
        if s.sandbox:
            argv = sandboxed(argv, home, work, ctl)
        with tpath.open("wb") as tf:
            tf.write(f"$ {' '.join(cli.argv[:2])} <sweep prompt>\n--- prompt ---\n{prompt}\n--- output ---\n".encode())
            p = await asyncio.create_subprocess_exec(*argv, cwd=work, env=agent_env("", str(ctl), home),
                                                     stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            pump = asyncio.create_task(Round._pump(p, tf))
            try:
                await asyncio.wait_for(asyncio.shield(pump), s.sweep_timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                Round._kill(p)
                await asyncio.gather(pump, return_exceptions=True)
                tf.write(b"\n[bench] sweep time limit reached\n")
        output = tpath.read_text(errors="replace").split("--- output ---", 1)[-1]
        model = Round._model(cli, tpath) or home_model(agent, home)
        return output, model, timed_out
    finally:
        try:
            await home_box.sync()
        except Exception:
            log.exception("login sync after %s's sweep failed", agent)
        home_box.cleanup()
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(ctl, ignore_errors=True)


async def sweep(s: Settings, db: DB, api: Agentpit, date: str | None = None, runner=None,
                agents: list[str] | None = None) -> dict[str, str]:
    """Run today's sweep (idempotent per agent per day). Returns {agent: status}."""
    date = date or today()
    agents = agents or s.agents
    runner = runner or run_once
    if s.sandbox and runner is run_once and not await sandbox_ok():
        log.error("forecast sweep %s skipped: the agent sandbox does not work here", date)
        return {a: "skipped" for a in agents}
    rows = db.q("SELECT * FROM sweep_markets WHERE sweep_date=?", date)
    if rows:
        markets = [{"id": r["market_id"], "question": r["question"], "outcomes_list": json.loads(r["outcomes"]),
                    "prices_list": json.loads(r["prices"]), "endDate": r["end_date"]} for r in rows]
    else:
        everything, off = [], 0
        while True:
            page = await api.markets(limit=1000, offset=off)
            everything += page
            if len(page) < 1000:
                break
            off += 1000
        already = {r["market_id"] for r in db.q("SELECT market_id FROM sweep_markets")}
        markets = pick_markets(s, everything, already)
        for m in markets:
            db.x("INSERT OR IGNORE INTO sweep_markets(market_id,sweep_date,question,outcomes,prices,end_date,category)"
                 " VALUES(?,?,?,?,?,?,?)", str(m["id"]), date, m["question"], json.dumps(m["outcomes_list"]),
                 json.dumps(m["prices_list"]), m.get("endDate"), category(m["question"]))
    if not markets:
        log.info("forecast sweep %s: no eligible markets", date)
        return {}
    prompt = build_prompt(s, markets)
    status: dict[str, str] = {}

    async def one(agent: str):
        if db.one("SELECT 1 FROM sweep_runs WHERE sweep_date=? AND agent=? AND status IN ('ok','failed')", date, agent):
            status[agent] = "done"
            return
        tpath = s.transcripts_dir / "sweep" / date / f"{agent}.txt"
        try:
            output, model, timed_out = await runner(s, agent, prompt, tpath)
        except Exception as e:
            log.exception("forecast sweep for %s failed to run", agent)
            output, model, timed_out = f"[bench] runner error: {e}", None, False
        got = parse(agent, output, markets)
        kind = None if got else infra_failure(output)
        if kind:  # not the agent's fault: skipped for the day, never scored as missing
            st, detail = "infra", kind
        else:
            st, detail = ("ok", None) if got else ("failed", "time limit" if timed_out else "no valid forecasts")
            for mid, probs in got.items():
                db.x("INSERT OR IGNORE INTO forecasts(sweep_date,agent,market_id,probs,model_reported) VALUES(?,?,?,?,?)",
                     date, agent, mid, json.dumps(probs), model)
        db.x("INSERT INTO sweep_runs(sweep_date,agent,status,detail,n_forecasts,model_reported,transcript_path)"
             " VALUES(?,?,?,?,?,?,?) ON CONFLICT(sweep_date,agent) DO UPDATE SET status=excluded.status,"
             " detail=excluded.detail, n_forecasts=excluded.n_forecasts, model_reported=excluded.model_reported,"
             " transcript_path=excluded.transcript_path",
             date, agent, st, detail, len(got), model, str(tpath))
        status[agent] = st
        log.info("forecast sweep %s: %s %s (%d of %d markets)", date, agent, st, len(got), len(markets))

    await asyncio.gather(*(one(a) for a in agents))
    return status


async def score_due(s: Settings, db: DB, api: Agentpit) -> int:
    """Settle sweep markets that have closed: score every forecast with the Brier score. Returns how many."""
    now = datetime.now(timezone.utc)
    n = 0
    for r in db.q("SELECT * FROM sweep_markets WHERE state='open'"):
        try:
            if datetime.fromisoformat((r["end_date"] or "").replace("Z", "+00:00")) > now:
                continue
        except ValueError:
            pass
        try:
            m = await api.market(r["market_id"])
        except Exception:
            log.exception("could not fetch sweep market %s", r["market_id"])
            continue
        if m and m.get("winner"):
            db.x("UPDATE sweep_markets SET winner=?, state='resolved' WHERE market_id=?", m["winner"], r["market_id"])
            for f in db.q("SELECT agent, probs FROM forecasts WHERE market_id=? AND brier IS NULL", r["market_id"]):
                db.x("UPDATE forecasts SET brier=? WHERE agent=? AND market_id=?",
                     brier(json.loads(f["probs"]), m["winner"]), f["agent"], r["market_id"])
            n += 1
        elif m and m.get("closed"):
            try:
                end = datetime.fromisoformat((r["end_date"] or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if now > end + timedelta(days=s.void_after_days):
                db.x("UPDATE sweep_markets SET state='void' WHERE market_id=?", r["market_id"])
    return n


# ---------- statistics ----------

def _bootstrap_ci(xs: list[float], rng: random.Random, b: int) -> tuple[float, float]:
    if len(xs) < 2:
        return (xs[0], xs[0]) if xs else (float("nan"), float("nan"))
    means = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(b))
    return means[int(0.025 * b)], means[min(b - 1, int(0.975 * b))]


def _sign_test(diffs: list[float]) -> float:
    """Two-sided exact sign test p-value (ties dropped)."""
    neg, pos = sum(1 for d in diffs if d < 0), sum(1 for d in diffs if d > 0)
    n = neg + pos
    if n == 0:
        return 1.0
    k = min(neg, pos)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def metrics(db: DB, agents: list[str], b: int = 2000, seed: int = 20261009) -> dict:
    """Forecast accuracy per agent and for the Crowd: mean Brier, bootstrap 95% CI, n, coverage, and a paired
    comparison with the Crowd on the same markets (bootstrap CI of the mean difference + sign test)."""
    rng = random.Random(seed)
    resolved = {r["market_id"]: r for r in db.q("SELECT * FROM sweep_markets WHERE state='resolved'")}
    crowd = {mid: brier(crowd_probs(json.loads(r["outcomes"]), json.loads(r["prices"])), r["winner"])
             for mid, r in resolved.items()}
    rows = []
    for a in agents:
        fs = [f for f in db.q("SELECT market_id, brier FROM forecasts WHERE agent=? AND brier IS NOT NULL", a)
              if f["market_id"] in crowd]
        # coverage: resolved markets from days this agent's sweep ran (infra days are not held against it)
        days = [d["sweep_date"] for d in db.q("SELECT sweep_date FROM sweep_runs WHERE agent=? AND status IN ('ok','failed')", a)]
        offered = sum(1 for r in resolved.values() if r["sweep_date"] in days)
        xs = [f["brier"] for f in fs]
        diffs = [f["brier"] - crowd[f["market_id"]] for f in fs]
        row = {"agent": a, "name": AGENT_NAMES.get(a, a), "n": len(xs), "offered": offered,
               "coverage": round(len(xs) / offered, 4) if offered else None,
               "brier": round(sum(xs) / len(xs), 4) if xs else None, "ci": None,
               "vs_crowd": None}
        if xs:
            lo, hi = _bootstrap_ci(xs, rng, b)
            row["ci"] = [round(lo, 4), round(hi, 4)]
            dlo, dhi = _bootstrap_ci(diffs, rng, b)
            mean_d = sum(diffs) / len(diffs)
            verdict = "better" if dhi < 0 else ("worse" if dlo > 0 else "inconclusive")
            row["vs_crowd"] = {"mean_diff": round(mean_d, 4), "ci": [round(dlo, 4), round(dhi, 4)],
                               "sign_test_p": _sign_test(diffs), "verdict": verdict}
        rows.append(row)
    ranked = sorted(rows, key=lambda r: (r["brier"] is None, r["brier"] if r["brier"] is not None else 9))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i if r["brier"] is not None else None
    cx = list(crowd.values())
    c = {"name": "The Crowd", "n": len(cx), "brier": round(sum(cx) / len(cx), 4) if cx else None, "ci": None}
    if cx:
        lo, hi = _bootstrap_ci(cx, rng, b)
        c["ci"] = [round(lo, 4), round(hi, 4)]
    return {"agents": ranked, "crowd": c, "markets_resolved": len(resolved),
            "markets_open": db.one("SELECT COUNT(*) c FROM sweep_markets WHERE state='open'")["c"],
            "sweeps": db.one("SELECT COUNT(DISTINCT sweep_date) c FROM sweep_runs")["c"],
            "metric": "Brier score, ½·Σ(p−y)², lower is better; 95% bootstrap CI; paired vs the Crowd (market price at sweep time)"}


def sweeps(db: DB) -> list[dict]:
    """Per-day sweep log for the site: who ran, status, how many forecasts, transcript path."""
    out: dict[str, dict] = {}
    for r in db.q("SELECT * FROM sweep_runs ORDER BY sweep_date DESC, agent"):
        d = out.setdefault(r["sweep_date"], {"date": r["sweep_date"], "runs": []})
        d["runs"].append({"agent": r["agent"], "status": r["status"], "detail": r["detail"],
                          "n": r["n_forecasts"], "model": r["model_reported"],
                          "transcript": f"transcripts/sweep/{r['sweep_date']}/{r['agent']}.txt"})
    return list(out.values())


# ---------- sealing ----------

def seal(s: Settings, db: DB, date: str) -> str | None:
    """Write the day's forecasts to export/forecasts/<date>.json before any of their markets can resolve and
    record its SHA-256. The file is published with the next site deploy, so the deploy history time-stamps
    the forecasts: nobody, including the operator, can edit them after the fact without changing the hash."""
    path = s.export_dir / "forecasts" / f"{date}.json"
    if path.exists():
        return (db.get(f"sealed_{date}") or {}).get("sha256")
    markets = [{"id": r["market_id"], "question": r["question"], "outcomes": json.loads(r["outcomes"]),
                "prices_at_sweep": json.loads(r["prices"]), "closes": r["end_date"]}
               for r in db.q("SELECT * FROM sweep_markets WHERE sweep_date=? ORDER BY market_id", date)]
    rows = db.q("SELECT agent, market_id, probs, model_reported FROM forecasts WHERE sweep_date=? ORDER BY agent, market_id",
                date)
    if not rows:
        return None
    doc = {"benchmark": "AgentpitBench", "kind": "forecast sweep", "date": date,
           "methodology_version": s.methodology_version, "markets": markets,
           "forecasts": [{"agent": r["agent"], "market_id": r["market_id"], "probabilities": json.loads(r["probs"]),
                          "model": r["model_reported"]} for r in rows]}
    data = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    db.put(f"sealed_{date}", {"date": date, "sha256": sha, "file": f"data/forecasts/{date}.json",
                              "forecasts": len(rows), "sealed_at": time.time()})
    sealed = sorted((db.get(k["k"]) for k in db.q("SELECT k FROM kv WHERE k LIKE 'sealed_%'")),
                    key=lambda d: d["date"], reverse=True)
    (s.export_dir / "sealed.json").write_text(json.dumps(sealed, indent=1))
    log.info("forecast sweep %s sealed: sha256 %s (%d forecasts)", date, sha, len(rows))
    return sha


def sealed_list(db: DB) -> list[dict]:
    return sorted((db.get(r["k"]) for r in db.q("SELECT k FROM kv WHERE k LIKE 'sealed_%'")),
                  key=lambda d: d["date"], reverse=True)


# ---------- launch-day duels ----------

def queue_duel(db: DB, agent: str, old: str, new: str) -> int:
    db.x("CREATE TABLE IF NOT EXISTS duels (duel_id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL,"
         " old_model TEXT NOT NULL, new_model TEXT NOT NULL, sweep_date TEXT, status TEXT NOT NULL DEFAULT 'pending',"
         " created_at REAL NOT NULL, tweet_id TEXT)")
    return db.x("INSERT INTO duels(agent,old_model,new_model,created_at) VALUES(?,?,?,?)", agent, old, new, time.time())


def _duels(db: DB, status: str | None = None) -> list:
    if not db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='duels'"):
        return []
    return db.q("SELECT * FROM duels" + (" WHERE status=?" if status else "") + " ORDER BY duel_id",
                *((status,) if status else ()))


def detect_model_changes(db: DB) -> list[int]:
    """When config.AGENT_MODELS moves an agent to a new flagship, queue its old-vs-new duel once."""
    cur = {a: m[0] for a, m in AGENT_MODELS.items()}
    last = db.get("agent_models")
    queued = []
    if last:
        for a, model in cur.items():
            if a in last and last[a] != model:
                queued.append(queue_duel(db, a, last[a], model))
    db.put("agent_models", cur)
    return queued


async def duels_due(s: Settings, db: DB, api: Agentpit, runner=None) -> list[dict]:
    """Run pending duels on today's sweep markets: both models, same prompt, same sandbox. A labelled
    exhibition: stored as agents 'duel<N>-old' / 'duel<N>-new', never in the season tables."""
    detect_model_changes(db)
    date = today()
    markets = [{"id": r["market_id"], "question": r["question"], "outcomes_list": json.loads(r["outcomes"]),
                "prices_list": json.loads(r["prices"]), "endDate": r["end_date"]}
               for r in db.q("SELECT * FROM sweep_markets WHERE sweep_date=?", date)]
    done = []
    if not markets:
        return done
    runner = runner or run_once
    prompt = build_prompt(s, markets)
    for d in _duels(db, "pending"):
        async def side(tag: str, model: str):
            tpath = s.transcripts_dir / "sweep" / date / f"duel{d['duel_id']}-{tag}.txt"
            out, reported, _ = await runner(s, d["agent"], prompt, tpath, cli_with_model(d["agent"], model))
            got = parse(d["agent"], out, markets)
            for mid, probs in got.items():
                db.x("INSERT OR IGNORE INTO forecasts(sweep_date,agent,market_id,probs,model_reported) VALUES(?,?,?,?,?)",
                     date, f"duel{d['duel_id']}-{tag}", mid, json.dumps(probs), reported or model)
            return len(got)
        n_old, n_new = await asyncio.gather(side("old", d["old_model"]), side("new", d["new_model"]))
        db.x("UPDATE duels SET status='run', sweep_date=? WHERE duel_id=?", date, d["duel_id"])
        done.append({"duel_id": d["duel_id"], "agent": d["agent"], "old": n_old, "new": n_new})
        log.info("launch-day duel %s (%s %s vs %s): %d / %d forecasts", d["duel_id"], d["agent"],
                 d["old_model"], d["new_model"], n_old, n_new)
    return done


def duel_result(db: DB, duel: dict, b: int = 2000) -> dict | None:
    """Paired comparison once every duel market has settled: mean Brier per side, CI of the difference, n."""
    open_left = db.one("SELECT COUNT(*) c FROM sweep_markets WHERE sweep_date=? AND state='open'", duel["sweep_date"])["c"]
    if open_left:
        return None
    old = {f["market_id"]: f["brier"] for f in db.q("SELECT market_id, brier FROM forecasts WHERE agent=? AND brier IS NOT NULL",
                                                      f"duel{duel['duel_id']}-old")}
    new = {f["market_id"]: f["brier"] for f in db.q("SELECT market_id, brier FROM forecasts WHERE agent=? AND brier IS NOT NULL",
                                                      f"duel{duel['duel_id']}-new")}
    both = sorted(old.keys() & new.keys())
    if not both:
        return {"n": 0}
    diffs = [new[m] - old[m] for m in both]
    lo, hi = _bootstrap_ci(diffs, random.Random(duel["duel_id"]), b)
    return {"n": len(both), "old": round(sum(old[m] for m in both) / len(both), 4),
            "new": round(sum(new[m] for m in both) / len(both), 4), "diff": round(sum(diffs) / len(both), 4),
            "ci": [round(lo, 4), round(hi, 4)], "sign_test_p": _sign_test(diffs),
            "verdict": "new better" if hi < 0 else ("old better" if lo > 0 else "inconclusive")}


def duels(db: DB) -> list[dict]:
    out = []
    for d in _duels(db):
        out.append({"duel_id": d["duel_id"], "agent": d["agent"], "name": AGENT_NAMES.get(d["agent"], d["agent"]),
                    "old_model": d["old_model"], "new_model": d["new_model"], "date": d["sweep_date"],
                    "status": d["status"], "result": duel_result(db, dict(d)) if d["status"] != "pending" else None})
    return out


# ---------- contrarian calls ----------

def contrarian(db: DB, agents: list[str], threshold: float = 0.30) -> list[dict]:
    """Sweep forecasts at least `threshold` (30 points) away from the market price on some outcome: the side
    the agent backs against the market, how far apart, and how it resolved."""
    snap = {r["market_id"]: r for r in db.q("SELECT * FROM sweep_markets")}
    out = []
    for f in db.q("SELECT * FROM forecasts ORDER BY sweep_date DESC"):
        if f["agent"] not in agents or f["market_id"] not in snap:
            continue
        r = snap[f["market_id"]]
        market = crowd_probs(json.loads(r["outcomes"]), json.loads(r["prices"]))
        probs = json.loads(f["probs"])
        side, gap = max(((k, probs.get(k, 0) - market.get(k, 0)) for k in market), key=lambda kv: kv[1])
        if gap < threshold - 1e-9:
            continue
        out.append({"agent": f["agent"], "name": AGENT_NAMES.get(f["agent"], f["agent"]), "date": f["sweep_date"],
                    "market_id": f["market_id"], "question": r["question"], "outcome": side,
                    "agent_p": round(probs.get(side, 0), 3), "market_p": round(market.get(side, 0), 3),
                    "gap": round(gap, 3), "state": r["state"], "winner": r["winner"],
                    "right": (r["winner"] == side) if r["state"] == "resolved" else None})
    return out


# ---------- significance ----------

def sign_test_rounds(rounds: list[dict], agent: str) -> tuple[int, float]:
    """Paired sign test of an agent's per-round profit against the Crowd's, over counted resolved rounds."""
    diffs = []
    for r in rounds:
        if r["state"] != "resolved" or r.get("exhibition") or r["crowd"]["pnl"] is None:
            continue
        e = next((e for e in r["entries"] if e["agent"] == agent), None)
        if e is not None:
            diffs.append((e["pnl"] or 0) - r["crowd"]["pnl"])
    return len(diffs), _sign_test(diffs)


def significance(rounds: list[dict], forecast_metrics: dict, agents: list[str]) -> dict:
    """The one-line verdict the cards and badge carry: n (every resolved observation behind the tests: counted
    betting rounds plus scored sweep forecasts), and whether anything beats the Crowd at p<0.05."""
    n_rounds = sum(1 for r in rounds if r["state"] == "resolved" and not r.get("exhibition"))
    n_forecasts = sum(r.get("n") or 0 for r in forecast_metrics.get("agents", []))
    hits = []
    for row in forecast_metrics.get("agents", []):
        vs = row.get("vs_crowd")
        if vs and vs["verdict"] != "inconclusive" and vs["sign_test_p"] < 0.05:
            hits.append({"agent": row["agent"], "test": "forecast", "p": vs["sign_test_p"], "direction": vs["verdict"]})
    for a in agents:
        n, p = sign_test_rounds(rounds, a)
        if n and p < 0.05:
            hits.append({"agent": a, "test": "betting", "p": p, "direction": None})
    n = n_rounds + n_forecasts
    label = f"n={n} · significant vs the Crowd (p<0.05)" if hits else f"n={n} · not yet significant"
    return {"n": n, "n_rounds": n_rounds, "n_forecasts": n_forecasts, "significant": hits, "label": label}
