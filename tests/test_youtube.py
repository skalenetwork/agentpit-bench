"""YouTube Shorts (docs/youtube-shorts-spec.md §12): metadata, status fields, the resumable upload against a fake
server, idempotency, isolation, pause/dry-run, channel guard, publishing and comment, analytics parsing,
retention, void, quota ledger, OAuth login. Fixtures only: no network, no real Google calls."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from agentpitbench import cli, exports, motd, xstats, youtube, ytstats
from agentpitbench.config import Settings
from agentpitbench.db import DB

from test_publish import AGENTS, PICKS, make_round

DAY = "2026-10-12"
NOW = datetime(2026, 10, 12, 18, tzinfo=timezone.utc).timestamp()
SIZE = 10_000


# ---------------------------------------------------------------- fakes

class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""
        self.content = self.text.encode()

    def json(self):
        return self._body if self._body is not None else {}


class FakeGoogle:
    """A scripted YouTube: `put_script` is a list of responses for successive PUTs with bytes (or a callable);
    status queries (empty PUT) pop from `status_script`. Records every call."""

    def __init__(self, channel="UCabc", put_script=None, status_script=None, video_id="vid1"):
        self.channel, self.video_id = channel, video_id
        self.calls: list[tuple[str, str, dict]] = []
        self.put_script = list(put_script or [])
        self.status_script = list(status_script or [])
        self.sessions = 0
        self.videos_items: list[dict] = []
        self.on_put = None
        self.fail_comment = False

    def post(self, url, **kw):  # token endpoint
        self.calls.append(("POST", url, kw))
        return Resp(200, {"access_token": "fresh", "expires_in": 3600})

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url.startswith(youtube.UPLOAD_URL):
            self.sessions += 1
            return Resp(200, {}, {"Location": f"https://upload.example/session/{self.sessions}"})
        if url.startswith("https://upload.example/session/"):
            cr = kw["headers"]["Content-Range"]
            if cr.startswith("bytes */"):
                return self.status_script.pop(0)
            if self.on_put:
                self.on_put(kw)
            r = self.put_script.pop(0) if self.put_script else Resp(201, self._video())
            return r() if callable(r) else r
        if url.endswith("/channels"):
            return Resp(200, {"items": [{"id": self.channel, "snippet": {"title": "AgentpitBench"},
                                         "statistics": {"subscriberCount": "12", "viewCount": "340",
                                                        "videoCount": "3"}}]})
        if url.endswith("/videoCategories"):
            return Resp(200, {"items": [{"id": "28", "snippet": {"assignable": True}}]})
        if url.endswith("/videos") and method == "GET":
            return Resp(200, {"items": self.videos_items})
        if url.endswith("/videos") and method == "PUT":
            return Resp(200, {"id": kw["json"]["id"]})
        if url.endswith("/commentThreads"):
            if self.fail_comment:
                return Resp(403, {"error": {"errors": [{"reason": "forbidden"}]}})
            return Resp(200, {"id": "c1"})
        if url == youtube.ANALYTICS_URL:
            if "dimensions" in kw["params"]:
                return Resp(200, RETENTION)
            return Resp(200, TOTALS)
        raise AssertionError(f"unexpected {method} {url}")

    def _video(self, vid=None):
        return {"id": vid or self.video_id, "status": {"privacyStatus": "private", "uploadStatus": "uploaded"}}

    def n(self, pred) -> int:
        return sum(1 for c in self.calls if pred(c))

    @property
    def posts(self) -> int:
        return self.n(lambda c: c[1].startswith(youtube.UPLOAD_URL))

    @property
    def bytes_puts(self) -> list[str]:
        return [c[2]["headers"]["Content-Range"] for c in self.calls
                if c[1].startswith("https://upload.example") and not c[2]["headers"]["Content-Range"].startswith("bytes */")]


TOTALS = {"columnHeaders": [{"name": n} for n in ("views", "engagedViews", "estimatedMinutesWatched",
                                                   "averageViewDuration", "averageViewPercentage", "likes",
                                                   "comments", "shares", "subscribersGained")],
          "rows": [[1500, 900, 600.5, 24, 61.2, 40, 5, 7, 3]]}
RETENTION = {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"},
                               {"name": "relativeRetentionPerformance"}],
             "rows": [[0.01, 1.4, 0.5], [0.24, 0.9, 0.5], [0.51, 0.7, 0.4], [0.74, 0.5, 0.4], [1.0, 0.3, 0.3]]}


@pytest.fixture
def yt(tmp_path, monkeypatch):
    for k in ("X_CLIENT_ID", "X_CLIENT_SECRET", "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    tf = tmp_path / "yt_oauth2.json"
    monkeypatch.setenv("BENCH_YT_TOKEN_FILE", str(tf))
    monkeypatch.setenv("YT_CLIENT_ID", "cid")
    monkeypatch.setenv("YT_CLIENT_SECRET", "csecret")
    tf.write_text(json.dumps({"access_token": "tok", "refresh_token": "r1", "expires_at": time.time() + 3600,
                              "scope": " ".join(youtube.SCOPES), "created_at": time.time()}))
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=False, deploy_debounce_s=0, animate=False,
                 youtube_enabled=True, motd_enabled=True, youtube_channel_id="UCabc")
    db = DB(s.db_path)
    rid = make_round(db, PICKS, state="resolved", winner="Nexus")
    rec = film(s, db, rid)
    return s, db, rec


def film(s, db, rid, day=DAY, **extra):
    rnd = exports.round_record(s, db, db.round(rid))
    d = motd.film_dir(s, day)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"motd-{day}-tall.mp4").write_bytes(bytes(range(256)) * (SIZE // 256) + b"x" * (SIZE % 256))
    rec = {"date": day, "round_id": rid, "question": rnd["question"], "winner": rnd["winner"],
           "hook": "3 AIs said CYBERSHOKE. Grok said Nexus.",
           "score": {"total": 75, "signals": [{"key": "lone_wolf", "label": "Lone wolf won", "points": 40},
                                              {"key": "upset", "label": "Upset", "points": 35}]},
           "duration_s": 52.0, "files": {"tall": f"motd-{day}-tall.mp4", "wide": f"motd-{day}-wide.mp4"},
           "music": "theme.m4a", **extra}
    (d / "film.json").write_text(json.dumps(rec))
    return rec


def client(s, db, g):
    return youtube.Client(s, db, youtube.Token(http=g), http=g, sleep=lambda _x: None)


# ---------------------------------------------------------------- 1. metadata

def test_title_is_hook_bar_question_and_clips_question_first(yt):
    s, db, rec = yt
    t = youtube.title(rec)
    assert len(t) <= 100 and t.startswith(rec["hook"] + " | ")   # the hook survives: the question is clipped
    assert t.endswith("…")
    long = {**rec, "hook": "H" * 90, "question": "Q" * 200}
    t2 = youtube.title(long)
    assert len(t2) == 100 and len(t2.split(" | ")[1]) == 30 and t2.startswith("H" * 60)  # then the hook
    # exactly two parts, hook + " | " + question: no P&L, no outcome, no score
    assert youtube.title({**rec, "question": "Will it rain?"}) == rec["hook"] + " | Will it rain?"


def test_title_prefers_the_scripts_hook_line():
    base = {"hook": "old hook", "question": "Q?"}
    assert youtube.title({**base, "script": {"hook": "Scripted hook."}}) == "Scripted hook. | Q?"
    assert youtube.title({**base, "script": [{"kind": "hook", "text": "From scenes."}, {"kind": "x"}]}) \
        == "From scenes. | Q?"
    assert youtube.title({**base, "script": {"scenes": [{"hook": "Nested."}]}}) == "Nested. | Q?"
    assert youtube.title({**base, "script": {"concept": "heist"}}) == "old hook | Q?"   # no hook line: fallback
    assert youtube.title({**base, "script": "free text"}) == "old hook | Q?"


def test_angle_brackets_stripped(yt):
    s, db, rec = yt
    r = {**rec, "hook": "<b>Bold</b> move", "question": "Is 3 > 2 < 4?"}
    v = youtube.video_resource(s, r)
    for txt in (v["snippet"]["title"], v["snippet"]["description"]):
        assert "<" not in txt and ">" not in txt


def test_description_numbers_lines_and_limits(yt):
    s, db, rec = yt
    rnd = exports.round_record(s, db, db.round(rec["round_id"]))
    d = youtube.description(s, rec, rnd)
    lines = d.split("\n")
    assert lines[0] == rec["question"] and lines[1] == "Result: Nexus."
    for e in rnd["entries"]:
        if e["outcome"]:
            assert f"{e['outcome']} at {motd._cents(e['avg_price'])} → {motd._signed(e['pnl'])} tokens" in d
    assert "Grok (Grok Default): Nexus at 21¢" in d  # the model the run reported (grok-default), not config
    assert "Why this round: Lone wolf won +40, Upset +35" in d
    assert "EXHIBITION" not in d
    assert lines[-1] == "#AgentpitBench #AI #PredictionMarkets"
    assert len(d.encode()) <= 5000
    big = {**rec, "question": "Будет ли 東京 " * 400}
    assert len(youtube.description(s, big, rnd).encode()) <= 5000


def test_exhibition_line_only_for_exhibition(yt):
    s, db, rec = yt
    rnd = exports.round_record(s, db, db.round(rec["round_id"]))
    assert "EXHIBITION" not in youtube.description(s, rec, rnd)
    d = youtube.description(s, rec, {**rnd, "exhibition": True, "exclusion": "summon"})
    assert "EXHIBITION: summoned round, not counted in the season" in d


def test_only_host_is_the_site_and_utm_is_exact(yt):
    s, db, rec = yt
    rnd = exports.round_record(s, db, db.round(rec["round_id"]))
    assert "agentpit.dev" in rnd["market_link"]          # the export has the market link...
    text = youtube.metadata_text(s, rec, rnd)            # ...but no generated text does
    hosts = set(re.findall(r"https?://([^/\s?]+)", text)) | set(re.findall(r"\b([a-z0-9.-]+\.(?:org|dev|com))/", text))
    assert hosts == {"agentpitbench.org"}
    assert youtube.round_url_utm(s, rec) == (f"https://agentpitbench.org/round/{rec['round_id']}/?utm_source=youtube"
                                             f"&utm_medium=social&utm_campaign=motd&utm_content={DAY}")
    assert youtube.comment_text(s, rec).startswith(
        f"Every pick, price and transcript from this round: agentpitbench.org/round/{rec['round_id']}\n")


def test_tags_fixed_plus_category_within_500():
    t = youtube.tags({"question": "Will Bitcoin close above $120k?"})
    assert t[:8] == youtube.FIXED_TAGS and t[-1] == "Crypto"
    assert youtube.tags_len(t) <= 500
    assert youtube.tags_len(["a b", "c"]) == 3 + 2 + 1 + 1


# ---------------------------------------------------------------- 2. status fields

def test_status_fields_and_synthetic_override(yt):
    s, db, rec = yt
    v = youtube.video_resource(s, rec)
    assert v["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                           "containsSyntheticMedia": False, "embeddable": True, "publicStatsViewable": True}
    assert v["snippet"]["defaultLanguage"] == "en" and v["snippet"]["categoryId"] == "28"
    assert "defaultAudioLanguage" not in v["snippet"]
    s.youtube_privacy, s.youtube_synthetic_media = "public", True
    v = youtube.video_resource(s, {**rec, "script": {"hook": "h"}})
    assert v["status"]["privacyStatus"] == "public" and v["status"]["containsSyntheticMedia"] is True
    assert v["snippet"]["defaultAudioLanguage"] == "en"   # a narrated film declares its audio language


# ---------------------------------------------------------------- 3. resumable upload

def _up(s, db, rec, g):
    return youtube.upload(s, db, client(s, db, g), rec, motd.film_dir(s, DAY))


def test_upload_201_first_put_and_session_persisted_before_bytes(yt):
    s, db, rec = yt
    g = FakeGoogle()
    seen = []
    g.on_put = lambda kw: seen.append(youtube.row(db, "motd-" + DAY)["upload_session"])
    r = _up(s, db, rec, g)
    assert r["state"] == "uploaded" and r["video_id"] == "vid1" and r["upload_session"] is None
    assert seen == ["https://upload.example/session/1"]
    post = next(c for c in g.calls if c[1].startswith(youtube.UPLOAD_URL))
    assert post[2]["headers"]["X-Upload-Content-Length"] == str(SIZE)
    assert post[2]["params"]["uploadType"] == "resumable" and post[2]["json"]["status"]["selfDeclaredMadeForKids"] is False
    assert g.bytes_puts == [f"bytes 0-{SIZE - 1}/{SIZE}"]


def test_upload_503_then_308_range_resumes_from_5000(yt):
    s, db, rec = yt
    g = FakeGoogle(put_script=[Resp(503)], status_script=[Resp(308, headers={"Range": "bytes=0-4999"})])
    r = _up(s, db, rec, g)
    assert r["state"] == "uploaded" and g.posts == 1
    assert g.bytes_puts == [f"bytes 0-{SIZE - 1}/{SIZE}", f"bytes 5000-{SIZE - 1}/{SIZE}"]
    assert len(next(c for c in reversed(g.calls) if c[1].startswith("https://upload.example"))[2]["data"]) == SIZE - 5000


def test_upload_308_without_range_restarts_from_zero(yt):
    s, db, rec = yt
    g = FakeGoogle(put_script=[Resp(502)], status_script=[Resp(308)])
    r = _up(s, db, rec, g)
    assert r["state"] == "uploaded" and g.bytes_puts == [f"bytes 0-{SIZE - 1}/{SIZE}"] * 2


def test_upload_status_200_recovers_video_id_without_second_post(yt):
    s, db, rec = yt
    g = FakeGoogle(put_script=[Resp(500)], status_script=[Resp(200, {"id": "recovered"})])
    r = _up(s, db, rec, g)
    assert r["video_id"] == "recovered" and g.posts == 1 and len(g.bytes_puts) == 1


def test_upload_404_starts_one_new_session(yt):
    s, db, rec = yt
    g = FakeGoogle(put_script=[Resp(503)], status_script=[Resp(404)])
    r = _up(s, db, rec, g)
    assert r["state"] == "uploaded" and g.posts == 2


def test_upload_400_fails_without_retry(yt):
    s, db, rec = yt
    g = FakeGoogle(put_script=[Resp(400, {"error": {"errors": [{"reason": "invalidTitle"}]}})])
    r = _up(s, db, rec, g)
    assert r["state"] == "failed" and "invalidTitle" in r["error"] and g.posts == 1 and len(g.bytes_puts) == 1
    assert _up(s, db, rec, g)["state"] == "failed" and g.posts == 1      # no automatic retry


def test_upload_session_403_quota_fails(yt):
    s, db, rec = yt
    g = FakeGoogle()
    orig = g.request
    g.request = lambda m, u, **kw: (Resp(403, {"error": {"errors": [{"reason": "uploadLimitExceeded"}]}})
                                    if u.startswith(youtube.UPLOAD_URL) else orig(m, u, **kw))
    r = _up(s, db, rec, g)
    assert r["state"] == "failed" and "uploadLimitExceeded" in r["error"]
    assert int(db.get("yt_auth_failures", 0)) == 0          # a quota 403 isn't an auth failure


# ---------------------------------------------------------------- 4. idempotency

def test_two_cycles_and_a_restart_mid_upload_make_one_post(yt):
    s, db, rec = yt
    g = FakeGoogle(put_script=[Resp(503), Resp(503), Resp(503)],
                   status_script=[Resp(308, headers={"Range": "bytes=0-999"})] * 2 + [Resp(308, headers={"Range": "bytes=0-1999"})])
    out = ytstats.pull_due(s, db, client(s, db, g), now=NOW)
    assert out["uploaded"] == [{"motd-" + DAY: "uploading"}] and g.posts == 1
    assert youtube.row(db, "motd-" + DAY)["upload_session"] == "https://upload.example/session/1"
    # "restart": a fresh client and process state, same DB; the stored session is resumed
    g2 = FakeGoogle(status_script=[Resp(308, headers={"Range": "bytes=0-1999"})])
    g2.sessions = 1
    ytstats.pull_due(s, db, client(s, db, g2), now=NOW)
    assert g2.posts == 0 and g2.bytes_puts == [f"bytes 2000-{SIZE - 1}/{SIZE}"]
    assert youtube.row(db, "motd-" + DAY)["state"] == "uploaded"
    g3 = FakeGoogle()
    ytstats.pull_due(s, db, client(s, db, g3), now=NOW)
    assert g3.posts == 0


# ---------------------------------------------------------------- 5. isolation

async def test_task_off_unless_both_flags(yt):
    s, db, rec = yt
    assert cli.youtube_task(Settings(youtube_enabled=False, motd_enabled=True), db) is None
    assert cli.youtube_task(Settings(youtube_enabled=True, motd_enabled=False), db) is None
    assert cli.youtube_task(s, db) is not None


async def test_raising_cycle_is_contained_by_every(yt):
    import asyncio
    s, db, rec = yt

    class Boom:
        def __getattr__(self, k):
            raise RuntimeError("google down")
    ran = []

    async def other():
        ran.append(1)
    cyc = cli.youtube_task(s, db, client=Boom())
    t1 = asyncio.create_task(cli._every(0.01, cyc, "youtube"))
    t2 = asyncio.create_task(cli._every(0.01, other, "other"))
    await asyncio.sleep(0.1)
    t1.cancel(), t2.cancel()
    assert len(ran) >= 3
    assert db.get(f"motd_{DAY}") is None                    # YouTube never writes the film's X state


def test_x_failure_doesnt_stop_upload_and_no_film_no_calls(yt):
    s, db, rec = yt
    db.put(f"motd_{DAY}", "post-failed")
    g = FakeGoogle()
    ytstats.pull_due(s, db, client(s, db, g), now=NOW)
    assert youtube.row(db, "motd-" + DAY)["state"] == "uploaded"
    assert db.get(f"motd_{DAY}") == "post-failed"
    g2 = FakeGoogle()
    later = NOW + 3 * 86400                                  # film too old: no backfill
    s.youtube_comment = False
    db.x("DELETE FROM yt_videos")
    ytstats.pull_due(s, db, client(s, db, g2), now=later)
    assert g2.posts == 0


# ---------------------------------------------------------------- 6. pause and dry-run

def test_paused_film_is_never_uploaded_after_resume(yt):
    s, db, rec = yt
    s.posting_paused = True
    g = FakeGoogle()
    ytstats.pull_due(s, db, client(s, db, g), now=NOW)
    assert youtube.row(db, "motd-" + DAY)["state"] == "paused" and g.posts == 0
    s.posting_paused = False
    ytstats.pull_due(s, db, client(s, db, g), now=NOW)
    assert youtube.row(db, "motd-" + DAY)["state"] == "paused" and g.posts == 0


def test_paused_still_reads_metrics_but_no_comment(yt):
    s, db, rec = yt
    g = FakeGoogle()
    ytstats.pull_due(s, db, client(s, db, g), now=NOW)
    s.posting_paused = True
    g.videos_items = [{"id": "vid1", "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                       "statistics": {"viewCount": "10"}}]
    ytstats.pull_due(s, db, client(s, db, g), now=NOW + 60)
    ytstats.pull_due(s, db, client(s, db, g), now=NOW + 3700)
    assert ytstats.snapshot(db, "motd-" + DAY, "1h")["views"] == 10
    assert g.n(lambda c: c[1].endswith("/commentThreads")) == 0


def test_dry_run_writes_outbox_and_makes_no_calls(yt):
    s, db, rec = yt
    s.dry_run = True
    g = FakeGoogle()
    ytstats.pull_due(s, db, client(s, db, g), now=NOW)
    assert g.calls == []
    assert youtube.row(db, "motd-" + DAY)["state"] == "outbox"
    j = json.loads((s.outbox_dir / f"yt-motd-{DAY}.json").read_text())
    assert j["snippet"]["title"] == youtube.title(rec) and j["file"].endswith("-tall.mp4")


# ---------------------------------------------------------------- 7. channel guard

def test_channel_guard_refuses_mismatch(yt, caplog):
    s, db, rec = yt
    g = FakeGoogle(channel="UCpersonal")
    out = ytstats.pull_due(s, db, client(s, db, g), now=NOW)
    r = youtube.row(db, "motd-" + DAY)   # left resumable: a fixed config uploads on the next cycle
    assert g.posts == 0 and r["video_id"] is None and r["upload_session"] is None and out["uploaded"] == []
    assert "channel guard" in caplog.text
    s.youtube_channel_id = ""
    with pytest.raises(youtube.ChannelMismatch):
        _up(s, db, rec, FakeGoogle())


# ---------------------------------------------------------------- 8. publishing

def test_published_at_comment_once_and_buckets_from_publish(yt):
    s, db, rec = yt
    g = FakeGoogle()
    c = client(s, db, g)
    ytstats.pull_due(s, db, c, now=NOW)
    g.videos_items = [{"id": "vid1", "status": {"privacyStatus": "private", "uploadStatus": "processed"},
                       "statistics": {"viewCount": "0"}}]
    ytstats.pull_due(s, db, c, now=NOW + 7200)
    r = youtube.row(db, "motd-" + DAY)
    assert r["published_at"] is None and r["comment_id"] is None   # private: no comment, no buckets
    assert g.n(lambda c: c[1].endswith("/commentThreads")) == 0
    g.videos_items[0]["status"]["privacyStatus"] = "public"
    pub = NOW + 10_000
    ytstats.pull_due(s, db, c, now=pub)
    r = youtube.row(db, "motd-" + DAY)
    assert r["published_at"] == pub and r["comment_id"] == "c1"
    assert ytstats.snapshot(db, "motd-" + DAY, "1h") is None       # age from published_at, not upload
    ytstats.pull_due(s, db, c, now=pub + 3600)
    ytstats.pull_due(s, db, c, now=pub + 3700)
    assert ytstats.snapshot(db, "motd-" + DAY, "1h")["age_s"] == 3600
    assert g.n(lambda c: c[1].endswith("/commentThreads")) == 1
    body = next(c for c in g.calls if c[1].endswith("/commentThreads"))[2]["json"]
    assert body["snippet"]["videoId"] == "vid1"


def test_refused_comment_is_recorded_once_and_reads_continue(yt):
    s, db, rec = yt
    g = FakeGoogle()
    g.fail_comment = True
    c = client(s, db, g)
    ytstats.pull_due(s, db, c, now=NOW)
    g.videos_items = [{"id": "vid1", "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                       "statistics": {"viewCount": "5"}}]
    ytstats.pull_due(s, db, c, now=NOW + 60)
    ytstats.pull_due(s, db, c, now=NOW + 60 + 3600)
    assert g.n(lambda c: c[1].endswith("/commentThreads")) == 1
    assert youtube.row(db, "motd-" + DAY)["comment_id"] == "failed:forbidden"
    assert ytstats.snapshot(db, "motd-" + DAY, "1h")["views"] == 5


def test_soft_cap_never_blocks_the_upload_guard(yt):
    s, db, rec = yt
    s.youtube_units_budget = 0
    assert _up(s, db, rec, FakeGoogle())["state"] == "uploaded"


def test_locked_private_flag_after_30_min(yt):
    s, db, rec = yt
    s.youtube_privacy = "public"
    g = FakeGoogle()
    _up(s, db, rec, g)
    up = youtube.row(db, "motd-" + DAY)["uploaded_at"]
    assert ytstats.health(db, now=up + 600)["locked_private"] == []
    assert ytstats.health(db, now=up + 1900)["locked_private"] == ["motd-" + DAY]
    db.x("UPDATE yt_videos SET privacy='public'")
    assert ytstats.health(db, now=up + 1900)["locked_private"] == []


def test_rejected_upload_surfaces(yt):
    s, db, rec = yt
    g = FakeGoogle()
    c = client(s, db, g)
    ytstats.pull_due(s, db, c, now=NOW)
    g.videos_items = [{"id": "vid1", "status": {"privacyStatus": "private", "uploadStatus": "rejected",
                                                 "rejectionReason": "claim"}}]
    ytstats.pull_due(s, db, c, now=NOW + 600)
    assert youtube.row(db, "motd-" + DAY)["state"] == "rejected"
    assert ytstats.health(db)["rejected"] == [{"post_key": "motd-" + DAY, "error": "claim"}]


# ---------------------------------------------------------------- 9-10. analytics and retention

def test_analytics_parsing_and_video_retention_keys(yt):
    s, db, rec = yt
    assert ytstats.parse_totals(TOTALS)["engaged_views"] == 900
    assert ytstats.parse_totals({"rows": []}) == {}
    no_rel = {"columnHeaders": RETENTION["columnHeaders"][:2], "rows": [[0.5, 0.8]]}
    assert ytstats.parse_retention(no_rel) == [[0.5, 0.8, None]]
    g = FakeGoogle()
    c = client(s, db, g)
    ytstats.pull_due(s, db, c, now=NOW)
    db.x("UPDATE yt_videos SET published_at=?, privacy='public', upload_status='processed', comment_id='c1'", NOW)
    g.videos_items = [{"id": "vid1", "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                       "statistics": {"viewCount": "1500"}}]
    ytstats.pull_due(s, db, c, now=NOW + 7 * 86400 + 60)
    a = ytstats.snapshot(db, "motd-" + DAY, "7d", "analytics")
    assert a["engaged_views"] == 900 and a["avg_view_pct"] == 61.2 and a["data_through"]
    vr = ytstats.video_retention(db, "motd-" + DAY)
    assert set(vr) == {"plays", "p25", "p50", "p75", "p100", "video_views", "age_s", "taken_at"}
    assert (vr["p25"], vr["p50"], vr["p75"], vr["p100"]) == (0.9, 0.7, 0.5, 0.3)
    assert vr["plays"] == 1500 and vr["video_views"] == 900
    assert g.n(lambda c: c[1] == youtube.ANALYTICS_URL) == 2
    ytstats.pull_due(s, db, c, now=NOW + 7 * 86400 + 700)
    assert g.n(lambda c: c[1] == youtube.ANALYTICS_URL) == 2      # once per bucket
    rows = ytstats.by_film(db, NOW - 86400)
    assert rows[0]["post_key"] == "motd-" + DAY and rows[0]["yt_engaged_7d"] == 900
    assert ytstats.followers_series(db)[0]["subscribers"] == 12
    blk = ytstats.digest_block(s, db, NOW + 86400)
    assert blk["yesterday"]["state"] == "uploaded" and blk["warnings"] == []


# ---------------------------------------------------------------- 11. void

def test_void_hides_filmed_round_and_failure_never_raises(yt):
    s, db, rec = yt
    g = FakeGoogle()
    c = client(s, db, g)
    _up(s, db, rec, g)
    assert ytstats.hide_round(s, db, c, rec["round_id"]) is True
    upd = [x for x in g.calls if x[0] == "PUT" and x[1].endswith("/videos")]
    assert len(upd) == 1 and upd[0][2]["json"]["status"] == youtube.status_fields(s, "private")
    assert youtube.row(db, "motd-" + DAY)["state"] == "hidden"

    class Boom:
        def set_private(self, _):
            raise RuntimeError("x")
    assert ytstats.hide_round(s, db, Boom(), rec["round_id"]) is False
    assert ytstats.hide_round(s, db, c, 999) is False


# ---------------------------------------------------------------- 12. quota ledger

def test_quota_costs_pacific_day_and_soft_cap(yt):
    s, db, rec = yt
    # 06:30 UTC on Oct 12 is 23:30 PDT on Oct 11; 07:00 UTC is the Pacific midnight
    t = datetime(2026, 10, 12, 6, 30, tzinfo=timezone.utc).timestamp()
    assert youtube.day_pt(t) == "2026-10-11"
    youtube.charge(db, s, "commentThreads.insert", t)
    youtube.charge(db, s, "videos.insert", t)
    youtube.charge(db, s, "videos.list", t)
    assert ytstats.quota(db, "2026-10-11") == {"day_pt": "2026-10-11", "default": 51, "uploads": 1}
    s.youtube_units_budget = 3
    g = FakeGoogle()
    c = client(s, db, g)
    c.videos(["a"]), c.videos(["b"]), c.videos(["c"])
    with pytest.raises(youtube.QuotaSkip):
        c.videos(["d"])
    assert c.start_session({}, 1)                        # uploads are a separate bucket: never capped


# ---------------------------------------------------------------- OAuth and CLI

def test_login_roundtrip_and_refresh(tmp_path):
    g = FakeGoogle()
    tok = youtube.Token(tmp_path / "t.json", "cid", "sec", http=g)
    url = tok.authorize_url("http://127.0.0.1:8765")
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["offline"] and set(q["scope"][0].split()) == set(youtube.SCOPES)
    assert q["code_challenge_method"] == ["S256"]
    g.post = lambda u, **kw: (g.calls.append(("POST", u, kw)) or
                              Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                         "scope": " ".join(youtube.SCOPES)}))
    with pytest.raises(youtube.AuthError):
        tok.finish_login("http://127.0.0.1:8765/?state=wrong&code=c")
    scopes = tok.finish_login(f"http://127.0.0.1:8765/?state={q['state'][0]}&code=c")
    assert scopes == set(youtube.SCOPES)
    assert (tmp_path / "t.json").stat().st_mode & 0o777 == 0o600
    assert g.calls[-1][2]["data"]["code_verifier"]
    g.post = lambda u, **kw: Resp(200, {"access_token": "b", "expires_in": 3600})
    assert tok.get(stale="a") == "b" and tok._load()["refresh_token"] == "r"   # Google doesn't rotate it
    g.post = lambda u, **kw: Resp(400, {"error": "invalid_grant"})
    with pytest.raises(youtube.AuthError):
        tok.get(stale="b")


def test_loopback_redirect_is_captured(tmp_path):
    import socket
    import threading
    import urllib.request
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    got = {}
    th = threading.Thread(target=lambda: got.update(u=youtube.wait_for_redirect(port, 10)))
    th.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/?state=s&code=c", timeout=1).read()
            break
        except OSError:
            time.sleep(0.05)
    th.join(5)
    assert got["u"] == f"http://127.0.0.1:{port}/?state=s&code=c"


def test_youtube_check_and_meta_and_upload_cli(yt, capsys):
    s, db, rec = yt
    g = FakeGoogle()
    cli.youtube_check(s, client=client(s, db, g))
    out = capsys.readouterr().out
    assert "channel guard: OK" in out and "category 28: assignable" in out and "token refresh: OK" in out
    s.youtube_channel_id = ""
    cli.youtube_check(s, client=client(s, db, FakeGoogle()))
    assert 'youtube_channel_id = "UCabc"' in capsys.readouterr().out
    cli.youtube_meta(s, DAY)
    assert youtube.title(rec) in capsys.readouterr().out
    s.youtube_channel_id = "UCabc"
    cli.youtube_upload(s, DAY, client=client(s, db, g))
    assert json.loads(capsys.readouterr().out)["video_id"] == "vid1"
    with pytest.raises(SystemExit):
        cli.youtube_upload(s, DAY, client=client(s, db, g))


def test_cli_parses_youtube_subcommands(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "youtube_meta", lambda s, d: seen.append(("meta", d)))
    monkeypatch.setattr(cli, "youtube_auth", lambda s, u, p, n: seen.append(("auth", u, p, n)))
    cli.main(["--config", "/nonexistent.toml", "youtube", "meta", "--date", DAY])
    cli.main(["--config", "/nonexistent.toml", "youtube", "auth", "--no-wait", "--port", "9000"])
    assert seen == [("meta", DAY), ("auth", None, 9000, True)]


def test_youtube_defaults_off():
    s = Settings()
    assert s.youtube_enabled is False and s.youtube_privacy == "private" and s.youtube_synthetic_media is False


def test_xstats_digest_has_youtube_block_only_when_enabled(yt):
    s, db, rec = yt
    assert "youtube" in xstats.digest(s, db, NOW)
    s.youtube_enabled = False
    assert "youtube" not in xstats.digest(s, db, NOW)
