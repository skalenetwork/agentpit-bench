"""AI scene images (scenes.py, cards.poster, publish._poster): prompt rules, the sealed-pick guard, the cap, cache and
fallback paths with a fake HTTP transport, and the 1080x1350 composite."""
import base64
import json
import struct
import zlib

import httpx
import pytest

from agentpitbench import cards, mascots, scenes
from agentpitbench.cards import CardRenderer
from agentpitbench.publish import BenchPublisher
from agentpitbench.scenes import SceneRefused, SceneService, build_prompt

from test_publish import PICKS, env, make_round, rec  # noqa: F401  (env is a fixture)


def png(w=64, h=80, rgb=(40, 120, 220)) -> bytes:
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def size(path) -> tuple[int, int]:
    return struct.unpack(">II", path.read_bytes()[16:24])


def interactions(img: bytes) -> dict:
    """generateContent answer (the shape the live API returns)."""
    return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png",
                                                                     "data": base64.b64encode(img).decode()}}]}}]}


def steps_shape(img: bytes) -> dict:
    return {"steps": [{"type": "model_output", "content": [{"type": "text", "text": "ok"},
                                                          {"type": "image", "mime_type": "image/png",
                                                           "data": base64.b64encode(img).decode()}]}]}


class Fake:
    """httpx.MockTransport that records requests; `plan` is a list of status codes / bodies, last one repeats."""

    def __init__(self, *plan):
        self.plan, self.calls, self.urls = list(plan) or [interactions(png())], [], []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(json.loads(req.content))
        self.urls.append(str(req.url))
        self.key = req.headers.get("x-goog-api-key")
        step = self.plan.pop(0) if len(self.plan) > 1 else self.plan[0]
        return httpx.Response(step, json={}) if isinstance(step, int) else httpx.Response(200, json=step)


def results_round(s, db, picks=PICKS, winner="Nexus", mid="m1"):
    return rec(s, db, make_round(db, picks, state="resolved", winner=winner, market_id=mid))


@pytest.fixture
def svc(env, monkeypatch):
    s, db = env
    s.scene_images_enabled = True
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(SceneService, "quota_until", 0.0)
    return s, db


# ---------- prompts ----------

def rnd_with(round_id, picks, winner):
    ents = [{"agent": a, "name": a.title(), "avg_price": 0.6, "outcome": o, "won": (o == winner) if winner else None}
            for a, o in picks.items()]
    return {"round_id": round_id, "state": "resolved" if winner else "running", "winner": winner, "entries": ents,
            "question": "Counter-Strike: CYBERSHOKE Esports vs Nexus (BO3) decider", "outcomes": ["CYBERSHOKE", "Nexus"]}


WIPE = {"claude": "CYBERSHOKE", "codex": "CYBERSHOKE", "agy": "CYBERSHOKE", "grok": "CYBERSHOKE"}
LONE = {**WIPE, "grok": "Nexus"}


def test_prompt_hard_rules_always_present():
    for kind in ("reveal", "results"):
        for rid in range(1, 15):
            p = build_prompt(rnd_with(rid, LONE, "Nexus"), kind).lower()
            for clause in ("no text", "no letters", "no numbers", "no logos", "no real people", "no real team crests",
                           "no violence", "family-friendly", "bottom third", "uncluttered"):
                assert clause in p, clause
            for name in ("prof", "slim", "tank", "lucky"):   # the same four-robot character sheet every time
                assert name in p


def test_prompt_variety_and_determinism():
    prompts = {build_prompt(rnd_with(i, LONE, "Nexus"), "results") for i in range(1, 25)}
    assert len(prompts) >= 20
    a = build_prompt(rnd_with(7, LONE, "Nexus"), "results")
    assert a == build_prompt(rnd_with(7, LONE, "Nexus"), "results")
    assert a != build_prompt(rnd_with(7, WIPE, "Nexus"), "reveal")   # kind is part of the seed


def test_situations_shape_the_scene():
    assert "pit" in build_prompt(rnd_with(1, WIPE, "Nexus"), "results")
    assert "podium" in build_prompt(rnd_with(1, WIPE, "CYBERSHOKE"), "results")
    assert "spotlight" in build_prompt(rnd_with(1, LONE, "Nexus"), "results")
    assert "tug-of-war" in build_prompt(rnd_with(1, {"claude": "A1", "codex": "A1", "agy": "B1", "grok": "B1"}, None),
                                        "reveal")
    assert "unison" in build_prompt(rnd_with(1, WIPE, None), "reveal")


