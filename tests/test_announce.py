"""announce.py: the operator's X draft per deploy, delivered privately on Telegram (never a real message here:
every send goes through a fake http object to a .invalid API URL)."""
import json
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from agentpitbench import announce, opai
from agentpitbench.config import Settings
from agentpitbench.twitter import tweet_len
from conftest import copy_lock_v10

DAY = 1_791_900_000.0          # 2026-10-13 ~ 12:00 UTC
V11 = ("Gemini now plays on Gemini 3.8 Flash (High): Google removed Gemini 3.1 Pro from the Antigravity CLI on "
       "2026-10-12. Earlier rounds keep the model they were played on.")
V12 = ("Up to 10 rounds a day (was 5), and at most one round per event per day: a ladder of thresholds on the same "
       "question counts as one event.")


class Http:
    def __init__(self, fail: int = 0):
        self.calls, self.fail = [], fail

    def post(self, url, data=None, files=None, timeout=None):
        assert url.startswith("https://telegram.invalid/bot"), url   # never the real API
        self.calls.append((url, data))
        ok = len(self.calls) > self.fail

        class R:
            status_code = 200 if ok else 502
            text = ""

            def json(self_):
                return {"ok": True, "result": {"message_id": 7}} if ok else {"ok": False, "error_code": 502,
                                                                              "description": "bad gateway"}
        return R()


@pytest.fixture
def lock(tmp_path):
    p = tmp_path / "methodology.json"
    copy_lock_v10(p)
    return p


def add_version(lock, v, text):
    d = json.loads(lock.read_text())
    d["version"] = v
    d["changelog"].append({"date": "2026-10-13", "version": v, "text": text, "approved_by": "operator"})
    lock.write_text(json.dumps(d))


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setenv("TELEGRAM_OPERATOR_CHAT", "42")
    return Settings(data_dir=tmp_path / "var", dry_run=False, telegram_api_url="https://telegram.invalid")


@pytest.fixture
def repo(monkeypatch):
    """A fake git: HEAD and the commit subjects between two shas."""
    r = {"head": "a" * 40, "log": []}
    monkeypatch.setattr(announce, "head", lambda: r["head"])
    monkeypatch.setattr(announce, "changes", lambda since, until, since_date=None: list(r["log"]))
    return r


def test_draft_fits_and_keeps_the_link(s):
    vers = [{"version": "1.1", "text": V11}, {"version": "1.2", "text": V12}]
    subjects = ["Site: dark/light switch (Auto -> Light -> Dark)", "Every round post carries the full market name"]
    text, source = announce.write(s, None, vers, subjects)
    assert source == "template"
    assert tweet_len(text) <= 280
    assert text.endswith(" https://agentpitbench.org/methodology/#v1.2")
    assert "v1.1:" in text and "v1.2:" in text and "Gemini 3.8 Flash" in text and "10 rounds a day" in text
    long = announce.fit("x " * 400, "https://agentpitbench.org/")
    assert tweet_len(long) <= 280 and long.endswith(" https://agentpitbench.org/")
    text, _ = announce.write(s, None, [], subjects)          # no version: the site home
    assert text.startswith("New on AgentpitBench: Site: dark/light switch") and text.endswith(" https://agentpitbench.org/")


def test_generated_entry_text_is_formatted():
    e = {"version": "1.3", "text": "Market selection: {param} {old} → {new} (automatic weekly review; evidence: {link})",
         "values": {"param": "daily_round_cap", "old": "10", "new": "8", "link": "https://x.org/c/1.3.json"}}
    assert announce.entry_text(e).startswith("Market selection: daily_round_cap 10 → 8")


def test_intent_url_round_trips():
    d = "AgentpitBench v1.2: a ladder & one bet — 10/day? #x https://agentpitbench.org/methodology/#v1.2"
    u = announce.intent_url(d)
    assert u.startswith("https://x.com/intent/post?text=") and " " not in u and "&" not in u.split("?", 1)[1]
    assert parse_qs(urlparse(u).query)["text"] == [d] and unquote(u.split("=", 1)[1]) == d
    msg = announce.message({"draft": d, "versions": [{"version": "1.2"}], "source": "template"})
    assert 'href="https://x.com/intent/post?text=' in msg and "&amp; one bet" in msg


