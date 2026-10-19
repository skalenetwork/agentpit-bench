"""Threads mirror (docs/threads-spec.md): text, image, carousel, video polling, reply chain, idempotency, gating,
token refresh, failure isolation. No real calls: a fake HTTP object answers the Graph API."""
import asyncio
import json
import time

import pytest

from agentpitbench import threads
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.threads import Mirror, Token, build_text
from agentpitbench.twitter import Poster


class Resp:
    def __init__(self, status=200, j=None):
        self.status_code, self._j = status, j or {}
        self.text = json.dumps(self._j)
        self.content = self.text.encode()

    def json(self):
        return self._j


class FakeHTTP:
    def __init__(self, video_states=("FINISHED",), fail_publish=False, deployed=True, fail_media=False):
        self.calls, self.published, self.containers, self.n = [], [], {}, 0
        self.video_states, self.fail_publish, self.deployed, self.fail_media = list(video_states), fail_publish, deployed, fail_media
        self.polls = 0

    def head(self, url, **kw):
        self.calls.append(("HEAD", url, kw))
        return Resp(200 if self.deployed else 404)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        d = kw["data"]
        assert d["access_token"]
        if url.endswith("/threads_publish"):
            if self.fail_publish:
                return Resp(400, {"error": {"message": "nope"}})
            self.n += 1
            self.published.append({**self.containers[d["creation_id"]], "id": f"t{self.n}"})
            return Resp(200, {"id": f"t{self.n}"})
        if url.endswith("/threads"):
            if self.fail_media and d["media_type"] in ("IMAGE", "VIDEO") and not d.get("is_carousel_item"):
                return Resp(400, {"error": {"message": "bad media"}})
            cid = f"c{len(self.containers) + 1}"
            self.containers[cid] = {k: v for k, v in d.items() if k != "access_token"}
            return Resp(200, {"id": cid})
        raise AssertionError(url)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if url.endswith("refresh_access_token"):
            return Resp(200, {"access_token": "NEW", "expires_in": 5184000})
        cid = url.rsplit("/", 1)[-1]
        self.polls += 1
        if self.containers[cid].get("media_type") == "VIDEO" and self.video_states:
            st = self.video_states.pop(0)
            return Resp(200, {"status": st, "error_message": "bad codec" if st == "ERROR" else None})
        return Resp(200, {"status": "FINISHED"})

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return Resp(200, {"success": True})


def run(c):
    return asyncio.run(c)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_REFRESH_TOKEN", "BSKY_HANDLE", "BSKY_APP_PASSWORD", "THREADS_USER_ID", "THREADS_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    monkeypatch.setenv("BENCH_THREADS_TOKEN_FILE", str(tmp_path / "threads_oauth2.json"))
    monkeypatch.setattr(threads.time, "sleep", lambda s: None)

    async def no_wait(s):
        return None
    monkeypatch.setattr(threads.asyncio, "sleep", no_wait)
    s = Settings(data_dir=tmp_path / "var", dry_run=True, threads_enabled=True, animate=False)
    return s, DB(s.db_path)


def live(env, monkeypatch, **fake_kw):
    """X stays in its outbox (dry ids); Threads goes live against the fake. A fresh token file avoids a refresh."""
    s, db = env
    monkeypatch.setenv("THREADS_USER_ID", "42")
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "tok")
    p = Poster(s, db)
    s.dry_run = False
    fake = FakeHTTP(**fake_kw)
    p.threads.http = fake
    (s.data_dir.parent / "threads_oauth2.json").write_text(json.dumps(
        {"access_token": "tok", "seed": "tok", "expires_at": time.time() + 40 * 86400, "tried_at": 0}))
    assert p.threads.live and not p.live
    return p, fake


def card(s, name):
    f = s.cards_dir / "1" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\0" * 10)
    return f


# ---------- text ----------

def test_build_text():
    s = Settings()
    t = build_text(s, "Resolved. #AgentpitBench #Sports @AnthropicAI\nhttps://agentpitbench.org/round/7/?utm_source=x&utm_medium=social",
                   "results", 7)
    assert "@AnthropicAI" not in t and "Anthropic" in t
    assert "utm_source=threads" in t and t.count("#") == 1 and "#AgentpitBench" in t and "Sports" in t
    assert "https://agentpitbench.org/round/7/" in t   # full URL, not a display form
    reveal = build_text(s, "Picks are in.", "reveal", 7)
    assert reveal.endswith("/round/7/?utm_source=threads&utm_medium=social&utm_campaign=reveal")


