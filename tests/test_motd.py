"""Match of the Day (docs/match-of-the-day-spec.md v0.4): drama score, selection window, script, the 45-60 s rule,
the daily hook (flag, idempotency, dry-run outbox) and a two-format render smoke test with the music bed."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentpitbench import media, motd, music
from agentpitbench.cards import CardRenderer
from agentpitbench.config import AGENT_NAMES, Settings
from agentpitbench.db import DB
from agentpitbench.twitter import Poster

from test_publish import AGENTS, env, make_round  # noqa: F401  (env is a fixture)

DAY = "2026-10-12"
IN_WINDOW = datetime(2026, 10, 11, 15, tzinfo=timezone.utc).timestamp()


def E(agent, outcome, won, price=0.5, conf=0.5, quote=None, pnl=None, decided=60.0, statement=None, exit_reason="bet"):
    return {"agent": agent, "name": AGENT_NAMES[agent], "outcome": outcome, "won": won if outcome else False,
            "avg_price": price if outcome else None, "confidence": conf if outcome else None, "quote": quote,
            "pnl": pnl if pnl is not None else ((100 / price - 100) if won else -100.0) if outcome else None,
            "decided_s": decided if outcome else None, "statement": statement, "exit_reason": exit_reason,
            "stake_filled": 100 if outcome else 0, "model_reported": PLAYED.get(agent)}


# the model each agent reported in these rounds: ChatGPT on GPT-6 Luna, though config now names GPT-6 Astra
PLAYED = {"claude": "claude-fable-5-1", "codex": "gpt-6-luna", "agy": "Gemini 3.1 Pro (High)", "grok": "grok-4.7"}


def R(rid, entries, winner="A", resolved_at=IN_WINDOW, exhibition=False, humans=None, collected=1, **kw):
    return {"round_id": rid, "state": "resolved", "winner": winner, "resolved_at": resolved_at,
            "exhibition": exhibition, "question": f"Market {rid}?", "outcomes": ["A", "B"],
            "prices_at_start": [0.6, 0.4], "end_date": "2026-10-11T12:00:00Z", "entries": entries, "stake": 100.0,
            "crowd": {"outcome": "A", "price": 0.6, "won": winner == "A", "pnl": 66.67 if winner == "A" else -100.0},
            "humans": humans or {"picked": 0, "by_outcome": {}, "right": None}, "humans_collected": collected, **kw}


def keys(sc):
    return {s["key"]: s["points"] for s in sc["signals"]}


# ---------- score ----------

def test_lone_wolf_and_upset():
    r = R(1, [E("claude", "B", True, price=0.2), E("codex", "A", False), E("agy", "A", False), E("grok", "A", False)],
          winner="B")
    sc = motd.score(r, [r])
    assert keys(sc) == {"lone_wolf": 40, "upset": 35}
    assert sc["total"] == 75 and sc["factor"] == 1.0
    assert motd.hook_line(r, sc) == "3 AIs said A. Claude said B."
    assert motd.key_agent(r, sc)["agent"] == "claude"


def test_lone_wolf_needs_two_losing_bettors():
    # one winner, one loser, two no-bets: not a lone-wolf story
    r = R(1, [E("claude", "B", True), E("codex", "A", False), E("agy", None, False), E("grok", None, False)], winner="B")
    assert "lone_wolf" not in keys(motd.score(r, [r]))


def test_two_two_and_two_one_one_splits_are_not_lone_wolf():
    r22 = R(1, [E("claude", "A", True), E("codex", "A", True), E("agy", "B", False), E("grok", "B", False)])
    assert keys(motd.score(r22, [r22])) == {}
    r211 = R(2, [E("claude", "A", True), E("codex", "A", True), E("agy", "B", False), E("grok", "C", False)])
    assert keys(motd.score(r211, [r211])) == {}


def test_wipeout_and_unanimous():
    w = R(1, [E(a, "A", False) for a in AGENTS], winner="B")
    sc = motd.score(w, [w])
    assert keys(sc) == {"wipeout": 30} and motd.hook_line(w, sc) == "All 4 AIs backed A."
    u = R(2, [E(a, "A", True) for a in AGENTS])
    assert keys(motd.score(u, [u])) == {"unanimous": 5}
    # a wipeout needs three bettors
    w2 = R(3, [E("claude", "A", False), E("codex", "A", False), E("agy", None, False), E("grok", None, False)], winner="B")
    assert "wipeout" not in keys(motd.score(w2, [w2]))


def test_quotes_two_points_each():
    r = R(1, [E("claude", "A", True, quote="hi"), E("codex", "A", True, quote="  "), E("agy", "B", False, quote="yo"),
              E("grok", "B", False)])
    assert keys(motd.score(r, [r])) == {"quotes": 4}


def test_no_grudge_signal():
    """The grudge match (betting head to head under another name) is gone (docs/simple-scoring-spec.md D4)."""
    hist = [R(i, [E("claude", "A", True), E("codex", "B", False), E("agy", "A", True), E("grok", "A", True)],
              resolved_at=IN_WINDOW - 86400 * (4 - i)) for i in (1, 2)]
    now = R(3, [E("claude", "A", True), E("codex", "B", False), E("agy", "A", True), E("grok", "A", True)])
    sc = motd.score(now, hist + [now])
    assert "grudge" not in keys(sc) and "grudge" not in motd.SIGNALS
    assert "opposite sides" not in motd.hook_line(now, sc)


def test_streak_flip_counts_only_earlier_counted_rounds():
    def past(i, won_grok, **kw):
        return R(i, [E("grok", "A", won_grok), E("claude", "A", won_grok)], winner="A" if won_grok else "B",
                 resolved_at=IN_WINDOW - 1000 * (10 - i), **kw)
    hist = [past(1, True), past(2, True), past(3, True)]
    now = R(9, [E("grok", "B", False), E("claude", "A", True), E("codex", "A", True), E("agy", "A", True)])
    sc = motd.score(now, hist + [now])
    assert keys(sc)["streak_flip"] == 15
    assert motd.key_agent(now, sc)["agent"] == "grok"
    # an exhibition in the middle doesn't count; a later round doesn't either
    hist2 = [past(1, True), past(2, True, exhibition=True), past(3, True),
             R(10, [E("grok", "A", True)], resolved_at=IN_WINDOW + 10)]
    assert "streak_flip" not in keys(motd.score(now, hist2 + [now]))


def test_humans_signal():
    h = {"picked": 6, "by_outcome": {"B": 4, "A": 2}, "right": 4}
    ents = [E("claude", "A", False), E("codex", "A", False), E("agy", "A", False), E("grok", "B", True)]
    r = R(1, ents, winner="B", humans=h)
    assert keys(motd.score(r, [r]))["humans"] == 15
    assert "humans" not in keys(motd.score(R(1, ents, winner="B", humans=h, collected=0), []))
    assert "humans" not in keys(motd.score(R(1, ents, winner="B", humans={**h, "picked": 4}), []))
    tie = {"picked": 6, "by_outcome": {"B": 3, "A": 3}, "right": 3}
    assert "humans" not in keys(motd.score(R(1, ents, winner="B", humans=tie), []))


def test_exhibition_factor():
    r = R(1, [E(a, "A", False) for a in AGENTS], winner="B", exhibition=True)
    sc = motd.score(r, [r], exhibition_factor=0.5)
    assert sc["raw"] == 30 and sc["total"] == 15 and sc["factor"] == 0.5


# ---------- pick ----------

def test_pick_window_ties_and_quiet_day():
    wipe = lambda rid, t: R(rid, [E(a, "A", False) for a in AGENTS], winner="B", resolved_at=t)  # noqa: E731
    lo, hi = motd.window(DAY)
    assert hi - lo == 86400 and datetime.fromtimestamp(lo, timezone.utc).strftime("%Y-%m-%d") == "2026-10-11"
    early, late = wipe(1, lo + 60), wipe(2, hi - 60)
    outside = R(3, [E("claude", "B", True, price=0.1), E("codex", "A", False), E("agy", "A", False),
                    E("grok", "A", False)], winner="B", resolved_at=hi + 1)
    p = motd.pick([early, late, outside], DAY)
    assert p["round"]["round_id"] == 2 and [c["round_id"] for c in p["candidates"]] == [2, 1]
    quiet = R(4, [E(a, "A", True) for a in AGENTS])  # unanimous and right: 5 < 20
    assert motd.pick([quiet], DAY) is None
    assert motd.pick([], DAY) is None
    void = {**wipe(5, lo + 5), "state": "void"}
    assert motd.pick([void], DAY) is None
    one_bet = R(6, [E("claude", "A", False), E("codex", None, False)], winner="B")
    assert not motd.eligible(one_bet)


# ---------- script ----------

LB = {"updated_at": IN_WINDOW, "season": "2026-10", "rounds_counted": 12,
      "agents": [{"agent": a, "name": AGENT_NAMES[a], "wins": 3, "losses": 1, "net_pnl": 12.5} for a in AGENTS]}
THINK = ["I'll check the order book and recent form before betting on this one.",
         "The favourite has won nine of its last ten series on LAN, which matters here.",
         "The underdog plays with a stand-in, so the market price looks fair to me.",
         "Placing the bet now: the favourite at a fair price with the clock running down.",
         "Bet placed on A with all 100 tokens."]


def lone():
    return R(7, [E("claude", "B", True, price=0.2, conf=0.3, quote="Watch this.", decided=120),
                 E("codex", "A", False, price=0.62, conf=0.7, decided=30, statement="I got this one wrong."),
                 E("agy", "A", False, price=0.63, conf=0.9, decided=45),
                 E("grok", None, False, exit_reason="timeout")], winner="B")


def test_script_scenes_names_numbers_and_rounds_strip():
    r = lone()
    sc = motd.score(r, [r])
    scenes = motd.script(r, sc, LB, THINK)
    kinds = [s["kind"] for s in scenes]
    assert kinds == ["hook", "setup", "pick", "pick", "pick", "nobet", "think", "reveal", "after", "close"]
    picks = [s for s in scenes if s["kind"] == "pick"]
    assert [p["name"] for p in picks] == ["ChatGPT", "Gemini", "Claude"]  # decision-time order, codex shown as ChatGPT
    assert all("Codex" not in json.dumps(s) for s in scenes)
    claude = picks[2]
    assert claude["key"] and claude["dur"] == 6.5 and claude["entry"] == "20¢" and claude["conf"] == "30%"
    assert claude["mkt"] == "40%" and claude["against"] is True  # market had B at 40%, Claude said 30%
    assert picks[0]["dur"] == 3.5 and picks[0]["mkt"] == "60%"
    nobet = next(s for s in scenes if s["kind"] == "nobet")
    assert nobet["dur"] == 2.0 and nobet["why"] == "timed out" and nobet["name"] == "Grok"
    # the strip: the betting record's sample on every scene, no significance claim (simple-scoring-spec §4)
    assert claude["sig"] == scenes[0]["sig"] == "12 rounds · season 2026-10"
    assert claude["model_line"].startswith("Claude Fable")
    think = next(s for s in scenes if s["kind"] == "think")
    assert think["agent"] == "claude" and think["lines"] == THINK[:4]  # stops at the bet, nothing after it
    reveal = next(s for s in scenes if s["kind"] == "reveal")
    assert reveal["winner"] == "B" and reveal["right"] == 1
    assert reveal["rows"][0]["name"] == "Claude" and reveal["rows"][0]["pnl_s"] == "+400"
    after = next(s for s in scenes if s["kind"] == "after")
    assert after["statements"] == [{"agent": "codex", "name": "ChatGPT", "text": "I got this one wrong."}]
    close = scenes[-1]
    assert close["url"] == "agentpitbench.org/round/7"
    assert "forecast" not in close["standings"] and "crowd_brier" not in close["standings"]
    assert [a["name"] for a in close["standings"]["betting"]] == ["Claude", "ChatGPT", "Gemini", "Grok"]
    assert motd.MIN_S <= sum(s["dur"] for s in scenes) <= motd.MAX_S


def test_scene_skipping_and_length_floor():
    r = lone()
    for e in r["entries"]:
        e["statement"] = None
    sc = motd.score(r, [r])
    scenes = motd.script(r, sc, LB, THINK[:2])  # too few lines: no thinking scene; no statements: no aftermath
    kinds = [s["kind"] for s in scenes]
    assert "think" not in kinds and "after" not in kinds
    total = sum(s["dur"] for s in scenes)
    assert motd.MIN_S <= total <= motd.MAX_S
    assert scenes[-1]["dur"] > 5.0  # the Close holds longer to reach 45 s


def test_length_cap():
    scenes = [{"kind": "pick", "dur": 6.5} for _ in range(12)]
    out = motd.fit_length(scenes)
    assert abs(sum(s["dur"] for s in out) - motd.MAX_S) < 0.01 and min(s["dur"] for s in out) >= 2.5


def test_thinking_lines_filter():
    from agentpitbench.motd import thinking_lines
    lines = ["You are competing in AgentpitBench against three other AI agents.",   # the bench's own prompt
             "Market: Will it rain?",
             "usage: bench bet [-h] --outcome OUTCOME --rationale RATIONALE --confidence",
             "**Order book** is thin but the favourite has won nine of its last ten.",
             "See https://example.com/a-very-long-link-to-some-source for the odds table",
             "- The underdog plays with a stand-in so the price looks fair enough.",
             "I have successfully placed the bet on your behalf, good luck!",
             "This line comes after the bet and must never be shown."]
    assert thinking_lines(lines) == ["Order book is thin but the favourite has won nine of its last ten.",
                                     "The underdog plays with a stand-in so the price looks fair enough."]


# ---------- the daily hook, posting ----------

def test_daily_disabled_does_nothing(env):
    s, db = env
    assert not s.motd_enabled  # default OFF

    class Boom:
        def __getattr__(self, k):
            raise AssertionError("must not be touched")

    import asyncio
    assert asyncio.run(motd.daily(s, db, Boom(), Boom(), now=IN_WINDOW + 86400)) is None
    assert db.get(f"motd_{DAY}") is None


async def test_daily_makes_the_day_film_once_and_posts_nothing(env, monkeypatch):
    """v0.6: films go out in each round's results post; the daily job only publishes the day's most dramatic
    round film for YouTube and /films/ (once per date, at or after motd_hour_utc), and backfills before that."""
    s, db = env
    s.motd_enabled = True
    t17 = datetime(2026, 10, 12, 17, 5, tzinfo=timezone.utc).timestamp()
    calls, backfills = [], []

    async def fake_day(s_, db_, cards, day):
        calls.append(day)
        return {"date": day, "round_id": 1, "files": {"tall": "x.mp4"}}

    async def fake_backfill(s_, db_, cards, limit=1):
        backfills.append(1)
        return []

    monkeypatch.setattr(motd, "day_film", fake_day)
    monkeypatch.setattr(motd, "backfill", fake_backfill)
    poster = Poster(s, db)
    assert await motd.daily(s, db, None, poster, now=t17 - 3600) is None and calls == [] and backfills
    rec = await motd.daily(s, db, None, poster, now=t17)
    assert rec and calls == [DAY] and db.get(f"motd_{DAY}") == "done"
    assert await motd.daily(s, db, None, poster, now=t17 + 3600) is None and calls == [DAY]  # once per date
    assert not list(s.outbox_dir.glob("motd-*.json")) if s.outbox_dir.exists() else True  # no X post


async def test_day_film_links_the_chosen_round_film_and_renders_missing_formats(env, monkeypatch):
    s, db = env
    rid = 7
    src = motd.round_film_dir(s, rid)
    src.mkdir(parents=True)
    (src / "motd-2026-10-11-square.mp4").write_bytes(b"sq")
    (src / "film.json").write_text(json.dumps({"date": "2026-10-11", "round_id": rid,
                                               "files": {"square": "motd-2026-10-11-square.mp4"}}))
    rnd = {"round_id": rid}
    monkeypatch.setattr(motd, "pick", lambda rounds, day, *a: {"round": rnd, "score": {"total": 50}, "candidates": []})
    from agentpitbench import exports
    monkeypatch.setattr(exports, "export", lambda s_, db_: {"rounds": [], "leaderboard": {}})
    made = []

    async def fake_make(s_, db_, cards, day, round_id=None, out_dir=None, formats=()):
        made.append(formats)
        for f in formats:
            (out_dir / f"motd-{day}-{f}.mp4").write_bytes(f.encode())
        rec = json.loads((out_dir / "film.json").read_text())
        rec["files"].update({f: f"motd-{day}-{f}.mp4" for f in formats})
        (out_dir / "film.json").write_text(json.dumps(rec))
        return rec

    monkeypatch.setattr(motd, "make_film", fake_make)
    rec = await motd.day_film(s, db, None, DAY)
    assert made == [("tall", "wide")] and rec["date"] == DAY and rec["round_date"] == "2026-10-11"
    out = motd.film_dir(s, DAY)
    assert (out / rec["files"]["tall"]).read_bytes() == b"tall" and json.loads((out / "film.json").read_text())["date"] == DAY
    # /films/ lists the round film once (it has the 16:9 now), not the day copy as well
    assert [f["round_id"] for f in motd.films(s)] == [rid]


async def test_backfill_renders_tall_and_wide_only_when_idle(env, monkeypatch):
    s, db = env
    d = motd.round_film_dir(s, 3)
    d.mkdir(parents=True)
    (d / "film.json").write_text(json.dumps({"date": DAY, "round_id": 3, "files": {"square": "a.mp4"}}))
    made = []

    async def fake_make(s_, db_, cards, day, round_id=None, out_dir=None, formats=()):
        made.append((round_id, formats))
        return {"round_id": round_id}

    monkeypatch.setattr(motd, "make_film", fake_make)
    monkeypatch.setattr(motd, "_running", lambda db_: True)
    assert await motd.backfill(s, db, None) == [] and made == []
    monkeypatch.setattr(motd, "_running", lambda db_: False)
    assert await motd.backfill(s, db, None) == [3] and made == [(3, ("tall", "wide"))]


async def test_round_film_waits_for_running_rounds_then_renders_square(env, monkeypatch):
    s, db = env
    s.motd_writer_wait_s = 60
    states = iter([True, True, False])
    monkeypatch.setattr(motd, "_running", lambda db_: next(states, False))
    slept, made = [], []

    async def fake_sleep(x):
        slept.append(x)

    async def fake_make(s_, db_, cards, day, round_id=None, out_dir=None, formats=()):
        made.append((round_id, formats, out_dir))
        return {"round_id": round_id}

    monkeypatch.setattr(motd, "make_film", fake_make)
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.6, 0.7)}, state="resolved", winner="CYBERSHOKE")
    assert await motd.round_film(s, db, None, rid, sleep=fake_sleep)
    assert slept == [20, 20] and made == [(rid, ("square",), motd.round_film_dir(s, rid))]


@pytest.mark.parametrize("ready", [True, False])
async def test_results_post_carries_the_round_film_or_the_plain_clip(env, monkeypatch, tmp_path, ready):
    """v0.6: the round's square film replaces the plain clip in the SAME results post (same key, same text);
    if it isn't ready by motd_round_deadline_s, the plain clip goes out and the film is never posted alone."""
    import asyncio
    from agentpitbench.publish import BenchPublisher
    s, db = env
    s.motd_enabled, s.animate, s.motd_round_deadline_s = True, True, 0.2
    rid = make_round(db, {"claude": ("Nexus", 0.4, 0.6), "codex": ("CYBERSHOKE", 0.6, 0.6)}, state="resolved",
                     winner="Nexus")
    plain = tmp_path / "plain.mp4"
    plain.write_bytes(b"\0")

    async def fake_round_film(s_, db_, cards, round_id, **kw):
        if not ready:
            await asyncio.sleep(5)
        d = motd.round_film_dir(s_, round_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "motd-x-square.mp4").write_bytes(b"\0")
        return {"round_id": round_id, "files": {"square": "motd-x-square.mp4"}}

    async def fake_clip(self, coro):
        coro.close()
        return plain

    monkeypatch.setattr(motd, "round_film", fake_round_film)
    monkeypatch.setattr(BenchPublisher, "_clip", fake_clip)
    pub = BenchPublisher(s, db)
    try:
        await pub.on_resolved(rid)
    finally:
        await pub.close()
    post = json.loads((s.outbox_dir / f"r{rid}-results.json").read_text())
    assert post["image"].endswith("motd-x-square.mp4" if ready else "plain.mp4")
    assert not list(s.outbox_dir.glob("motd-*.json"))  # never a separate film post


