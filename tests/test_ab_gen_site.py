"""A/B generator (fake runner), publish with arms, site CTA / /go/ hop / /experiments/, Cloudflare clicks."""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_publish import AGENTS, PICKS, make_round  # noqa: E402

from agentpitbench import ab, ab_gen, ab_report, cf, exports, opai, orchestrator, site  # noqa: E402
from agentpitbench.config import Settings  # noqa: E402
from agentpitbench.db import DB  # noqa: E402
from agentpitbench.publish import BenchPublisher  # noqa: E402

HOOK = {"situations": {"agree": "Unanimous call", "alone": "{score}: {lone} stands apart",
                       "dead_split": "{score}: split down the middle", "nobody_agrees": "{score}: no two alike",
                       "nway": "{score}: a split field"}}
NOON = datetime(2026, 10, 12, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_ACCESS_TOKEN", "X_OAUTH2_REFRESH_TOKEN", "CF_API_TOKEN", "CF_ACCOUNT_TAG", "CF_SITE_TAG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, deploy_debounce_s=0, animate=False,
                 ab_gen_enabled=True)
    db = DB(s.db_path)
    ab.seed(db)
    db.put("sweep_2026-10-12", "ok")
    return s, db


def runner_returning(stdout: str, rc: int = 0, calls: list | None = None):
    async def run(cmd, stdin, cwd, timeout):
        if calls is not None:
            calls.append((cmd, stdin))
        return rc, stdout
    return run


def cli_json(answer: dict) -> str:
    return json.dumps({"type": "result", "is_error": False, "result": "```json\n" + json.dumps(answer) + "\n```",
                       "modelUsage": {"claude-sonnet-x": {}}})


# 5. generator

async def test_generator_bad_json_and_timeout_record_failure(env):
    s, db = env
    r = await ab_gen.run(s, db, idle=lambda: True, now=NOON, runner=runner_returning("not json"))
    assert r["status"] == "failed" and db.get("ab_gen_2026-10-12")["status"] == "failed"
    assert db.one("SELECT COUNT(*) c FROM ab_arms WHERE output_id IS NOT NULL")["c"] == 0
    db.x("DELETE FROM kv WHERE k='ab_gen_2026-10-12'")
    r = await ab_gen.run(s, db, idle=lambda: True, now=NOON, runner=runner_returning("timeout", rc=None))
    assert r["status"] == "failed"


async def test_generator_valid_output_live_queued_and_tagged(env):
    s, db = env
    calls = []
    answer = {"reveal.hook": [HOOK, {"situations": {"agree": "All in"}}],
              "reveal.cta": [{"text": "Your call"}, {"text": "Pick a side"}, {"text": "Who has it"},
                             {"text": "Claude says hi"}],
              "press.head": [{"text": "From the losing bench:"}]}
    r = await ab_gen.run(s, db, idle=lambda: True, now=NOON, runner=runner_returning(cli_json(answer), calls=calls))
    assert r["status"] == "ok" and r["accepted"] == 5 and r["rejected"] == 2
    cmd, prompt = calls[0]
    assert cmd[:2] == ["claude", "-p"] and "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert "reveal.hook" in prompt and "{lone}" in prompt and "Picks" not in prompt.split("Slots")[0][:0]
    rows = db.q("SELECT * FROM operator_ai_outputs")
    assert len(rows) == 7 and {r["model"] for r in rows} == {"claude-sonnet-x"}
    assert {r["prompt_version"] for r in rows} == {ab_gen.AB_PROMPT_VERSION}
    assert sum(1 for r in rows if not r["accepted"]) == 2 and all(r["reject_reason"] for r in rows if not r["accepted"])
    st = [a["status"] for a in ab.arms(db, "reveal.cta", None) if not a["is_control"]]
    assert st == ["live", "live", "queued"]                     # ab_max_arms = 3: control + 2
    again = await ab_gen.run(s, db, idle=lambda: True, now=NOON + 60, runner=runner_returning(cli_json(answer)))
    assert again["status"] == "skipped"                          # once per day


