"""`agentpitbench` command: run the bench, or run pieces of it by hand."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, exports
from .agentpit import Agentpit, unreachable
from .db import DB, leaderboard
from .orchestrator import NullPublisher, Round, sandbox_ok, warm_login
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
        except Exception as e:
            if unreachable(e):
                log.warning("%s: network unavailable (%s: %s); retrying in %ds", name, type(e).__name__,
                            str(e)[:200], seconds)
            else:
                log.exception("%s failed", name)
        await asyncio.sleep(seconds)


X_METRICS_INTERVAL_S = 600


def metrics_task(s: config.Settings, db: DB, pub):
    """The X analytics cycle (docs/x-analytics-spec.md §3), or None when there is no poster (NullPublisher).
    Reads run in a thread, so posting and rounds never wait on them; pull_due itself skips without
    can_read (dry_run or no OAuth 2.0 token). posting_paused doesn't stop reads."""
    poster = getattr(pub, "poster", None)
    if poster is None:
        return None
    from . import xstats

    async def x_metrics():
        try:
            await asyncio.to_thread(xstats.pull_due, s, db, poster)
        finally:
            await asyncio.to_thread(xstats.digest_due, s, db)
            if s.bluesky_enabled:  # free public reads (docs/bluesky-spec.md §6)
                from . import bskystats
                try:
                    await asyncio.to_thread(bskystats.pull, s, db)
                except Exception:
                    log.exception("bluesky metrics failed")
    return x_metrics


def reply_back_task(s: config.Settings, db: DB, pub):
    """F2 reply-back (docs/virality-2-spec.md), or None unless "reply-back" is in post_kinds and there is a
    poster. replyback.poll itself skips while killed, without can_read, or over its daily read budget."""
    poster = getattr(pub, "poster", None)
    from . import replyback
    if poster is None or not replyback.enabled(s):
        return None
    replyback.ensure_schema(db)
    return lambda: replyback.poll(s, db, poster)


YOUTUBE_INTERVAL_S = 600


def youtube_task(s: config.Settings, db: DB, client=None):
    """The YouTube Shorts cycle (docs/youtube-shorts-spec.md §8), or None unless youtube_enabled and motd_enabled.
    A separate loop (never inside motd.daily), run in a thread, so a slow upload can't delay X, rounds or the
    scheduler. Without a token it still runs in dry-run (outbox) mode, and otherwise logs once and idles."""
    if not (s.youtube_enabled and (s.motd_enabled or s.week_episode_enabled)):
        return None
    from . import youtube, ytstats

    async def cycle():
        c = client or youtube.client_from_settings(s, db)
        if c is None and not s.dry_run:
            if not db.get("yt_no_token_logged"):
                log.error("youtube_enabled but no YouTube token: run `agentpitbench youtube auth`")
                db.put("yt_no_token_logged", True)
            return
        await asyncio.to_thread(ytstats.pull_due, s, db, c)
    return cycle

def rounds_paused(s: config.Settings, db: DB) -> bool:
    """New rounds (batch, High-Stakes, summons, manual) are held while an anomaly or the operator paused them.
    In-flight rounds finish; the forecast sweep is never paused."""
    from .opai import paused
    if paused(s, db, "rounds"):
        log.warning("new rounds paused (%s); see `agentpitbench ops status`", (db.get("pause") or {}).get("reason"))
        return True
    return False


async def ops_tick(s: config.Settings, db: DB) -> None:
    """Every 5 min: anomaly detectors (pause + draft + alert), market-title translations, summary catch-up,
    the flag-gated daily cadence decision (§9) and the Sunday selection review (§5.3)."""
    from . import anomaly, market_i18n, policy, selection_review, summaries
    await asyncio.to_thread(anomaly.tick, s, db)
    for fn in (market_i18n.atick, summaries.atick, policy.due, selection_review.due):
        try:
            await fn(s, db)
        except Exception:
            log.exception("%s failed", fn.__module__)