async def test_tracker_publishes_in_the_background_and_catches_up_after_a_restart(env, monkeypatch):
    import asyncio
    from agentpitbench.tracker import Tracker
    s, db = env
    s.motd_enabled = True
    rid = make_round(db, {"claude": ("Nexus", 0.4, 0.6)}, state="resolved", winner="Nexus")
    calls = []

    class Pub:
        async def on_resolved(self, round_id):
            calls.append(round_id)

    db.put(f"publishing_{rid}", time.time())  # a results post interrupted by a restart
    tr = Tracker(s, db, None, Pub())
    tr.catch_up()
    await asyncio.sleep(0)
    await asyncio.gather(*tr._tasks)
    assert calls == [rid] and db.get(f"publishing_{rid}") is None
    tr.catch_up()  # done: nothing pending
    assert calls == [rid]


async def test_cli_motd_post_posts_a_rendered_film_once(env, monkeypatch, tmp_path):
    """`agentpitbench motd post --film DIR`: the production post path for an already-rendered film (dry-run here
    goes to the outbox), the template X text, the round link as a self-reply, once per date; the daily job then
    sees the date as done."""
    from agentpitbench import cli
    s, db = env
    film = tmp_path / "film"
    film.mkdir()
    (film / f"motd-{DAY}-tall.mp4").write_bytes(b"\0")
    (film / "film.json").write_text(json.dumps({
        "date": DAY, "round_id": 9, "hook": "All rise.", "post_hook": "Gemini took No at 84.5¢.",
        "files": {"tall": f"motd-{DAY}-tall.mp4"}}))
    monkeypatch.setattr(cli, "make_publisher", lambda s_, db_: SimpleNamespace(poster=Poster(s_, db_)))
    monkeypatch.setattr(cli, "DB", lambda path: db)
    tid = await cli.motd_post(s, str(film))
    assert tid and db.get(f"motd_{DAY}") == "done"
    out = json.loads((s.outbox_dir / f"motd-{DAY}.json").read_text())
    assert out["text"] == "Gemini took No at 84.5¢. Match of the Day #AgentpitBench"  # template, not the AI's hook
    assert json.loads((s.outbox_dir / f"motd-{DAY}-link.json").read_text())["reply_to"] == tid
    assert await cli.motd_post(s, str(film)) == tid  # idempotent: same post_key, no second post
    assert db.one("SELECT COUNT(*) n FROM tweets WHERE post_key=?", f"motd-{DAY}")["n"] == 1