def test_changelog_anchors_unique(lock):
    from agentpitbench import methodology
    add_version(lock, "1.1", V11)
    ids = methodology.changelog_anchors(lock)
    assert ids[0] == "v1.1" and ids.count("v1.0") == 1 and len(ids) == len(methodology.changelog(lock))


def test_first_run_is_a_baseline_then_a_deploy_sends_once(s, lock, repo):
    http = Http()
    assert not announce.tick(s, None, http, now=DAY, lock_path=lock)
    assert announce.state(s)["last_sha"] == "a" * 40 and not http.calls
    assert not announce.tick(s, None, http, now=DAY + 300, lock_path=lock)       # nothing new
    repo.update(head="b" * 40, log=["Site: dark/light switch"])
    add_version(lock, "1.1", V11)
    assert announce.tick(s, None, http, now=DAY + 600, lock_path=lock)
    assert len(http.calls) == 1
    data = http.calls[0][1]
    assert data["chat_id"] == "42" and data["parse_mode"] == "HTML" and "Post on X" in data["text"]
    assert "v1.1" in data["text"] and "#v1.1" in data["text"]
    st = announce.state(s)
    assert st == {"last_sha": "b" * 40, "last_version": "1.1", "last_sent_day": "2026-10-13"}
    assert announce.pending(s) is None and len(announce.sent(s)) == 1
    for k in range(3):                                                            # idempotent
        assert not announce.tick(s, None, http, now=DAY + 900 + k, lock_path=lock)
    assert len(http.calls) == 1


def test_one_draft_per_utc_day_covers_later_deploys(s, lock, repo):
    http = Http()
    announce.tick(s, None, http, now=DAY, lock_path=lock)
    repo.update(head="b" * 40, log=["Site: dark/light switch"])
    assert announce.tick(s, None, http, now=DAY + 60, lock_path=lock)
    repo.update(head="c" * 40, log=["Every round post carries the full market name"])
    add_version(lock, "1.2", V12)
    assert not announce.tick(s, None, http, now=DAY + 3600, lock_path=lock)       # same UTC day: deferred
    repo.update(head="d" * 40, log=["Every round post carries the full market name", "Reveal timing"])
    assert not announce.tick(s, None, http, now=DAY + 7200, lock_path=lock)
    assert len(http.calls) == 1 and announce.pending(s) is None
    assert announce.tick(s, None, http, now=DAY + 86400, lock_path=lock)          # next day: one draft for both
    assert len(http.calls) == 2
    rec = announce.sent(s)[-1]
    assert rec["from_sha"] == "b" * 40 and rec["to_sha"] == "d" * 40 and [v["version"] for v in rec["versions"]] == ["1.2"]
    assert "Reveal timing" in rec["changes"]


def test_nothing_reader_visible_moves_the_baseline(s, lock, repo):
    http = Http()
    announce.tick(s, None, http, now=DAY, lock_path=lock)
    repo.update(head="b" * 40, log=[])
    assert not announce.tick(s, None, http, now=DAY + 60, lock_path=lock)
    assert announce.state(s)["last_sha"] == "b" * 40 and announce.pending(s) is None and not http.calls


def test_dry_run_never_sends(s, lock, repo, caplog):
    s.dry_run = True
    http = Http()
    announce.tick(s, None, http, now=DAY, lock_path=lock)
    repo.update(head="b" * 40, log=["Site: dark/light switch"])
    with caplog.at_level("INFO"):
        for k in range(3):
            assert not announce.tick(s, None, http, now=DAY + 60 + k, lock_path=lock)
    assert not http.calls and announce.pending(s)["state"] == "pending"
    assert sum("dry run" in r.message for r in caplog.records) == 1
    s.dry_run = False                                                             # going live sends it
    assert announce.tick(s, None, http, now=DAY + 120, lock_path=lock) and len(http.calls) == 1