def test_sealed_pick_guard():
    r = rnd_with(3, LONE, None)
    for kind in ("decision", "sweep", "split-pre"):
        with pytest.raises(SceneRefused):
            build_prompt(r, kind)
    for kind in ("reveal", "results"):
        p = build_prompt(rnd_with(3, LONE, "Nexus"), kind).lower()
        for word in scenes.forbidden_terms(rnd_with(3, LONE, "Nexus")):
            if word in ("esports",):
                continue
            assert word not in p.split(), word       # no question words, outcomes or winner in the prompt
    with pytest.raises(SceneRefused):                 # last line of defence if a label ever matches the template
        scenes.assert_sealed({"winner": "Prof", "entries": []}, build_prompt(r, "reveal"))


# ---------- service: fallback, cap, cache, cost ----------

async def test_disabled_or_no_key_makes_no_call(env, monkeypatch):
    s, db = env
    fake = Fake()
    r = results_round(s, db)
    assert await SceneService(s, db, fake.transport).scene(r, "results", "k1") is None   # disabled (default)
    s.scene_images_enabled = True
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert await SceneService(s, db, fake.transport).scene(r, "results", "k1") is None   # no key
    assert fake.calls == [] and db.one("SELECT COUNT(*) n FROM scene_images")["n"] == 0


async def test_generate_logs_cost_and_caches(svc):
    s, db = svc
    fake = Fake()
    sv = SceneService(s, db, fake.transport)
    r = results_round(s, db)
    p = await sv.scene(r, "results", "r1-results")
    assert p == s.data_dir / "scenes" / "r1-results.png" and p.read_bytes()[:4] == b"\x89PNG"
    assert fake.key == "test-key" and len(fake.calls) == 1
    body = fake.calls[0]
    assert fake.urls[0].endswith(f"/models/{s.scene_images_model}:generateContent")
    assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert body["generationConfig"]["imageConfig"]["aspectRatio"] == "4:5"
    assert "no text" in body["contents"][0]["parts"][-1]["text"].lower()
    row = db.one("SELECT * FROM scene_images")
    assert row["status"] == "ok" and row["cost_usd"] == pytest.approx(s.scene_image_cost_usd)
    assert await sv.scene(r, "results", "r1-results") == p and len(fake.calls) == 1    # cache: no second request
    assert sv.used_today() == 1 and sv.spent_today() == pytest.approx(s.scene_image_cost_usd)


async def test_interactions_steps_shape_is_accepted(svc):
    s, db = svc
    assert await SceneService(s, db, Fake(steps_shape(png())).transport).scene(results_round(s, db), "results", "k") is not None


async def test_cap_reached_falls_back(svc):
    s, db = svc
    s.scene_images_per_day = 2
    fake = Fake()
    sv = SceneService(s, db, fake.transport)
    r = results_round(s, db)
    assert await sv.scene(r, "results", "a") and await sv.scene(r, "results", "b")
    assert await sv.scene(r, "results", "c") is None and len(fake.calls) == 2
    assert await sv.scene(r, "results", "a") is not None                  # a cached post still works at the cap


async def test_http_errors_retry_once_then_fall_back(svc):
    s, db = svc
    fake = Fake(503, 503)
    sv = SceneService(s, db, fake.transport)
    assert await sv.scene(results_round(s, db), "results", "k") is None
    assert len(fake.calls) == 2                                           # one retry
    row = db.one("SELECT * FROM scene_images")
    assert row["status"] == "failed" and row["cost_usd"] == 0
    flaky = Fake(500, interactions(png()))
    assert await SceneService(s, db, flaky.transport).scene(results_round(s, db, mid="m2"), "results", "k2")
    assert len(flaky.calls) == 2
    bad = Fake(400)                                                       # a client error is not retried
    assert await SceneService(s, db, bad.transport).scene(results_round(s, db, mid="m3"), "results", "k3") is None
    assert len(bad.calls) == 1
    blocked = Fake({"candidates": [{"content": {"parts": [{"text": "I cannot"}]}}]})
    assert await SceneService(s, db, blocked.transport).scene(results_round(s, db, mid="m4"), "results", "k4") is None
    ignored = Fake({"steps": [{"type": "model_output", "content": [{"type": "text", "text": "I cannot"}]}]})
    assert await SceneService(s, db, ignored.transport).scene(results_round(s, db, mid="m5"), "results", "k5") is None


async def test_quota_429_is_quiet_not_retried_and_backs_off(svc, caplog):
    s, db = svc
    fake = Fake(429)
    sv = SceneService(s, db, fake.transport)
    r = results_round(s, db)
    with caplog.at_level("INFO"):
        assert await sv.scene(r, "results", "q1") is None
        assert await sv.scene(r, "results", "q2") is None          # backoff: no second call
    assert len(fake.calls) == 1
    assert not [x for x in caplog.records if x.levelname in ("WARNING", "ERROR")]
    assert db.one("SELECT status FROM scene_images")["status"] == "quota"
    assert sv.used_today() == 0                                    # quota refusals do not eat the daily cap


