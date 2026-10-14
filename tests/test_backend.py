"""The bot + site side of the backend (docs/backend-api.md), all off unless backend_enabled. No network: the Worker is
an httpx.MockTransport, agentpit a fake."""
import asyncio
import json
import re
from datetime import datetime, timezone

import httpx
import pytest

from agentpitbench import backend, exports, site, xstats
from agentpitbench.config import Settings
from agentpitbench.db import DB
from agentpitbench.reveal_window import start_ok
from test_publish import AGENTS, PICKS, make_round

KICK = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp()   # Oct 15, 8:00PM ET (EDT)
NOW = KICK - 30 * 3600


def market(mid="501", desc="This match is scheduled for October 15 at 8:00PM ET.", end="2026-10-16T02:00:00Z", **kw):
    return {"id": mid, "question": f"Will Team {mid} win?", "slug": f"team-{mid}", "conditionId": f"0x{mid}",
            "active": True, "acceptingOrders": True, "closed": False, "endDate": end, "description": desc,
            "outcomes_list": ["Yes", "No"], "prices_list": [0.55, 0.45], "token_ids": ["t1", "t2"],
            "events": [{"title": f"Team {mid} vs Rivals"}], **kw}


class Api:
    def __init__(self, *ms):
        self.ms = {str(m["id"]): m for m in ms}

    async def market(self, mid):
        return self.ms.get(str(mid))


class Worker:
    """Records what the bot sends; answers GETs from a table."""

    def __init__(self, answers=None, fail=False):
        self.sent, self.answers, self.fail = [], answers or {}, fail

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503)
        if req.method == "POST":
            self.sent.append((req.url.path, req.headers.get("X-Bench-Key"), json.loads(req.content)))
            return httpx.Response(200, json={"ok": True})
        key = req.url.path.rsplit("/v1/", 1)[1]
        period = req.url.params.get("period")
        body = self.answers.get(f"{key}?{period}" if period else key)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    def client(self, s):
        return backend.Backend(s, transport=httpx.MockTransport(self))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(backend.KEY_ENV, "k-123")
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=False, backend_enabled=True,
                 round_start_window=True, round_start_min_h=3, round_start_max_h=12)
    return s, DB(s.db_path)


def seen(db, *ms):
    for m in ms:
        db.x("INSERT INTO seen_markets(market_id, first_seen, eligible, condition_id) VALUES(?,?,1,?)",
             str(m["id"]), NOW - 600, m["conditionId"])


def test_lock_time_is_the_earliest_start_less_a_margin(env):
    s, _ = env
    m = market()
    lock = backend.lock_time(s, m["question"], m["endDate"], m["description"], NOW, 48 * 3600)
    earliest = KICK - 12 * 3600
    assert earliest - backend.LOCK_MARGIN_S - 60 <= lock <= earliest - backend.LOCK_MARGIN_S
    assert not start_ok(m["question"], m["endDate"], m["description"], lock, 3, 12)  # never after the AIs start
    # a market whose round may start right now cannot take a pick before the AIs: not listed
    assert backend.lock_time(s, m["question"], m["endDate"], m["description"], KICK - 6 * 3600, 48 * 3600) is None
    # beyond the horizon: not listed yet
    assert backend.lock_time(s, m["question"], m["endDate"], m["description"], KICK - 80 * 3600, 48 * 3600) is None


def test_upcoming_lists_eligible_markets_with_a_predictable_start(env):
    s, db = env
    good, ineligible = market("501"), market("502", closed=True)
    seen(db, good, ineligible)
    items = asyncio.run(backend.upcoming(s, db, Api(good, ineligible), NOW))
    assert [x["market_id"] for x in items] == ["501"]
    x = items[0]
    assert x["round_id"] is None and x["outcomes"] == ["Yes", "No"] and x["event_title"] == "Team 501 vs Rivals"
    assert x["locks_at"] < KICK - 12 * 3600 and x["opens_at"] == round(NOW - 600) and "team-501" in x["url"]
    # without the F1b start window a round may start at any batch: nothing is pickable before the AIs
    s.round_start_window = False
    assert asyncio.run(backend.upcoming(s, db, Api(good), NOW)) == []