def test_build_text_trims_to_500_keeping_last_line():
    s = Settings()
    t = build_text(s, "word " * 200 + "\nhttps://agentpitbench.org/round/7/", "results", 7)
    assert len(t) <= 500 and t.endswith("https://agentpitbench.org/round/7/") and "…" in t


# ---------- gating ----------

def test_disabled_is_a_noop(env, monkeypatch):
    s, db = env
    s.threads_enabled = False
    p = Poster(s, db)
    p.threads.http = None
    assert run(p.post("r1-reveal", "reveal", "Picks", round_id=1)) == "dry-r1-reveal"
    assert db.q("SELECT * FROM threads_posts") == [] and not (s.outbox_dir / "threads").exists()


def test_missing_creds_is_one_info_log(env, caplog):
    s, db = env
    s.dry_run = False
    m = Mirror(s, db, http=None)
    with caplog.at_level("INFO", logger="agentpitbench.threads"):
        assert run(m.post("k1", "reveal", "Picks", round_id=1)) is None
        assert run(m.post("k2", "reveal", "Picks", round_id=1)) is None
    assert sum("THREADS_USER_ID" in r.message for r in caplog.records) == 1
    assert db.q("SELECT * FROM threads_posts") == []


def test_dry_run_writes_outbox(env):
    s, db = env
    p = Poster(s, db)
    p.threads.http = None
    run(p.post("r1-reveal", "reveal", "Picks", round_id=1))
    assert json.loads((s.outbox_dir / "threads" / "r1-reveal.json").read_text())["media_id"] == "dry-r1-reveal"
    assert db.one("SELECT media_id FROM threads_posts WHERE post_key='r1-reveal'")["media_id"] == "dry-r1-reveal"


