"""Telegram mirror (docs/telegram-spec.md): HTML text, gating, threading, media, limits, deletes, failure
isolation. No real Telegram calls: a fake HTTP object answers every Bot API request."""
import asyncio
import json

import pytest

from agentpitbench import motd, telegram
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.telegram import build, fit, tlen, to_html
from agentpitbench.twitter import Poster, reveal_text

from test_bluesky import png

TOKEN = "123456:SECRET-token"


class Resp:
    def __init__(self, status=200, j=None):
        self.status_code, self._j = status, j or {}
        self.text = json.dumps(self._j)

    def json(self):
        return self._j


class FakeHTTP:
    """Answers sendMessage, sendPhoto, sendVideo, sendAnimation, sendMediaGroup, deleteMessage."""

    def __init__(self, fail=None, rate_limit_once=False):
        self.calls, self.n = [], 100
        self.fail, self.rate_limit_once = fail or {}, rate_limit_once

    def post(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        assert url.startswith(f"https://api.telegram.org/bot{TOKEN}/")
        data = {k: (json.loads(v) if k in ("reply_parameters", "link_preview_options", "media") else v)
                for k, v in (data or {}).items()}
        self.calls.append((method, data, sorted(files or {})))
        if self.rate_limit_once:
            self.rate_limit_once = False
            return Resp(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                              "parameters": {"retry_after": 3}})
        if method in self.fail:
            code, desc = self.fail[method]
            return Resp(code, {"ok": False, "error_code": code, "description": desc})
        if method == "deleteMessage":
            return Resp(200, {"ok": True, "result": True})
        if method == "sendMediaGroup":
            out = []
            for _ in data["media"]:
                self.n += 1
                out.append({"message_id": self.n})
            return Resp(200, {"ok": True, "result": out})
        self.n += 1
        return Resp(200, {"ok": True, "result": {"message_id": self.n}})

    def sends(self):
        return [(m, d) for m, d, _ in self.calls if m != "deleteMessage"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_REFRESH_TOKEN", "BSKY_HANDLE", "BSKY_APP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    slept = []

    async def no_wait(s):
        slept.append(s)
    monkeypatch.setattr(telegram.asyncio, "sleep", no_wait)
    s = Settings(data_dir=tmp_path / "var", dry_run=True, telegram_enabled=True, animate=False)
    db = DB(s.db_path)
    s.slept = slept
    return s, db


def live(env, monkeypatch, **fake_kw):
    """X stays in its outbox (dry ids); Telegram goes 'live' against the fake."""
    s, db = env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@agentpitbench")
    p = Poster(s, db)
    s.dry_run = False  # X has no credentials, so it still writes its outbox
    fake = FakeHTTP(**fake_kw)
    p.tg.http = fake
    assert p.tg.live and not p.live
    return p, fake


def run(c):
    return asyncio.run(c)


def rows(p):
    return [dict(r) for r in p.db.q("SELECT * FROM tg_posts")]


# ---------- text ----------

def test_html_escapes_and_links():
    s = Settings()
    x = ("Resolved: <Yes> & more. @AnthropicAI wins. #AgentpitBench\n"
         "https://agentpitbench.org/round/7/?utm_source=x&utm_medium=social")
    out, preview = build(s, x, "results", 7, 1024)
    assert "&lt;Yes&gt; &amp; more" in out and "@AnthropicAI" not in out and "Anthropic wins" in out
    assert out.endswith('<a href="https://agentpitbench.org/round/7/?utm_source=telegram&amp;utm_medium=social">'
                        'agentpitbench.org/round/7</a>')
    assert preview == "https://agentpitbench.org/round/7/?utm_source=telegram&utm_medium=social"


def test_reveal_share_your_pick_and_round_link():
    rnd = {"entries": [{"name": "Claude", "outcome": "Yes"}, {"name": "Grok", "outcome": None}],
           "outcomes": ["Yes", "No"]}
    x = reveal_text(rnd, "Split")
    assert "Reply with your pick" in x
    out, _ = build(Settings(), x, "reveal", 9, 1024)
    assert "Reply with" not in out
    assert ('<a href="https://agentpitbench.org/round/9/?utm_source=telegram&amp;utm_medium=social'
            '&amp;utm_campaign=reveal#pick">Share your pick</a>: Yes / No') in out
    assert out.endswith('utm_campaign=reveal">agentpitbench.org/round/9</a>')


def test_legacy_opener_invite_reworded():
    out, _ = build(Settings(), "Claude bets 100 on Yes.\nReply with your pick: Yes / No #AgentpitBench", "decision",
                   4, 1024)
    assert "Reply with" not in out and "utm_campaign=opener#pick\">Share your pick</a>" in out
    assert not out.endswith("</a>")   # no appended round line outside the reveal


def test_limits_count_utf16_and_keep_last_line():
    body = "💬" * 600 + "\nagentpitbench.org/round/1"
    assert tlen("💬") == 2
    t = fit(body, 1024)
    assert tlen(t) <= 1024 and t.endswith("\nagentpitbench.org/round/1")
    assert to_html("a < b", []) == "a &lt; b"


# ---------- gating ----------

def test_disabled_does_nothing(env):
    s, db = env
    s.telegram_enabled = False
    p = Poster(s, db)
    assert run(p.post("k1", "reveal", "hello")) == "dry-k1"
    assert not (s.outbox_dir / "telegram").exists() and not rows(p)


@pytest.mark.parametrize("why", ["dry_run", "paused", "no_creds"])
def test_outbox_when_not_live(env, monkeypatch, why):
    s, db = env
    if why != "no_creds":
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("TELEGRAM_CHANNEL", "@agentpitbench")
    s.dry_run = why == "dry_run"
    s.posting_paused = why == "paused"
    p = Poster(s, db)
    p.tg.http = None  # any network call would fail
    res = run(p.post("r1-reveal", "reveal", "Picks are in.\nReply with your pick: Yes / No #AgentpitBench",
                     round_id=1))
    if why == "paused":  # a pause records nothing anywhere
        assert res is None and not rows(p)
        assert not (s.outbox_dir / "telegram" / "r1-reveal.json").exists()
        # and the mirror itself refuses while paused, even if called directly
        assert run(p.tg.post("r1-reveal", "reveal", "x", round_id=1)) is None and not rows(p)
        return
    out = json.loads((s.outbox_dir / "telegram" / "r1-reveal.json").read_text())
    assert out["message_id"] == "dry-r1-reveal" and out["method"] == "sendMessage" and "Share your pick" in out["text"]
    assert rows(p)[0]["message_id"] == "dry-r1-reveal"


def test_unmirrored_kinds_skipped(env):
    s, db = env
    p = Poster(s, db)
    run(p.post("summon-reply-1", "summon", "You summoned it"))
    assert not rows(p)


# ---------- live (fake) ----------

def test_round_thread_shape(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch)
    cards = [png(tmp_path / f"decision-{a}.png") for a in ("claude", "codex", "agy", "grok")]
    reveal = run(p.post("r1-reveal", "reveal", "Split.\nReply with your pick: Yes / No #AgentpitBench", cards,
                        round_id=1))
    still = png(tmp_path / "results.png")
    video = tmp_path / "results.mp4"
    video.write_bytes(b"\0" * 100)
    results = run(p.post("r1-results", "results", "Resolved: Yes. #AgentpitBench\nhttps://agentpitbench.org/round/1/",
                         video, round_id=1, quote_of=reveal, bsky_fallback=still))
    press1 = run(p.post("r1-press-1", "press", "Grok: we go again.", reply_to=results, round_id=1))
    run(p.post("r1-press-2", "press", "Gemini: noted.", reply_to=press1, round_id=1))
    (m1, group), (m2, vid), (m3, p1), (m4, p2) = fake.sends()
    assert (m1, m2, m3, m4) == ("sendMediaGroup", "sendVideo", "sendMessage", "sendMessage")
    assert len(group["media"]) == 4 and group["media"][0]["parse_mode"] == "HTML"
    assert "Share your pick" in group["media"][0]["caption"] and "caption" not in group["media"][1]
    assert fake.calls[0][2] == ["p0", "p1", "p2", "p3"] and group["chat_id"] == "@agentpitbench"
    assert vid["reply_parameters"]["message_id"] == 101 and vid["supports_streaming"] == "true"  # quote -> reply
    assert p1["reply_parameters"]["message_id"] == 105 and p2["reply_parameters"]["message_id"] == 106
    assert json.loads(p.db.one("SELECT message_ids FROM tg_posts WHERE post_key='r1-reveal'")["message_ids"]) == \
        ["101", "102", "103", "104"]
    run(p.post("r1-press-1", "press", "Grok: we go again.", reply_to=results, round_id=1))   # idempotent
    assert len(fake.sends()) == 4


def test_link_preview_and_motd_link_reply(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch)
    film = tmp_path / "motd.mp4"
    film.write_bytes(b"\0" * 10)
    tid = run(p.post("motd-2026-10-12", "motd", "Hook Match of the Day #AgentpitBench", film, round_id=3))
    run(p.post("motd-2026-10-12-link", "motd-link",
               "Every pick: https://agentpitbench.org/round/3/?utm_source=x&utm_medium=social&utm_campaign=motd",
               reply_to=tid, round_id=3))
    (_, f), (_, link) = fake.sends()
    assert link["reply_parameters"]["message_id"] == 101
    assert link["link_preview_options"]["url"].startswith("https://agentpitbench.org/round/3/?utm_source=telegram")


def test_motd_post_passes_poster_still(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch)
    monkeypatch.setattr(telegram, "UPLOAD_MAX", 5)   # the film is "too big": the still goes out
    out = tmp_path / "film"
    out.mkdir()
    (out / "motd-d-tall.mp4").write_bytes(b"\0" * 10)
    png(out / "poster.png")
    s, db = env
    rec = {"date": "2026-10-12", "round_id": 3, "hook": "Hook", "files": {"tall": "motd-d-tall.mp4"}}
    run(motd.post(s, db, p, rec, out))
    assert [m for m, _ in fake.sends()] == ["sendPhoto", "sendMessage"]


def test_video_rejected_falls_back_to_still(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch, fail={"sendVideo": (400, "Bad Request: wrong file")})
    video = tmp_path / "c.mp4"
    video.write_bytes(b"\0" * 10)
    run(p.post("m1", "milestone", "Streak", video, bsky_fallback=png(tmp_path / "s.png")))
    assert [m for m, _ in fake.sends()] == ["sendVideo", "sendPhoto"]


def test_rate_limit_waits_retry_after(env, monkeypatch):
    p, fake = live(env, monkeypatch, rate_limit_once=True)
    s, _ = env
    assert run(p.post("m2", "milestone", "Streak")) == "dry-m2"
    assert s.slept == [3] and len(fake.sends()) == 2 and rows(p)[0]["message_id"] == "101"


def test_failure_never_blocks_x_and_token_redacted(env, monkeypatch, caplog):
    p, fake = live(env, monkeypatch, fail={"sendMessage": (400, f"Bad Request: chat not found bot{TOKEN}")})
    assert run(p.post("r2-reveal", "reveal", "Picks", round_id=2)) == "dry-r2-reveal"
    assert rows(p) == []

    def boom(url, **kw):
        raise ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
    fake.post = boom
    assert run(p.post("r3-reveal", "reveal", "Picks", round_id=3)) == "dry-r3-reveal"
    assert TOKEN not in caplog.text and "<token>" in caplog.text


def test_reply_without_mirrored_parent_is_skipped(env, monkeypatch):
    p, fake = live(env, monkeypatch, fail={"sendMessage": (400, "Bad Request")})
    results = run(p.post("r1-results", "results", "Resolved", round_id=1))   # X ok, Telegram failed
    fake.fail = {}
    assert run(p.post("r1-press-1", "press", "Grok: we go again.", reply_to=results, round_id=1)) == "dry-r1-press-1"
    assert fake.sends() == [("sendMessage", fake.calls[0][1])] and rows(p) == []


def test_void_round_deletes_every_message(env, monkeypatch, tmp_path):
    p, fake = live(env, monkeypatch)
    cards = [png(tmp_path / f"d{i}.png") for i in range(2)]
    run(p.post("r5-reveal", "reveal", "Picks", cards, round_id=5))
    fake.fail = {"deleteMessage": (400, "Bad Request: message to delete not found")}   # already gone counts
    run(p.tg.delete_round(5))
    assert [d["message_id"] for m, d, _ in fake.calls if m == "deleteMessage"] == [101, 102]
    assert rows(p) == []


def test_void_round_keeps_row_on_failure(env, monkeypatch):
    p, fake = live(env, monkeypatch)
    run(p.post("r6-reveal", "reveal", "Picks", round_id=6))
    fake.fail = {"deleteMessage": (400, "Bad Request: message can't be deleted")}
    run(p.tg.delete_round(6))
    assert len(rows(p)) == 1


def test_void_round_dry_removes_outbox(env):
    s, db = env
    p = Poster(s, db)
    run(p.post("r7-reveal", "reveal", "Picks", round_id=7))
    assert (s.outbox_dir / "telegram" / "r7-reveal.json").exists()
    run(p.tg.delete_round(7))
    assert not (s.outbox_dir / "telegram" / "r7-reveal.json").exists() and rows(p) == []
