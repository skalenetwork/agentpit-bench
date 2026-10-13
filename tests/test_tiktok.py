"""TikTok drafts (docs/tiktok-spec.md): caption and disclosure builders, the inbox upload against a fake TikTok,
idempotency, schedule (18:00 UTC), pause/dry-run/stop, account guard, auto-stop (stop codes, removal, For You),
draft -> published matching, metrics, OAuth login, CLI. Fixtures only: no network, no real TikTok calls."""
import json
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from agentpitbench import alerts, cli, tiktok, ttstats, xstats
from agentpitbench.config import Settings
from agentpitbench.db import DB

from test_publish import AGENTS, PICKS, make_round
from test_youtube import DAY, SIZE, film

AT_18 = datetime(2026, 10, 12, 18, 5, tzinfo=timezone.utc).timestamp()
AT_17 = datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc).timestamp()


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body
        self.text = json.dumps(body) if body is not None else ""
        self.content = self.text.encode()
        self.headers = {}

    def json(self):
        return self._body if self._body is not None else {}


def ok(data):
    return Resp(200, {"data": data, "error": {"code": "ok", "message": "", "log_id": "l"}})


def err(status, code):
    return Resp(status, {"data": {}, "error": {"code": code, "message": code, "log_id": "l"}})


class FakeTikTok:
    """A scripted TikTok. `init_script` pops responses for init calls; `status_data`, `videos` and `listed` feed
    status/fetch, video/query and video/list. Records every call."""

    def __init__(self, open_id="oid1"):
        self.open_id = open_id
        self.calls: list[tuple[str, str, dict]] = []
        self.init_script: list = []
        self.status_data = {"status": "SEND_TO_USER_INBOX"}
        self.videos: list[dict] = []
        self.listed: list[dict] = []
        self.put_status = 201

    def post(self, url, **kw):   # token endpoint
        self.calls.append(("POST", url, kw))
        return Resp(200, {"access_token": "fresh", "expires_in": 86400, "refresh_token": "r2",
                          "refresh_expires_in": 31536000, "open_id": self.open_id,
                          "scope": ",".join(tiktok.SCOPES), "token_type": "Bearer"})

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url.endswith(tiktok.INBOX_INIT) or url.endswith("/" + tiktok.DIRECT_INIT):
            if self.init_script:
                r = self.init_script.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            return ok({"publish_id": "v_inbox_1", "upload_url": "https://upload.tiktok.example/u/1"})
        if url.startswith("https://upload.tiktok.example/"):
            return Resp(self.put_status, {})
        if url.endswith(tiktok.STATUS_FETCH):
            return ok(self.status_data)
        if url.endswith(tiktok.USER_INFO):
            return ok({"user": {"open_id": self.open_id, "display_name": "AgentpitBench"}})
        if url.endswith(tiktok.CREATOR_INFO):
            return ok({"creator_username": "agentpitbench", "max_video_post_duration_sec": 600,
                       "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]})
        if url.endswith(tiktok.VIDEO_QUERY):
            ids = set(kw["json"]["filters"]["video_ids"])
            return ok({"videos": [v for v in self.videos if v["id"] in ids]})
        if url.endswith(tiktok.VIDEO_LIST):
            return ok({"videos": self.listed, "has_more": False})
        raise AssertionError(f"unexpected {method} {url}")

    def n(self, suffix) -> int:
        return sum(1 for c in self.calls if c[1].endswith(suffix))


@pytest.fixture
def tt(tmp_path, monkeypatch):
    for k in ("X_CLIENT_ID", "X_CLIENT_SECRET", "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    tf = tmp_path / "tt_oauth2.json"
    monkeypatch.setenv("BENCH_TT_TOKEN_FILE", str(tf))
    monkeypatch.setenv("TT_CLIENT_KEY", "ckey")
    monkeypatch.setenv("TT_CLIENT_SECRET", "csecret")
    tf.write_text(json.dumps({"access_token": "tok", "refresh_token": "r1", "expires_at": time.time() + 3600,
                              "refresh_expires_at": time.time() + 300 * 86400, "open_id": "oid1",
                              "scope": ",".join(tiktok.SCOPES), "created_at": time.time()}))
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=False, deploy_debounce_s=0, animate=False,
                 tiktok_enabled=True, motd_enabled=True, tiktok_open_id="oid1")
    db = DB(s.db_path)
    rid = make_round(db, PICKS, state="resolved", winner="Nexus")
    rec = film(s, db, rid)
    return s, db, rec


def client(s, db, g):
    return tiktok.Client(s, db, tiktok.Token(http=g), http=g, sleep=lambda _x: None)


def _d(s):
    from agentpitbench import motd
    return motd.film_dir(s, DAY)


# ---------------------------------------------------------------- caption and disclosure (pure)

def test_caption_has_full_market_name_result_site_and_hashtags(tt):
    s, db, rec = tt
    c = tiktok.caption(s, rec)
    lines = c.split("\n")
    assert lines[0] == rec["hook"] and lines[1] == rec["question"]
    assert "Result: Nexus." in c and f"agentpitbench.org/round/{rec['round_id']}" in c
    assert "http" not in c and "agentpit." not in c.replace("agentpitbench.org", "")
    assert lines[-1].startswith("#AI #ChatGPT #Claude #Gemini #Grok") and "#Esports" in lines[-1]
    assert "AI-voiced" not in c


def test_caption_long_market_name_kept_hook_clipped_within_limit(tt):
    s, _, rec = tt
    q = "Will " + "very long market name " * 60 + "happen?"
    c = tiktok.caption(s, {**rec, "question": q, "hook": "H" * 3000})
    assert len(c) <= tiktok.CAPTION_MAX and tiktok._clean(q) in c


def test_no_betting_cta_ever(tt):
    s, _, rec = tt
    c = tiktok.caption(s, {**rec, "hook": "Tail Grok or fade Claude? Bet now!"})
    assert not tiktok.CTA_RE.search(c) and "Bet now" not in c
    for r in (rec, {**rec, "narrated": True}, {**rec, "winner": None}):
        assert not tiktok.CTA_RE.search(tiktok.caption(s, r))


def test_post_info_and_ai_label_follow_operator_decisions(tt):
    s, _, rec = tt
    p = tiktok.post_info(s, rec)
    assert p["disable_comment"] is False and p["disable_duet"] is True and p["disable_stitch"] is True
    assert p["brand_content_toggle"] is False and p["is_aigc"] is False
    n = {**rec, "narrated": True}
    assert tiktok.post_info(s, n)["is_aigc"] is True and "AI-voiced." in tiktok.caption(s, n)
    note = tiktok.draft_note(s, n)
    assert "AI-generated content label: ON" in note and "duet OFF" in note and "comments ON" in note


# ---------------------------------------------------------------- the inbox upload

def test_inbox_upload_single_chunk_publish_id_and_alert(tt):
    s, db, rec = tt
    g = FakeTikTok()
    r = tiktok.upload(s, db, client(s, db, g), rec, _d(s))
    assert r["state"] == "sent" and r["publish_id"] == "v_inbox_1" and r["mode"] == "inbox"
    init = next(c for c in g.calls if c[1].endswith(tiktok.INBOX_INIT))
    assert init[2]["json"] == {"source_info": {"source": "FILE_UPLOAD", "video_size": SIZE, "chunk_size": SIZE,
                                               "total_chunk_count": 1}}
    put = next(c for c in g.calls if c[0] == "PUT")
    assert put[2]["headers"]["Content-Range"] == f"bytes 0-{SIZE - 1}/{SIZE}" and "Authorization" not in put[2]["headers"]
    a = alerts.recent(db)[0]
    assert a["kind"] == "tiktok-draft" and rec["question"] in a["body"]
    tiktok.upload(s, db, client(s, db, g), rec, _d(s), manual=True)
    assert g.n(tiktok.INBOX_INIT) == 1                       # idempotent: never a second draft


def test_schedule_18_utc_and_isolated_from_x(tt):
    s, db, rec = tt
    g = FakeTikTok()
    assert ttstats.pull_due(s, db, client(s, db, g), AT_17)["uploaded"] == []
    assert g.calls == []
    out = ttstats.pull_due(s, db, client(s, db, g), AT_18)
    assert out["uploaded"] == [{f"tt-{DAY}": "sent"}]
    ttstats.pull_due(s, db, client(s, db, g), AT_18 + 600)
    assert g.n(tiktok.INBOX_INIT) == 1


def test_pause_dry_run_and_stop_touch_no_network(tt):
    s, db, rec = tt
    g = FakeTikTok()
    s.posting_paused = True
    assert tiktok.upload(s, db, client(s, db, g), rec, _d(s))["state"] == "paused"
    assert tiktok.upload(s, db, client(s, db, g), rec, _d(s))["state"] == "paused"   # not retried by the cycle
    s.posting_paused, s.dry_run = False, True
    assert tiktok.upload(s, db, None, rec, _d(s), manual=True)["state"] == "outbox"
    assert json.loads((s.outbox_dir / f"tt-tt-{DAY}.json").read_text())["caption"] == tiktok.caption(s, rec)
    s.dry_run = False
    tiktok.kill(s, db, "test", manual=True)
    assert tiktok.upload(s, db, client(s, db, g), rec, _d(s), manual=True)["state"] == "stopped"
    assert ttstats.pull_due(s, db, client(s, db, g), AT_18)["uploaded"] == []
    assert g.n(tiktok.INBOX_INIT) == 0


def test_account_guard_refuses_other_account(tt, caplog):
    s, db, rec = tt
    g = FakeTikTok(open_id="someone-else")
    c = client(s, db, g)
    c.token.path.write_text(json.dumps({**json.loads(c.token.path.read_text()), "open_id": "someone-else"}))
    ttstats.pull_due(s, db, c, AT_18)
    assert g.n(tiktok.INBOX_INIT) == 0 and "account guard" in caplog.text
    s.tiktok_open_id = ""
    with pytest.raises(tiktok.AccountMismatch):
        tiktok.upload(s, db, c, rec, _d(s), manual=True)


def test_stop_code_on_init_stops_tiktok_and_alerts(tt):
    s, db, rec = tt
    g = FakeTikTok()
    g.init_script = [err(403, "spam_risk_user_banned_from_posting")]
    r = tiktok.upload(s, db, client(s, db, g), rec, _d(s))
    assert r["state"] == "failed" and tiktok.killed(db)["reason"].startswith("spam_risk_user_banned")
    assert {a["kind"] for a in alerts.recent(db)} >= {"tiktok"}


def test_transient_init_error_retries_then_400_fails_without_stop(tt):
    s, db, rec = tt
    g = FakeTikTok()
    g.init_script = [err(503, "internal_error"), ok({"publish_id": "p2", "upload_url": "https://upload.tiktok.example/2"})]
    assert tiktok.upload(s, db, client(s, db, g), rec, _d(s))["publish_id"] == "p2"
    s2, db2, rec2 = s, DB(s.db_path), rec
    db2.x("DELETE FROM tt_videos")
    g2 = FakeTikTok()
    g2.init_script = [err(400, "invalid_params")]
    assert tiktok.upload(s2, db2, client(s2, db2, g2), rec2, _d(s2))["state"] == "failed"
    assert not tiktok.killed(db2) and g2.n(tiktok.INBOX_INIT) == 1


def test_direct_mode_checks_creator_info_and_sends_post_info(tt):
    s, db, rec = tt
    s.tiktok_mode = "direct"
    g = FakeTikTok()
    tiktok.upload(s, db, client(s, db, g), rec, _d(s))
    init = next(c for c in g.calls if c[1].endswith("/" + tiktok.DIRECT_INIT))
    pi = init[2]["json"]["post_info"]
    assert pi["privacy_level"] == "PUBLIC_TO_EVERYONE" and pi["disable_duet"] is True
    assert g.n(tiktok.CREATOR_INFO) == 1
    db.x("DELETE FROM tt_videos")
    s.tiktok_privacy = "FOLLOWER_OF_CREATOR"
    assert tiktok.upload(s, db, client(s, db, FakeTikTok()), rec, _d(s))["state"] == "failed"


# ---------------------------------------------------------------- after the upload

def test_draft_published_is_matched_and_metrics_bucketed(tt):
    s, db, rec = tt
    g = FakeTikTok()
    c = client(s, db, g)
    ttstats.pull_due(s, db, c, AT_18)
    up = tiktok.row(db, f"tt-{DAY}")["uploaded_at"]
    g.listed = [{"id": "999", "title": "something else", "create_time": int(up) + 100},
                {"id": "7001", "title": tiktok.caption(s, rec), "create_time": int(up) + 300,
                 "share_url": "https://www.tiktok.com/@agentpitbench/video/7001"}]
    out = ttstats.pull_due(s, db, c, AT_18 + 600)
    r = tiktok.row(db, f"tt-{DAY}")
    assert out["matched"] == 1 and r["state"] == "published" and r["video_id"] == "7001"
    g.videos = [{"id": "7001", "view_count": 1200, "like_count": 50, "comment_count": 4, "share_count": 9}]
    ttstats.pull_due(s, db, c, r["published_at"] + 3700)
    assert ttstats.snapshot(db, f"tt-{DAY}", "1h")["views"] == 1200
    ttstats.pull_due(s, db, c, r["published_at"] + 3800)
    assert db.one("SELECT COUNT(*) n FROM tt_metrics")["n"] == 1
    assert ttstats.by_film(db, AT_18 - 86400)[0]["tt_views_24h"] is None


def test_status_publish_complete_and_failed_stop_reason(tt):
    s, db, rec = tt
    g = FakeTikTok()
    c = client(s, db, g)
    tiktok.upload(s, db, c, rec, _d(s))
    g.status_data = {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7002]}
    ttstats.pull_due(s, db, c, AT_18)
    assert tiktok.row(db, f"tt-{DAY}")["video_id"] == "7002"
    db.x("UPDATE tt_videos SET state='sent', video_id=NULL")
    g.status_data = {"status": "FAILED", "fail_reason": "spam_risk_user_banned_from_posting"}
    ttstats.pull_due(s, db, c, AT_18)
    assert tiktok.row(db, f"tt-{DAY}")["state"] == "failed" and tiktok.killed(db)


def test_removed_video_stops_tiktok(tt):
    s, db, rec = tt
    g = FakeTikTok()
    c = client(s, db, g)
    tiktok.upload(s, db, c, rec, _d(s))
    db.x("UPDATE tt_videos SET state='published', video_id='7003', published_at=?", AT_18)
    g.videos = []
    out = ttstats.pull_due(s, db, c, AT_18 + 3700)
    assert out["removed"] == 1 and tiktok.row(db, f"tt-{DAY}")["state"] == "removed"
    assert "removed" in tiktok.killed(db)["reason"]


def test_foryou_check_stops_after_five_low_videos(tt):
    s, db, rec = tt
    for i in range(5):
        db.x("INSERT INTO tt_videos(post_key, state, video_id, published_at) VALUES(?,?,?,?)",
             f"tt-2026-10-0{i + 1}", "published", f"v{i}", AT_18 + i)
    for i in range(4):
        assert ttstats.record_foryou(s, db, f"tt-2026-10-0{i + 1}", 3)["verdict"] == "pending"
    assert not tiktok.killed(db)
    assert ttstats.record_foryou(s, db, "tt-2026-10-05", 20)["verdict"] == "restricted"
    assert "For You" in tiktok.killed(db)["reason"]
    tiktok.resume(db)
    assert ttstats.record_foryou(s, db, "tt-2026-10-05", 40)["verdict"] == "ok" and not tiktok.killed(db)


def test_void_round_alerts_operator(tt):
    s, db, rec = tt
    tiktok.upload(s, db, client(s, db, FakeTikTok()), rec, _d(s))
    assert ttstats.void_round(s, db, rec["round_id"]) is True
    assert alerts.recent(db)[0]["subject"].startswith("Voided round on TikTok")
    assert ttstats.void_round(s, db, 99999) is False


# ---------------------------------------------------------------- OAuth and CLI

def test_login_roundtrip_and_rotating_refresh(tmp_path):
    g = FakeTikTok()
    tok = tiktok.Token(tmp_path / "t.json", "ckey", "sec", http=g)
    url = tok.authorize_url("https://agentpitbench.org/tiktok/callback/")
    q = parse_qs(urlparse(url).query)
    assert url.startswith(tiktok.AUTH_URL) and q["client_key"] == ["ckey"]
    assert q["scope"] == [",".join(tiktok.SCOPES)] and q["response_type"] == ["code"]
    with pytest.raises(tiktok.AuthError):
        tok.finish_login("https://agentpitbench.org/tiktok/callback/?state=wrong&code=c")
    scopes = tok.finish_login(f"https://agentpitbench.org/tiktok/callback/?code=c&state={q['state'][0]}")
    assert scopes == set(tiktok.SCOPES) and tok.open_id() == "oid1"
    assert (tmp_path / "t.json").stat().st_mode & 0o777 == 0o600
    sent = g.calls[-1][2]["data"]
    assert sent["grant_type"] == "authorization_code" and sent["client_key"] == "ckey"
    assert tok.get(stale="fresh") == "fresh" and tok._load()["refresh_token"] == "r2"   # rotated token kept
    g.post = lambda u, **kw: Resp(400, {"error": "invalid_grant", "error_description": "expired"})
    with pytest.raises(tiktok.AuthError):
        tok.get(stale="fresh")


def test_tiktok_check_meta_upload_cli(tt, capsys):
    s, db, rec = tt
    g = FakeTikTok()
    cli.tiktok_check(s, client=client(s, db, g))
    out = capsys.readouterr().out
    assert "account guard: OK" in out and "token refresh: OK" in out and "missing: none" in out
    s.tiktok_open_id = ""
    cli.tiktok_check(s, client=client(s, db, FakeTikTok()))
    assert 'tiktok_open_id = "oid1"' in capsys.readouterr().out
    cli.tiktok_meta(s, DAY)
    assert rec["question"] in capsys.readouterr().out
    s.tiktok_open_id = "oid1"
    cli.tiktok_upload(s, DAY, client=client(s, db, g))
    assert json.loads(capsys.readouterr().out)["publish_id"] == "v_inbox_1"
    with pytest.raises(SystemExit):
        cli.tiktok_upload(s, DAY, client=client(s, db, g))


def test_cli_parses_tiktok_subcommands(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "tiktok_meta", lambda s, d: seen.append(("meta", d)))
    monkeypatch.setattr(cli, "tiktok_auth", lambda s, u, d: seen.append(("auth", u, d)))
    monkeypatch.setattr(cli, "tiktok_switch", lambda s, r: seen.append(("switch", r)))
    monkeypatch.setattr(cli, "tiktok_foryou", lambda s, d, p: seen.append(("fyp", d, p)))
    for argv in (["meta", "--date", DAY], ["auth"], ["off", "strike"], ["on"], ["foryou", "--date", DAY, "--pct", "4"]):
        cli.main(["--config", "/nonexistent.toml", "tiktok", *argv])
    assert seen == [("meta", DAY), ("auth", None, False), ("switch", "strike"), ("switch", None),
                    ("fyp", DAY, 4.0)]


def test_tiktok_defaults_off_and_operator_settings():
    s = Settings()
    assert s.tiktok_enabled is False and s.tiktok_mode == "inbox" and s.tiktok_post_hour_utc == 18
    assert s.tiktok_comments is True and s.tiktok_duet is False and s.tiktok_stitch is False
    assert s.tiktok_handle == "agentpitbench"
    assert cli.tiktok_task(s, None) is None
    assert cli.tiktok_redirect(s) == "https://agentpitbench.org/tiktok/callback/"


def test_digest_has_tiktok_block_only_when_enabled(tt):
    s, db, rec = tt
    assert "tiktok" in xstats.digest(s, db, AT_18)
    s.tiktok_enabled = False
    assert "tiktok" not in xstats.digest(s, db, AT_18)