def test_non_mirror_kind_is_skipped(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    assert run(p.threads.post("x", "reply-back", "hi")) is None and fake.published == []


# ---------- live (fake) ----------

def test_text_post(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    mid = run(p.post("r1-motd", "motd", "Match of the day #AgentpitBench", round_id=1))
    assert mid == "dry-r1-motd"   # the X id
    assert fake.published == [{"media_type": "TEXT", "text": "Match of the day #AgentpitBench", "id": "t1"}]
    assert p.db.one("SELECT media_id FROM threads_posts")["media_id"] == "t1"


def test_image_and_carousel(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    s = p.s
    run(p.post("r1-milestone", "milestone", "Streak", card(s, "results.png"), round_id=1))
    run(p.post("r1-reveal", "reveal", "Picks are in.", [card(s, "decision-claude.png"), card(s, "decision-grok.png")],
               round_id=1))
    one, car = fake.published
    assert one["media_type"] == "IMAGE" and one["image_url"] == "https://agentpitbench.org/cards/1/results.png"
    assert car["media_type"] == "CAROUSEL" and car["children"] == "c2,c3" and "round/1" in car["text"]
    assert fake.containers["c2"]["is_carousel_item"] == "true"


def test_undeployed_media_falls_back_to_text_and_link(env, monkeypatch):
    p, fake = live(env, monkeypatch, deployed=False)
    run(p.post("r1-milestone", "milestone", "Streak", card(p.s, "results.png"), round_id=1))
    (post,) = fake.published
    assert post["media_type"] == "TEXT" and post["text"].endswith("/cards/1/results.png")
    outside = p.s.data_dir / "elsewhere.png"
    outside.write_bytes(b"\0")
    run(p.post("r1-milestone2", "milestone", "Streak", outside, round_id=1))
    assert fake.published[1]["media_type"] == "TEXT" and "elsewhere" not in fake.published[1]["text"]


def test_video_polls_until_finished(env, monkeypatch):
    p, fake = live(env, monkeypatch, video_states=("IN_PROGRESS", "IN_PROGRESS", "FINISHED"))
    run(p.post("r1-results", "results", "Resolved", card(p.s, "results.mp4"), round_id=1))
    (post,) = fake.published
    assert post["media_type"] == "VIDEO" and post["video_url"].endswith("/cards/1/results.mp4")
    assert fake.polls == 3
    pub = [i for i, c in enumerate(fake.calls) if c[1].endswith("threads_publish")][0]
    assert sum(c[0] == "GET" for c in fake.calls[:pub]) == 3   # all polling happens before the publish


def test_video_error_falls_back_to_still_then_text(env, monkeypatch):
    p, fake = live(env, monkeypatch, video_states=("ERROR",))
    run(p.post("r1-results", "results", "Resolved", card(p.s, "results.mp4"), round_id=1,
               bsky_fallback=card(p.s, "results.png")))
    assert fake.published[0]["media_type"] == "IMAGE" and fake.published[0]["image_url"].endswith("results.png")
    p2, fake2 = live(env, monkeypatch, video_states=("ERROR",))
    run(p2.post("r2-results", "results", "Resolved", card(p2.s, "r.mp4"), round_id=2))
    assert fake2.published[0]["media_type"] == "TEXT"


def test_reply_chain_and_quote(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    reveal = run(p.post("r1-reveal", "reveal", "Picks are in.", round_id=1))
    results = run(p.post("r1-results", "results", "Resolved", round_id=1, quote_of=reveal))
    press1 = run(p.post("r1-press-1", "press", "Grok: again.", reply_to=results, round_id=1))
    run(p.post("r1-press-2", "press", "Gemini: noted.", reply_to=press1, round_id=1))
    reveal_p, results_p, p1, p2 = fake.published
    assert "quote_post_id" not in reveal_p and results_p["quote_post_id"] == "t1"
    assert p1["reply_to_id"] == "t2" and p2["reply_to_id"] == "t3"
    rows = {r["post_key"]: r["root_id"] for r in p.db.q("SELECT * FROM threads_posts")}
    assert rows["r1-press-2"] == "t2" and rows["r1-reveal"] == "t1"


def test_idempotent_per_post_key(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    run(p.post("r1-reveal", "reveal", "Picks", round_id=1))
    run(p.post("r1-reveal", "reveal", "Picks", round_id=1))
    assert len(fake.published) == 1


def test_reply_without_mirrored_parent_is_skipped(env, monkeypatch):
    p, fake = live(env, monkeypatch, fail_publish=True)
    results = run(p.post("r1-results", "results", "Resolved", round_id=1))   # X ok, Threads rejected
    fake.fail_publish = False
    run(p.post("r1-press-1", "press", "Grok: again.", reply_to=results, round_id=1))
    assert fake.published == [] and p.db.q("SELECT * FROM threads_posts") == []


def test_failure_never_breaks_x_or_bluesky(env, monkeypatch):
    p, fake = live(env, monkeypatch, fail_publish=True)

    def boom(*a, **k):
        raise RuntimeError("network down")
    fake.post = boom
    assert run(p.post("r1-reveal", "reveal", "Picks", round_id=1)) == "dry-r1-reveal"   # X post still returned
    assert p.db.one("SELECT tweet_id FROM tweets WHERE post_key='r1-reveal'")
    assert p.db.q("SELECT * FROM threads_posts") == []
    # an exception escaping the mirror itself is swallowed by the Poster hook
    async def explode(*a, **k):
        raise ValueError("bug")
    p.threads.post = explode
    assert run(p.post("r1-results", "results", "Resolved", round_id=1)) == "dry-r1-results"


def test_delete_round(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    run(p.post("r1-reveal", "reveal", "Picks", round_id=1))
    run(p.threads.delete_round(1))
    assert ("DELETE", "https://graph.threads.net/v1.0/t1") == fake.calls[-1][:2]
    assert p.db.q("SELECT * FROM threads_posts") == []


# ---------- token ----------

def test_token_refreshes_near_expiry_and_stores(tmp_path):
    f, fake = tmp_path / "t.json", FakeHTTP()
    f.write_text(json.dumps({"access_token": "old", "seed": "seed", "expires_at": time.time() + 3 * 86400, "tried_at": 0}))
    assert Token("seed", f, fake).get() == "NEW"
    d = json.loads(f.read_text())
    assert d["access_token"] == "NEW" and d["expires_at"] > time.time() + 50 * 86400
    assert Token("seed", f, fake).get() == "NEW" and sum(c[1].endswith("refresh_access_token") for c in fake.calls) == 1


def test_token_far_from_expiry_is_kept_and_new_env_token_wins(tmp_path):
    f, fake = tmp_path / "t.json", FakeHTTP()
    f.write_text(json.dumps({"access_token": "kept", "seed": "seed", "expires_at": time.time() + 30 * 86400, "tried_at": 0}))
    assert Token("seed", f, fake).get() == "kept" and not fake.calls
    assert Token("fresh-env", f, fake).get() == "NEW"   # a new env token replaces the file and is refreshed


def test_token_refresh_failure_keeps_working_token(tmp_path):
    class Bad(FakeHTTP):
        def get(self, url, **kw):
            return Resp(400, {"error": {"message": "too young"}})
    f = tmp_path / "t.json"
    assert Token("env", f, Bad()).get() == "env"
    assert json.loads(f.read_text())["tried_at"] > 0   # not retried for 6 h