async def test_excluded_keyword_market_gets_no_scene(svc):
    s, db = svc
    s.excluded_keywords = ["decider"]
    fake = Fake()
    assert await SceneService(s, db, fake.transport).scene(results_round(s, db), "results", "k") is None
    assert fake.calls == []


async def test_pre_reveal_kind_makes_no_call(svc):
    s, db = svc
    fake = Fake()
    assert await SceneService(s, db, fake.transport).scene(results_round(s, db), "decision", "k") is None
    assert fake.calls == []


# ---------- compositing ----------

async def test_poster_renders_1080x1350_with_untouched_logos(svc):
    s, db = svc
    r = results_round(s, db)
    cr = CardRenderer(s)
    try:
        html = cr.poster_html(r, "results", png())
        assert mascots.data_uri(mascots.LOGOS["claude"][0]) in html     # the real logo file, byte for byte
        assert "data:image/png;base64," in html and "1080px" in html
        out = await cr.poster(r, "results", png())
    finally:
        await cr.close()
    assert out.name == "results-poster.png" and size(out) == (1080, 1350)
    rv = rec(s, db, make_round(db, PICKS, market_id="m9"))
    cr = CardRenderer(s)
    try:
        rhtml = cr.poster_html(rv, "reveal", png())
        assert "CYBERSHOKE" in rhtml and "Nexus" in rhtml                # reveal shows each pick
        assert (await cr.poster(rv, "reveal", png())).name == "split-poster.png"
    finally:
        await cr.close()


def test_football_is_soccer_not_american_football():
    for q in ("Will Arsenal FC beat Chelsea FC on Oct 30?", "Premier League: who wins?"):
        v = scenes.sport_vocab(q) or ""
        assert "soccer" in v and "not an american football" in v
    assert "american-football" in scenes.sport_vocab("NFL: Chiefs vs Bills")
    assert "basketball" in scenes.sport_vocab("NBA finals game 3")
    ps = [build_prompt({**rnd_with(i, WIPE, "Nexus"), "question": "Arsenal FC vs Chelsea FC"}, "results")
          for i in range(1, 40)]
    assert any("soccer" in p for p in ps) and not any("Arsenal" in p or "american-football" in p for p in ps)


async def test_jpeg_answers_are_accepted_and_composited(svc):
    s, db = svc
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64                      # the live API returns JPEG for this model
    r = results_round(s, db)
    p = await SceneService(s, db, Fake(interactions(jpeg)).transport).scene(r, "results", "j")
    assert p and p.read_bytes().startswith(b"\xff\xd8")
    assert "data:image/jpeg;base64," in CardRenderer(s).poster_html(r, "results", p)


def test_poster_hooks():
    mk = rnd_with
    assert scenes.poster_hook(mk(1, WIPE, "Nexus"), "results") == "0-4. ALL FOUR WRONG."
    assert scenes.poster_hook(mk(1, WIPE, "CYBERSHOKE"), "results") == "4-0. ALL FOUR RIGHT."
    assert scenes.poster_hook(mk(1, LONE, "Nexus"), "results") == "GROK ALONE. 1-3."
    assert scenes.poster_hook(mk(1, WIPE, None), "reveal") == "THEY ALL AGREE."
    assert scenes.poster_hook(mk(1, LONE, None), "reveal").startswith("3–1: GROK GOES ALONE")


# ---------- publisher wiring ----------