def test_pusher_sends_markets_started_rounds_and_results_once(env):
    s, db = env
    w = Worker()
    seen(db, market("501"))
    rid = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m1")
    live = make_round(db, PICKS, state="running", market_id="m3", question="Will Team Alpha win the grand final?")
    changes = []
    p = backend.Pusher(s, db, Api(market("501")), on_change=lambda: changes.append(1), client=w.client(s))
    asyncio.run(p.tick(NOW))
    paths = [x[0] for x in w.sent]
    assert paths.count("/v1/ingest/markets") == 2 and paths.count("/v1/ingest/results") == 1
    assert all(k == "k-123" for _, k, _ in w.sent)
    started = next(b for path, _, b in w.sent if path.endswith("markets") and b[0]["round_id"])
    assert started == [{**started[0], "market_id": "m3", "round_id": live}]
    assert started[0]["locks_at"] == started[0]["opens_at"]          # picks lock when the round started
    res = next(b for path, _, b in w.sent if path.endswith("results"))
    assert res == [{"market_id": "m1", "round_id": rid, "winner": "CYBERSHOKE",
                    "ai_picks": {"claude": "CYBERSHOKE", "codex": "CYBERSHOKE", "agy": "CYBERSHOKE", "grok": "Nexus"}}]
    assert changes == [1] and json.loads((s.export_dir / "upcoming.json").read_text())[0]["market_id"] == "501"
    # idempotent: nothing new, nothing re-sent (the markets list only after backend_push_s, and unchanged)
    n = len(w.sent)
    asyncio.run(p.tick(NOW + 60))
    asyncio.run(p.tick(NOW + s.backend_push_s + 1))
    assert len(w.sent) == n and changes == [1]


def test_pusher_never_raises_and_sends_nothing_when_off_dry_or_keyless(env, monkeypatch):
    s, db = env
    make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m1")
    down = Worker(fail=True)
    p = backend.Pusher(s, db, Api(), client=down.client(s))
    asyncio.run(p.tick(NOW))                         # a 503: logged, retried next tick
    assert db.get(backend.SENT_RESULTS) is None
    w = Worker()
    for tweak in ({"backend_enabled": False}, {"dry_run": True}):
        s2 = Settings(**{**s.__dict__, **tweak})
        asyncio.run(backend.Pusher(s2, db, Api(), client=w.client(s2)).tick(NOW))
    monkeypatch.delenv(backend.KEY_ENV)
    asyncio.run(backend.Pusher(s, db, Api(), client=w.client(s)).tick(NOW))
    assert w.sent == []


def test_scoreboard_cache_and_fallback(env):
    s, _ = env
    sb = {"humans": {"picks": 10, "correct": 6}, "by_ai": {"claude": {"human_wins": 3, "ai_wins": 1, "ties": 6}},
          "top": [{"name": "Calm Otter 4821", "correct": 6, "picks": 10}]}
    assert backend.refresh_scoreboard(s, Worker({"scoreboard?week": sb, "scoreboard?season": sb}).client(s))
    assert backend.scoreboard(s)["season"]["humans"]["correct"] == 6
    assert not backend.refresh_scoreboard(s, Worker(fail=True).client(s))   # the Worker is down: the cache stays
    assert backend.scoreboard(s)["week"]["by_ai"]["claude"]["human_wins"] == 3


def test_digest_lists_comments_to_review_only_when_on(env, monkeypatch):
    s, db = env
    w = Worker({"admin/review": {"since": "2026-10-14T00:00:00Z", "comments": [{"id": 7, "text": "spam", "hidden": True, "reports": 3}]}})
    real = backend.Backend
    monkeypatch.setattr(backend, "Backend", lambda s: real(s, transport=httpx.MockTransport(w)))
    out = xstats.digest(s, db, NOW)
    assert out["comments_review"] == {"items": [{"id": 7, "text": "spam", "hidden": True, "reports": 3}], "n": 1, "hidden": 1}
    s.backend_enabled = False
    assert "comments_review" not in xstats.digest(s, db, NOW)


# ---------------------------------------------------------------- the free API (docs/free-api-spec.md)

