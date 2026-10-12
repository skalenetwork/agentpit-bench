"""Bluesky mirror (docs/bluesky-spec.md): text, facets, outbox gating, threads, embeds, failure isolation. No real
Bluesky calls: a fake HTTP object answers every XRPC request."""
import asyncio
import json
import struct
import zlib

import pytest

from agentpitbench import bluesky, bskystats, exports, site
from agentpitbench.bluesky import Mirror, adapt, build_text, facets, fit300
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.publish import card_alt
from agentpitbench.twitter import Poster, results_text


def png(path, w=1200, h=675, pad=0):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" * 10)) + chunk(b"IEND", b"") + b"\0" * pad)
    path.write_bytes(data)
    return path


class Resp:
    def __init__(self, status=200, j=None):
        self.status_code, self._j = status, j or {}
        self.text = json.dumps(self._j)
        self.content = self.text.encode()

    def json(self):
        return self._j

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHTTP:
    """Answers createSession, uploadBlob, createRecord, deleteRecord, getServiceAuth, video upload, getPosts."""

    def __init__(self, fail_post=False, fail_video=False, expire_once=False):
        self.calls, self.records, self.n = [], [], 0
        self.fail_post, self.fail_video, self.expire_once = fail_post, fail_video, expire_once

    def _answer(self, method, url, kw):
        self.calls.append((method, url, kw))
        nsid = url.rsplit("/", 1)[-1]
        if nsid == "com.atproto.server.createSession":
            return Resp(200, {"did": "did:plc:bench", "accessJwt": "A", "refreshJwt": "R", "didDoc": {"service": [
                {"id": "#atproto_pds", "serviceEndpoint": "https://pds.example.host"}]}})
        if nsid == "com.atproto.server.refreshSession":
            return Resp(200, {"accessJwt": "A2", "refreshJwt": "R2"})
        if nsid == "com.atproto.repo.uploadBlob":
            if self.expire_once:
                self.expire_once = False
                return Resp(400, {"error": "ExpiredToken"})
            return Resp(200, {"blob": {"$type": "blob", "ref": {"$link": f"b{len(self.calls)}"},
                                       "mimeType": kw["headers"]["Content-Type"], "size": len(kw["data"])}})
        if nsid == "com.atproto.repo.createRecord":
            if self.fail_post:
                return Resp(400, {"error": "InvalidRequest"})
            self.n += 1
            self.records.append(kw["json"]["record"])
            return Resp(200, {"uri": f"at://did:plc:bench/app.bsky.feed.post/r{self.n}", "cid": f"c{self.n}"})
        if nsid == "com.atproto.repo.deleteRecord":
            return Resp(200, {})
        if nsid == "com.atproto.server.getServiceAuth":
            return Resp(200, {"token": "S"})
        if nsid == "app.bsky.video.uploadVideo":
            if self.fail_video:
                return Resp(400, {"error": "too big"})
            return Resp(200, {"jobStatus": {"jobId": "j1", "state": "JOB_STATE_ENCODING"}})
        if nsid == "app.bsky.video.getJobStatus":
            return Resp(200, {"jobStatus": {"jobId": "j1", "state": "JOB_STATE_COMPLETED",
                                            "blob": {"$type": "blob", "ref": {"$link": "vid"}, "mimeType": "video/mp4"}}})
        if nsid == "com.atproto.identity.resolveHandle":
            return Resp(200, {"did": "did:plc:anthropic"})
        if nsid == "app.bsky.feed.getPosts":
            return Resp(200, {"posts": [{"uri": u, "likeCount": 3, "repostCount": 1, "replyCount": 2, "quoteCount": 0}
                                        for u in kw["params"]["uris"]]})
        raise AssertionError(f"unexpected call {url}")

    def post(self, url, **kw):
        return self._answer("POST", url, kw)

    def get(self, url, **kw):
        return self._answer("GET", url, kw)

    def request(self, method, url, **kw):
        return self._answer(method, url, kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_REFRESH_TOKEN", "BSKY_HANDLE", "BSKY_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    monkeypatch.setattr(bluesky.time, "sleep", lambda s: None)

    async def no_wait(s):
        return None
    monkeypatch.setattr(bluesky.asyncio, "sleep", no_wait)
    s = Settings(data_dir=tmp_path / "var", dry_run=True, bluesky_enabled=True, animate=False)
    db = DB(s.db_path)
    return s, db


def live(env, monkeypatch, **fake_kw):
    """X stays in its outbox (dry ids); Bluesky goes 'live' against the fake."""
    s, db = env
    monkeypatch.setenv("BSKY_HANDLE", "agentpitbench.org")
    monkeypatch.setenv("BSKY_APP_PASSWORD", "app-pass")
    p = Poster(s, db)
    s.dry_run = False  # X has no credentials, so it still writes its outbox
    fake = FakeHTTP(**fake_kw)
    p.bsky.http = fake
    assert p.bsky.live and not p.live
    return p, fake


def run(c):
    return asyncio.run(c)


# ---------- text ----------

def test_adapt_vendor_handles_and_links():
    s = Settings()
    text = "Resolved: Yes. Claude wins. #AgentpitBench @AnthropicAI @xai\nhttps://agentpitbench.org/round/7/?utm_source=x&utm_medium=social"
    out, links = adapt(text, s)
    assert "@AnthropicAI" not in out and "Anthropic" in out and "xAI" in out
    assert "agentpitbench.org/round/7" in out and "https://" not in out
    assert links == [("agentpitbench.org/round/7",
                      "https://agentpitbench.org/round/7/?utm_source=bluesky&utm_medium=social")]
    s2 = Settings(bluesky_vendor_handles={"claude": "anthropic.com"})
    assert "@anthropic.com" in adapt("x @AnthropicAI y", s2)[0]


def test_facets_utf8_byte_offsets():
    text = "Split — Claude: Yes · Grok: No at 42¢ 💬 #AgentpitBench @anthropic.com\nagentpitbench.org/round/3"
    fs = facets(text, [("agentpitbench.org/round/3", "https://agentpitbench.org/round/3/?a=1")],
                {"anthropic.com": "did:plc:a"})
    raw = text.encode()
    got = {f["features"][0]["$type"].rsplit("#", 1)[1]: raw[f["index"]["byteStart"]:f["index"]["byteEnd"]].decode()
           for f in fs}
    assert got == {"tag": "#AgentpitBench", "mention": "@anthropic.com", "link": "agentpitbench.org/round/3"}
    assert [f["index"]["byteStart"] for f in fs] == sorted(f["index"]["byteStart"] for f in fs)


def test_results_text_fits_300_with_full_url():
    s = Settings()
    rnd = {"state": "resolved", "winner": "Yes", "question": "Q " * 80, "entries": [
        {"agent": a, "name": a.title() * 3, "outcome": "Yes" if i % 2 else "No", "won": bool(i % 2)}
        for i, a in enumerate(["claude", "codex", "agy", "grok"])]}
    board = [{"name": a * 4, "wins": 10, "losses": 9} for a in ("claude", "codex", "agy", "grok")]
    url = "https://agentpitbench.org/round/123/?utm_source=x&utm_medium=social&utm_campaign=results&utm_content=fun"
    x = results_text(s, rnd, board, url, {"played": 3, "wins": 2, "losses": 1})
    text, links = build_text(s, x, "results", 123)
    assert len(text) <= 300 and links and links[0][1].startswith("https://agentpitbench.org/round/123/?utm_source=bluesky")
    assert text.endswith("agentpitbench.org/round/123")


def test_fit300_keeps_last_line():
    t = fit300("a" * 400 + "\nagentpitbench.org/round/1")
    assert len(t) <= 300 and t.endswith("\nagentpitbench.org/round/1")


def test_reveal_gets_round_link():
    text, links = build_text(Settings(), "Picks are in.\nClaude: Yes\nReply with your pick: Yes / No #AgentpitBench",
                             "reveal", 9)
    assert text.endswith("agentpitbench.org/round/9") and "utm_campaign=reveal" in links[-1][1]


def test_card_alt():
    rnd = {"question": "Will it rain?"}
    assert card_alt(rnd, {"name": "Claude", "outcome": "Yes", "stake_filled": 100, "avg_price": 0.42}) == \
        'Claude decision card: bets 100 on Yes at 42¢ on "Will it rain?".'
    assert "no bet" in card_alt(rnd, {"name": "Grok", "outcome": None})


# ---------- gating ----------

def test_disabled_does_nothing(env):
    s, db = env
    s.bluesky_enabled = False
    p = Poster(s, db)
    assert run(p.post("k1", "reveal", "hello")) == "dry-k1"
    assert not (s.outbox_dir / "bluesky").exists()
    assert not db.q("SELECT * FROM bsky_posts")


@pytest.mark.parametrize("why", ["dry_run", "paused", "no_creds"])
def test_outbox_when_not_live(env, monkeypatch, why):
    s, db = env
    if why != "no_creds":
        monkeypatch.setenv("BSKY_HANDLE", "h")
        monkeypatch.setenv("BSKY_APP_PASSWORD", "p")
    s.dry_run = why == "dry_run"
    s.posting_paused = why == "paused"
    p = Poster(s, db)
    p.bsky.http = None  # any network call would fail
    assert run(p.post("r1-reveal", "reveal", "Picks are in. #AgentpitBench", round_id=1)) == "dry-r1-reveal"
    out = json.loads((s.outbox_dir / "bluesky" / "r1-reveal.json").read_text())
    assert out["uri"] == "dry-r1-reveal" and out["facets"]
    assert db.one("SELECT uri FROM bsky_posts WHERE post_key='r1-reveal'")["uri"] == "dry-r1-reveal"


def test_unmirrored_kinds_skipped(env):
    s, db = env
    p = Poster(s, db)
    run(p.post("summon-reply-1", "summon", "You summoned it"))
    assert not db.q("SELECT * FROM bsky_posts")


# ---------- live (fake) ----------

def test_round_thread_shape(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch)
    cards = [png(tmp_path / f"decision-{a}.png") for a in ("claude", "grok")]
    reveal = run(p.post("r1-reveal", "reveal", "Picks are in. #AgentpitBench", cards, round_id=1,
                        alt=["Claude card", "Grok card"]))
    still = png(tmp_path / "results.png")
    video = tmp_path / "results.mp4"
    video.write_bytes(b"\0" * 100)
    results = run(p.post("r1-results", "results", "Resolved: Yes. #AgentpitBench\nhttps://agentpitbench.org/round/1/",
                         video, round_id=1, quote_of=reveal, bsky_fallback=still))
    press1 = run(p.post("r1-press-1", "press", "Grok: we go again.", reply_to=results, round_id=1))
    run(p.post("r1-press-2", "press", "Gemini: noted.", reply_to=press1, round_id=1))
    r_reveal, r_results, r_p1, r_p2 = fake.records
    assert [i["alt"] for i in r_reveal["embed"]["images"]] == ["Claude card", "Grok card"]
    assert r_reveal["embed"]["images"][0]["aspectRatio"] == {"width": 1200, "height": 675}
    assert r_results["embed"]["$type"] == "app.bsky.embed.recordWithMedia"
    assert r_results["embed"]["record"]["record"] == {"uri": "at://did:plc:bench/app.bsky.feed.post/r1", "cid": "c1"}
    assert r_results["embed"]["media"]["$type"] == "app.bsky.embed.video"
    assert r_p1["reply"]["root"]["cid"] == "c2" and r_p1["reply"]["parent"]["cid"] == "c2"
    assert r_p2["reply"]["root"]["cid"] == "c2" and r_p2["reply"]["parent"]["cid"] == "c3"
    # service auth audience is the PDS from the didDoc
    sa = next(kw for m, u, kw in fake.calls if u.endswith("getServiceAuth"))
    assert sa["params"]["aud"] == "did:web:pds.example.host"
    assert sum(u.endswith("createSession") for _, u, _ in fake.calls) == 1   # one login per process
    # idempotent per post_key
    run(p.post("r1-press-1", "press", "Grok: we go again.", reply_to=results, round_id=1))
    assert len(fake.records) == 4


def test_video_failure_falls_back_to_still(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch, fail_video=True)
    video = tmp_path / "c.mp4"
    video.write_bytes(b"\0" * 10)
    run(p.post("motd-1", "motd", "Film", video, bsky_fallback=png(tmp_path / "s.png")))
    assert fake.records[0]["embed"]["$type"] == "app.bsky.embed.images"


def test_big_image_reencoded_or_dropped(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch)
    monkeypatch.setattr(bluesky, "IMAGE_MAX", 50)
    run(p.post("m1", "milestone", "Streak", png(tmp_path / "big.png", pad=200)))
    assert len(fake.records) == 1   # posted either way


def test_failure_never_blocks_x(env, monkeypatch):
    p, fake = live(env, monkeypatch, fail_post=True)
    assert run(p.post("r2-reveal", "reveal", "Picks", round_id=2)) == "dry-r2-reveal"
    assert db_rows(p) == []

    def boom(*a, **k):
        raise RuntimeError("network down")
    p.bsky.http.post = boom
    p.bsky._client = None
    assert run(p.post("r3-reveal", "reveal", "Picks", round_id=3)) == "dry-r3-reveal"


def test_expired_token_retry_keeps_content_type(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch, expire_once=True)
    run(p.post("m2", "milestone", "Streak", png(tmp_path / "a.png")))
    ups = [kw for _, u, kw in fake.calls if u.endswith("uploadBlob")]
    assert len(ups) == 2 and ups[1]["headers"]["Content-Type"] == "image/png"
    assert ups[1]["headers"]["Authorization"] == "Bearer A2"
    assert len(fake.records) == 1


def test_reply_without_mirrored_parent_is_skipped(env, monkeypatch):
    p, fake = live(env, monkeypatch, fail_post=True)
    results = run(p.post("r1-results", "results", "Resolved", round_id=1))   # X ok, Bluesky failed
    fake.fail_post = False
    assert run(p.post("r1-press-1", "press", "Grok: we go again.", reply_to=results, round_id=1)) == "dry-r1-press-1"
    assert fake.records == [] and db_rows(p) == []


def db_rows(p):
    return [dict(r) for r in p.db.q("SELECT * FROM bsky_posts")]


def test_mention_facet_resolved_and_cached(env, monkeypatch):
    s, db = env
    s.bluesky_vendor_handles = {"claude": "anthropic.com"}
    p, fake = live(env, monkeypatch)
    run(p.post("r1-results", "results", "Resolved. #AgentpitBench @AnthropicAI", round_id=1))
    f = [x for x in fake.records[0]["facets"] if x["features"][0]["$type"].endswith("mention")]
    assert f and f[0]["features"][0]["did"] == "did:plc:anthropic"
    assert db.get("bsky_did_anthropic.com") == "did:plc:anthropic"


def test_void_round_deletes(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    run(p.post("r5-reveal", "reveal", "Picks", round_id=5))
    run(p.bsky.delete_round(5))
    assert any(u.endswith("deleteRecord") and kw["json"]["rkey"] == "r1" for _, u, kw in fake.calls)
    assert db_rows(p) == []


def test_bskystats_pull(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    run(p.post("r1-reveal", "reveal", "Picks", round_id=1))
    s, db = env
    assert bskystats.pull(s, db, http=fake) == 1
    assert bskystats.pull(s, db, http=fake) == 0   # hourly at most
    m = db.one("SELECT * FROM bsky_metrics")
    assert (m["likes"], m["reposts"], m["replies"]) == (3, 1, 2)
    s.bluesky_enabled = False
    assert bskystats.pull(s, db, http=fake) == 0


# ---------- handle proof ----------

def test_well_known_atproto_did(env):
    s, db = env
    exports.export(s, db)
    site.build(s)
    assert not (s.site_dir / ".well-known" / "atproto-did").exists()
    assert (s.site_dir / ".nojekyll").exists()
    s.bluesky_did = "did:plc:abc123"
    site.build(s)
    assert (s.site_dir / ".well-known" / "atproto-did").read_text() == "did:plc:abc123"
