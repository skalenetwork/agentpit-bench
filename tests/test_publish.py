"""Publishing layer in dry-run: cards, outbox tweets, thread order, site build. Four agents per round."""
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import pytest

from agentpitbench import exports, site
from agentpitbench.cards import CardRenderer, results_headline, split_headline
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.publish import BenchPublisher
from agentpitbench.twitter import MAX_LEN, Poster, decision_text, results_text, split_text, tweet_len

AGENTS = ["claude", "codex", "agy", "grok"]
LONG = ("The order book is thin but the favourite has won nine of its last ten series on LAN, and the "
        "underdog is playing with a stand-in, so I am taking the favourite at a fair price here. " * 3)


def make_round(db: DB, picks: dict, state="awaiting_resolution", winner=None, slug="cs2-a-vs-b",
               question="Counter-Strike: CYBERSHOKE Esports vs Nexus (BO3) - CCT Europe Series 4 Group B decider",
               market_id="m1") -> int:
    m = {"id": market_id, "slug": slug, "question": question, "outcomes_list": ["CYBERSHOKE", "Nexus"],
         "prices_list": [0.62, 0.38], "endDate": "2026-10-10T20:00:00Z", "conditionId": "0xabc"}
    rid = db.create_round(m)
    for i, a in enumerate(AGENTS):
        run = db.create_run(rid, a, cli_version="1.0", model_reported=f"{a}-default")
        p = picks.get(a)
        if p is None:
            db.record_bet(run, outcome=None, decided_s=None)
            db.finish_run(run, "timeout", None, None)
            continue
        outcome, price, conf = p
        db.record_bet(run, outcome=outcome, token_id="t", rationale=LONG if a == "claude" else "Nexus looked sharp in qualifiers.",
                      confidence=conf, avg_price=price, shares_filled=100 / price, stake_filled=100,
                      decided_s=60 + 30 * i)
        db.finish_run(run, "bet", None, None)
    if state == "resolved":
        for e in db.round_entries(rid):
            if e["outcome"]:
                payout = e["shares_filled"] if e["outcome"] == winner else 0
                db.settle_bet(e["run_id"], payout, payout - e["stake_filled"])
        db.set_round(rid, state="resolved", winner=winner, resolved_at=time.time())
    else:
        db.set_round(rid, state=state)
    return rid


PICKS = {"claude": ("CYBERSHOKE", 0.63, 0.7), "codex": ("CYBERSHOKE", 0.63, 0.8),
         "agy": ("CYBERSHOKE", 0.64, 0.95), "grok": ("Nexus", 0.21, 0.55)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET",
              "X_OAUTH2_ACCESS_TOKEN", "X_OAUTH2_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BENCH_X_TOKEN_FILE", str(tmp_path / "x_oauth2.json"))
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, deploy_debounce_s=0, animate=False)
    return s, DB(s.db_path)


def rec(s, db, rid):
    return exports.round_record(s, db, db.round(rid))


def test_split_headlines(env):
    s, db = env
    mk = lambda picks, mid: rec(s, db, make_round(db, picks, market_id=mid))
    assert split_headline(mk(PICKS, "a")) == ("split", "3–1: Grok goes alone")
    two = {"claude": ("A", .5, .5), "codex": ("A", .5, .5), "agy": ("B", .5, .5), "grok": ("B", .5, .5)}
    assert split_headline(mk(two, "b"))[1] == "2–2: dead split"
    three = {"claude": ("A", .5, .5), "codex": ("A", .5, .5), "agy": ("B", .5, .5), "grok": ("C", .5, .5)}
    assert split_headline(mk(three, "c"))[1] == "2–1–1: 3-way split"
    agree = {"claude": ("A", .5, .5), "codex": ("A", .5, .5), "agy": ("A", .5, .5)}  # grok: no bet, not counted
    assert split_headline(mk(agree, "d")) == ("agree", "They all agree")


def test_texts_fit_and_tags(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="Nexus",
                     question="Will " + "very long question " * 20 + "?")
    r = rec(s, db, rid)
    board = exports.export(s, db)["leaderboard"]["agents"]
    texts = [decision_text(s, r, e) for e in r["entries"]]
    kind, h = split_headline(r)
    texts.append(split_text(s, r, kind, h))
    res = results_text(s, r, board, "https://agentpitbench.org/round/1/")
    texts.append(res)
    for t in texts:
        assert tweet_len(t) <= MAX_LEN, t
    claude = decision_text(s, r, r["entries"][0])
    assert claude.count('"') == 4 and "…\"" in claude  # clipped rationale keeps its closing quote
    assert "@xai" in res and "@AnthropicAI" not in res  # only the winner's vendor
    assert not any("@" in t for t in texts[:-1])  # never tag on decisions or splits
    assert "Grok 1-0" in res and "Claude 0-1" in res
    assert results_headline(r).startswith("Upset! Grok")