def test_rounds_reach_the_read_model_sealed_until_every_agent_finished(env):
    s, db = env
    w = Worker()
    done = make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m1")
    live = make_round(db, PICKS, state="running", market_id="m3", question="Will Team Alpha win the grand final?")
    (s.export_dir).mkdir(parents=True, exist_ok=True)
    (s.export_dir / site.LIVE_PRICES_FILE).write_text(json.dumps({"m3": {"prices": [0.7, 0.3], "at": 1}}))
    p = backend.Pusher(s, db, Api(), client=w.client(s))
    asyncio.run(p.tick(NOW))
    pushes = [b for path, _, b in w.sent if path == "/v1/ingest/rounds"]
    assert len(pushes) == 1
    body = pushes[0]
    assert body["meta"]["methodology_version"] == s.methodology_version
    assert [a["agent"] for a in body["meta"]["agents"]] == AGENTS
    by = {r["round_id"]: r for r in body["rounds"]}
    assert by[done]["sealed"] is False and by[done]["counts"] is True and by[done]["category"]
    assert {e["agent"]: e["outcome"] for e in by[done]["entries"]}["grok"] == "Nexus"
    assert by[done]["prices_now"] is None
    # the running round: no decision of any agent leaves the bot (the embargo), but its chance now does
    r = by[live]
    assert r["sealed"] is True and r["prices_now"] == {"prices": [0.7, 0.3]}
    blob = json.dumps(r)
    assert "Nexus looked sharp" not in blob and "transcripts/" not in blob
    for e in r["entries"]:
        assert all(e[k] is None for k in ("outcome", "rationale", "quote", "confidence", "transcript_url", "model_reported"))
    # nothing personal: no X handles, no wallets
    assert "username" not in json.dumps(body) and "wallet" not in json.dumps(body)
    # idempotent
    n = len(w.sent)
    asyncio.run(p.tick(NOW + 60))
    assert len(w.sent) == n
    # every agent finished: the round leaves `running` and goes again, now with the picks
    db.set_round(live, state="awaiting_resolution")
    asyncio.run(p.tick(NOW + 120))
    again = [b for path, _, b in w.sent[n:] if path == "/v1/ingest/rounds"]
    assert [x["round_id"] for x in again[0]["rounds"]] == [live]
    assert {e["agent"]: e["outcome"] for e in again[0]["rounds"][0]["entries"]}["grok"] == "Nexus"


def test_digest_has_the_api_usage_line(env, monkeypatch, caplog):
    s, db = env
    use = {"since": "2026-10-14", "enabled": True, "requests": 1234, "limited": 7, "anon_requests": 200,
           "keyed_requests": 1034, "active_keys": 2, "keys": 3,
           "top": [{"id": "k1", "prefix": "apb_abcdefgh", "label": "discord bot", "requests": 900, "limited": 7}]}
    w = Worker({"admin/review": {"comments": []}, "admin/api-usage": use})
    real = backend.Backend
    monkeypatch.setattr(backend, "Backend", lambda s: real(s, transport=httpx.MockTransport(w)))
    with caplog.at_level("INFO"):
        out = xstats.digest(s, db, NOW)
    assert out["api_usage"] == use
    line = backend.api_usage_line(use)
    assert line == ("API: 1234 requests (200 without a key), 2 active of 3 keys, 7 refused (429); "
                    "top: apb_abcdefgh discord bot (900)")
    assert line in caplog.text
    assert backend.api_usage_line({"error": "HTTP 503"}).startswith("API: unavailable")
    s.backend_enabled = False
    assert "api_usage" not in xstats.digest(s, db, NOW)


def test_operator_controls_switch_and_keys(env):
    s, _ = env
    w = Worker({"admin/keys": {"keys": []}, "admin/api-usage": {"enabled": True}})
    c = w.client(s)
    backend.api_switch(s, False, client=c)
    backend.api_keys(s, revoke="k1", client=c)
    backend.api_keys(s, limit=("k2", 50000), client=c)
    assert backend.api_keys(s, client=c) == {"keys": []}
    assert backend.api_switch(s, None, client=c) == {"enabled": True}
    assert [(p, b) for p, _, b in w.sent] == [
        ("/v1/admin/api", {"enabled": False}), ("/v1/admin/keys/k1", {"action": "revoke"}),
        ("/v1/admin/keys/k2", {"action": "limit", "daily_limit": 50000})]
    assert all(k == "k-123" for _, k, _ in w.sent)


# ---------------------------------------------------------------- the site