def test_retries_with_backoff_then_fails(s, lock, repo):
    http = Http(fail=99)
    announce.tick(s, None, http, now=DAY, lock_path=lock)
    repo.update(head="b" * 40, log=["Site: dark/light switch"])
    t = DAY + 60
    for _ in range(announce.MAX_ATTEMPTS):
        announce.tick(s, None, http, now=t, lock_path=lock)
        t += announce.BACKOFF_CAP_S
    rec = announce.pending(s)
    assert rec["state"] == "failed" and rec["attempts"] == announce.MAX_ATTEMPTS and "bad gateway" in rec["last_error"]
    assert "123:fake" not in json.dumps(rec)
    n = len(http.calls)
    announce.tick(s, None, http, now=t, lock_path=lock)
    assert len(http.calls) == n                                                    # failed waits for --resend
    http.fail = 0
    announce.resend(s, rec["id"])
    assert announce.tick(s, None, http, now=t + 1, lock_path=lock)


def test_backoff_spaces_attempts(s, lock, repo):
    http = Http(fail=1)
    announce.tick(s, None, http, now=DAY, lock_path=lock)
    repo.update(head="b" * 40, log=["Site: dark/light switch"])
    assert not announce.tick(s, None, http, now=DAY + 60, lock_path=lock)
    assert not announce.tick(s, None, http, now=DAY + 120, lock_path=lock)        # within the backoff
    assert len(http.calls) == 1
    assert announce.tick(s, None, http, now=DAY + 60 + announce.BACKOFF_S, lock_path=lock)


def test_backfill_today(s, lock, repo):
    add_version(lock, "1.1", V11)
    add_version(lock, "1.2", V12)
    repo.update(log=["Site: dark/light switch (Auto -> Light -> Dark)", "Every round post carries the full market name",
                     "Methodology v1.2: up to 10 rounds a day", "The reveal waits for its live window before kickoff"])
    rec = announce.backfill(s, None, ["1.1", "v1.2"], now=DAY, lock_path=lock)
    assert [v["version"] for v in rec["versions"]] == ["1.1", "1.2"] and rec["kind"] == "backfill"
    assert tweet_len(rec["draft"]) <= 280 and rec["draft"].endswith("/methodology/#v1.2")
    assert "Methodology v1.2: up" not in rec["draft"]
    http = Http()
    assert announce.tick(s, None, http, now=DAY + 1, lock_path=lock) and len(http.calls) == 1
    assert announce.state(s)["last_sent_day"] == "2026-10-13" and announce.state(s)["last_version"] == "1.2"
    assert not announce.tick(s, None, http, now=DAY + 2, lock_path=lock)
    with pytest.raises(ValueError):
        announce.backfill(s, None, ["9.9"], now=DAY, lock_path=lock)


def test_opai_text_is_used_when_valid(s, monkeypatch):
    vers = [{"version": "1.2", "text": V12}]
    url = announce.link(s, vers)
    seen = {}

    def fake_call(s_, db, task, payload, *, subject, validate):
        seen.update(task=task, payload=payload)
        data = {"text": seen["reply"]}
        return None if validate(data) else opai.Result(data=data, output_id=1, model="sonnet")

    monkeypatch.setattr(opai, "call", fake_call)
    seen["reply"] = "AgentpitBench v1.2: up to 10 rounds a day, one per event. A ladder of thresholds is one bet."
    text, source = announce.write(s, object(), vers, ["Site: dark/light switch"])
    assert source == "opai" and text == seen["reply"] + " " + url and seen["task"] == "release_tweet"
    assert seen["payload"]["max_chars"] == 280 - 1 - 23
    for bad in ("Ten rounds a day now.", "v1.2 is out, see https://agentpitbench.org", "v1.2 " + "x" * 260):
        seen["reply"] = bad
        assert announce.write(s, object(), vers, [])[1] == "template"


def test_subject_filters(monkeypatch):
    out = ("\x1eSpec: Threads mirror\ndocs/threads-spec.md\n"
           "\x1eSite: dark/light switch (operator 2026-10-13)\nagentpitbench/site.py\n"
           "\x1eLock tests start from a 1.0 baseline copy\ntests/conftest.py\n"
           "\x1edocs: marketing strategy\ndocs/marketing.md\n"
           "\x1eEvery round post carries the full market name (docs/longtail-search-spec.md)\nagentpitbench/publish.py\n")
    monkeypatch.setattr(announce, "git", lambda *a: out)
    assert announce.changes("a", "b") == ["Site: dark/light switch", "Every round post carries the full market name"]


def test_release_tweet_task_registered():
    assert opai.PROMPTS["release_tweet"] == 1 and "release_tweet" in opai.SCHEMAS
    assert Settings().opai_task_budget["release_tweet"] >= 1