async def test_cards_render(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="Nexus")
    r = rec(s, db, rid)
    board = exports.export(s, db)["leaderboard"]["agents"]
    cr = CardRenderer(s)
    try:
        paths = [await cr.decision(r, e, board) for e in r["entries"]]
        paths += [await cr.split(r, board), await cr.results(r, board)]
    finally:
        await cr.close()
    for p in paths:
        head = p.read_bytes()[:24]
        assert head[:8] == b"\x89PNG\r\n\x1a\n"
        assert (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) == (1200, 675)


async def test_publisher_thread_and_idempotency(env):
    s, db = env
    rid = make_round(db, {**PICKS, "agy": None})  # Gemini timed out
    pub = BenchPublisher(s, db)
    try:
        runs = {e["agent"]: e["run_id"] for e in db.round_entries(rid)}
        for a in AGENTS:
            await pub.on_bet(rid, runs[a])
        await pub.on_bets_done(rid)
        for e in db.round_entries(rid):
            e_run = e["run_id"]
            if e["agent"] == "claude":
                db.settle_bet(e_run, 0, -100)
        db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())
        await pub.on_resolved(rid)
        await pub.on_resolved(rid)  # retry must not double-post
        first = db.round(rid)["thread_tweet_id"]
        assert first == f"dry-r{rid}-decision-claude"
        out = sorted(s.outbox_dir.glob("*.json"))
        assert len(out) == 6  # 4 decisions + split + results
        tweets = {json.loads(p.read_text())["post_key"]: json.loads(p.read_text()) for p in out}
        assert tweets[f"r{rid}-decision-claude"]["reply_to"] is None
        for k, t in tweets.items():
            assert t["length"] <= MAX_LEN
            if "decision" in k and k != f"r{rid}-decision-claude":
                assert t["reply_to"] == first                      # decisions stay one thread
            if k.endswith(("-split", "-results")):
                assert t["reply_to"] is None and t["quote_of"] == first   # standalone, quoting the opener
        assert "Reply with your pick: CYBERSHOKE / Nexus" in tweets[f"r{rid}-decision-claude"]["text"]
        assert "Reply with your pick" not in tweets[f"r{rid}-decision-codex"]["text"]
        assert "timed out" in tweets[f"r{rid}-decision-agy"]["text"]
        assert db.round(rid)["results_tweet_id"] == f"dry-r{rid}-results"
        assert all(e["tweet_id"] for e in db.round_entries(rid))
        assert db.one("SELECT COUNT(*) c FROM tweets")["c"] == 6
    finally:
        await pub.close()
    assert (s.site_dir / "index.html").exists()  # close() flushed the pending site build


def test_site_build(env):
    s, db = env
    make_round(db, PICKS, state="resolved", winner="Nexus", market_id="m1")
    live = make_round(db, {"claude": ("Nexus", .4, .6), "grok": ("CYBERSHOKE", .6, .9)}, market_id="m2")
    for e in db.round_entries(1):
        t = s.transcripts_dir / "1" / f"{e['agent']}.txt"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("transcript")
        db.x("UPDATE runs SET transcript_path=? WHERE run_id=?", str(t), e["run_id"])
    exports.export(s, db)
    out = site.build(s)
    pages = ["index.html", "leaderboard/index.html", "round/1/index.html", f"round/{live}/index.html",
             *(f"agent/{a}/index.html" for a in AGENTS), "data/index.html", "splits/index.html",
             "hall-of-shame/index.html", "widget/index.html"]
    for p in pages:
        f = out / p
        assert f.exists(), p
        assert f.stat().st_size < 100_000, p
    home = (out / "index.html").read_text()
    assert "Claude vs Codex vs Gemini vs Grok" in home and "data-live" in home
    assert 'class="team dkb"' in home  # Grok's near-black colour gets a light ring in dark mode
    r1 = (out / "round/1/index.html").read_text()
    assert "Bet against the AIs" in r1 and "utm_source" in r1 and "transcripts/1/claude.txt" in r1
    assert (out / "transcripts/1/grok.txt").exists()
    shame = (out / "hall-of-shame/index.html").read_text()
    assert "95% sure" in shame  # Gemini's most confident wrong call leads
    badge = (out / "badge.svg").read_text()
    ET.fromstring(badge)
    assert "Grok 1-0" in badge and "Claude 0-1" in badge
    ET.fromstring((out / "feed.xml").read_text())
    assert json.loads((out / "feed.json").read_text())["items"]
    for name in ("rounds.json", "leaderboard.json", "bets.csv"):
        assert (out / "data" / name).exists()