def build(s, db):
    make_round(db, PICKS, state="resolved", winner="CYBERSHOKE", market_id="m1")
    make_round(db, PICKS, state="running", market_id="m3", question="Will Team Alpha win the grand final?")
    exports.export(s, db)
    return site.build(s)


def test_site_without_backend_has_no_trace_of_it(tmp_path):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True)
    out = build(s, DB(s.db_path))
    for page in ("index.html", "round/1/index.html", "c/live/index.html", "leaderboard/index.html"):
        html = (out / page).read_text()
        assert "data-pick" not in html and "gsi/client" not in html and "data-discuss" not in html, page
    assert not (out / "me").exists()
    assert "There are no accounts, no forms" in (out / "privacy/index.html").read_text()
    assert not (out / "developers").exists() and "developers/" not in (out / "index.html").read_text()
    terms = (out / "terms/index.html").read_text()   # the static API's reuse terms hold without the backend too
    assert 'id="api"' in terms and "developers/" not in terms


def test_site_with_backend(tmp_path):
    s = Settings(data_dir=tmp_path / "var", agents=AGENTS, dry_run=True, backend_enabled=True,
                 google_client_id="123.apps.googleusercontent.com")
    s.export_dir.mkdir(parents=True)
    future = datetime.now(timezone.utc).timestamp() + 5 * 3600
    (s.export_dir / backend.UPCOMING_FILE).write_text(json.dumps([
        {"market_id": "501", "round_id": None, "question": "Will Team 501 win?", "event_title": "Team 501 vs Rivals",
         "outcomes": ["Yes", "No"], "opens_at": 1, "locks_at": future, "url": ""},
        {"market_id": "502", "round_id": None, "question": "Closed already?", "event_title": "", "outcomes": ["Yes", "No"],
         "opens_at": 1, "locks_at": 100, "url": ""}]))
    (s.export_dir / backend.SCOREBOARD_FILE).write_text(json.dumps({"season": {
        "humans": {"picks": 10, "correct": 6}, "by_ai": {"claude": {"human_wins": 3, "ai_wins": 1, "ties": 6}},
        "top": [{"name": "<b>Calm Otter</b>", "correct": 6, "picks": 10}]}}))
    out = build(s, DB(s.db_path))
    home = (out / "index.html").read_text()
    assert "Coming up" in home and 'data-pick="501"' in home and 'data-pick="502"' not in home
    assert re.search(r'<div class="hp" data-pick="501"[^>]* hidden>', home)            # shown by the script only
    assert "Team 501 vs Rivals · Will Team 501 win?" in home                           # the full market name
    assert '<script src="https://accounts.google.com' not in home and "gsi/client" in home  # loaded on click only
    assert 'data-pick="501"' in (out / "c/live/index.html").read_text()
    rnd = (out / "round/1/index.html").read_text()
    assert 'data-discuss="1"' in rnd and 'data-pick="m1"' in rnd and 'data-winner="CYBERSHOKE"' in rnd
    assert "api.agentpitbench.org/v1" in rnd and "123.apps.googleusercontent.com" in rnd
    lb = (out / "leaderboard/index.html").read_text()
    assert "Humans vs the AIs" in lb and "3-1-6" in lb and "&lt;b&gt;Calm Otter" in lb and "The Crowd" not in lb[
        lb.index('id="humans-vs-ais"'):lb.index("Top humans")]
    me = (out / "me/index.html").read_text()
    assert '<meta name="robots" content="noindex">' in me and "data-me" in me
    assert "/me/</loc>" not in (out / "sitemap-en.xml").read_text()
    priv = (out / "privacy/index.html").read_text()
    assert "apb_s" in priv and "api.agentpitbench.org" in priv and "do not store your email" in priv
    assert "Sign-in, picks and comments" in (out / "terms/index.html").read_text()
    dev = (out / "developers/index.html").read_text()
    assert "https://api.agentpitbench.org/v2/leaderboard" in dev and "data-keys" in dev and "/me/keys" in dev
    assert 'href="../developers/"' in (out / "data/index.html").read_text() or "developers/" in (out / "data/index.html").read_text()
    assert (out / "es/developers/index.html").exists()
    terms = (out / "terms/index.html").read_text()
    assert 'id="api"' in terms and "Data: AgentpitBench (agentpitbench.org)" in terms and "developers/" in terms
    assert "hash of the key" in priv
