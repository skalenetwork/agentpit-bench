"""`agentpitbench` command: run the bench, or run pieces of it by hand."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from . import config, exports
from .agentpit import Agentpit
from .db import DB, leaderboard
from .orchestrator import NullPublisher, Round
from .tracker import Tracker
from .watcher import Watcher

log = logging.getLogger("agentpitbench")


def make_publisher(s, db):
    try:
        from .publish import BenchPublisher
    except ImportError:
        log.warning("publishing layer not available; tweets, cards and site are skipped")
        return NullPublisher()
    return BenchPublisher(s, db)


async def _every(seconds: float, fn, name: str):
    while True:
        try:
            await fn()
        except Exception:
            log.exception("%s failed", name)
        await asyncio.sleep(seconds)


async def run_forever(s: config.Settings) -> None:
    db = DB(s.db_path)
    api = Agentpit(s.api_url)
    pub = make_publisher(s, db)
    watcher, tracker = Watcher(s, db, api), Tracker(s, db, api, pub)
    tracker.recover()
    running: set[asyncio.Task] = set()

    async def start_batch():
        free = s.max_concurrent_rounds - len(running)
        for m in await watcher.pick(free):
            t = asyncio.create_task(Round(s, db, api, m, pub).run(), name=f"market-{m['id']}")
            running.add(t)
            t.add_done_callback(running.discard)
            log.info("round started for market %s: %s", m["id"], m["question"])

    async def poll():
        await watcher.poll()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("AgentpitBench running (dry_run=%s, posting_paused=%s)", s.dry_run, s.posting_paused)
    tasks = [
        asyncio.create_task(_every(s.poll_interval_s, poll, "watcher")),
        asyncio.create_task(_every(s.batch_interval_s, start_batch, "batch")),
        asyncio.create_task(_every(s.resolution_check_s, tracker.check, "tracker")),
    ]
    await stop.wait()
    log.info("stopping; in-flight rounds will be marked crashed on next start")
    for t in tasks + list(running):
        t.cancel()
    await asyncio.gather(*tasks, *running, return_exceptions=True)
    if hasattr(pub, "close"):
        await pub.close()
    await api.close()


async def one_round(s: config.Settings, market_id: str, agents: list[str] | None) -> None:
    db = DB(s.db_path)
    api = Agentpit(s.api_url)
    pub = make_publisher(s, db)
    m = await api.market(market_id)
    if not m:
        sys.exit(f"market {market_id} not found")
    if db.one("SELECT 1 FROM rounds WHERE market_id=?", str(m["id"])):
        sys.exit(f"market {market_id} already has a round")
    rid = await Round(s, db, api, m, pub, agents=agents).run()
    db.x("INSERT OR IGNORE INTO seen_markets(market_id, first_seen, eligible, started) VALUES(?,strftime('%s'),1,1)",
         str(m["id"]))
    print(json.dumps(exports.round_record(s, db, db.round(rid)), indent=1))
    if hasattr(pub, "close"):
        await pub.close()
    await api.close()


async def check(s: config.Settings) -> None:
    db = DB(s.db_path)
    api = Agentpit(s.api_url)
    pub = make_publisher(s, db)
    print("resolved:", await Tracker(s, db, api, pub).check())
    if hasattr(pub, "close"):
        await pub.close()
    await api.close()


async def whoami(s: config.Settings) -> None:
    for a in s.agents:
        key = s.agentpit_key(a)
        if not key:
            print(f"{a}: no AGENTPIT_KEY_{a.upper()}")
            continue
        c = Agentpit(s.api_url, key)
        try:
            me = await c.me()
            print(f"{a}: {me.get('handle')} {me.get('eth_address')}")
        except Exception as e:
            print(f"{a}: key rejected ({e})")
        finally:
            await c.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="agentpitbench")
    p.add_argument("--config", default="bench.toml")
    p.add_argument("--live", action="store_true", help="override dry_run: place real orders and post")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="watch markets, run rounds, track results (long-running)")
    r = sub.add_parser("round", help="run one round on a given market now")
    r.add_argument("market_id")
    r.add_argument("--agents", help="comma-separated subset, e.g. claude")
    sub.add_parser("check", help="check open rounds for resolution once")
    sub.add_parser("export", help="write rounds.json, leaderboard.json, bets.csv")
    sub.add_parser("leaderboard", help="print standings")
    sub.add_parser("whoami", help="verify each agent's agentpit key")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.load(a.config)
    if a.live:
        s.dry_run = False
    if a.cmd == "run":
        asyncio.run(run_forever(s))
    elif a.cmd == "round":
        asyncio.run(one_round(s, a.market_id, a.agents.split(",") if a.agents else None))
    elif a.cmd == "check":
        asyncio.run(check(s))
    elif a.cmd == "export":
        exports.export(s, DB(s.db_path))
        print(f"wrote {s.export_dir}")
    elif a.cmd == "leaderboard":
        print(json.dumps(leaderboard(DB(s.db_path), s.agents), indent=1))
    elif a.cmd == "whoami":
        asyncio.run(whoami(s))


if __name__ == "__main__":
    main()
