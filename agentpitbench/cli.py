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


def x_check(s: config.Settings, probe_upload: bool) -> None:
    """Refresh the X OAuth 2.0 token (rotating it), show scopes and account; optionally upload, never tweet."""
    from .twitter import ME_URL, NEEDED_SCOPES, Poster, token_file
    p = Poster(s, DB(s.db_path))
    if not p.oauth2.ready:
        sys.exit("OAuth 2.0 not configured: need X_CLIENT_ID, X_CLIENT_SECRET and X_OAUTH2_REFRESH_TOKEN")
    if not p.oauth2.scopes():
        p.oauth2.get(stale=p.oauth2._load().get("access_token"))  # first run: refresh so scopes are known
    scopes = p.oauth2.scopes()
    print(f"token file: {token_file()}  scopes: {' '.join(sorted(scopes))}")
    missing = NEEDED_SCOPES - scopes
    print("missing scopes:", ", ".join(sorted(missing)) if missing else "none")
    me = p._call("GET", ME_URL).json()["data"]
    print(f"posting as @{me['username']} ({me['name']})")
    if probe_upload:
        img = s.data_dir / "x-probe.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(_probe_png())
        print("media upload ok, id", p.upload(img), "(not attached to any tweet; expires in 24h)")


def x_login(s: config.Settings, redirected: str | None, redirect_uri: str | None) -> None:
    """Authorize @agentpitbench via X's login page with every scope the bench needs (incl. media.write)."""
    from .twitter import Poster
    p = Poster(s, DB(s.db_path))
    if not (p.oauth2.client_id and p.oauth2.client_secret):
        sys.exit("need X_CLIENT_ID and X_CLIENT_SECRET in the secrets file")
    if redirected:
        print("logged in; scopes:", " ".join(sorted(p.oauth2.finish_login(redirected))))
        return
    print("1. While logged into X as the bench account, open:\n")
    print(p.oauth2.authorize_url(redirect_uri or s.site_url))
    print("\n2. Click Authorize. You land on a page (it may 404) whose URL contains ?state=...&code=...")
    if sys.stdin.isatty():
        redirected = input("3. Paste that whole URL here and press Enter (the code expires in ~30 s):\n> ")
        print("logged in; scopes:", " ".join(sorted(p.oauth2.finish_login(redirected))))
    else:
        print("3. Copy that whole URL and run:  agentpitbench x-login --url '<that URL>'   (right away: the code expires quickly)")


def _probe_png() -> bytes:
    import struct, zlib
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x0b\x10\x20" * 64 for _ in range(64))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


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
    xc = sub.add_parser("x-check", help="refresh the X OAuth 2.0 token, show scopes and account (never tweets)")
    xc.add_argument("--upload", action="store_true", help="also upload a tiny test image (not attached to a tweet)")
    xl = sub.add_parser("x-login", help="authorize the X account with every scope the bench needs")
    xl.add_argument("--url", help="the URL X redirected you to (step 2)")
    xl.add_argument("--redirect-uri", help="callback URL registered on the X app (default: site_url)")
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
    elif a.cmd == "x-login":
        x_login(s, a.url, a.redirect_uri)
    elif a.cmd == "x-check":
        x_check(s, a.upload)


if __name__ == "__main__":
    main()