async def test_publisher_poster_falls_back_to_none(svc, monkeypatch):
    s, db = svc
    r = results_round(s, db)
    pub = BenchPublisher(s, db)
    try:
        monkeypatch.delenv("GEMINI_API_KEY")
        assert await pub._poster(r, "results", "k") is None            # no key: the plain card is posted
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        pub.scenes.transport = Fake(500).transport
        assert await pub._poster(r, "results", "k2") is None           # API down
        pub.scenes.transport = Fake().transport
        out = await pub._poster(r, "results", "k3")
        assert out and size(out) == (1080, 1350)
        monkeypatch.setattr(pub.cards, "poster", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render")))
        assert await pub._poster(r, "results", "k3") is None           # render failure never propagates
    finally:
        await pub.close()


# ---------- formats, gags, trading card, loop ----------

BOARD = [{"agent": "claude", "rank": 1, "wins": 6, "losses": 2, "win_rate": .75, "net_pnl": 220.0, "streak": 3},
         {"agent": "codex", "rank": 2, "wins": 4, "losses": 4, "win_rate": .5, "net_pnl": 20.0, "streak": -1},
         {"agent": "agy", "rank": 3, "wins": 3, "losses": 5, "win_rate": .375, "net_pnl": -60.0, "streak": -2},
         {"agent": "grok", "rank": 4, "wins": 2, "losses": 6, "win_rate": .25, "net_pnl": -180.0, "streak": -5}]


def test_gags_from_board():
    g = scenes.gags_from_board(BOARD)
    assert g["dunce"] == "grok" and g["leader"] == "claude" and g["chaos"] == 2 and not g["lead_changed"]
    assert g["stats"]["grok"]["streak"] == -5
    g = scenes.gags_from_board(BOARD, prev_leader="codex")
    assert g["lead_changed"] and g["prev_leader"] == "codex"
    calm = scenes.gags_from_board([{**b, "streak": 1} for b in BOARD])
    assert calm["dunce"] is None and calm["chaos"] == 0
    assert scenes.gags_from_board(None)["dunce"] is None


def test_gags_land_in_the_prompt_consistently():
    r = rnd_with(5, LONE, "Nexus")
    p = build_prompt(r, "results", gags=scenes.gags_from_board(BOARD, prev_leader="codex"))
    assert "Pit Boss" in p and "top hat" in p and "whistle" in p and "clipboard" in p
    assert "Lucky wears a tall pointed blank paper dunce cap" in p
    assert "Prof holds up a shiny golden trophy" in p and "Slim is cheekily snatching" in p
    assert "smoke" in p.lower()                                  # chaos stage 2
    plain = build_prompt(r, "results", gags=scenes.gags_from_board([{**b, "streak": 1} for b in BOARD]))
    assert "dunce" not in plain and "wisps of smoke" not in plain and "Pit Boss" in plain


def test_formats_prompt_and_choice():
    r = rnd_with(5, LONE, "Nexus")
    assert "3-panel comic strip" in build_prompt(r, "results", fmt="comic")
    assert "movie-poster" in build_prompt(r, "results", fmt="movie")
    tr = build_prompt(r, "results", fmt="trading")
    assert "ONE original cartoon robot" in tr and "Lucky" in tr and "Pit Boss" not in tr and "foil" in tr
    with pytest.raises(SceneRefused):
        build_prompt(rnd_with(5, WIPE, "Nexus"), "results", fmt="trading")   # no featured agent
    with pytest.raises(SceneRefused):
        build_prompt(r, "results", fmt="nope")
    for fmt in scenes.FORMATS:
        assert "no text" in build_prompt(r, "results", fmt=fmt).lower()
    seen = {scenes.choose_format(rnd_with(i, WIPE, "Nexus"), "results") for i in range(1, 60)}
    assert seen == {"scene", "comic"}                           # wipeouts lean to the comic strip
    seen = {scenes.choose_format(rnd_with(i, LONE, "Nexus"), "results") for i in range(1, 60)}
    assert "trading" in seen and "scene" in seen
    assert {scenes.choose_format(rnd_with(i, LONE, "Nexus"), "results", allowed=("scene",)) for i in range(40)} == {"scene"}


async def test_all_formats_render_with_our_text_only(svc):
    s, db = svc
    r = results_round(s, db)                       # Grok alone is right: a RARE trading card
    g = scenes.gags_from_board(BOARD)
    cr = CardRenderer(s)
    try:
        for fmt in scenes.FORMATS:
            html = cr.poster_html(r, "results", png(), fmt, g)
            assert f'class="fmt-{fmt}"' in html and mascots.data_uri(mascots.LOGOS["grok"][0]) in html
            out = await cr.poster(r, "results", png(), fmt, g)
            assert size(out) == (1080, 1350)
        trading = cr.poster_html(r, "results", png(), "trading", g)
        assert "RARE" in trading and "The Wild Card" in trading and "2-6" in trading and "L5" in trading
        assert "RARE" not in cr.poster_html(r, "results", png(), "scene", g)
        assert "Starring" in cr.poster_html(r, "results", png(), "movie", g)
    finally:
        await cr.close()


async def test_poster_loop_mp4_or_none(svc):
    from agentpitbench import media
    s, db = svc
    r = results_round(s, db)
    cr = CardRenderer(s)
    try:
        out = await cr.poster_clip(r, "results", png(), "scene", None, seconds=3, fps=6, particles="smoke")
    finally:
        await cr.close()
    if media.ffmpeg_exe():
        assert out and out.suffix == ".mp4" and out.stat().st_size > 1000
    else:
        assert out is None


async def test_publisher_loop_falls_back_to_none(svc, monkeypatch):
    s, db = svc
    s.animate = True
    r = results_round(s, db)
    pub = BenchPublisher(s, db)
    try:
        assert await pub._poster_loop(r, "nokey") is None            # no scene on disk: keep the still
        pub.scenes.transport = Fake().transport
        assert await pub._poster(r, "results", "lp", BOARD)
        async def boom(*a, **k):
            raise RuntimeError("encode")
        monkeypatch.setattr(pub.cards, "poster_clip", boom)
        assert await pub._poster_loop(r, "lp", BOARD) is None        # encode failure: the still stays
    finally:
        await pub.close()