async def test_generator_guards(env):
    s, db = env
    run = runner_returning(cli_json({}))
    assert (await ab_gen.run(s, db, idle=lambda: False, now=NOON, runner=run))["why"] == "a round is running"
    early = datetime(2026, 10, 12, 2, tzinfo=timezone.utc).timestamp()
    assert (await ab_gen.run(s, db, idle=lambda: True, now=early, runner=run))["status"] == "skipped"
    db.x("DELETE FROM kv WHERE k='sweep_2026-10-12'")
    assert "sweep" in (await ab_gen.run(s, db, idle=lambda: True, now=NOON, runner=run))["why"]
    s.ab_gen_enabled = False
    assert "disabled" in (await ab_gen.run(s, db, idle=lambda: True, now=NOON, runner=run))["why"]


async def test_generator_dry_inserts_nothing_and_budget(env):
    s, db = env
    answer = {"reveal.cta": [{"text": "Your call"}]}
    r = await ab_gen.run(s, db, idle=lambda: True, now=NOON, dry=True, runner=runner_returning(cli_json(answer)))
    assert r["candidates"][0]["errors"] == [] and not db.q("SELECT 1 FROM operator_ai_outputs")
    assert len(ab.arms(db, "reveal.cta")) == 1
    await ab_gen.run(s, db, idle=lambda: True, now=NOON, dry=True, runner=runner_returning(cli_json(answer)))
    r = await ab_gen.run(s, db, idle=lambda: True, now=NOON, dry=True, runner=runner_returning(cli_json(answer)))
    assert r["status"] == "failed"                               # 2 calls/day spent


def test_opai_parse():
    assert opai.parse(cli_json({"a": 1}), "sonnet") == ({"a": 1}, "claude-sonnet-x")
    assert opai.parse(json.dumps({"is_error": True, "result": "quota"}), "sonnet")[0] is None
    assert opai.parse("garbage", "sonnet") == (None, "sonnet")


# 7. publish with arms

async def _publish(s, db, rid, winner="Nexus", fail_video=False):
    pub = BenchPublisher(s, db)

    async def none(*a, **k):
        return None
    for name in ("decision", "split", "split_clip", "results", "human", "milestone", "trophy"):
        setattr(pub.cards, name, none)
    pub.engage.after_resolution = none
    pub.engage.model_changes = none
    pub.engage.collect_humans = none
    vid = s.cards_dir / "fake.mp4"

    async def clip(*a, **k):
        vid.parent.mkdir(parents=True, exist_ok=True)
        vid.write_bytes(b"mp4")
        return vid
    pub.cards.results_clip = clip
    if fail_video:
        real = pub.poster.post

        async def post(key, kind, text, image=None, **kw):
            if image and str(image).endswith(".mp4"):
                return None
            return await real(key, kind, text, image, **kw)
        pub.poster.post = post
    try:
        await pub.on_bets_done(rid)
        db.set_round(rid, state="resolved", winner=winner, resolved_at=time.time())
        for e in db.round_entries(rid):
            if e["outcome"]:
                payout = e["shares_filled"] if e["outcome"] == winner else 0
                db.settle_bet(e["run_id"], payout, payout - e["stake_filled"])
        await pub.on_resolved(rid)
    finally:
        await pub.close()
    return {json.loads(p.read_text())["post_key"]: json.loads(p.read_text()) for p in s.outbox_dir.glob("*.json")}


