"""X analytics (docs/x-analytics-spec.md §9): the read ledger, the metrics puller and the query layer.
Fixtures and a fake X only: nothing here touches the network."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentpitbench import cli, xstats
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.twitter import ME_URL, TWEETS_URL, Poster, RateLimited, _check

from test_virality import AGENTS, make_round

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc).timestamp()
TODAY = "2026-10-12"
H, D = 3600, 86400


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.text, self.headers = status, body, json.dumps(body), headers or {}

    def json(self):
        return self._body


class FakeX:
    """Token endpoint plus the read endpoints the bench uses. Records every request with its params."""

    def __init__(self, impressions=None, gone=(), lookup=None, timeline=None):
        self.calls, self.impressions, self.gone = [], impressions or {}, set(gone)
        self.lookup, self.timeline = lookup, timeline
        self.rate_limit_reset = None
        self.status = None

    def post(self, url, auth=None, data=None, timeout=None):
        return FakeResp(200, {"access_token": "a1", "refresh_token": "r1", "expires_in": 7200,
                              "scope": "tweet.read tweet.write users.read media.write offline.access"})

    def request(self, method, url, headers=None, timeout=None, params=None, **kw):
        assert method == "GET"
        self.calls.append((url, dict(params or {})))
        if self.rate_limit_reset is not None:
            return FakeResp(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": str(self.rate_limit_reset)})
        if self.status:
            return FakeResp(self.status, {"title": "Forbidden"})
        if url == ME_URL:
            return FakeResp(200, {"data": {"id": "999", "username": "agentpitbench", "name": "AgentpitBench",
                                           "public_metrics": {"followers_count": 120, "following_count": 5,
                                                              "tweet_count": 300, "listed_count": 2}}})
        if url.endswith("/999/tweets"):
            return FakeResp(200, self.timeline or {"data": [], "meta": {"result_count": 0}})
        if url.endswith("/mentions"):
            return FakeResp(200, {"data": [{"id": "5001", "author_id": "u1", "text": "@agentpitbench hi"}],
                                  "includes": {"users": [{"id": "u1", "username": "fan"}]}})
        if "search/recent" in url:
            return FakeResp(200, {"data": [{"id": "6001", "author_id": "u1", "text": "Nexus"},
                                           {"id": "6002", "author_id": "u2", "text": "CYBERSHOKE"}],
                                  "includes": {"users": [{"id": "u1", "username": "a"}, {"id": "u2", "username": "b"}]}})
        if url == TWEETS_URL:
            if self.lookup:
                return FakeResp(200, self.lookup)
            ids = params["ids"].split(",")
            non_public = "non_public_metrics" in params.get("tweet.fields", "")
            data = [{"id": i, "public_metrics": {"impression_count": self.impressions.get(i, 100), "like_count": 1,
                                                 "reply_count": 0, "retweet_count": 0, "quote_count": 0,
                                                 "bookmark_count": 0},
                     **({"non_public_metrics": {"impression_count": self.impressions.get(i, 100), "engagements": 5,
                                                "url_link_clicks": 1, "user_profile_clicks": 0}} if non_public else {})}
                    for i in ids if i not in self.gone]
            errors = [{"value": i, "title": "Not Found Error", "resource_type": "tweet", "parameter": "ids",
                       "resource_id": i, "detail": f"Could not find tweet with ids: [{i}]."}
                      for i in ids if i in self.gone]
            return FakeResp(200, {"data": data, **({"errors": errors} if errors else {})})
        raise AssertionError(f"unexpected X call {url}")

    def ids_requested(self) -> list[str]:
        return [i for u, p in self.calls if u == TWEETS_URL for i in p["ids"].split(",")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_OAUTH2_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    monkeypatch.setenv("X_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("X_OAUTH2_REFRESH_TOKEN", "r0")
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=False, deploy_debounce_s=0, animate=False)
    return s, DB(s.db_path)


def poster(s, db, fake) -> Poster:
    p = Poster(s, db)
    p.oauth2.http = fake
    return p


def add_post(db, tid, age_s, kind="results", image=None, round_id=None, now=NOW, key=None):
    db.x("INSERT INTO tweets(post_key,tweet_id,kind,round_id,text,image_path,posted_at) VALUES(?,?,?,?,?,?,?)",
         key or f"k-{tid}", tid, kind, round_id, "t", image, now - age_s)


def add_row(db, tid, bucket, age_s, impressions, taken_at=None, **cols):
    row = {"tweet_id": tid, "taken_at": taken_at if taken_at is not None else NOW - 1000 + age_s, "age_s": age_s,
           "bucket": bucket, "impressions": impressions, **cols}
    db.x(f"INSERT INTO tweet_metrics({','.join(row)}) VALUES({','.join('?' * len(row))})", *row.values())


def ledger(db, **where) -> list[dict]:
    sql = "SELECT * FROM x_reads" + (" WHERE " + " AND ".join(f"{k}=?" for k in where) if where else "")
    return [dict(r) for r in db.q(sql + " ORDER BY day, resource, id", *where.values())]


# 1. due rule
def test_due_rule(env):
    s, db = env
    for tid, age, img in [("101", 0.5 * H, None), ("102", 1.2 * H, None), ("103", 25 * H, None),
                          ("104", 73 * H, None), ("105", 73 * H, "/c/results.mp4"), ("106", 8 * D, None),
                          ("107", 3 * D, None), ("108", 9 * D, "/c/a.png,/c/b.png")]:
        add_post(db, tid, age, image=img)
    add_post(db, "dry-r1-results", 1.2 * H)                                  # outbox
    add_post(db, "109", 1.2 * H)
    db.x("UPDATE tweets SET deleted=1 WHERE tweet_id='109'")
    due = {t: d["bucket"] for t, d in xstats.due_posts(db, NOW).items()}
    assert due == {"102": "1h", "103": "24h", "105": "72h", "106": "7d", "108": "7d"}
    add_row(db, "102", "1h", 1.1 * H, 50)                                    # taken: not due again
    assert "102" not in xstats.due_posts(db, NOW)
    assert xstats.due_bucket(25 * H, False, {"1h"}) == "24h"
    assert xstats.due_bucket(30 * D, True, set()) == "7d"                    # 7d always backfills


# 2. 29-day split, never promoted/organic on routine pulls
def test_29_day_split_and_no_promoted(env):
    s, db = env
    fake = FakeX()
    add_post(db, "201", 30 * D, image="/c/replay.mp4")
    add_post(db, "202", 1.2 * H, image="/c/results.mp4")
    out = xstats.pull_due(s, db, poster(s, db, fake), now=NOW)
    assert out["stored"] == 2
    lookups = [p for u, p in fake.calls if u == TWEETS_URL]
    old = next(p for p in lookups if p["ids"] == "201")
    young = next(p for p in lookups if p["ids"] == "202")
    assert "non_public_metrics" not in old["tweet.fields"] and "non_public_metrics" not in old["media.fields"]
    assert "non_public_metrics" in young["tweet.fields"] and "non_public_metrics" in young["media.fields"]
    for u, p in fake.calls:
        assert "promoted_metrics" not in json.dumps(p) and "organic_metrics" not in json.dumps(p)
    assert xstats.snapshot(db, "201", "7d")["engagements"] is None             # public only
    assert xstats.snapshot(db, "202", "1h")["engagements"] == 5


# 3. ledger = X's bill: per resource per UTC day
def test_ledger_dedup_per_utc_day(env):
    s, db = env
    p = poster(s, db, FakeX())
    p.read(TWEETS_URL, {"ids": "301,302"}, "post", "metrics", now=NOW)
    p.read(TWEETS_URL, {"ids": "301"}, "post", "metrics", now=NOW + 3600)     # same UTC day: free
    assert len(ledger(db)) == 2
    assert xstats.reads(db, TODAY, s)["usd"] == pytest.approx(0.010)
    midnight = datetime(2026, 10, 13, 0, 0, 1, tzinfo=timezone.utc).timestamp()
    p.read(TWEETS_URL, {"ids": "301"}, "post", "metrics", now=midnight)       # after 00:00 UTC: billed again
    assert [r["day"] for r in ledger(db, id="301")] == [TODAY, "2026-10-13"]
    p.read(ME_URL, {}, "user", "me", now=NOW)                                 # dict data: one user
    assert ledger(db, resource="user") == [{"day": TODAY, "resource": "user", "id": "999", "caller": "me"}]
    p.read("https://api.x.com/2/tweets/search/recent", {}, "post", "replies", now=NOW)
    users = ledger(db, resource="user", caller="replies")
    assert {u["id"] for u in users} == {"u1", "u2"}                           # includes.users count as users
    r = xstats.reads(db, TODAY, s)
    assert r["by_resource"]["user"]["n"] == 3 and r["by_caller"]["replies"]["n"] == 4


# 4. budget
def test_budget_zero_rereads_only_billed_ids(env):
    s, db = env
    fake = FakeX()
    s.x_metrics_budget_usd = 0.0
    db.put("x_account_day", TODAY)
    add_post(db, "401", 1.2 * H)
    add_post(db, "402", 1.3 * H)
    db.x("INSERT INTO x_reads VALUES(?,?,?,?)", TODAY, "post", "401", "likes")   # already billed today
    out = xstats.pull_due(s, db, poster(s, db, fake), now=NOW)
    assert fake.ids_requested() == ["401"]
    assert out["skipped_budget"] == 1 and out["billed_new"] == 0
    assert xstats.health(db, NOW)["skipped_for_budget_today"] == 1
    assert xstats.snapshot(db, "401", "1h") and not xstats.snapshot(db, "402", "1h")


def test_budget_ten_cents_is_twenty_posts(env):
    s, db = env
    fake = FakeX()
    s.x_metrics_budget_usd = 0.10
    db.put("x_account_day", TODAY)
    for i in range(50):
        add_post(db, str(4100 + i), 1.2 * H + i)
    out = xstats.pull_due(s, db, poster(s, db, fake), now=NOW)
    assert len(fake.ids_requested()) == 20 and out["billed_new"] == 20 and out["skipped_budget"] == 30
    assert xstats.reads(db, TODAY, s)["metrics_usd"] == pytest.approx(0.10)
    fake.calls.clear()
    out = xstats.pull_due(s, db, poster(s, db, fake), now=NOW + 600)          # budget spent: only free re-reads
    assert out["billed_new"] == 0 and len(fake.ids_requested()) == 20


# 5. every caller writes ledger rows under its own name
def test_every_read_caller_is_ledgered(env, monkeypatch, capsys):
    s, db = env
    fake = FakeX()

    class P(Poster):
        def __init__(self, s_, db_):
            super().__init__(s_, db_)
            self.oauth2.http = fake
    monkeypatch.setattr("agentpitbench.twitter.Poster", P)
    cli.x_check(s, probe_upload=False)
    assert "missing scopes: none" in capsys.readouterr().out
    assert {r["caller"] for r in ledger(db)} == {"me"}                        # x-check's users/me read
    db.x("DELETE FROM x_reads")
    db.x("DELETE FROM kv")
    p = poster(s, db, fake)
    p.replies("123", 100)
    p.mentions(None)                                                         # my_user_id first: 'me'
    p.like_counts(["5001", "7001"])
    later = NOW + 400 * D                                                    # a UTC day of its own
    add_post(db, "501", 1.2 * H, now=later)
    xstats.pull_due(s, db, p, now=later)                                     # account + metrics
    callers = {r["caller"] for r in ledger(db)}
    assert callers == {"replies", "me", "mentions", "likes", "account", "metrics"}
    assert ledger(db, id="501")[0]["caller"] == "metrics"


# 6. Not Found, void_round, boosted
def test_not_found_is_flagged_and_never_reread(env):
    s, db = env
    fake = FakeX(gone={"602"})
    add_post(db, "601", 1.2 * H)
    add_post(db, "602", 1.2 * H)
    out = xstats.pull_due(s, db, poster(s, db, fake), now=NOW)
    assert out["deleted"] == 1 and db.one("SELECT deleted FROM tweets WHERE tweet_id='602'")["deleted"] == 1
    fake.calls.clear()
    xstats.pull_due(s, db, poster(s, db, fake), now=NOW + 23 * H)            # 601's 24h is due, 602 never again
    assert "602" not in fake.ids_requested() and "601" in fake.ids_requested()
    assert xstats.reward_inputs(db, "602", "1h") is None


async def test_void_round_removes_metric_rows(env):
    from agentpitbench.publish import BenchPublisher
    from test_virality import FakePoster, NoCards
    s, db = env
    s.dry_run = True
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7), "agy": ("Nexus", 0.4, 0.6)})
    add_post(db, "651", 2 * H, round_id=rid, key=f"r{rid}-reveal", kind="reveal")
    add_row(db, "651", "1h", 1.05 * H, 300)
    add_row(db, "651", None, 1.5 * H, 400)
    pub = BenchPublisher(s, db)

    class Del(FakePoster):
        async def delete(self, tweet_id, post_key=""):
            return True
    pub.cards, pub.poster = NoCards(), Del()
    try:
        await pub.void_round(rid)
    finally:
        await pub.close()
    assert db.one("SELECT COUNT(*) n FROM tweet_metrics")["n"] == 0


def test_boosted_excluded_from_rewards(env):
    s, db = env
    add_post(db, "661", 3 * H)
    add_post(db, "662", 3 * H)
    add_row(db, "661", "1h", 1.1 * H, 500)
    add_row(db, "662", "1h", 1.1 * H, 600)
    assert xstats.set_boosted(db, "661")
    assert xstats.reward_inputs(db, "661", "1h") is None
    assert [r["tweet_id"] for r in xstats.rewards(db, "1h")] == ["662"]
    assert xstats.reward_inputs(db, "662", "1h")["impressions"] == 600
    assert not xstats.set_boosted(db, "999999")                              # not ours


# 7. parsing: lookup and timeline fixtures map to every column
def test_parse_lookup_fixture(env):
    s, db = env
    body = json.loads((FIX / "x_lookup.json").read_text())
    add_post(db, "1900000000000000001", 1.2 * H, image="/c/results.mp4")
    add_post(db, "1900000000000000002", 1.3 * H, kind="reveal", image="/c/a.png,/c/b.png")
    add_post(db, "1900000000000000099", 1.4 * H)
    fake = FakeX(lookup=body)
    out = xstats.pull_due(s, db, poster(s, db, fake), now=NOW)
    assert out["deleted"] == 1
    r = xstats.snapshot(db, "1900000000000000001", "1h")
    assert {k: r[k] for k in ("impressions", "likes", "replies", "reposts", "quotes", "bookmarks", "engagements",
                              "link_clicks", "profile_clicks", "video_views", "p0", "p25", "p50", "p75", "p100")} == {
        "impressions": 4200, "likes": 40, "replies": 5, "reposts": 3, "quotes": 1, "bookmarks": 2,
        "engagements": 310, "link_clicks": 25, "profile_clicks": 12, "video_views": 1500,
        "p0": 1400, "p25": 900, "p50": 600, "p75": 400, "p100": 250}
    assert r["age_s"] == pytest.approx(1.2 * H) and r["organic_impressions"] is None
    still = xstats.snapshot(db, "1900000000000000002", "1h")
    assert still["impressions"] == 800 and still["video_views"] is None and still["p0"] is None
    assert json.loads(db.one("SELECT metrics_json FROM tweets WHERE tweet_id='1900000000000000001'")["metrics_json"]
                      )["impressions"] == 4200
    assert xstats.video_retention(db, "1900000000000000001") == {
        "plays": 1400, "p25": round(900 / 1400, 4), "p50": round(600 / 1400, 4), "p75": round(400 / 1400, 4),
        "p100": round(250 / 1400, 4), "video_views": 1500, "age_s": pytest.approx(1.2 * H), "taken_at": NOW}


def test_parse_timeline_fixture_skips_unknown_posts(env):
    s, db = env
    s.x_app_owned = True
    body = json.loads((FIX / "x_timeline.json").read_text())
    add_post(db, "1900000000000000011", 1.2 * H, kind="press")
    add_post(db, "1900000000000000012", 25 * H, kind="race", image="/c/race.mp4")
    fake = FakeX(timeline=body)                                               # ...013 has no tweets row
    xstats.pull_due(s, db, poster(s, db, fake), now=NOW)
    tl = [p for u, p in fake.calls if u.endswith("/999/tweets")]
    assert len(tl) == 1 and tl[0]["max_results"] == 100 and "non_public_metrics" in tl[0]["tweet.fields"]
    assert "exclude" not in tl[0]                                             # press/replay replies included
    assert tl[0]["start_time"] < tl[0]["end_time"]
    assert xstats.snapshot(db, "1900000000000000011", "1h")["engagements"] == 20
    race = xstats.snapshot(db, "1900000000000000012", "24h")
    assert (race["impressions"], race["link_clicks"], race["video_views"], race["p0"], race["p100"]) == (
        2600, 11, 980, 900, 90)
    assert db.one("SELECT COUNT(*) n FROM tweet_metrics WHERE tweet_id='1900000000000000013'")["n"] == 0
    assert {r["resource"] for r in ledger(db, caller="metrics")} == {"owned"}


def test_timeline_missing_due_post_gets_one_lookup(env):
    s, db = env
    s.x_app_owned = True
    add_post(db, "701", 1.2 * H)
    fake = FakeX(timeline={"data": [], "meta": {"result_count": 0}})
    xstats.pull_due(s, db, poster(s, db, fake), now=NOW)
    assert fake.ids_requested() == []
    xstats.pull_due(s, db, poster(s, db, fake), now=NOW + 600)               # second miss: settle by lookup
    assert fake.ids_requested() == ["701"] and xstats.snapshot(db, "701", "1h")


# 8. queries on synthetic rows
def test_reply_cap_hit(env):
    s, db = env
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7)})
    assert xstats.reply_cap_hit(db, rid) is None
    db.set_round(rid, thread_tweet_id="801")
    add_post(db, "801", 30 * H, round_id=rid, kind="reveal")
    assert xstats.reply_cap_hit(db, rid) is None
    add_row(db, "801", "1h", 1.1 * H, 900, replies=40)
    assert xstats.reply_cap_hit(db, rid, max_reply_reads=100) is False
    add_row(db, "801", "24h", 24.2 * H, 2000, replies=140)
    assert xstats.reply_cap_hit(db, rid, max_reply_reads=100) is True


def test_by_kind_and_by_category_and_by_agent(env):
    s, db = env
    cs = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7), "grok": ("Nexus", 0.4, 0.6)}, winner="Nexus")
    btc = make_round(db, {"claude": ("Yes", 0.5, 0.7), "agy": ("Yes", 0.5, 0.6)}, winner="Yes", mid="m2",
                     q="Will Bitcoin close above $120k on Oct 12?", outcomes_list=["Yes", "No"])
    rows = [("811", "results", cs, 1000, 10, 5), ("812", "decision", cs, 200, 4, None), ("813", "agree", btc, 300, 3, 1),
            ("814", "results", btc, 3000, 30, 9), ("815", "milestone", None, 0, 0, 0)]
    for tid, kind, rid, imp, likes, clicks in rows:
        add_post(db, tid, 30 * H, kind=kind, round_id=rid)
        add_row(db, tid, "24h", 24.1 * H, imp, likes=likes, replies=0, reposts=0, quotes=0, link_clicks=clicks)
    k = {r["kind"]: r for r in xstats.by_kind(db, NOW - 7 * D)}
    assert set(k) == {"results", "decision", "agree", "milestone"}           # legacy kinds included
    assert k["results"]["n"] == 2 and k["results"]["impressions_median"] == 2000
    assert k["results"]["interaction_rate"] == pytest.approx(40 / 4000, abs=1e-5)
    assert k["results"]["link_ctr"] == pytest.approx(14 / 4000, abs=1e-5)
    assert k["milestone"]["interaction_rate"] is None                        # 0 impressions skipped in rates
    assert k["decision"]["link_ctr"] is None                                  # no non-public counts
    cat = {r["category"]: r["n"] for r in xstats.by_category(db, NOW - 7 * D)}
    assert cat == {"Esports": 2, "Crypto": 2}
    ag = {(r["group"], r["key"]): r["n"] for r in xstats.by_agent(db, NOW - 7 * D)}
    assert ag == {("winner", "grok"): 2, ("winner", "claude"): 2, ("winner", "agy"): 2,
                  ("split", "split"): 2, ("split", "agree"): 2}
    assert xstats.by_kind(db, NOW + 1) == []                                  # since filters on posted_at


def test_reward_by_variant_and_series(env):
    s, db = env
    for i, (variant, imp, clicks) in enumerate([("fun", 1000, 10), ("fun", 3000, 20), ("plain", 500, 2)]):
        rid = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7)}, winner="Nexus", mid=f"v{i}")
        db.set_round(rid, headline_variant=variant)
        add_post(db, f"82{i}", 30 * H, round_id=rid)
        add_row(db, f"82{i}", "24h", 24.5 * H, imp, likes=5, replies=1, reposts=0, quotes=0, link_clicks=clicks)
    v = xstats.reward_by_variant(db, "24h")
    assert v["fun"] == {"n": 2, "impressions_median": 2000, "interactions_sum": 12, "link_clicks_sum": 30,
                        "impressions_sum": 4000}
    assert v["plain"]["n"] == 1
    assert {r["headline_variant"] for r in xstats.rewards(db, "24h")} == {"fun", "plain"}
    add_row(db, "820", None, 0.3 * H, 100, taken_at=NOW - 5000)
    add_row(db, "820", "1h", 1.05 * H, 400, taken_at=NOW - 4000)
    series = xstats.impressions_series(db, "820")
    assert [r["impressions"] for r in series] == [100, 400, 1000]
    assert [r["bucket"] for r in series] == [None, "1h", "24h"]
    assert set(series[0]) == {"taken_at", "age_s", "impressions", "engagements", "link_clicks", "bucket"}
    assert xstats.latest(db, "820")["impressions"] == 1000
    assert xstats.video_retention(db, "820") is None


def test_visibility_drop(env):
    s, db = env
    for i in range(12):                                                       # baseline: 12 posts at ~1000
        add_post(db, f"83{i:02d}", 10 * D - i * H)
        add_row(db, f"83{i:02d}", "1h", 1.05 * H, 1000 + i)
    for i in range(6):                                                        # the last 6: all under 20%
        add_post(db, f"84{i:02d}", 2 * D - i * H)
        add_row(db, f"84{i:02d}", "1h", 1.05 * H, 100)
    assert xstats.kind_baseline(db, "results", until=NOW) == pytest.approx(1002.5)  # 18 posts in 14 days
    drop = xstats.visibility_drop(db, NOW)
    assert drop["kind"] == "results" and len(drop["posts"]) == 6 and drop["baseline"] == pytest.approx(1005.5)
    db.x("UPDATE tweet_metrics SET impressions=500 WHERE tweet_id='8405'")
    assert xstats.visibility_drop(db, NOW) is None
    db.x("UPDATE tweet_metrics SET impressions=100 WHERE tweet_id='8405'")
    db.x("DELETE FROM tweet_metrics WHERE tweet_id IN ('8300','8301','8302')")  # < 10 baseline posts
    assert xstats.visibility_drop(db, NOW) is None


def test_followers_series_and_reads_and_digest(env):
    s, db = env
    fake = FakeX()
    p = poster(s, db, fake)
    xstats.pull_due(s, db, p, now=NOW - D)                                    # yesterday's account read
    db.x("UPDATE account_metrics SET followers=100")
    xstats.pull_due(s, db, p, now=NOW)                                        # today's: 120 followers
    assert xstats.followers_series(db) == [{"day": "2026-10-11", "followers": 100}, {"day": TODAY, "followers": 120}]
    assert xstats.followers_series(db, NOW) == [{"day": TODAY, "followers": 120}]
    add_post(db, "851", 20 * H)
    add_row(db, "851", None, 19 * H, 700, likes=3, replies=1, reposts=0, quotes=0, link_clicks=2)
    assert xstats.digest_due(s, db, NOW - 5 * H) is None                      # before 08:00 UTC
    d = xstats.digest_due(s, db, NOW)
    assert (d["posts"], d["impressions"], d["interactions"], d["link_clicks"], d["followers_delta"]) == (
        1, 700, 4, 2, 20)
    assert d["reads"]["by_caller"]["account"]["n"] == 1 and d["top"][0]["tweet_id"] == "851"
    assert json.loads((s.export_dir / "x-daily.json").read_text())["day"] == "2026-10-11"
    assert xstats.digest_due(s, db, NOW + 600) is None                        # once a day


# 9. dry_run, posting_paused, NullPublisher
def test_dry_run_reads_nothing_posting_paused_still_reads(env):
    from agentpitbench.orchestrator import NullPublisher
    s, db = env
    add_post(db, "901", 1.2 * H)
    fake = FakeX()
    s.dry_run = True
    assert xstats.pull_due(s, db, poster(s, db, fake), now=NOW)["requested"] == 0 and fake.calls == []
    s.dry_run, s.posting_paused = False, True
    assert xstats.pull_due(s, db, poster(s, db, fake), now=NOW)["requested"] == 1
    assert cli.metrics_task(s, db, NullPublisher()) is None
    from types import SimpleNamespace
    assert callable(cli.metrics_task(s, db, SimpleNamespace(poster=poster(s, db, fake))))


# 10. 429 with a reset header; 401/403 counted
def test_rate_limit_sets_resume_and_skips(env):
    s, db = env
    add_post(db, "1001", 1.2 * H)
    fake = FakeX()
    fake.rate_limit_reset = int(NOW + 900)
    p = poster(s, db, fake)
    out = xstats.pull_due(s, db, p, now=NOW)
    assert db.get("x_read_resume_at") == int(NOW + 900) and out["stored"] == 0
    assert xstats.health(db, NOW)["rate_limited_until"] == int(NOW + 900)
    assert ledger(db) == []                                                   # nothing came back: nothing billed
    fake.rate_limit_reset, n = None, len(fake.calls)
    xstats.pull_due(s, db, p, now=NOW + 600)
    assert len(fake.calls) == n                                               # before the reset: no request
    xstats.pull_due(s, db, p, now=NOW + 901)
    assert fake.ids_requested() == ["1001"] and xstats.health(db, NOW + 901)["rate_limited_until"] is None


def test_check_raises_rate_limited_still_runtime_error():
    with pytest.raises(RuntimeError) as e:
        _check(FakeResp(429, {}, {"x-rate-limit-reset": "1760270000"}))
    assert isinstance(e.value, RateLimited) and e.value.reset == 1760270000
    with pytest.raises(RateLimited) as e:
        _check(FakeResp(429, {}))
    assert e.value.reset == 0


def test_auth_failures_counted_and_reset(env):
    s, db = env
    add_post(db, "1011", 1.2 * H)
    fake = FakeX()
    fake.status = 403
    p = poster(s, db, fake)
    xstats.pull_due(s, db, p, now=NOW)
    xstats.pull_due(s, db, p, now=NOW + 600)
    assert xstats.health(db, NOW)["consecutive_auth_failures"] == 2
    fake.status = None
    xstats.pull_due(s, db, p, now=NOW + 1200)
    h = xstats.health(db, NOW + 1200)
    assert h["consecutive_auth_failures"] == 0 and h["last_ok"] == NOW + 1200


# 11. freshness
def test_freshness_rule(env):
    s, db = env
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7)}, winner="Nexus")
    db.set_round(rid, headline_variant="fun")
    add_post(db, "1101", 5 * H, round_id=rid)
    add_post(db, "1102", 5 * H, round_id=rid, key="k2")
    add_row(db, "1101", "1h", 2 * H, 500)                                     # late: not a 1 h reward
    add_row(db, "1102", "1h", 1.1 * H, 700)
    assert xstats.reward_inputs(db, "1101", "1h") is None
    assert xstats.snapshot(db, "1101", "1h")["impressions"] == 500            # still stored
    assert [r["tweet_id"] for r in xstats.rewards(db, "1h")] == ["1102"]
    assert xstats.reward_by_variant(db, "1h")["fun"]["n"] == 1
    r = xstats.reward_inputs(db, "1102", "1h")
    assert set(r) == set(xstats.REWARD_KEYS) and r["post_key"] == "k2" and r["round_id"] == rid
    s.x_reward_max_age_ratio = 2.5
    assert xstats.reward_inputs(db, "1101", "1h", s=s)["impressions"] == 500


# 12. series re-reads: own posts only
def test_series_rereads_only_own_posts(env):
    s, db = env
    fake = FakeX()
    add_post(db, "1201", 3 * H)
    add_row(db, "1201", "1h", 1.05 * H, 300)                                  # nothing due
    for tid, caller in (("1201", "metrics"), ("7777", "replies"), ("8888", "likes")):
        db.x("INSERT INTO x_reads VALUES(?,?,?,?)", TODAY, "post", tid, caller)
    db.put("x_account_day", TODAY)
    out = xstats.pull_due(s, db, poster(s, db, fake), now=NOW)
    assert fake.ids_requested() == ["1201"] and out["billed_new"] == 0
    assert [r["bucket"] for r in xstats.impressions_series(db, "1201")] == ["1h", None]
    s.x_series_free_rereads = False
    fake.calls.clear()
    xstats.pull_due(s, db, poster(s, db, fake), now=NOW + 600)
    assert fake.calls == []


# 13. ledger label for mentions
@pytest.mark.parametrize("owned,label", [(False, "post"), (True, "owned")])
def test_mentions_ledger_label(env, owned, label):
    s, db = env
    s.x_app_owned = owned
    poster(s, db, FakeX()).mentions(None)
    assert [r["resource"] for r in ledger(db, id="5001")] == [label]
    assert [(r["resource"], r["caller"]) for r in ledger(db, id="u1")] == [("user", "mentions")]


def test_engagement_numbers_stay_private(env):
    """Open question for the operator: until decided, no metric reaches the public export."""
    from agentpitbench import exports
    s, db = env
    rid = make_round(db, {"claude": ("CYBERSHOKE", 0.62, 0.7)}, winner="Nexus")
    add_post(db, "1301", 30 * H, round_id=rid)
    add_row(db, "1301", "24h", 24.1 * H, 98765, link_clicks=4321)
    db.x("UPDATE tweets SET metrics_json=? WHERE tweet_id='1301'", json.dumps({"impressions": 98765}))
    exports.export(s, db)
    for f in s.export_dir.rglob("*"):
        if f.is_file():
            body = f.read_text(errors="ignore")
            assert "98765" not in body and "impressions" not in body, f


def test_config_prices_partial_override(tmp_path, monkeypatch):
    from agentpitbench import config
    monkeypatch.setenv("BENCH_SECRETS_FILE", str(tmp_path / "none.env"))     # never the real secrets
    p = tmp_path / "bench.toml"
    p.write_text("x_app_owned = true\n[x_read_prices]\npost = 0.006\n")
    s = config.load(p)
    assert s.x_read_prices == {"post": 0.006, "user": 0.010, "owned": 0.001} and s.x_app_owned
    assert Settings().x_metrics_budget_usd == 1.0 and Settings().x_read_alert_usd == 5.0