async def run_forever(s: config.Settings) -> None:
    db = DB(s.db_path)
    api = Agentpit(s.api_url)
    pub = make_publisher(s, db)
    watcher, tracker = Watcher(s, db, api), Tracker(s, db, api, pub)
    tracker.recover()
    running: set[asyncio.Task] = set()

    async def start_batch():
        await watcher.poll()  # scan first: at startup the batch used to run before the first scan finished
        free = s.max_concurrent_rounds - len(running)
        if free <= 0 or rounds_paused(s, db):
            return
        if s.sandbox and not await sandbox_ok():  # never start rounds that would all 'crash' at launch
            log.error("batch skipped: the agent sandbox self-check failed; no rounds started, nothing posted")
            return
        # an agent whose most recent run hit a quota/infra failure (a later good run clears it)
        last_infra = db.last_infra_failure()
        if last_infra and time.time() - last_infra < s.infra_cooldown_s:  # e.g. a subscription quota hit:
            log.warning("batch skipped: an agent hit an infrastructure/quota failure %.1f h ago; "
                        "pausing new rounds for %.0f h", (time.time() - last_infra) / 3600, s.infra_cooldown_s / 3600)
            return  # retrying sooner would only void round after round while the others bet real tokens
        picked = await watcher.pick(free)
        if picked:  # refresh each login once, before parallel rounds copy the same refresh token
            await asyncio.gather(*(warm_login(a) for a in s.agents), return_exceptions=True)
        for m in picked:
            t = asyncio.create_task(Round(s, db, api, m, pub).run(), name=f"market-{m['id']}")
            running.add(t)
            t.add_done_callback(running.discard)
            log.info("round started for market %s: %s", m["id"], m["question"])

    async def poll():
        await watcher.poll()

    engage = getattr(pub, "engage", None)

    def launch(m: dict, after=None) -> None:
        async def go():
            if s.sandbox and not await sandbox_ok():
                log.error("round for market %s not started: the agent sandbox self-check failed", m["id"])
                return
            await asyncio.gather(*(warm_login(a) for a in s.agents), return_exceptions=True)
            rid = await Round(s, db, api, m, pub).run()
            if after:
                await after(rid)
        t = asyncio.create_task(go(), name=f"market-{m['id']}")
        running.add(t)
        t.add_done_callback(running.discard)
        log.info("round started for market %s: %s", m["id"], m["question"])

    async def scheduled():
        """Hourly: High-Stakes Friday, the day's summoned round, weekly race + awards, banner, champion."""
        if engage is None:
            return
        engage.api = api
        if len(running) < s.max_concurrent_rounds and not rounds_paused(s, db):
            m = await engage.high_stakes_market(watcher)
            if m:
                launch(m)
        if len(running) < s.max_concurrent_rounds and not rounds_paused(s, db):
            picked = await engage.pick_summon(watcher)
            if picked:
                row, m = picked
                launch(m, after=lambda rid: engage.summon_reply(row, rid))
        for job in (engage.weekly, engage.sunday_extras, engage.daily_banner, engage.champion_check):
            try:
                await job()
            except Exception:
                log.exception("%s failed", job.__name__)
        await ab_jobs()

    async def ab_jobs():
        """A/B (docs/ab-testing-spec.md): the generator (idle windows only), site-click counts, the digest."""
        from . import ab_gen, ab_report, cf
        try:
            r = await ab_gen.run(s, db, idle=lambda: not running)
            if r.get("status") != "skipped":
                log.info("A/B generator: %s", {k: v for k, v in r.items() if k != "candidates"})
        except Exception:
            log.exception("A/B generator failed")
        try:
            await asyncio.to_thread(cf.fetch_due, s, db)
        except Exception:
            log.exception("Cloudflare click counts failed")
        try:
            ab_report.digest_due(db, s)
        except Exception:
            log.exception("A/B digest failed")

    async def humans():
        if engage is not None:
            await engage.collect_due()

    async def daily_sweep():
        """06:00 UTC: the forecast sweep, sealed and published before any of its markets can resolve."""
        from . import forecast
        now = datetime.now(timezone.utc)
        day = forecast.today()
        if now.hour < s.sweep_hour_utc or db.get(f"sweep_{day}"):
            return
        status = await forecast.sweep(s, db, api)
        if status and all(v == "skipped" for v in status.values()):
            return  # sandbox broken: try again next hour
        await forecast.duels_due(s, db, api)
        forecast.seal(s, db, day)
        db.put(f"sweep_{day}", status or "empty")
        if hasattr(pub, "site"):
            from . import exports as ex
            ex.export(s, db)
            pub.site.request()

    async def score_forecasts():
        from . import forecast
        if await forecast.score_due(s, db, api) and engage is not None:
            await engage.duel_results()

    async def due_reveals():
        """Reveals held for their live window (virality-2 F1); also resumes holds after a restart."""
        if hasattr(pub, "post_due_reveals"):
            await pub.post_due_reveals()

    async def summons():
        if engage is not None:
            await engage.poll_summons()

    async def match_of_the_day():
        """Hourly; does nothing unless motd_enabled: at motd_hour_utc, yesterday's most dramatic round as a film."""
        if not s.motd_enabled or not s.motd_daily or not hasattr(pub, "cards"):
            return
        from . import motd
        if await motd.daily(s, db, pub.cards, pub.poster):
            pub.site.request()

    async def week_in_the_pit():
        """Hourly; does nothing unless week_episode_enabled: Sundays, the weekly episode (docs/week-in-the-pit-spec.md)."""
        if not s.week_episode_enabled or not hasattr(pub, "cards"):
            return
        from . import week
        if await week.weekly(s, db, pub.cards, pub.poster):
            pub.site.request()

    if engage is not None:
        await engage.cache_wallets()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("AgentpitBench running (dry_run=%s, posting_paused=%s)", s.dry_run, s.posting_paused)
    tasks = [
        asyncio.create_task(_every(s.poll_interval_s, poll, "watcher")),
        asyncio.create_task(_every(60, due_reveals, "due-reveals")),
        asyncio.create_task(_every(s.batch_interval_s, start_batch, "batch")),
        asyncio.create_task(_every(s.resolution_check_s, tracker.check, "tracker")),
        asyncio.create_task(_every(s.resolution_check_s, humans, "humans")),
        asyncio.create_task(_every(s.summon_poll_s, summons, "summons")),
        asyncio.create_task(_every(3600, scheduled, "scheduled")),
        asyncio.create_task(_every(1800, daily_sweep, "sweep")),
        asyncio.create_task(_every(3600, match_of_the_day, "match of the day")),
        asyncio.create_task(_every(1800, week_in_the_pit, "week in the pit")),
        asyncio.create_task(_every(s.resolution_check_s, score_forecasts, "forecast scoring")),
        asyncio.create_task(_every(300, lambda: ops_tick(s, db), "anomalies")),  # also runs once at startup
    ]
    x_metrics = metrics_task(s, db, pub)
    if x_metrics is not None:
        tasks.append(asyncio.create_task(_every(X_METRICS_INTERVAL_S, x_metrics, "x-metrics")))
    rb = reply_back_task(s, db, pub)
    if rb is not None:
        tasks.append(asyncio.create_task(_every(s.reply_back_poll_s, rb, "reply-back")))
    yt = youtube_task(s, db)
    if yt is not None:
        tasks.append(asyncio.create_task(_every(YOUTUBE_INTERVAL_S, yt, "youtube")))
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
    if rounds_paused(s, db):
        sys.exit("new rounds are paused (agentpitbench ops status)")
    api = Agentpit(s.api_url)
    pub = make_publisher(s, db)
    m = await api.market(market_id)
    if not m:
        sys.exit(f"market {market_id} not found")
    if not (m.get("active") and m.get("acceptingOrders")) or m.get("closed"):
        sys.exit(f"market {market_id} is not open for orders (closed or resolved)")
    if db.one("SELECT 1 FROM rounds WHERE market_id=?", str(m["id"])):
        sys.exit(f"market {market_id} already has a round")
    if s.sandbox and not await sandbox_ok():
        sys.exit("the agent sandbox self-check failed (see the log); refusing to start a round")
    rid = await Round(s, db, api, m, pub, agents=agents).run()
    db.x("INSERT OR IGNORE INTO seen_markets(market_id, first_seen, eligible, started) VALUES(?,strftime('%s'),1,1)",
         str(m["id"]))
    print(json.dumps(exports.round_record(s, db, db.round(rid)), indent=1))
    if hasattr(pub, "close"):
        await pub.close()
    await api.close()