async def test_posting_paused_records_nothing(env, monkeypatch):
    """posting_paused goes through the pause path (ai-automation spec §10): no dry- id, no tweets row."""
    s, db = env
    s.dry_run, s.posting_paused = False, True
    monkeypatch.setenv("X_API_KEY", "k")
    for k in ("X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
        monkeypatch.setenv(k, "v")
    poster = Poster(s, db)
    assert not poster.live
    out = tmp = motd.film_dir(s, DAY)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "f.mp4").write_bytes(b"\0")
    tid = await motd.post(s, db, poster, {"date": DAY, "round_id": 1, "hook": "h", "files": {"tall": "f.mp4"}}, out)
    assert tid is None and db.one("SELECT COUNT(*) n FROM tweets WHERE kind LIKE 'motd%'")["n"] == 0


# ---------- music and render ----------

def test_music_is_deterministic_and_committed():
    a = music.synth(1.0, rate=8000)
    assert a == music.synth(1.0, rate=8000) and max(abs(x) for x in a) <= 0.9 + 1e-9
    assert motd.MUSIC.exists() and (motd.MUSIC.parent / "MUSIC-LICENSE.txt").read_text().count("CC0") >= 2


async def test_render_both_formats_smoke(env):
    if not media.ffmpeg_exe():
        pytest.skip("no ffmpeg")
    s, db = env
    s.motd_fps = 5
    picks = {"claude": ("Nexus", 0.21, 0.4), "codex": ("CYBERSHOKE", 0.63, 0.8), "agy": ("CYBERSHOKE", 0.64, 0.9),
             "grok": ("CYBERSHOKE", 0.62, 0.7)}
    rid = make_round(db, picks, state="resolved", winner="Nexus")
    db.set_round(rid, resolved_at=datetime(2026, 10, 11, 9, tzinfo=timezone.utc).timestamp())
    cards = CardRenderer(s)
    try:
        rec = await motd.make_film(s, db, cards, DAY, scale=0.06)
    finally:
        await cards.close()
    assert rec and rec["round_id"] == rid and rec["score"]["total"] >= 75   # lone wolf + upset
    out = motd.film_dir(s, DAY)
    tall, wide = media.probe(out / rec["files"]["tall"]), media.probe(out / rec["files"]["wide"])
    assert tall["video"] == "1080x1920" and wide["video"] == "1920x1080"
    assert tall["audio"] and wide["audio"] and tall["duration"] > 1
    film = json.loads((out / "film.json").read_text())
    # the writer is unavailable in tests: the templated film, X text from the template, hook = the question
    assert film["scripted"] is False and film["narrated"] is False
    assert film["post_hook"] == "3 AIs said CYBERSHOKE. Claude said Nexus."
    assert len(film["hook"]) <= motd.HOOK_MAX and film["title_line"].endswith("Match of the Day")
    assert [f["round_id"] for f in motd.films(s)] == [rid]
    from agentpitbench import site
    built = site.build(s)
    page = (built / "round" / str(rid) / "index.html").read_text()
    assert f"cards/motd/{DAY}/{rec['files']['wide']}" in page and "Why this round" in page
    assert (built / "films" / "index.html").read_text().count("<video") == 1
    assert (built / "cards" / "motd" / DAY / rec["files"]["tall"]).exists()
    # the players show a JPEG of the PNG poster (the PNG is fetched even with preload="none")
    assert film["poster"] and f'poster="../../cards/motd/{DAY}/poster-web.jpg"' in page
    assert f'poster="../cards/motd/{DAY}/poster-web.jpg"' in (built / "films" / "index.html").read_text()
    web = built / "cards" / "motd" / DAY / "poster-web.jpg"
    assert web.exists() and web.stat().st_size < (out / film["poster"]).stat().st_size
