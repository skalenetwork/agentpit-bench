"""Engagement extras: newsworthy priority, quotes, humans, milestones, seasons, race, summons, high stakes,
on-chain proof, grudges, commentary, replays, awards, model changes, banner, dataset. Dry-run, no network."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentpitbench import exports, media, virality as v
from agentpitbench.cards import CardRenderer
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.engage import Engage, iso_week, model_change_posts, replay_candidate, weekly_awards
from agentpitbench.orchestrator import Round, build_prompt
from agentpitbench.publish import BenchPublisher
from agentpitbench.twitter import MAX_LEN, decision_text, results_text, tweet_len
from agentpitbench.watcher import Watcher

AGENTS = ["claude", "codex", "agy", "grok"]
OUT = ["CYBERSHOKE", "Nexus"]


def market(mid="m1", q="Counter-Strike: CYBERSHOKE vs Nexus (BO3)", prices=(0.62, 0.38), **kw):
    m = {"id": mid, "slug": f"s-{mid}", "question": q, "outcomes_list": OUT, "prices_list": list(prices),
         "token_ids": ["t1", "t2"], "endDate": "2026-10-10T20:00:00Z", "conditionId": "0xabc"}
    m.update(kw)
    return m


def make_round(db, picks, winner=None, mid="m1", resolved_at=None, models=None, **mkw):
    rid = db.create_round(market(mid, **mkw))
    for i, a in enumerate(AGENTS):
        run = db.create_run(rid, a, model_reported=(models or {}).get(a, f"{a}-1"))
        p = picks.get(a)
        if p is None:
            db.record_bet(run, outcome=None)
            db.finish_run(run, "timeout", None, None)
            continue
        o, price, conf = p[:3]
        db.record_bet(run, outcome=o, avg_price=price, shares_filled=100 / price, stake_filled=100,
                      confidence=conf, decided_s=p[3] if len(p) > 3 else 60 + i, rationale="because",
                      quote=p[4] if len(p) > 4 else None)
        db.finish_run(run, "bet", None, None)
    if winner:
        for e in db.round_entries(rid):
            if e["outcome"]:
                pay = e["shares_filled"] if e["outcome"] == winner else 0
                db.settle_bet(e["run_id"], pay, pay - e["stake_filled"])
        db.set_round(rid, state="resolved", winner=winner, resolved_at=resolved_at or time.time())
    else:
        db.set_round(rid, state="awaiting_resolution")
    return rid


class FakePoster:
    def __init__(self, replies=None, mentions=None, likes=None):
        self.posts, self._replies, self._mentions, self._likes = [], replies or [], mentions or [], likes or {}
        self.can_read = True

    async def post(self, key, kind, text, image=None, reply_to=None, round_id=None, quote_of=None):
        if any(p["key"] == key for p in self.posts):
            return next(p["id"] for p in self.posts if p["key"] == key)
        self.posts.append({"key": key, "kind": kind, "text": text, "image": image, "reply_to": reply_to,
                           "quote_of": quote_of, "id": f"t{len(self.posts) + 1}"})
        return self.posts[-1]["id"]

    def replies(self, cid, max_reads):
        return self._replies[:max_reads]

    def my_user_id(self):
        return "bench"

    def mentions(self, since, n):
        return self._mentions

    def like_counts(self, ids):
        return {i: self._likes.get(i, 0) for i in ids}


class NoCards:
    def __getattr__(self, name):
        async def f(*a, **k):
            return None
        return f


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_CLIENT_ID", "X_CLIENT_SECRET", "X_OAUTH2_REFRESH_TOKEN", "X_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "tok.json"))
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, deploy_debounce_s=0, animate=False)
    return s, DB(s.db_path)


# 1. newsworthy first
def test_news_priority():
    esports = {"id": "9", "question": "Counter-Strike: X vs Y (BO3) - CCT Europe Grand Final", "volume": "9999"}
    final = {"id": "1", "question": "Will the Lakers win the NBA Finals?", "volume": "0"}
    election = {"id": "2", "question": "Who will win the 2026 Brazil presidential election?", "volume": "0"}
    league = {"id": "3", "question": "Arsenal vs Chelsea: Premier League", "volume": "5"}
    other = {"id": "4", "question": "Will it rain in Paris on Friday?", "volume": "50"}
    assert [v.news_tier(m) for m in (esports, final, election, league, other)] == [0, 3, 3, 2, 1]
    order = sorted([esports, final, election, league, other], key=v.market_priority, reverse=True)
    assert [m["id"] for m in order] == ["2", "1", "3", "4", "9"]   # tie at tier 3 -> newest id; esports last


# 2. quotes
def test_clean_quote():
    assert v.clean_quote("  See you  at the\nbottom, Grok. ") == ("See you at the bottom, Grok.", None)
    assert v.clean_quote("x" * 200)[0].endswith("…") and len(v.clean_quote("x" * 200)[0]) == 120
    assert v.clean_quote("check example.com")[0] is None
    assert v.clean_quote("hey @OpenAI")[0] is None
    assert v.clean_quote("you absolute r3tard")[1] == "quote dropped by the abuse filter"
    assert v.clean_quote("k y s")[0] is None or v.clean_quote("kys")[0] is None
    assert v.clean_quote(None) == (None, None)


def test_quote_flows_to_export_and_tweet(env):
    s, db = env
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.63, 0.7, 30, "Codex is cooked.")})
    r = exports.round_record(s, db, db.round(rid))
    e = r["entries"][0]
    assert e["quote"] == "Codex is cooked."
    t = decision_text(s, r, e)
    assert '💬 "Codex is cooked."' in t and tweet_len(t) <= MAX_LEN


# 4. humans
def test_parse_and_pick_humans():
    assert v.parse_pick("@agentpitbench nexus all day", OUT) == "Nexus"
    assert v.parse_pick("not CYBERSHOKE, Nexus", OUT) == "CYBERSHOKE"       # first named wins
    assert v.parse_pick("nexusX", OUT) is None
    replies = [
        {"id": "1", "author_id": "a", "username": "ann", "text": "Nexus", "created_at": "2026-10-09T10:00:00Z"},
        {"id": "2", "author_id": "a", "username": "ann", "text": "CYBERSHOKE", "created_at": "2026-10-09T11:00:00Z"},
        {"id": "3", "author_id": "b", "username": "bob", "text": "cybershoke", "created_at": "2026-10-11T00:00:00Z"},
        {"id": "4", "author_id": "bench", "username": "agentpitbench", "text": "Nexus", "created_at": "2026-10-09T10:00:00Z"},
        {"id": "5", "author_id": "c", "username": "cy", "text": "who knows", "created_at": "2026-10-09T10:00:00Z"},
    ]
    picks = v.human_picks(replies, OUT, "2026-10-10T20:00:00Z", {"bench"})
    assert [(p["username"], p["pick"]) for p in picks] == [("ann", "Nexus")]   # one per user, before close, not us


async def test_humans_collect_score_board(env):
    s, db = env
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.63, 0.7), "grok": ("Nexus", 0.4, 0.6)})
    db.set_round(rid, thread_tweet_id="111")
    replies = [{"id": str(i), "author_id": f"u{i}", "username": f"user{i}", "text": OUT[i % 2],
                "created_at": "2026-10-09T10:00:00Z"} for i in range(5)]
    eng = Engage(s, db, FakePoster(replies=replies), NoCards())
    assert await eng.collect_humans(rid) == 5
    assert await eng.collect_humans(rid) == 0                                # read once
    for e in db.round_entries(rid):
        pay = e["shares_filled"] if e["outcome"] == "Nexus" else 0
        if e["outcome"]:
            db.settle_bet(e["run_id"], pay, pay - 100)
    db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())
    eng.score_humans(rid)
    data = exports.export(s, db)
    r = data["rounds"][0]
    assert r["humans"]["picked"] == 5 and r["humans"]["right"] == 2 and r["humans"]["by_outcome"] == {"CYBERSHOKE": 3, "Nexus": 2}
    hb = data["leaderboard"]["humans"]
    assert [h["username"] for h in hb][:2] == ["user1", "user3"] and hb[0]["accuracy"] == 1.0 and hb[-1]["wins"] == 0


async def test_dry_thread_is_never_read(env):
    s, db = env
    rid = make_round(db, {"claude": ("Nexus", 0.4, 0.6)})
    db.set_round(rid, thread_tweet_id="dry-r1-decision-claude")
    p = FakePoster(replies=[{"id": "1", "author_id": "a", "text": "Nexus"}])
    assert await Engage(s, db, p, NoCards()).collect_humans(rid) == 0
    assert db.round(rid)["humans_collected"] == 1


# 5. race + encoding
def test_race_contexts_and_awards(env):
    s, db = env
    now = time.time()
    make_round(db, {"claude": ("CYBERSHOKE", 0.5, 0.9, 290), "grok": ("Nexus", 0.5, 0.85)}, "CYBERSHOKE", "a", now)
    make_round(db, {"claude": ("Nexus", 0.25, 0.6), "codex": ("CYBERSHOKE", 0.75, 0.95), "agy": ("CYBERSHOKE", 0.75, 0.5)},
               "Nexus", "b", now)
    rounds = sorted(exports.export(s, db)["rounds"], key=lambda r: r["round_id"])
    ctx = media.race_contexts(rounds, AGENTS, "2026-W41", sub=4, hold=3)
    assert len(ctx) == 2 * 4 + 3
    last = {r["key"]: r for r in ctx[-1]["rows"]}
    assert last["claude"]["value"] == 400 and last["claude"]["y"] == 0            # +100, +300: leader on top
    assert last["crowd"]["value"] == pytest.approx(100 / 0.62 - 100 - 100, abs=0.2)
    assert all(0 <= r["left"] <= 100 and r["width"] >= 0 for c in ctx for r in c["rows"])
    aw = {a["title"]: a for a in weekly_awards(rounds)}
    assert aw["MVP"]["agent"] == "claude"
    assert aw["Most Overconfident"]["agent"] == "codex"                       # 95% sure, lost
    assert aw["Buzzer-Beater"]["agent"] == "claude" and "10s left" in aw["Buzzer-Beater"]["detail"]
    assert aw["Contrarian"]["agent"] == "claude"


def test_encode_mp4(tmp_path):
    from PIL import Image
    d = tmp_path / "f"
    d.mkdir()
    frames = []
    for i in range(6):
        p = d / f"f{i:05d}.png"
        Image.new("RGB", (1200, 675), (i * 40, 0, 0)).save(p)
        frames.append(p)
    out = media.encode(frames, tmp_path / "race", fps=6)
    assert out and out.suffix == (".mp4" if media.ffmpeg_exe() else ".gif") and out.stat().st_size > 0


# 6. milestones
def test_milestones():
    base = {"season": "2026-10", "leader": "claude", "streaks": {"claude": 3, "grok": -3}, "names": dict(zip(AGENTS, ["Claude", "Codex", "Gemini", "Grok"])),
            "vs_crowd": -1, "ai_avg": -5, "crowd_net": 10, "rounds": 49, "all_time_pnl": {"claude": 480}}
    assert v.detect_milestones(None, base, 1) == []
    cur = {**base, "leader": "grok", "streaks": {"claude": -1, "grok": 4}, "vs_crowd": 1, "rounds": 50,
           "all_time_pnl": {"claude": 520}}
    keys = [k for k, *_ in v.detect_milestones(base, cur, 7)]
    assert "ms-streak-grok-W4-r7" in keys and "ms-lead-2026-10-grok-r7" in keys
    assert "ms-crowd-2026-10-ahead-r7" in keys and "ms-rounds-50" in keys and "ms-pnl-500" in keys
    nxt = {**cur, "streaks": {"claude": -1, "grok": 5}}
    assert not [k for k, *_ in v.detect_milestones(cur, nxt, 8) if "streak" in k]   # 5: next post at 6


# 7. seasons + champion
async def test_seasons_and_champion(env):
    s, db = env
    now = time.time()
    last_month = v.season_bounds(v.prev_season(v.season_of(now)))[0] + 3600
    make_round(db, {"claude": ("Nexus", 0.5, 0.9), "grok": ("CYBERSHOKE", 0.5, 0.9)}, "Nexus", "a", last_month)
    make_round(db, {"grok": ("Nexus", 0.5, 0.9), "claude": ("CYBERSHOKE", 0.5, 0.9)}, "Nexus", "b", now)
    lb = exports.export(s, db)["leaderboard"]
    assert {a["agent"]: a["net_pnl"] for a in lb["agents"]}["claude"] == -100            # this month
    assert {a["agent"]: a["net_pnl"] for a in lb["all_time"]["agents"]}["claude"] == 0
    p = FakePoster()
    eng = Engage(s, db, p, NoCards())
    await eng.champion_check(now)
    await eng.champion_check(now)
    champ = [x for x in p.posts if x["kind"] == "champion"]
    assert len(champ) == 1 and champ[0]["text"].startswith("Claude wins AgentpitBench season")
    assert (s.data_dir / "datasets" / f"agentpitbench-{v.prev_season(v.season_of(now))}" / "README.md").exists()


# 8. summon
async def test_summon_flow(env):
    s, db = env
    m_ok = {**market("77", q="Who wins the 2026 election?"), "active": True, "acceptingOrders": True, "closed": False,
            "endDate": datetime.fromtimestamp(time.time() + 2 * 86400, timezone.utc).isoformat().replace("+00:00", "Z")}

    class Api:
        async def market(self, mid):
            return m_ok if str(mid) == "77" else None

        async def market_by_slug(self, slug):
            return m_ok if slug == "s-77" else None

        async def book(self, tok):
            return {"asks": [{"price": "0.5", "size": "500"}]}

    mentions = [
        {"id": "900", "author_id": "x", "username": "fan", "text": "@agentpitbench do this https://t.co/abc",
         "created_at": "2026-10-09T10:00:00Z", "entities": {"urls": [{"expanded_url": "https://polymarket.com/market/s-77?ref=1"}]}},
        {"id": "901", "author_id": "y", "username": "fan2", "text": "@agentpitbench hi", "created_at": "2026-10-09T10:00:00Z"},
    ]
    p = FakePoster(mentions=mentions, likes={"900": 12})
    eng = Engage(s, db, p, NoCards(), api=Api())
    assert await eng.poll_summons() == 1 and db.get("mentions_since") == "901"
    w = Watcher(s, db, Api())
    db.x("UPDATE summons SET created_at=?", time.time())
    late = datetime.now(timezone.utc).replace(hour=s.summon_hour_utc, minute=5).timestamp()
    row, m = await eng.pick_summon(w, now=late)
    assert m["id"] == "77" and row["likes"] == 12
    assert await eng.pick_summon(w, now=late) is None                       # max 1 per day
    rid = db.create_round(m)
    assert db.round(rid)["exhibition"] == 1
    db.set_round(rid, thread_tweet_id="555")
    await eng.summon_reply(row, rid)
    reply = p.posts[-1]
    assert reply["reply_to"] == "900" and "x.com/agentpitbench/status/555" in reply["text"]
    make_round(db, {"claude": ("Nexus", 0.5, 0.9)}, "Nexus", "z")
    db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())
    lb = exports.export(s, db)["leaderboard"]
    assert lb["crowd"]["played"] == 1 and w.rounds_today() == 1             # exhibition outside table and cap


# 9. tail / fade and 13. on-chain proof
def test_tail_fade_and_onchain(env):
    s, db = env
    rid = make_round(db, {"grok": ("Nexus", 0.4, 0.6)})
    db.put("wallet_grok", "0xdDab197c135A4892f799C778E989AD343a740a98")
    run = db.one("SELECT run_id FROM runs WHERE agent='grok'")["run_id"]
    db.x("UPDATE bets SET tx_hashes=? WHERE run_id=?", json.dumps(["0xabc123"]), run)
    e = next(e for e in exports.round_record(s, db, db.round(rid))["entries"] if e["agent"] == "grok")
    assert "outcome=Nexus" in e["tail_url"] and "utm_content=tail" in e["tail_url"]
    assert "outcome=CYBERSHOKE" in e["fade_url"] and "utm_campaign=agentpitbench_r1" in e["fade_url"]
    assert e["wallet_url"] == "https://skale-base-explorer.skalenodes.com/address/0xdDab197c135A4892f799C778E989AD343a740a98"
    assert e["txs"] == [{"hash": "0xabc123", "url": "https://skale-base-explorer.skalenodes.com/tx/0xabc123"}]


# 11. commentary
def test_commentary_deterministic(env):
    s, db = env
    rid = make_round(db, {"grok": ("Nexus", 0.21, 0.6), "claude": ("CYBERSHOKE", 0.63, 0.9),
                          "codex": ("CYBERSHOKE", 0.63, 0.9)}, "Nexus")
    r = exports.round_record(s, db, db.round(rid))
    line = v.commentary(r)
    assert line == v.commentary(json.loads(json.dumps(r)))
    assert "Grok" in line and "21¢" in line
    sweep = make_round(db, {"grok": ("CYBERSHOKE", 0.6, 0.6), "claude": ("CYBERSHOKE", 0.63, 0.9)}, "CYBERSHOKE", "m2")
    assert v.commentary(exports.round_record(s, db, db.round(sweep))) == \
        "Clean sweep for the favourite: every AI rode CYBERSHOKE home."
    t = results_text(s, r, [], "https://x.test/round/1/", None, line)
    assert line not in t and tweet_len(t) <= MAX_LEN                   # the card carries the commentary
    assert t.startswith("Resolved: Nexus. Grok went alone and was right. 1-2.")


# 12. high stakes
async def test_high_stakes(env, monkeypatch):
    s, db = env
    m_hi = {**market("5", q="Bitcoin above $150k on Friday?"), "active": True, "acceptingOrders": True, "closed": False,
            "endDate": datetime.fromtimestamp(time.time() + 86400, timezone.utc).isoformat().replace("+00:00", "Z")}
    m_lo = {**m_hi, "id": "6", "question": "CS2: A vs B (BO1)"}

    class Api:
        async def market(self, mid):
            return {"5": m_hi, "6": m_lo}.get(str(mid))

        async def book(self, tok):
            return {"asks": [{"price": "0.5", "size": "5000"}]}

    for mid in ("5", "6"):
        db.x("INSERT INTO seen_markets(market_id, first_seen, eligible) VALUES(?,?,1)", mid, time.time())
    eng = Engage(s, db, FakePoster(), NoCards(), api=Api())
    w = Watcher(s, db, Api())
    fri = datetime(2026, 10, 9, s.high_stakes_hour_utc, 30, tzinfo=timezone.utc).timestamp()
    assert await eng.high_stakes_market(w, fri - 86400) is None              # Thursday
    m = await eng.high_stakes_market(w, fri)
    assert m["id"] == "5" and m["bench_stake"] == 500 and m["high_stakes"]
    assert await eng.high_stakes_market(w, fri + 3600) is None               # once per ISO week
    assert "one bet of 500 tokens" in build_prompt(m, []) and "HIGH STAKES" in build_prompt(m, [])
    assert "one bet of 100 tokens" in build_prompt(m_lo, [])
    rid = db.create_round(m)
    run = db.create_run(rid, "claude")
    db.record_bet(run, outcome="CYBERSHOKE", avg_price=0.5, shares_filled=1000, stake_filled=500)
    db.set_round(rid, state="resolved", winner="CYBERSHOKE", resolved_at=time.time())
    r = exports.round_record(s, db, db.round(rid))
    assert r["stake"] == 500 and r["high_stakes"] and r["crowd"]["pnl"] == pytest.approx(500 / 0.62 - 500, abs=0.01)
    rnd = Round(s, db, Api(), m, None, agents=["claude"])
    assert rnd._stake() == 500


# 14. grudge
def test_grudge(env):
    s, db = env
    for i in range(3):
        make_round(db, {"claude": ("Nexus", 0.4, 0.6), "grok": ("CYBERSHOKE", 0.6, 0.6)}, "Nexus" if i else "CYBERSHOKE", f"g{i}")
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.6, 0.6), "grok": ("Nexus", 0.4, 0.6), "codex": ("Nexus", 0.4, 0.6)}, mid="g3")
    rounds = exports.export(s, db)["rounds"]
    rnd = next(r for r in rounds if r["round_id"] == rid)
    g = v.grudge(rnd, rounds)
    assert (g["a"], g["b"], g["clash"]) == ("claude", "grok", 4) and (g["a_wins"], g["b_wins"]) == (2, 1)
    assert v.grudge_headline(g) == "GRUDGE MATCH: Claude vs Grok — clash #4"
    first = next(r for r in rounds if r["round_id"] == 1)
    assert v.grudge(first, rounds) is None


# 15. replay
def test_replay_text_and_candidate(env):
    t = "\n".join([
        "$ claude -p <prompt>", "--- prompt ---", "secret prompt", "--- output ---",
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Nexus looks underpriced."}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "huge book dump"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {"command": "bench book"}}]}}),
        "My key is AGENTPIT_KEY=0f1e2d3c-4b5a-4697-a8b9-cadbecfd0011 do not leak",
        "token: sk-abcdefghijklmnopqrstu",
        "I'll run bench bet --outcome Nexus --confidence 0.6",
        "after the bet line",
    ])
    lines = v.reasoning_lines(t)
    assert lines[0] == "Nexus looks underpriced." and "huge book dump" not in " ".join(lines)
    assert "0f1e2d3c" not in " ".join(lines) and "sk-abc" not in " ".join(lines) and "[redacted]" in " ".join(lines)
    assert lines[-1].startswith("I'll run bench bet") and "secret prompt" not in lines
    s, db = env
    rid = make_round(db, {"grok": ("Nexus", 0.21, 0.6), "claude": ("CYBERSHOKE", 0.6, 0.6)}, "Nexus")
    assert replay_candidate(exports.round_record(s, db, db.round(rid)))["agent"] == "grok"
    rid2 = make_round(db, {"grok": ("Nexus", 0.5, 0.6), "claude": ("Nexus", 0.5, 0.6)}, "Nexus", "m9")
    assert replay_candidate(exports.round_record(s, db, db.round(rid2))) is None


# 18. model changes
def test_model_change(env):
    s, db = env
    make_round(db, {"codex": ("Nexus", 0.5, 0.5)}, mid="a", models={"codex": "gpt-6"})
    rid = make_round(db, {"codex": ("Nexus", 0.5, 0.5)}, mid="b", models={"codex": "gpt-6-luna"})
    posts = model_change_posts(db, rid)
    assert [(k, a, n, o) for k, a, n, o in posts if a == "codex"] == [("model-codex-gpt-6-luna", "codex", "gpt-6-luna", "gpt-6")]
    assert not [p for p in posts if p[1] == "claude"]                         # unchanged
    db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())
    bm = {(r["agent"], r["model"]) for r in exports.export(s, db)["leaderboard"]["by_model"]}
    assert ("codex", "gpt-6-luna") in bm


# 23. dataset
def test_dataset(env):
    s, db = env
    now = time.time()
    rid = make_round(db, {"claude": ("Nexus", 0.5, 0.6)}, "Nexus", resolved_at=now)
    tdir = s.transcripts_dir / str(rid)
    tdir.mkdir(parents=True)
    (tdir / "claude.txt").write_text("--- output ---\nkey=0f1e2d3c-4b5a-4697-a8b9-cadbecfd0011\nthinking")
    from agentpitbench import dataset
    out = dataset.build(s, db, v.season_of(now), upload=False)
    names = {p.name for p in out.iterdir()}
    assert "README.md" in names and "transcripts.jsonl" in names
    assert ("bets.parquet" in names) or ({"bets.csv", "bets.jsonl"} <= names)
    tr = (out / "transcripts.jsonl").read_text()
    assert "0f1e2d3c" not in tr and "@misc{agentpitbench" in (out / "README.md").read_text()


# 16. widget
def test_widget_files():
    root = Path(__file__).resolve().parent.parent / "agentpitbench" / "widget"
    js = (root / "agentpit-widget.js").read_text()
    assert "data-agentpitbench-market" in js and "/data/rounds.json" in js and "attachShadow" in js
    assert len(js.encode()) < 8000 and "import " not in js
    assert "data-agentpitbench-market" in (root / "README.md").read_text()


# cards: every new card renders at the right size (also used to eyeball them)
async def test_new_cards_render(env):
    s, db = env
    now = time.time()
    for i in range(3):
        make_round(db, {"claude": ("Nexus", 0.4, 0.6), "grok": ("CYBERSHOKE", 0.6, 0.6)}, "CYBERSHOKE", f"p{i}", now)
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.63, 0.7, 75, "Grok, you're about to learn what a favourite is."),
                          "codex": ("CYBERSHOKE", 0.63, 0.8), "agy": ("CYBERSHOKE", 0.64, 0.95),
                          "grok": ("Nexus", 0.21, 0.55)}, "Nexus", "main", now, high_stakes=True, bench_stake=500)
    db.put("wallet_claude", "0x85B0328F9c3DF06d47000a57FE3FAE8A1626856a")
    for i, (u, pick) in enumerate([("satoshi", "Nexus"), ("vitalik", "Nexus"), ("degen", "CYBERSHOKE")]):
        db.x("INSERT INTO humans(round_id,user_id,username,pick,won) VALUES(?,?,?,?,?)", rid, f"u{i}", u, pick, pick == "Nexus")
    data = exports.export(s, db)
    lb = data["leaderboard"]
    r = next(x for x in data["rounds"] if x["round_id"] == rid)
    cr = CardRenderer(s)
    try:
        paths = [await cr.decision(r, r["entries"][0], lb["agents"]),
                 await cr.split(r, lb["agents"], headline="GRUDGE MATCH: Claude vs Grok — clash #4",
                                grudge={"a_name": "Claude", "b_name": "Grok", "a_wins": 1, "b_wins": 2}),
                 await cr.results(r, lb["agents"], lb["crowd"], cast=v.commentary(r), top_humans=lb["humans"][:3]),
                 await cr.trophy("2026-09", lb["agents"], lb["crowd"]),
                 await cr.milestone("ms-x", "Grok has won 4 straight", "Hottest hand in the pit", "grok", "2026-10"),
                 await cr.human_winner(r, {"username": "satoshi", "user_id": "u0", "pick": "Nexus"}, ["Claude", "Codex", "Gemini"]),
                 await cr.awards("2026-W41", weekly_awards([x for x in data["rounds"] if x["state"] == "resolved"]))]
        frames = await cr.frames("race.html", media.race_contexts(sorted(data["rounds"], key=lambda x: x["round_id"]),
                                                                  AGENTS, "2026-W41", sub=2, hold=1), s.cards_dir / "race")
        paths.append(frames[-1])
        banner = await cr.banner(lb["agents"], lb["crowd"], lb["season"])
    finally:
        await cr.close()
    for p in paths:
        head = p.read_bytes()[:24]
        assert (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) == (1200, 675), p
    head = banner.read_bytes()[:24]
    assert (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) == (1500, 500)
    import os
    if os.environ.get("BENCH_KEEP_CARDS"):
        import shutil
        shutil.copytree(s.cards_dir, os.environ["BENCH_KEEP_CARDS"], dirs_exist_ok=True)


async def test_publisher_resolution_extras(env):
    s, db = env
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.63, 0.7), "grok": ("Nexus", 0.21, 0.55)})
    pub = BenchPublisher(s, db)
    pub.cards = NoCards()
    pub.engage.cards = NoCards()
    try:
        for e in db.round_entries(rid):
            await pub.on_bet(rid, e["run_id"])
        await pub.on_bets_done(rid)
        for e in db.round_entries(rid):
            if e["outcome"]:
                pay = e["shares_filled"] if e["outcome"] == "Nexus" else 0
                db.settle_bet(e["run_id"], pay, pay - 100)
        db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())
        await pub.on_resolved(rid)
    finally:
        await pub.close()
    res = json.loads((s.outbox_dir / f"r{rid}-results.json").read_text())
    assert res["text"].startswith("Resolved: Nexus. Grok went alone and was right.")   # data hook; commentary is on the card
    assert db.get("milestone_state")["rounds"] == 1



def test_market_refs_reads_our_link_and_polymarket():
    from agentpitbench.virality import market_refs
    url = "https://agentpit.dev/start?market={slug}"
    assert market_refs(["https://agentpit.dev/start?market=nhl-a&utm_source=x"], "", url) == ["nhl-a"]
    assert market_refs([], "bet on https://polymarket.com/market/btc-84k please", url) == ["btc-84k"]
    assert market_refs(["https://example.com/market/x"], "", url) == []