def test_deploy_skipped_in_dry_run(env):
    s, db = env
    exports.export(s, db)
    site.build(s)
    assert site.deploy(s) is False
    assert not (s.data_dir / "gh-pages").exists()


def test_poster_idempotent(env):
    s, db = env
    p = Poster(s, db)
    import asyncio
    a = asyncio.run(p.post("k1", "x", "hello"))
    b = asyncio.run(p.post("k1", "x", "hello again"))
    assert a == b == "dry-k1"
    assert len(list(s.outbox_dir.glob("*.json"))) == 1


# The Crowd: favourite-at-start reference baseline
def test_crowd_pick_and_season(env):
    s, db = env
    upset = make_round(db, PICKS, state="resolved", winner="Nexus")              # favourite 0.62 loses
    fav = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m2")
    assert exports.crowd_pick(s, ["A", "B"], [0.5, 0.5], "resolved", "A")["outcome"] is None  # tie: no pick
    r = rec(s, db, upset)
    assert r["crowd"] == {"outcome": "CYBERSHOKE", "price": 0.62, "won": False, "pnl": -100.0}
    assert rec(s, db, fav)["crowd"]["pnl"] == pytest.approx(100 / 0.62 - 100, abs=0.01)
    lb = exports.export(s, db)["leaderboard"]
    c = lb["crowd"]
    assert (c["played"], c["wins"], c["losses"], c["name"]) == (2, 1, 1, "The Crowd")
    assert [len(c["series"])] == [2] and "crowd" not in lb["series"] and "beat" not in c
    beat = {b["agent"]: b["beat_crowd"] for b in lb["agents"]}
    assert beat == {"grok": 1, "claude": 0, "codex": 0, "agy": 0}           # crowd is never ranked
    assert all(b["agent"] != "crowd" for b in lb["agents"])


def test_crowd_in_headline_tweet_badge(env):
    s, db = env
    picks = {**PICKS, "grok": ("Nexus", 0.40, 0.55)}                           # not an upset price
    rid = make_round(db, picks, state="resolved", winner="Nexus")
    r = rec(s, db, rid)
    lb = exports.export(s, db)["leaderboard"]
    assert results_headline(r) == "Grok beats the crowd"
    res = results_text(s, r, lb["agents"], "https://x.test/round/1/", lb["crowd"])
    assert "Crowd 0-1" in res and tweet_len(res) <= MAX_LEN and "@xai" in res
    assert "Crowd 0-1" in site.badge_svg(lb["agents"], lb["crowd"])
    assert split_headline(r)[1] == "3–1: Grok goes alone"                     # crowd not counted in splits


# OAuth 2.0 token rotation and posting, against a fake X
class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeX:
    """Token endpoint rotates refresh tokens and rejects reused ones; API accepts only the current access token."""
    def __init__(self, scope="tweet.write media.write users.read offline.access"):
        self.n, self.valid_refresh, self.valid_access, self.scope, self.calls = 0, "r0", None, scope, []

    def post(self, url, auth=None, data=None, timeout=None):
        self.calls.append(("token", data["refresh_token"]))
        if data["refresh_token"] != self.valid_refresh:
            return FakeResp(400, {"error": "invalid_request"})
        self.n += 1
        self.valid_refresh, self.valid_access = f"r{self.n}", f"a{self.n}"
        return FakeResp(200, {"access_token": self.valid_access, "refresh_token": self.valid_refresh,
                              "expires_in": 7200, "scope": self.scope})

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append((method, url))
        if headers["Authorization"] != f"Bearer {self.valid_access}":
            return FakeResp(401, {"title": "Unauthorized"})
        if url.endswith("/media/upload"):
            assert kw["data"] == {"media_category": "tweet_image"}
            assert kw["files"]["media"][1] == b"png"                           # full bytes, also on a retry
            return FakeResp(200, {"data": {"id": "m1"}})
        return FakeResp(201, {"data": {"id": f"t{len(self.calls)}", "text": kw["json"]["text"]}})