async def test_publish_with_arms_keeps_invariants(env):
    s, db = env
    s.animate = True
    ab.add_arm(db, s, "reveal.hook", HOOK)
    ab.add_arm(db, s, "reveal.cta", {"text": "Your call"})
    rh = {"situations": {k: "Final: {winner}, {record}" + (" for {lone}" if k.startswith("lone") else "")
                         + (" ({winners} over {losers})" if k == "split" else "") for k in ab.SLOTS["results.hook"]["situations"]}}
    assert ab.validate("results.hook", rh) == []
    ab.add_arm(db, s, "results.hook", rh)
    ab.add_arm(db, s, "results.cta", {"text": "Every pick and price"})
    for slot in ("reveal.hook", "reveal.cta", "results.hook", "results.cta", "tags", "results.media"):
        live = [a for a in ab.arms(db, slot) if not a["is_control"]]
        for a in ab.arms(db, slot):
            if a["arm_id"] != live[0]["arm_id"]:
                for i in range(50):
                    db.x("INSERT INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at)"
                         " VALUES(?,?,?,?,?,?)", f"x-{a['arm_id']}-{i}", slot, a["arm_id"], a["arm_id"], "x", 0)
    rid = make_round(db, PICKS)
    tw = await _publish(s, db, rid, fail_video=False)
    rev, res = tw[f"r{rid}-reveal"]["text"], tw[f"r{rid}-results"]["text"]
    assert rev.startswith("3–1: Grok stands apart.\n") and "\nYour call\nReply with your pick: CYBERSHOKE / Nexus" in rev
    assert "#AgentpitBench #AI" in rev or "#AgentpitBench #PredictionMarkets" in rev
    assert "Claude: CYBERSHOKE · ChatGPT: CYBERSHOKE · Gemini: CYBERSHOKE · Grok: Nexus" in rev and "http" not in rev
    assert res.startswith("Final: Nexus, 1-3 for Grok\n") and "@xai" in res
    assert res.count("https://") == 1 and f"/round/{rid}/" in res and "\nEvery pick and price\nhttps://" in res
    assert tw[f"r{rid}-results"]["image"].endswith(".png")                 # the still arm: no clip posted
    real = {r["slot"]: r for r in db.q("SELECT * FROM ab_assignments WHERE post_key=?", f"r{rid}-results")}
    assert all(r["realized_arm_id"] == r["arm_id"] for r in real.values())
    assert exports.round_record(s, db, db.round(rid))["share_hook"] == "Final: Nexus, 1-3 for Grok"
    assert db.round(rid)["headline_variant"] == db.get(f"ab_tone_{ab.utc_day()}")   # tone per day


async def test_video_failure_records_still(env):
    s, db = env
    s.animate = True
    still = next(a for a in ab.arms(db, "results.media") if a["code_key"] == "still")
    for i in range(50):   # make exploration pick the clip (control)
        db.x("INSERT INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at) VALUES(?,?,?,?,?,?)",
             f"x-{i}", "results.media", still["arm_id"], still["arm_id"], "x", 0)
    rid = make_round(db, PICKS)
    await _publish(s, db, rid, fail_video=True)
    a = db.one("SELECT * FROM ab_assignments WHERE post_key=? AND slot='results.media'", f"r{rid}-results")
    assert a["arm_id"] != still["arm_id"] and a["realized_arm_id"] == still["arm_id"]


async def test_void_round_excludes_assignments(env):
    s, db = env
    rid = make_round(db, PICKS)
    await _publish(s, db, rid)
    pub = BenchPublisher(s, db)
    try:
        await pub.void_round(rid)
    finally:
        await pub.close()
    rows = db.q("SELECT excluded FROM ab_assignments WHERE post_key LIKE ?", f"r{rid}-%")
    assert rows and all(r["excluded"] == "void" for r in rows)


# 8. site

def test_site_cta_go_hop_and_experiments(env):
    s, db = env
    aid, _ = ab.add_arm(db, s, "site.round_cta", {"text": "Take the other side"})
    rid = make_round(db, PICKS, state="resolved", winner="Nexus")
    exports.export(s, db)
    out = site.build(s)
    en = (out / "round" / str(rid) / "index.html").read_text()
    arm = db.one("SELECT arm_id FROM ab_assignments WHERE slot='site.round_cta'")["arm_id"]
    assert f'href="../../go/round-cta/{arm}/{rid}/"' in en
    de = (out / "de" / "round" / str(rid) / "index.html").read_text()
    assert "Wette gegen die KIs →" in de and "go/round-cta" not in de          # other languages: translated control
    link = exports.round_record(s, db, db.round(rid))["market_link"]
    for a in ab.arms(db, "site.round_cta", None):                            # every arm's hop exists
        hop = (out / "go" / "round-cta" / str(a["arm_id"]) / str(rid) / "index.html").read_text()
        assert link.replace("&", "&amp;") in hop and "noindex" in hop and "cloudflareinsights" in hop
        assert "location.replace" in hop and "setTimeout(go,300)" in hop
    assert "go/" not in (out / "sitemap.xml").read_text()
    exp = (out / "experiments" / "index.html").read_text()
    for banned in ("model", "prompt_version", "output_id", "sonnet"):
        assert banned not in exp.lower().replace("og:", "") or banned == "model" and "data-model" not in exp
    data = json.loads((out / "data" / "experiments.json").read_text())
    assert not any(k in json.dumps(data) for k in ("prompt_version", "output_id", "\"model\""))
    assert (out / "de" / "experiments" / "index.html").exists()