async def motd_post(s: config.Settings, film: str) -> str | None:
    """Post one already-rendered film to X the production way (motd.post: the 9:16 film with the template post
    text, then the round link as a self-reply; post_key motd-<date>, so it posts once per date and the 17:00 job
    won't post a second film that day). dry_run / posting_paused send it to the outbox as usual."""
    from pathlib import Path
    from . import motd
    d = Path(film)
    rec = json.loads((d / "film.json").read_text())
    db = DB(s.db_path)
    pub = make_publisher(s, db)
    poster = getattr(pub, "poster", None)
    if poster is None:
        raise SystemExit("no X poster available (publishing layer missing)")
    try:
        tid = await motd.post(s, db, poster, rec, d)
    finally:
        if hasattr(pub, "close"):
            await pub.close()
    if tid:
        db.put(f"motd_{rec['date']}", "done")  # the daily job sees today's film as done
    print(f"posted {rec['date']} round {rec['round_id']}: {tid}" if tid else "not posted (see log)")
    return tid


async def motd_render(s: config.Settings, day: str | None, round_id: int | None, out: str | None) -> None:
    """Render a Match of the Day film for review. Never posts."""
    from . import motd
    from .cards import CardRenderer
    db = DB(s.db_path)
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cards = CardRenderer(s)
    try:
        rec = await motd.make_film(s, db, cards, day, round_id=round_id, out_dir=out)
    finally:
        await cards.close()
    print(json.dumps(rec, indent=1) if rec else f"no film for {day} (quiet day or round not eligible)")


async def week_render(s: config.Settings, wk: str | None, out: str | None, writer: bool,
                      voice_lines: int | None, now: float | None = None) -> None:
    """Render a Week in the Pit episode for review (docs/week-in-the-pit-spec.md). Never posts or uploads."""
    from . import week
    from .cards import CardRenderer
    db = DB(s.db_path)
    wk = wk or week.iso_week(now or time.time())
    cards = CardRenderer(s)
    try:
        rec = await week.make_episode(s, db, cards, wk, out_dir=out, writer=writer, voice_lines=voice_lines,
                                      now=now)
    finally:
        await cards.close()
    print(json.dumps(rec, indent=1, ensure_ascii=False) if rec else f"no episode for {wk} (no rounds or films)")


async def check(s: config.Settings) -> None:
    db = DB(s.db_path)
    api = Agentpit(s.api_url)
    pub = make_publisher(s, db)
    print("resolved:", await Tracker(s, db, api, pub).check())
    if hasattr(pub, "close"):
        await pub.close()
    await api.close()


async def sweep_now(s: config.Settings) -> None:
    from . import forecast
    db, api = DB(s.db_path), Agentpit(s.api_url)
    day = forecast.today()
    print(await forecast.sweep(s, db, api, day))
    print("sealed:", forecast.seal(s, db, day))
    exports.export(s, db)
    await api.close()