def poster2(env, monkeypatch, fake):
    s, db = env
    s.dry_run = False
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    monkeypatch.setenv("X_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("X_OAUTH2_REFRESH_TOKEN", "r0")
    p = Poster(s, db)
    p.oauth2.http = fake
    return p


async def test_oauth2_rotation_persisted_and_posting(env, monkeypatch, tmp_path):
    fake = FakeX()
    p = poster2(env, monkeypatch, fake)
    assert p.live
    img = tmp_path / "c.png"
    img.write_bytes(b"png")
    tid = await p.post("k1", "decision", "hello", image=img)
    assert tid and tid.startswith("t")
    store = json.loads((tmp_path / "x_oauth2.json").read_text())
    assert store["refresh_token"] == "r1" and store["access_token"] == "a1"       # rotated pair saved
    assert oct((tmp_path / "x_oauth2.json").stat().st_mode & 0o777) == "0o600"
    await p.post("k2", "split", "again", reply_to=tid)                         # reuses token, no refresh
    assert [c for c in fake.calls if c[0] == "token"] == [("token", "r0")]
    p2 = Poster(env[0], env[1]); p2.oauth2.http = fake                           # new process: reads file, not env seed
    fake.valid_access = "revoked"                                              # server-side expiry -> 401
    assert await p2.post("k3", "results", "third", image=img)
    assert [c[1] for c in fake.calls if c[0] == "token"] == ["r0", "r1"]       # one forced refresh, chain intact


async def test_oauth2_dead_refresh_token_is_permanent(env, monkeypatch):
    fake = FakeX()
    fake.valid_refresh = "something-else"
    p = poster2(env, monkeypatch, fake)
    assert await p.post("k1", "decision", "hello") is None                     # logged, not retried 5x
    assert len([c for c in fake.calls if c[0] == "token"]) == 1


def test_oauth2_concurrent_refresh_spends_token_once(env, monkeypatch):
    import threading
    fake = FakeX()
    p = poster2(env, monkeypatch, fake)
    out = []
    ts = [threading.Thread(target=lambda: out.append(p.oauth2.get())) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == ["a1"] * 5 and len([c for c in fake.calls if c[0] == "token"]) == 1


def test_oauth2_login_pkce(env, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    fake = FakeX()
    p = poster2(env, monkeypatch, fake)
    url = p.oauth2.authorize_url("https://example.test/cb")
    q = parse_qs(urlparse(url).query)
    assert "media.write" in q["scope"][0] and q["code_challenge_method"] == ["S256"]
    with pytest.raises(Exception, match="state mismatch"):
        p.oauth2.finish_login("https://example.test/cb?state=wrong&code=c")
    fake.post = lambda url, auth=None, data=None, timeout=None: FakeResp(200, {
        "access_token": "A", "refresh_token": "R", "expires_in": 7200, "scope": "tweet.write media.write"})
    assert p.oauth2.finish_login(f"https://example.test/cb?state={q['state'][0]}&code=c") == {"tweet.write", "media.write"}
    assert p.oauth2.get() == "A" and not p.oauth2.path.with_suffix(".pending").exists()


def test_only_results_tweets_carry_links(env):
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="Nexus")
    r = rec(s, db, rid)
    kind, h = split_headline(r)
    plain = [decision_text(s, r, e) for e in r["entries"]] + [split_text(s, r, kind, h)]
    assert not any("http" in t for t in plain)          # X bills link posts ~13x
    assert "http" in results_text(s, r, [], "https://x.test/round/1/")



# results hook, grammar, split replies, animated clips
def _rec(entries, winner="Predators", state="resolved"):
    return {"state": state, "winner": winner, "question": "Predators vs. Canadiens",
            "entries": [{"name": n, "agent": n.lower(), "outcome": o, "won": (o == winner) if o else False}
                        for n, o in entries]}


def test_results_hook_from_data():
    from agentpitbench.twitter import results_hook
    four = ["Claude", "Codex", "Gemini", "Grok"]
    assert results_hook(_rec([(n, "Canadiens") for n in four])) == "Resolved: Predators. All 4 AIs backed Canadiens. 0-4."
    assert results_hook(_rec([(n, "Predators") for n in four])) == "Resolved: Predators. All 4 AIs backed it. 4-0."
    lone = [("Claude", "Canadiens"), ("Codex", "Predators"), ("Gemini", "Canadiens"), ("Grok", "Canadiens")]
    assert results_hook(_rec(lone)) == "Resolved: Predators. Codex went alone and was right. 1-3."
    odd = [("Claude", "Predators"), ("Codex", "Predators"), ("Gemini", "Canadiens"), ("Grok", "Predators")]
    assert results_hook(_rec(odd)) == "Resolved: Predators. Claude & Codex & Grok cash; Gemini alone got it wrong."
    even = [("Claude", "Predators"), ("Codex", "Predators"), ("Gemini", "Canadiens"), ("Grok", "Canadiens")]
    assert results_hook(_rec(even)) == "Resolved: Predators. Claude & Codex win, Gemini & Grok lose. 2-2."
    assert results_hook(_rec([(n, None) for n in four])) == "Resolved: Predators. No AI placed a bet."


def test_commentary_has_no_verb_on_the_outcome():
    from agentpitbench import virality
    for rid in range(6):
        r = {**_rec([(n, "Canadiens") for n in ("Claude", "Codex")]), "round_id": rid, "crowd": {}}
        line = virality.commentary(r)
        assert "Predators lands" not in line and "Predators comes" not in line


async def test_humans_read_from_opener_and_split(env):
    from agentpitbench.engage import Engage
    s, db = env
    rid = make_round(db, PICKS)
    db.set_round(rid, thread_tweet_id="100")
    db.x("INSERT INTO tweets(post_key,tweet_id,kind,round_id) VALUES(?,?,?,?)", f"r{rid}-split", "200", "split", rid)
    s.max_reply_reads = 5
    calls = []

    class P:
        can_read = True
        def replies(self, conv, n):
            calls.append((conv, n))
            if conv == "100":
                return [{"id": "1", "author_id": "u1", "username": "a", "text": "Nexus", "created_at": "2026-10-09T10:00:00Z"},
                        {"id": "2", "author_id": "u2", "username": "b", "text": "nexus!", "created_at": "2026-10-09T10:05:00Z"}]
            return [{"id": "3", "author_id": "u2", "username": "b", "text": "CYBERSHOKE", "created_at": "2026-10-09T09:00:00Z"},
                    {"id": "4", "author_id": "u3", "username": "c", "text": "cybershoke", "created_at": "2026-10-09T11:00:00Z"}]
        def my_user_id(self):
            return "me"

    eng = Engage(s, db, P(), None)
    assert await eng.collect_humans(rid) == 3
    assert calls == [("100", 5), ("200", 3)]                              # one budget across both posts
    picks = {r["user_id"]: r["pick"] for r in db.q("SELECT user_id, pick FROM humans WHERE round_id=?", rid)}
    assert picks == {"u1": "Nexus", "u2": "CYBERSHOKE", "u3": "CYBERSHOKE"}  # earliest reply wins, either post


async def test_split_and_results_post_video_with_png_fallback(env, monkeypatch):
    s, db = env
    s.animate = True
    rid = make_round(db, PICKS)
    pub = BenchPublisher(s, db)
    posted = []
    vid = s.cards_dir / "fake.mp4"

    async def fake_clip(*a, **k):
        vid.parent.mkdir(parents=True, exist_ok=True)
        vid.write_bytes(b"mp4")
        return vid

    async def fake_post(key, kind, text, image=None, reply_to=None, round_id=None, quote_of=None):
        posted.append((key, Path(image).suffix if image else None))
        return None if (image and Path(image).suffix == ".mp4" and kind == "results") else f"t-{key}"

    monkeypatch.setattr(pub.cards, "split_clip", fake_clip)
    monkeypatch.setattr(pub.cards, "results_clip", fake_clip)
    monkeypatch.setattr(pub.poster, "post", fake_post)
    try:
        await pub.on_bets_done(rid)
        db.set_round(rid, state="resolved", winner="Nexus", resolved_at=time.time())
        await pub.on_resolved(rid)
    finally:
        await pub.close()
    assert (f"r{rid}-split", ".mp4") in posted
    assert posted.count((f"r{rid}-results", ".mp4")) == 1 and (f"r{rid}-results", ".png") in posted  # fallback


async def test_clip_renders_mp4_ending_on_the_static_card(env):
    from agentpitbench import media
    if not media.ffmpeg_exe():
        pytest.skip("no ffmpeg")
    s, db = env
    rid = make_round(db, PICKS, state="resolved", winner="Nexus")
    data = exports.export(s, db)
    r = next(x for x in data["rounds"] if x["round_id"] == rid)
    cr = CardRenderer(s)
    try:
        t0 = time.time()
        out = await cr.results_clip(r, data["leaderboard"]["agents"], data["leaderboard"]["crowd"])
        split = await cr.split_clip(r, data["leaderboard"]["agents"])
    finally:
        await cr.close()
    assert out and out.suffix == ".mp4" and 0 < out.stat().st_size < 5_000_000
    assert split and split.suffix == ".mp4" and split.stat().st_size < 5_000_000
    assert time.time() - t0 < 120
