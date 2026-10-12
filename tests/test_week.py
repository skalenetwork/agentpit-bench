"""Week in the Pit (docs/week-in-the-pit-spec.md): the week's data (races, films, awards, best human, teaser, twist),
slots, the validator, the templated host, the writer's guards, re-cut spans, the credit guard, posting and YouTube,
and a small render smoke test."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentpitbench import media, opai, week
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.twitter import Poster

from test_motd import E, R
from test_publish import env, make_round  # noqa: F401  (env is a fixture)

WEEK = "2026-W42"   # Monday 12 October .. Sunday 18 October 2026
MON = datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp()
SUN18 = datetime(2026, 10, 18, 18, 5, tzinfo=timezone.utc).timestamp()
AG = ["claude", "codex", "agy", "grok"]


def at(day, hour=12):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp()


def rounds_week():
    r1 = R(1, [E("claude", "B", True, price=0.2), E("codex", "A", False), E("agy", "A", False), E("grok", "A", False)],
           winner="B", resolved_at=at(13))
    r2 = R(2, [E("claude", "A", False), E("codex", "B", True), E("agy", "B", True), E("grok", "A", False)],
           winner="B", resolved_at=at(15))
    old = R(3, [E("claude", "A", True)], resolved_at=at(5))                      # last week
    ex = R(4, [E("claude", "A", True)], resolved_at=at(14), exhibition=True)    # exhibition: not in the race
    return [r1, r2, old, ex]


def data_for(rounds=None, films=None, human=None, teaser_rounds=None, twist=True, forecast=True):
    rounds = rounds if rounds is not None else week.week_rounds(rounds_week(), MON, SUN18)
    from agentpitbench.engage import weekly_awards
    d = {"week": WEEK, "lo": MON, "cut": SUN18, "rounds": rounds, "pnl": week.pnl_race(rounds, AG),
         "forecast": ({"keys": AG + ["crowd"], "steps": [{"claude": 0.2, "codex": 0.18, "agy": 0.25, "grok": None,
                                                          "crowd": 0.21}], "days": ["2026-10-12"],
                       "final": {"claude": 0.2, "codex": 0.18, "agy": 0.25, "grok": None, "crowd": 0.21},
                       "leader": "codex", "n": {"claude": 5, "codex": 5, "agy": 5, "grok": 0, "crowd": 5}}
                      if forecast else None),
         "films": films if films is not None else [{"date": "2026-10-14", "round_id": 1, "score": 75, "film": None,
                                                    "round": rounds[0]}],
         "awards": weekly_awards(rounds), "human": human, "teaser": teaser_rounds or [],
         "twist": ({"type": "lone_wolf_won", "surprise": 80, "agents": ["claude"], "names": ["Claude"],
                    "fact": "Claude was alone on B against 3 AIs, and the only one paid.", "round_id": 1,
                    "question": "Market 1?"} if twist else None)}
    d["slots"] = week.slots(d)
    return d


# ---------- the week ----------

def test_week_window_and_film_days():
    lo, hi = week.week_bounds(WEEK)
    assert lo == MON and hi - lo == 7 * 86400 and week.iso_week(SUN18) == WEEK
    days = week.film_days(WEEK)
    assert days[0] == "2026-10-11" and days[-1] == "2026-10-17" and len(days) == 7  # Sunday before .. Saturday


def test_week_rounds_and_pnl_race():
    wr = week.week_rounds(rounds_week(), MON, SUN18)
    assert [r["round_id"] for r in wr] == [1, 2]   # last week's and the exhibition round are out
    race = week.pnl_race(wr, AG)
    assert race["steps"][0] == {k: 0.0 for k in AG + ["crowd"]} and len(race["steps"]) == 3
    assert race["final"]["claude"] == pytest.approx(400 - 100) and race["leader"] == "claude"
    assert race["record"]["claude"] == {"wins": 1, "losses": 1} and race["labels"] == [1, 2]


def test_forecast_race_cumulative_by_sweep_day(env):
    s, db = env
    for mid, day, win, briers in (("m1", "2026-10-12", "Yes", {"claude": 0.1, "codex": 0.3}),
                                   ("m2", "2026-10-13", "No", {"claude": 0.5, "codex": 0.1}),
                                   ("m0", "2026-10-05", "No", {"claude": 0.9})):   # last week: out
        db.x("INSERT INTO sweep_markets(market_id,sweep_date,question,outcomes,prices,winner,state) "
             "VALUES(?,?,?,?,?,?,'resolved')", mid, day, "q", json.dumps(["Yes", "No"]), json.dumps([0.6, 0.4]), win)
        for a, b in briers.items():
            db.x("INSERT INTO forecasts(sweep_date,agent,market_id,probs,brier) VALUES(?,?,?,?,?)", day, a, mid, "{}", b)
    fc = week.forecast_race(db, AG, MON, SUN18)
    assert fc["days"] == ["2026-10-12", "2026-10-13"]
    assert fc["steps"][0]["claude"] == 0.1 and fc["final"]["claude"] == 0.3 and fc["final"]["codex"] == 0.2
    assert fc["leader"] == "codex" and fc["final"]["grok"] is None and fc["n"]["claude"] == 2
    assert week.forecast_race(db, AG, MON + 30 * 86400, MON + 37 * 86400) is None


def test_top_films_rank_by_published_score_and_fill_quiet_days():
    rs = rounds_week()
    films = [{"date": "2026-10-16", "round_id": 2, "score": {"total": 40}, "title": "Two"}]
    top = week.top_films(films, rs, week.film_days(WEEK))
    # round 1 resolved on the 13th: the 14th's film day, no film record -> filled by the drama score (75)
    assert [(f["round_id"], f["film"] is not None) for f in top] == [(1, False), (2, True)]
    assert top[0]["date"] == "2026-10-14" and top[0]["score"] == 75


def test_best_human_handles_filtered_and_ties():
    picks = [{"username": "ann", "won": 1}, {"username": "ann", "won": 0}, {"username": "bob", "won": 1},
             {"username": "bad handle!", "won": 1}, {"username": "bad handle!", "won": 1},
             {"username": "@zed", "won": 1}, {"username": "zed", "won": 1}]
    h = week.best_human(picks)
    assert h == {"handle": "zed", "wins": 2, "picks": 2}   # most wins; the invalid handle never counts
    assert week.best_human([{"username": "ann", "won": 0}]) is None
    assert week.best_human([{"username": "ann", "won": 1}, {"username": "bob", "won": 1},
                            {"username": "bob", "won": 0}])["handle"] == "ann"   # tie: better accuracy


def test_teaser_only_revealed_open_rounds():
    open1 = R(10, [E("claude", "A", None)], state="awaiting_resolution", end_date="2026-10-22T00:00:00Z")
    open2 = R(11, [E("claude", "B", None)], state="awaiting_resolution", end_date="2026-10-20T00:00:00Z")
    running = R(12, [E("claude", "A", None)], state="running", end_date="2026-10-20T00:00:00Z")
    sealed = R(13, [E("claude", "A", None)], state="awaiting_resolution", sealed=True, end_date="2026-10-20T00:00:00Z")
    nobet = R(14, [E("claude", None, None)], state="awaiting_resolution", end_date="2026-10-20T00:00:00Z")
    assert [r["round_id"] for r in week.teaser([open1, open2, running, sealed, nobet], SUN18)] == [11, 10]


def test_week_twist_is_the_biggest_true_twist():
    rs = week.week_rounds(rounds_week(), MON, SUN18)
    t = week.week_twist(rs, lambda r: {})
    assert t["round_id"] == 1 and t["surprise"] >= 80 and "Claude" in t["fact"]
    assert week.week_twist([], lambda r: {}) is None


# ---------- slots, validator, templated host ----------

def test_slots_carry_every_number():
    d = data_for(human={"handle": "zed", "wins": 2, "picks": 3})
    sl = d["slots"]
    assert sl["pnl_leader"] == "Claude" and sl["pnl_leader_pnl"] == "+300" and sl["n_rounds"] == "2"
    assert sl["brier_leader"] == "ChatGPT" and sl["chatgpt_brier"] == "0.180" and "grok_brier" not in sl
    assert sl["f1_claude_pick"] == "B" and sl["f1_date"] == "14 October" and sl["a1_name"] == "Claude"
    assert sl["human"] == "@zed" and sl["human_wins"] == "2"


def test_templated_lines_pass_the_validator():
    for kw in ({}, {"human": {"handle": "zed", "wins": 2, "picks": 3}}, {"twist": False, "forecast": False},
               {"teaser_rounds": [R(10, [E("claude", "A", None)], state="awaiting_resolution")]}):
        d = data_for(**kw)
        pl = week.payload(d)
        script = week.templated(d)
        assert set(script["lines"]) == set(pl["keys"])
        assert week.validate(script, pl) is None, kw


def good(d):
    sc = week.templated(d)
    return {"title": "Lone Wolves and Long Shots", "lines": dict(sc["lines"])}


@pytest.mark.parametrize("key,text,why", [
    ("welcome", "Welcome to week 42 in the pit.", "digits"),
    ("welcome", "Welcome back, all four of them.", "number word"),
    ("welcome", "Welcome! {nope}", "unknown slot"),
    ("welcome", "Welcome to the {week", "stray brace"),
    ("welcome", "Welcome. The OpenAI bot is warming up.", "lab"),
    ("welcome", "Welcome. Luna is warming up.", "model"),
    ("welcome", "Welcome. Claude is the smartest of them all.", "superiority"),
    ("welcome", "Welcome. Grok, you clueless contraption.", "put-down"),
    ("race_pnl", "Grok leads the money race.", "the leader is Claude"),
    ("race_forecast", "Claude is on top of the forecast table.", "the leader is ChatGPT"),
    ("film_1", "Claude took A, and the rest followed.", "picked B"),
    ("film_1", "ChatGPT won it.", "but ChatGPT lost"),
    ("award_1", "And the most valuable player is someone special.", "winner"),
    ("cold_open", "What a week it was.", "must name Claude"),
    ("signoff", "Follow @someone for more.", "handles"),
    ("welcome", "W" * 130, "too long"),
])
def test_validator_rejects_each_rule(key, text, why):
    d = data_for()
    pl = week.payload(d)
    sc = good(d)
    assert week.validate(sc, pl) is None
    sc["lines"][key] = text
    err = week.validate(sc, pl)
    assert err and why.split()[0].lower() in err.lower() or (err and why in err), (key, err)


def test_validator_accepts_slots_years_in_film_lines_and_a_correct_leader():
    d = data_for()
    pl = week.payload(d)
    sc = good(d)
    sc["lines"]["race_pnl"] = "Claude leads the money race at {claude_week_pnl}, with {pnl_leader} out front."
    sc["lines"]["film_1"] = "A club founded in 1903. {f1_claude_pick} for Claude, and Claude cashed in."
    assert week.validate(sc, pl) is None
    sc["title"] = "Week 42"
    assert "title" in week.validate(sc, pl)


def test_teaser_may_not_state_results():
    d = data_for(teaser_rounds=[R(10, [E("claude", "A", None)], state="awaiting_resolution")])
    pl = week.payload(d)
    sc = good(d)
    sc["lines"]["teaser"] = "Claude already won the next round."
    assert "result" in week.validate(sc, pl)


# ---------- the writer's guards ----------

def test_writer_runs_once_a_week_only_when_idle(env, fake_claude):
    s, db = env
    s.opai_enabled = True
    d = data_for()
    script = good(d)
    fake_claude.respond(script)
    rid = make_round(db, {"claude": ("A", 0.5, 0.5)}, state="running", market_id="run1")
    sc, meta = week.write(s, db, d)
    assert meta["scripted"] is False and "running" in meta["why_templated"] and fake_claude.calls == []
    db.set_round(rid, state="awaiting_resolution")
    sc, meta = week.write(s, db, d)
    assert meta["scripted"] is True and sc["title"] == script["title"] and len(fake_claude.calls) == 1
    assert "weekly_episode.v1" in fake_claude.calls[0]["stdin"].splitlines()[0]
    # a re-render reuses the accepted script; a second call in the same week is never made
    sc, meta = week.write(s, db, d)
    assert meta.get("reused") and len(fake_claude.calls) == 1
    db.x("DELETE FROM operator_ai_outputs")
    sc, meta = week.write(s, db, d)
    assert meta["scripted"] is False and "weekly" in meta["why_templated"] and len(fake_claude.calls) == 1


def test_rejected_script_falls_back_to_templated_lines(env, fake_claude):
    s, db = env
    s.opai_enabled = True
    d = data_for()
    bad = good(d)
    bad["lines"]["welcome"] = "Welcome to week 42."
    fake_claude.respond(bad)
    sc, meta = week.write(s, db, d)
    assert meta["scripted"] is False and sc == week.templated(d)
    row = db.one("SELECT accepted, reject_reason FROM operator_ai_outputs WHERE purpose='weekly_episode'")
    assert row["accepted"] == 0 and "digits" in row["reject_reason"]


def test_task_is_registered_and_budgeted():
    s = Settings()
    assert "weekly_episode" in s.opai_tasks and s.opai_task_budget["weekly_episode"] == 1
    assert opai.PROMPTS["weekly_episode"] == 1 and opai.prompt_text("weekly_episode")
    assert opai.manifest() == json.loads(opai.prompt_path("MANIFEST.json").read_text())
    assert opai.model_for(s, "weekly_episode") == "fable" and s.week_episode_enabled is False


def test_partial_budget_table_keeps_code_defaults(tmp_path):
    from agentpitbench import config
    p = tmp_path / "bench.toml"
    p.write_text('[opai_task_budget]\nrate = 5\n')
    s = config.load(p)
    assert s.opai_task_budget["rate"] == 5 and s.opai_task_budget["weekly_episode"] == 1
    assert s.opai_task_budget["motd_script"] == 3


# ---------- scenes, re-cuts, credits ----------

def test_host_scenes_and_segments():
    d = data_for(human={"handle": "zed", "wins": 2, "picks": 3})
    segs = week.build_scenes(week.templated(d), d, {"significance": {"multi_label": "x"}, "agents": []}, "site.org")
    kinds = [x["kind"] for seg in segs for x in seg.get("scenes", [])]
    assert kinds[:4] == ["w_cold", "w_title", "w_race_pnl", "w_race_fc"] and kinds[-1] == "w_signoff"
    assert [seg["type"] for seg in segs] == ["host", "host", "film", "host"]
    assert "w_human" in kinds and kinds.count("w_award") == len(d["awards"])
    cold = segs[0]["scenes"][0]
    assert cold["caption"].startswith("The twist of the week. Claude was alone") and cold["url"].startswith("site.org/films/")
    assert all("{" not in x["caption"] for seg in segs for x in seg.get("scenes", []))


def test_recut_spans_scripted_and_templated():
    sc = [{"kind": k, "dur": d} for k, d in (("s_cold_open", 3), ("s_title", 2), ("s_market", 6), ("s_background", 6),
                                             ("s_character", 5), ("s_misdirect", 4), ("s_result", 7), ("s_twist", 9),
                                             ("s_coda", 6))]
    spans = week.recut_spans(sc)
    assert spans == [(0.0, 3.0), (5.0, 11.0), (26.0, 42.0), (42.0, 48.0)] or spans == [(0.0, 3.0), (5.0, 11.0), (26.0, 48.0)]
    assert sum(b - a for a, b in spans) <= week.RECUT_S
    short = week.recut_spans(sc, max_s=20)
    assert sum(b - a for a, b in short) <= 20 and any(a <= 30 < b for a, b in short)   # the twist survives
    tpl = [{"kind": k, "dur": d} for k, d in (("hook", 3), ("setup", 5), ("pick", 4), ("reveal", 6), ("close", 5))]
    assert week.recut_spans(tpl) == [(0.0, 8.0), (12.0, 23.0)]
    assert week.trailer_span(sc) == (33.0, 42.0) and week.trailer_span(tpl) == (12.0, 18.0)


def test_credit_guard_all_or_nothing(env, monkeypatch, tmp_path):
    s, db = env
    scenes = [{"caption": "x" * 300, "line_key": "a"}, {"caption": "y" * 300, "line_key": "b"},
              {"caption": "end card", "line_key": None}]
    chosen, need = week.plan_voice(s, scenes, budget=1000)
    assert len(chosen) == 2 and need == 600
    chosen, need = week.plan_voice(s, scenes, budget=500)
    assert chosen == [] and need == 600                          # over budget: captions only
    week._tts_path(s, scenes[0]["caption"]).write_bytes(b"x")   # a cached line is free
    chosen, need = week.plan_voice(s, scenes, budget=500)
    assert len(chosen) == 2 and need == 300
    chosen, need = week.plan_voice(s, scenes, budget=200)
    assert chosen == [scenes[0]] and need == 300
    chosen, need = week.plan_voice(s, scenes, budget=1000, max_new=0)
    assert chosen == [scenes[0]] and need == 0


def test_music_never_generates(env):
    s, _ = env
    p, rec = week.music_track(s)
    assert rec.get("engine") == "procedural"


# ---------- posting, schedule, YouTube ----------

def _episode(s, with_trailer=True):
    out = week.episode_dir(s, WEEK)
    out.mkdir(parents=True, exist_ok=True)
    files = {"wide": f"week-{WEEK}-wide.mp4", "square": f"week-{WEEK}-square.mp4"}
    if with_trailer:
        files["trailer"] = f"week-{WEEK}-trailer.mp4"
    for f in files.values():
        (out / f).write_bytes(b"\0")
    rec = {"week": WEEK, "title": "Lone Wolves", "files": files, "n_rounds": 2,
           "films": [{"round_id": 1, "question": "Market 1?", "date": "2026-10-14"}],
           "awards": [{"title": "MVP", "name": "Claude", "detail": "+300 tokens this week"}],
           "pnl_leader": "Claude", "brier_leader": "ChatGPT", "narrated": True,
           "post_after": datetime(2026, 10, 18, 18, tzinfo=timezone.utc).timestamp()}
    (out / "episode.json").write_text(json.dumps(rec))
    return rec, out


async def test_post_square_on_x_trailer_on_bluesky(env):
    s, db = env
    s.bluesky_enabled = True
    rec, out = _episode(s)
    poster = Poster(s, db)
    tid = await week.post(s, db, poster, rec, out)
    x = json.loads((s.outbox_dir / f"week-{WEEK}.json").read_text())
    assert tid and x["image"].endswith("-square.mp4") and "#AgentpitBench" in x["text"] and "http" not in x["text"]
    link = json.loads((s.outbox_dir / f"week-{WEEK}-link.json").read_text())
    assert link["reply_to"] == tid and f"#week-{WEEK}" in link["text"]
    b = json.loads((s.outbox_dir / "bluesky" / f"week-{WEEK}.json").read_text())
    assert b["media"][0].endswith("-trailer.mp4")
    assert await week.post(s, db, poster, rec, out) == tid   # idempotent per key


async def test_post_falls_back_to_the_trailer_when_x_refuses(env, monkeypatch):
    s, db = env
    rec, out = _episode(s)
    sent = []

    class P:
        live = True

        async def post(self, key, kind, text, media=None, **kw):
            sent.append((key, Path(media).name if media else None))
            return None if (media and str(media).endswith("-square.mp4")) else "t1"
    tid = await week.post(s, db, P(), rec, out)
    assert tid == "t1" and [m for _, m in sent[:2]] == [f"week-{WEEK}-square.mp4", f"week-{WEEK}-trailer.mp4"]


async def test_posting_paused_posts_nothing(env, monkeypatch):
    s, db = env
    s.dry_run, s.posting_paused = False, True
    rec, out = _episode(s)
    assert await week.post(s, db, Poster(s, db), rec, out) is None
    assert db.one("SELECT COUNT(*) n FROM tweets WHERE kind LIKE 'week%'")["n"] == 0


async def test_weekly_schedule_flag_idle_wait_and_order(env, monkeypatch):
    s, db = env
    calls = []

    async def fake(s_, db_, cards, wk, **kw):
        calls.append(wk)
        return _episode(s_)[0]
    monkeypatch.setattr(week, "make_episode", fake)
    poster = Poster(s, db)
    assert await week.weekly(s, db, None, poster, now=SUN18) is None and calls == []   # off by default
    s.week_episode_enabled = True
    sun = lambda h: datetime(2026, 10, 18, h, 5, tzinfo=timezone.utc).timestamp()  # noqa: E731
    assert await week.weekly(s, db, None, poster, now=sun(12)) is None and calls == []  # before prep
    assert await week.weekly(s, db, None, poster, now=at(17, 15)) is None and calls == []  # a Saturday
    rid = make_round(db, {"claude": ("A", 0.5, 0.5)}, state="running", market_id="run1")
    assert await week.weekly(s, db, None, poster, now=sun(14)) is None and db.get(f"week_{WEEK}") == "waiting"
    db.set_round(rid, state="awaiting_resolution")
    assert await week.weekly(s, db, None, poster, now=sun(15)) is None and calls == [WEEK]
    assert db.get(f"week_{WEEK}") == "rendered"
    assert await week.weekly(s, db, None, poster, now=sun(18)) is None   # waits for the race/awards post
    db.put(f"race_{WEEK}", "t0")
    rec = await week.weekly(s, db, None, poster, now=sun(18))
    assert rec and db.get(f"week_{WEEK}") == "done" and (s.outbox_dir / f"week-{WEEK}.json").exists()
    assert await week.weekly(s, db, None, poster, now=sun(19)) is None and calls == [WEEK]


def test_youtube_regular_video_with_its_own_key(env):
    from agentpitbench import youtube, ytstats
    s, db = env
    s.week_episode_enabled = True
    rec, out = _episode(s)
    res = week.yt_resource(s, rec)
    assert len(res["snippet"]["title"]) <= 100 and "Week in the Pit" in res["snippet"]["title"]
    assert "#shorts" not in res["snippet"]["description"].lower() and "/round/1/" in res["snippet"]["description"]
    assert ytstats.episode_candidates(s, SUN18 - 3600) == []        # before the post time
    (cand, d), = ytstats.episode_candidates(s, SUN18)
    r = youtube.upload(s, db, None, cand, d, key=f"week-{WEEK}", file_key="wide", resource=res)
    assert r["state"] == "outbox" and r["file"].endswith("-wide.mp4")
    assert json.loads((s.outbox_dir / f"yt-week-{WEEK}.json").read_text())["snippet"]["title"] == res["snippet"]["title"]
    out_ = ytstats.pull_due(s, db, None, now=SUN18)
    assert out_["uploaded"] == []   # already handled: not re-uploaded
    assert "films" in week.yt_comment(s, rec)


# ---------- render smoke test ----------

async def test_render_smoke(env, monkeypatch):
    if not media.ffmpeg_exe():
        pytest.skip("no ffmpeg")
    from agentpitbench.cards import CardRenderer
    s, db = env
    s.week_episode_fps = 4
    s.motd_fps = 4
    picks = {"claude": ("Nexus", 0.21, 0.4), "codex": ("CYBERSHOKE", 0.63, 0.8), "agy": ("CYBERSHOKE", 0.64, 0.9),
             "grok": ("CYBERSHOKE", 0.62, 0.7)}
    rid = make_round(db, picks, state="resolved", winner="Nexus")
    db.set_round(rid, resolved_at=at(13, 9))
    monkeypatch.setattr(week, "RECUT_S", 3.0)
    monkeypatch.setattr(week, "TRAILER_FILM_S", 1.0)
    cards = CardRenderer(s)
    try:
        rec = await week.make_episode(s, db, cards, WEEK, writer=False, now=at(18, 15), scale=0.15)
    finally:
        await cards.close()
    assert rec and rec["scripted"] is False and rec["films"][0]["round_id"] == rid and rec["films"][0]["rendered_here"]
    out = week.episode_dir(s, WEEK)
    wide, square = media.probe(out / rec["files"]["wide"]), media.probe(out / rec["files"]["square"])
    assert wide["video"] == "1920x1080" and square["video"] == "1080x1080" and wide["audio"] and square["audio"]
    assert media.probe(out / rec["files"]["trailer"])["video"] == "1080x1080"
    assert not (out / "work").exists() and json.loads((out / "episode.json").read_text())["week"] == WEEK
    from agentpitbench import site
    built = site.build(s)
    assert f'id="week-{WEEK}"' in (built / "films" / "index.html").read_text()