async def duel(s: config.Settings, agent: str, old: str, new: str) -> None:
    from . import forecast
    db, api = DB(s.db_path), Agentpit(s.api_url)
    forecast.queue_duel(db, agent, old, new)
    print(await forecast.duels_due(s, db, api))
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
    from . import xstats
    from .twitter import ME_URL, NEEDED_SCOPES, READ_SCOPES, Poster, token_file
    db = DB(s.db_path)
    p = Poster(s, db)
    if not p.oauth2.ready:
        sys.exit("OAuth 2.0 not configured: need X_CLIENT_ID, X_CLIENT_SECRET and X_OAUTH2_REFRESH_TOKEN")
    if not p.oauth2.scopes():
        p.oauth2.get(stale=p.oauth2._load().get("access_token"))  # first run: refresh so scopes are known
    scopes = p.oauth2.scopes()
    print(f"token file: {token_file()}  scopes: {' '.join(sorted(scopes))}")
    missing = (NEEDED_SCOPES | READ_SCOPES) - scopes
    print("missing scopes:", ", ".join(sorted(missing)) if missing else "none",
          "(re-run x-login)" if missing else "")
    me = p.read(ME_URL, {}, "user", "me")["data"]   # a billed user read: ledgered like every other read
    print(f"posting as @{me['username']} ({me['name']})")
    r = xstats.reads(db, s=s)
    print(f"reads today: ${r['usd']:.3f} ({r['n']} billed); by caller: "
          + (", ".join(f"{c} {v['n']} ${v['usd']:.3f}" for c, v in r["by_caller"].items()) or "none"))
    print(f"metrics budget: ${s.x_metrics_budget_usd:.2f}/day (spent ${r['metrics_usd']:.3f}); "
          f"alert at ${s.x_read_alert_usd:.2f}/day")
    print(f"x_app_owned={s.x_app_owned}: metrics pull in {'timeline' if s.x_app_owned else 'lookup'} mode")
    if probe_upload:
        img = s.data_dir / "x-probe.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(_probe_png())
        print("media upload ok, id", p.upload(img), "(not attached to any tweet; expires in 24h)")


def x_metrics_now(s: config.Settings, force_budget: bool) -> None:
    """One pull_due cycle now (billed reads; respects the daily budget unless --force-budget)."""
    from . import xstats
    from .twitter import Poster
    db = DB(s.db_path)
    p = Poster(s, db)
    if not p.can_read:
        sys.exit("cannot read from X: dry_run is on or OAuth 2.0 is not configured")
    print(json.dumps(xstats.pull_due(s, db, p, force_budget=force_budget)))


def _table(rows: list[dict]) -> str:
    if not rows:
        return "  (no rows)"
    cols = list(rows[0])
    fmt = lambda v: "-" if v is None else (f"{v:.4f}" if isinstance(v, float) and v < 1 else str(v))  # noqa: E731
    cells = [[fmt(r.get(c)) for c in cols] for r in rows]
    w = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    return "\n".join("  " + "  ".join(x.ljust(w[i]) for i, x in enumerate(line)) for line in [cols, *cells])


def x_stats(s: config.Settings, days: float, by: str, bucket: str) -> None:
    """Private engagement tables for the operator (nothing here is published)."""
    from . import xstats
    db = DB(s.db_path)
    since = time.time() - days * 86400
    fn = {"kind": xstats.by_kind, "agent": xstats.by_agent, "category": xstats.by_category}[by]
    print(f"by {by}, {bucket} snapshots, posts from the last {days:g} day(s):")
    print(_table(fn(db, since, bucket)))
    if bucket in ("1h", "24h"):
        print(f"\nheadline variant (results, {bucket}):")
        print(_table([{"variant": k, **v} for k, v in xstats.reward_by_variant(db, bucket, since=since, s=s).items()]))
    print("\nfollowers:")
    print(_table(xstats.followers_series(db, since)[-int(days) - 1:]))
    r = xstats.reads(db, s=s)
    print(f"\nreads today ({r['day']}): {r['n']} billed, ${r['usd']:.3f} "
          f"(metrics ${r['metrics_usd']:.3f} of ${s.x_metrics_budget_usd:.2f})")
    print(_table([{"caller": k, **v} for k, v in r["by_caller"].items()]))
    print("\nhealth:", json.dumps(xstats.health(db)))
    drop = xstats.visibility_drop(db)
    if drop:
        print("VISIBILITY DROP:", json.dumps(drop))
    from . import northstar
    print()
    print(northstar.line(northstar.status(db)))