def test_experiments_numbers_only_from_old_posts(env, monkeypatch):
    s, db = env
    from agentpitbench import xstats
    now = time.time()
    tags = ab.arms(db, "tags")
    for i, age in enumerate((1, 10)):
        key = f"r{i}-results"
        db.x("INSERT INTO ab_assignments(post_key,slot,arm_id,realized_arm_id,kind,assigned_at) VALUES(?,?,?,?,?,?)",
             key, "tags", tags[1]["arm_id"], tags[1]["arm_id"], "results", now - age * 86400)
        db.x("INSERT INTO tweets(post_key,tweet_id,kind,posted_at) VALUES(?,?,?,?)", key, f"t{i}", "results",
             now - age * 86400)
    monkeypatch.setattr(xstats, "reward_inputs", lambda db, tid, b, s=None: {"replies": 3} if b == "24h" else None)
    monkeypatch.setattr(xstats, "rewards", lambda *a, **k: [])
    d = ab_report.experiments(db, s, now)
    arm = next(a for sl in d["slots"] if sl["slot"] == "tags" for a in sl["arms"] if a["arm"] == tags[1]["arm_id"])
    assert arm["n"] == 1                                                     # the 1-day-old post isn't counted
    full = next(a for a in ab.posterior(db, s, "tags", now) if a["arm_id"] == tags[1]["arm_id"])
    assert full["n"] == 2 and full["n_obs"] == 2


# Cloudflare

def test_cf_without_token_is_silent(env):
    s, db = env
    ab.choose(db, s, "site.round_cta", "site-2026-10-01", kind="site")
    assert cf.fetch_due(s, db) == 0 and db.q("SELECT 1 FROM kv WHERE k LIKE 'cf_missing_%'")
    assert ab.observations(db, s, "site.round_cta") == []


def test_cf_fetch_and_site_reward(env, monkeypatch):
    s, db = env
    monkeypatch.setenv("CF_API_TOKEN", "tok")
    monkeypatch.setenv("CF_ACCOUNT_TAG", "acct")
    ab.add_arm(db, s, "site.round_cta", {"text": "Take the other side"})
    c = ab.choose(db, s, "site.round_cta", "site-2026-10-01", kind="site")
    sent = []

    class R:
        status_code = 200

        def json(self):
            return {"data": {"viewer": {"accounts": [{"rumPageloadEventsAdaptiveGroups": [
                {"count": 3, "avg": {"sampleInterval": 2}, "dimensions": {"requestPath": f"/go/round-cta/{c.arm_id}/7/"}},
                {"count": 9, "avg": {"sampleInterval": 1}, "dimensions": {"requestPath": "/round/7/"}}]}]}}}

    class H:
        @staticmethod
        def post(url, headers, json, timeout):
            sent.append((url, headers, json))
            return R()
    assert cf.fetch_due(s, db, now=NOON, http=H) == 1
    url, headers, body = sent[0]
    assert url == cf.GRAPHQL_URL and headers["Authorization"] == "Bearer tok"
    q = body["query"]
    assert "rumPageloadEventsAdaptiveGroups" in q and '"acct"' in q and "2026-10-01T00:00:00Z" in q
    assert "2026-10-02T00:00:00Z" in q and "requestPath" in q and 'requestHost: "agentpitbench.org"' in q
    assert db.q("SELECT path, views FROM cf_clicks")[0]["views"] == 6
    o = ab.observations(db, s, "site.round_cta")
    assert len(o) == 1 and o[0]["arm_id"] == c.arm_id
    assert cf.fetch_due(s, db, now=NOON, http=H) == 0                      # a fetched day is never re-read