def north_star(s: config.Settings, live: bool, as_json: bool) -> None:
    """The month-1 North Star (docs/marketing-strategy.md; agentpitbench/northstar.py). Completed days come from
    the bot's daily Cloudflare read; --live adds today so far (one Cloudflare query, nothing stored)."""
    from . import cf, northstar
    db = DB(s.db_path)
    today = None
    if live:
        today = cf.read_day(s, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        if today is None:
            print("no CF_API_TOKEN / CF_ACCOUNT_TAG: completed days only", file=sys.stderr)
    st = northstar.status(db, today=today)
    if as_json:
        print(json.dumps(st, indent=1))
        return
    print(northstar.report(st))


def x_boosted(s: config.Settings, tweet_id: str, off: bool, check: bool) -> None:
    from . import xstats
    db = DB(s.db_path)
    if check:
        from .twitter import Poster
        p = Poster(s, db)
        if not p.can_read:
            sys.exit("cannot read from X: dry_run is on or OAuth 2.0 is not configured")
        out = xstats.boost_check(db, p, tweet_id)
        print(json.dumps(out))
        if out.get("found") and not out.get("promoted"):
            print("promoted_metrics empty: the post was never promoted")
        return
    if not xstats.set_boosted(db, tweet_id, not off):
        sys.exit(f"{tweet_id} is not one of the bench's posts")
    print(f"{tweet_id}: boosted={'0' if off else '1'}" + ("" if off else " (excluded from rewards)"))


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


def youtube_auth(s: config.Settings, redirected: str | None, port: int, no_wait: bool) -> None:
    """One-time: authorize the AgentpitBench brand channel (installed-app OAuth, loopback redirect)."""
    from . import youtube
    tok = youtube.Token()
    if not tok.configured:
        sys.exit("need YT_CLIENT_ID and YT_CLIENT_SECRET in the secrets file (~/.config/agentpitbench/secrets.env)")
    if redirected:
        print("logged in; scopes:", " ".join(sorted(tok.finish_login(redirected))))
        print("next: agentpitbench youtube check")
        return
    redirect = f"http://127.0.0.1:{port}"
    url = tok.authorize_url(redirect)
    print("1. Open this URL in a browser signed in as the Workspace account that owns the brand channel:\n")
    print(url)
    print("\n2. Choose the AgentpitBench channel (not the personal one) on the account picker, then Allow.")
    print(f"   Headless server? Forward the port from your laptop first:  ssh -L {port}:127.0.0.1:{port} <server>")
    if no_wait:
        print("3. The browser lands on http://127.0.0.1:... (it may fail to load). Copy that whole URL and run:\n"
              "   agentpitbench youtube auth --url '<that URL>'")
        return
    print(f"3. Waiting up to 5 minutes for the redirect on {redirect} ... "
          "(if it can't reach here, Ctrl-C and use --url with the address-bar URL)")
    got = youtube.wait_for_redirect(port)
    if not got:
        sys.exit("no redirect received; copy the browser's address-bar URL and run: "
                 "agentpitbench youtube auth --url '<URL>'")
    print("logged in; scopes:", " ".join(sorted(tok.finish_login(got))))
    print("next: agentpitbench youtube check")


def youtube_check(s: config.Settings, client=None) -> None:
    """Token refreshes, scopes, the channel behind the token vs youtube_channel_id, category, quota, health."""
    from . import youtube, ytstats
    db = DB(s.db_path)
    tok = client.token if client else youtube.Token()
    print(f"token file: {tok.path} ({'present' if tok.path.exists() else 'MISSING'})")
    if not tok.configured:
        sys.exit("need YT_CLIENT_ID and YT_CLIENT_SECRET in the secrets file")
    if not tok.ready:
        sys.exit("no token: run `agentpitbench youtube auth`")
    tok.get(stale=tok._load().get("access_token"))   # force a refresh: proves the refresh token works
    d = tok._load()
    age_days = (time.time() - d.get("created_at", time.time())) / 86400
    print(f"token refresh: OK (refresh token is {age_days:.1f} days old; an Internal app's doesn't expire at 7 days)")
    missing = set(youtube.SCOPES) - tok.scopes()
    print("scopes:", " ".join(sorted(tok.scopes())), "| missing:", ", ".join(sorted(missing)) or "none")
    c = client or youtube.Client(s, db, tok)
    ch = c.my_channel()
    sn = ch.get("snippet", {})
    print(f"token channel: {ch['id']}  {sn.get('title')!r}  {sn.get('customUrl') or ''}")
    if not s.youtube_channel_id:
        print(f"channel guard: youtube_channel_id is empty (uploads refused). If this is the AgentpitBench brand "
              f"channel, put\n  youtube_channel_id = \"{ch['id']}\"\nin bench.toml")
    elif ch["id"] == s.youtube_channel_id:
        print("channel guard: OK")
    else:
        print(f"channel guard: MISMATCH (bench.toml has {s.youtube_channel_id}); uploads are refused. "
              "Re-run `youtube auth` and choose the AgentpitBench channel.")
    print(f"category {s.youtube_category_id}:",
          "assignable" if c.category_ok(s.youtube_category_id) else "NOT assignable")
    print(f"youtube_enabled={s.youtube_enabled} motd_enabled={s.motd_enabled} youtube_privacy={s.youtube_privacy} "
          f"dry_run={s.dry_run} posting_paused={s.posting_paused}")
    print("quota today (Pacific):", json.dumps(ytstats.quota(db)))
    print("health:", json.dumps(ytstats.health(db)))


def _film(s: config.Settings, day: str):
    from . import motd
    f = motd.film_dir(s, day) / "film.json"
    if not f.exists():
        sys.exit(f"no film for {day} ({f})")
    return json.loads(f.read_text()), f.parent


def youtube_meta(s: config.Settings, day: str) -> None:
    from . import youtube, ytstats
    rec, _ = _film(s, day)
    print(youtube.metadata_text(s, rec, ytstats.round_export(s, DB(s.db_path), rec["round_id"])))


def youtube_upload(s: config.Settings, day: str, client=None) -> None:
    from . import youtube, ytstats
    db = DB(s.db_path)
    rec, d = _film(s, day)
    cur = youtube.row(db, youtube.post_key(day))
    if cur and cur.get("video_id"):
        sys.exit(f"already uploaded: https://youtube.com/shorts/{cur['video_id']}")
    c = client or youtube.client_from_settings(s, db)
    if c is None and not (s.dry_run or s.posting_paused):
        sys.exit("no YouTube token: run `agentpitbench youtube auth`")
    print(json.dumps(youtube.upload(s, db, c, rec, d, ytstats.round_export(s, db, rec["round_id"]), manual=True),
                     indent=1))


def youtube_stats(s: config.Settings, days: float) -> None:
    from . import ytstats
    db = DB(s.db_path)
    since = time.time() - days * 86400
    print(f"films from the last {days:g} day(s), X beside YouTube:")
    print(_table(ytstats.by_film(db, since)))
    print("\nsubscribers:")
    print(_table(ytstats.followers_series(db, since)))
    print("\nquota:", json.dumps(ytstats.quota(db)))
    print("health:", json.dumps(ytstats.health(db)))


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
    sub.add_parser("sweep", help="run today's forecast sweep now (idempotent) and seal it")
    du = sub.add_parser("duel", help="launch-day duel: an agent's old vs new model on today's sweep markets")
    du.add_argument("agent")
    du.add_argument("old_model")
    du.add_argument("new_model")
    ds = sub.add_parser("dataset", help="build the monthly open dataset (and upload if HF/Kaggle creds are set)")
    ds.add_argument("--month", required=True, help="YYYY-MM")
    mo = sub.add_parser("motd", help="render a Match of the Day film (both formats) for review; never posts. "
                                     "`motd post --film DIR` posts an already-rendered film to X")
    mo.add_argument("action", nargs="?", choices=["render", "post"], default="render")
    mo.add_argument("--film", help="post: the film directory (with film.json and the 9:16 mp4)")
    mo.add_argument("--date", help="film date YYYY-MM-DD (covers rounds resolved the day before; default today UTC)")
    mo.add_argument("--round", type=int, help="film this round instead of the day's pick")
    mo.add_argument("--out", help="output directory (default: cards/motd/<date>/)")
    wk = sub.add_parser("week", help="render a Week in the Pit episode (16:9, 1:1 and the trailer) for review; "
                                     "never posts")
    wk.add_argument("--week", help="ISO week YYYY-Www (default: this week so far)")
    wk.add_argument("--out", help="output directory (default: cards/week/<week>/)")
    wk.add_argument("--no-writer", action="store_true", help="templated host lines (no opai call)")
    wk.add_argument("--voice-lines", type=int, default=None,
                    help="narrate at most this many uncached host lines (sample renders); default: all in budget")
    wk.add_argument("--now", help="render as of this UTC time (ISO), e.g. a past week's Sunday evening")
    xc = sub.add_parser("x-check", help="refresh the X OAuth 2.0 token, show scopes and account (never tweets)")
    xc.add_argument("--upload", action="store_true", help="also upload a tiny test image (not attached to a tweet)")
    xm = sub.add_parser("x-metrics", help="run one X metrics pull now (billed reads, within the daily budget)")
    xm.add_argument("--force-budget", action="store_true", help="operator only: ignore x_metrics_budget_usd")
    xs = sub.add_parser("x-stats", help="print the private X engagement tables (no X reads)")
    xs.add_argument("--days", type=float, default=7)
    xs.add_argument("--by", choices=["kind", "agent", "category"], default="kind")
    xs.add_argument("--bucket", choices=["1h", "24h", "72h", "7d"], default="24h")
    ns = sub.add_parser("northstar", help="month-1 North Star: leaderboard visitors vs 1,000, Beat the AIs "
                                          "attempts vs 100 (private; Cloudflare, cookie-free)")
    ns.add_argument("--live", action="store_true", help="also read today so far from Cloudflare (stores nothing)")
    ns.add_argument("--json", action="store_true")
    xb = sub.add_parser("x-boosted", help="mark a post as promoted (excluded from rewards)")
    xb.add_argument("tweet_id")
    g = xb.add_mutually_exclusive_group()
    g.add_argument("--off", action="store_true", help="unmark it")
    g.add_argument("--check", action="store_true", help="one billed read of its organic/promoted metrics")
    abp = sub.add_parser("ab", help="A/B testing of post wording: status, retire, propose, validate, add, digest")
    absub = abp.add_subparsers(dest="ab_cmd", required=True)
    st = absub.add_parser("status", help="arms per slot with n, mean ± sd, p_best and a posted example")
    st.add_argument("--slot")
    rt = absub.add_parser("retire", help="retire an arm (never the control); promotes the oldest queued arm")
    rt.add_argument("arm_id", type=int)
    rt.add_argument("--note")
    pr = absub.add_parser("propose", help="run the copy generator now (same guards, incl. the idle check)")
    pr.add_argument("--dry", action="store_true", help="print candidates and validation results, insert nothing")
    va = absub.add_parser("validate", help="validate a hand-written arm set: {slot: [template, ...]}")
    va.add_argument("file")
    ad = absub.add_parser("add", help="insert a hand-written arm set (operator-written arms)")
    ad.add_argument("file")
    dg = absub.add_parser("digest", help="print the A/B digest")
    dg.add_argument("--date")
    au = absub.add_parser("audit", help="A/B v2 data audit (read-only): slots, posts/day, rewards, covariates")
    au.add_argument("--since", help="window start YYYY-MM-DD (default ab_audit_since)")
    au.add_argument("--include-stale", action="store_true",
                    help="count 24 h snapshots taken late too (the plan's §0 table did)")
    au.add_argument("--json", action="store_true")
    pw = absub.add_parser("power", help="A/B v2: posts/day per slot -> days to detect d = 0.8 / 0.5 / 0.3 / 0.2")
    pw.add_argument("--slot", help="one slot (default: all)")
    pw.add_argument("--since", help="window start for posts/day (default ab_audit_since)")
    pw.add_argument("--arms", type=int, default=2)
    pw.add_argument("--posts-per-day", type=float, help="override the measured rate")
    pw.add_argument("--var-factor", type=float, default=1.0, help="variance left after covariate adjustment")
    sm = absub.add_parser("sim", help="A/B v2 simulator (read-only): v1 vs v2 rules and policies on bootstrapped rewards")
    sm.add_argument("--since")
    sm.add_argument("--rate", type=float, default=3.0, help="posts per day per slot")
    sm.add_argument("--reps", type=int, default=1000)
    sm.add_argument("--out", help="write the markdown table here (e.g. docs/ab-v2-sim.md)")
    rbp = sub.add_parser("reply-back", help="F2 reply-back kill switch: --off (manual flag, alerts), --on, status")
    g = rbp.add_mutually_exclusive_group()
    g.add_argument("--off", metavar="REASON", help="switch reply-back off now")
    g.add_argument("--on", action="store_true", help="clear the kill switch (after review)")
    xl = sub.add_parser("x-login", help="authorize the X account with every scope the bench needs")
    xl.add_argument("--url", help="the URL X redirected you to (step 2)")
    xl.add_argument("--redirect-uri", help="callback URL registered on the X app (default: site_url)")
    yt = sub.add_parser("youtube", help="YouTube Shorts: auth, check, meta, upload, stats")
    ytsub = yt.add_subparsers(dest="yt_cmd", required=True)
    ya = ytsub.add_parser("auth", help="one-time: connect the AgentpitBench brand channel (OAuth, loopback)")
    ya.add_argument("--url", help="the 127.0.0.1 URL the browser was redirected to (finishes the login)")
    ya.add_argument("--port", type=int, default=8765, help="loopback port for the redirect (default 8765)")
    ya.add_argument("--no-wait", action="store_true", help="print the URL only; finish later with --url")
    ytsub.add_parser("check", help="verify the token refreshes and the channel matches youtube_channel_id")
    ym = ytsub.add_parser("meta", help="print a film's title, description and tags (no network)")
    ym.add_argument("--date", required=True)
    yu = ytsub.add_parser("upload", help="upload one film now (respects pause and dry-run)")
    yu.add_argument("--date", required=True)
    ys = ytsub.add_parser("stats", help="X beside YouTube per film, subscribers, quota")
    ys.add_argument("--days", type=float, default=7)
    from . import opscli  # operator AI, methodology lock, ops/alerts/incidents (docs/ai-automation-spec.md §14)
    opscli.add_parsers(sub)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.load(a.config)
    if a.live:
        s.dry_run = False
    if a.cmd in opscli.COMMANDS:
        opscli.dispatch(s, a)
    elif a.cmd == "run":
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
    elif a.cmd == "dataset":
        from . import dataset
        print(dataset.build(s, DB(s.db_path), a.month))
    elif a.cmd == "whoami":
        asyncio.run(whoami(s))
    elif a.cmd == "sweep":
        asyncio.run(sweep_now(s))
    elif a.cmd == "duel":
        asyncio.run(duel(s, a.agent, a.old_model, a.new_model))
    elif a.cmd == "motd" and a.action == "post":
        if not a.film:
            raise SystemExit("motd post needs --film DIR")
        asyncio.run(motd_post(s, a.film))
    elif a.cmd == "motd":
        asyncio.run(motd_render(s, a.date, a.round, a.out))
    elif a.cmd == "week":
        asyncio.run(week_render(s, a.week, a.out, not a.no_writer, a.voice_lines,
                                datetime.fromisoformat(a.now).replace(tzinfo=timezone.utc).timestamp() if a.now else None))
    elif a.cmd == "x-login":
        x_login(s, a.url, a.redirect_uri)
    elif a.cmd == "x-check":
        x_check(s, a.upload)
    elif a.cmd == "x-metrics":
        x_metrics_now(s, a.force_budget)
    elif a.cmd == "x-stats":
        x_stats(s, a.days, a.by, a.bucket)
    elif a.cmd == "x-boosted":
        x_boosted(s, a.tweet_id, a.off, a.check)
    elif a.cmd == "northstar":
        north_star(s, a.live, a.json)
    elif a.cmd == "youtube":
        if a.yt_cmd == "auth":
            youtube_auth(s, a.url, a.port, a.no_wait)
        elif a.yt_cmd == "check":
            youtube_check(s)
        elif a.yt_cmd == "meta":
            youtube_meta(s, a.date)
        elif a.yt_cmd == "upload":
            youtube_upload(s, a.date)
        elif a.yt_cmd == "stats":
            youtube_stats(s, a.days)
    elif a.cmd == "ab":
        ab_cli(s, a)
    elif a.cmd == "reply-back":
        reply_back_cli(s, a.off, a.on)


def reply_back_cli(s: config.Settings, off: str | None, on: bool) -> None:
    from . import replyback
    db = DB(s.db_path)
    if off:
        replyback.kill(s, db, off, manual=True)
    elif on:
        replyback.resume(db)
    replyback.ensure_schema(db)
    print(json.dumps({"listed_in_post_kinds": replyback.enabled(s), "killed": replyback.killed(db),
                      "answered_today": replyback.answered_today(db, time.time()),
                      "optouts": db.one("SELECT COUNT(*) n FROM reply_optouts")["n"]}, indent=1))


def _round_running(db: DB) -> bool:
    return db.one("SELECT 1 FROM rounds WHERE state='running' LIMIT 1") is not None


def ab_cli(s: config.Settings, a) -> None:
    from . import ab, ab_gen, ab_report
    if a.ab_cmd == "audit":   # read-only: never seeds or migrates
        import dataclasses
        from . import ab_audit
        if a.include_stale:
            s = dataclasses.replace(s, x_reward_max_age_ratio=1e9)
        r = ab_audit.audit(DB.readonly(s.db_path), s, a.since)
        if a.json:
            print(json.dumps(r, indent=1, ensure_ascii=False))
        else:
            ab_audit.print_audit(r)
        return
    if a.ab_cmd == "power":
        from . import ab_audit, ab_sim
        rates = {x["slot"]: x["kind_posts_per_day"] for x in
                 ab_audit.slot_report(DB.readonly(s.db_path), s, ab_audit.ts(a.since or s.ab_audit_since), time.time())}
        for sl in ([a.slot] if a.slot else list(ab.SLOTS)):
            rate = a.posts_per_day if a.posts_per_day is not None else (1.0 if sl == "tone" else rates.get(sl))  # tone: 1 obs/day
            if rate is None:
                print(f"{sl}: no X posts/day measured (dead, or not an X slot); pass --posts-per-day")
                continue
            print("\n".join(ab_sim.power_lines(sl, rate, a.arms, a.var_factor)))
        return
    if a.ab_cmd == "sim":
        from . import ab_sim
        ab_sim.main(["--rate", str(a.rate), "--reps", str(a.reps)] + (["--since", a.since] if a.since else [])
                    + (["--out", a.out] if a.out else []) + (["--config", a.config] if a.config else []))
        return
    db = DB(s.db_path)
    ab.seed(db)
    if a.ab_cmd == "status":
        ab_report.print_status(db, s, a.slot)
    elif a.ab_cmd == "retire":
        try:
            promoted = ab.retire(db, a.arm_id, a.note)
        except ValueError as e:
            sys.exit(str(e))
        print(f"arm {a.arm_id} retired" + (f"; arm {promoted} is live" if promoted else ""))
    elif a.ab_cmd == "propose":
        if not a.dry and not s.ab_gen_enabled:
            sys.exit("ab_gen_enabled = false: review a --dry batch first, then set ab_gen_enabled = true in bench.toml")
        r = asyncio.run(ab_gen.run(s, db, idle=lambda: not _round_running(db), dry=a.dry, force=True))
        print(json.dumps(r, indent=1, ensure_ascii=False))
    elif a.ab_cmd in ("validate", "add"):
        sets = json.loads(Path(a.file).read_text())
        bad = 0
        for slot, items in sets.items():
            for tpl in items if isinstance(items, list) else [items]:
                errs = ab.validate(slot, tpl)
                bad += bool(errs)
                if a.ab_cmd == "add" and not errs:
                    aid, status = ab.add_arm(db, s, slot, tpl)
                    print(f"{slot}: arm {aid} {status}")
                else:
                    print(f"{slot}: {'OK' if not errs else 'REJECTED: ' + '; '.join(errs)}  {json.dumps(tpl, ensure_ascii=False)}")
        if bad:
            sys.exit(1)
    elif a.ab_cmd == "digest":
        print(json.dumps(ab_report.digest(db, s, a.date), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
