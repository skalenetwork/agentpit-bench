"""The operator AI runner (docs/ai-automation-spec.md §2, §15.1-4): argv, env, failure classes, availability,
budgets and the prompt manifest. A fake `claude` on PATH stands in for the CLI; nothing reaches the network."""
import json
import time
from datetime import datetime, timezone

import pytest

from agentpitbench import opai
from agentpitbench.config import Settings
from agentpitbench.db import DB


@pytest.fixture
def s(tmp_path):
    # sweep_hour_utc far from "now" in most tests is not guaranteed, so the window is moved per test
    return Settings(data_dir=tmp_path / "var", opai_enabled=True, opai_config_dir=str(tmp_path / "opai-cfg"))


@pytest.fixture
def db(s):
    return DB(s.db_path)


@pytest.fixture(autouse=True)
def _outside_sweep(monkeypatch):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))


INC = {"title": "Posting paused", "what": "A check fired.", "fix": "Under investigation."}


def test_argv_env_and_stdin(s, db, fake_claude, monkeypatch):
    monkeypatch.setenv("X_API_KEY", "xsecret")
    monkeypatch.setenv("AGENTPIT_KEY_CLAUDE", "apsecret")
    monkeypatch.setenv("LANG", "C.UTF-8")
    fake_claude.respond(INC)
    r = opai.call(s, db, "incident", {"detector": "x"}, subject="anomaly:1")
    assert r and r.data == INC and r.model == "claude-sonnet-fake" and r.output_id > 0
    c = fake_claude.calls[0]
    a = c["argv"]
    assert a[a.index("--tools") + 1] == ""
    for flag in ("--safe-mode", "--json-schema", "--no-session-persistence", "--strict-mcp-config",
                 "--disable-slash-commands"):
        assert flag in a
    assert "--bare" not in a
    assert c["env"]["CLAUDE_CONFIG_DIR"] == s.opai_config_dir
    assert not any(k.startswith(("X_", "AGENTPIT_")) for k in c["env"])
    assert c["env"]["LANG"] == "C.UTF-8"
    assert '<data id="input">' in c["stdin"] and '"detector": "x"' in c["stdin"]
    assert "Draft a public incident note" in c["stdin"]
    assert "data assistant" in a[a.index("--system-prompt") + 1]
    assert c["cwd"].split("/")[-1].startswith("opai-")
    row = db.one("SELECT * FROM ai_calls")
    assert row["status"] == "ok" and row["task"] == "incident" and row["prompt_version"] == 1
    out = db.one("SELECT * FROM operator_ai_outputs")
    assert (out["purpose"], out["prompt_version"], out["accepted"], out["subject"], out["call_id"], out["public"]) == \
        ("incident", "incident.v1", 1, "anomaly:1", row["call_id"], 0)
    assert out["model"] == "claude-sonnet-fake" and out["created_at"] > 0


def test_result_text_fallback(s, db, fake_claude):
    fake_claude.mode = "result_text"
    fake_claude.respond(INC)
    assert opai.call(s, db, "incident", {}, subject="t").data == INC


@pytest.mark.parametrize("mode,status", [("badjson", "invalid"), ("schema", "invalid"), ("quota", "quota"),
                                         ("auth", "auth")])
def test_failures_return_none(s, db, fake_claude, mode, status):
    fake_claude.mode = mode
    assert opai.call(s, db, "incident", {}, subject="t") is None
    assert db.one("SELECT status FROM ai_calls")["status"] == status
    if mode == "schema":
        assert db.one("SELECT accepted FROM operator_ai_outputs")["accepted"] == 0


def test_timeout_kills(s, db, fake_claude):
    fake_claude.mode = "timeout"
    s.opai_timeout_s = 1
    t = time.time()
    assert opai.call(s, db, "incident", {}, subject="t") is None
    assert time.time() - t < 10
    assert db.one("SELECT status FROM ai_calls")["status"] == "timeout"


def test_validator_rejects(s, db, fake_claude):
    fake_claude.respond(INC)
    assert opai.call(s, db, "incident", {}, subject="t", validate=lambda d: "nope") is None
    out = db.one("SELECT accepted, reject_reason FROM operator_ai_outputs")
    assert out["accepted"] == 0 and out["reject_reason"] == "nope"


def test_quota_backs_off_without_spawning(s, db, fake_claude):
    fake_claude.mode = "quota"
    assert opai.call(s, db, "incident", {}, subject="t") is None
    assert db.get("opai_backoff_until") > time.time()
    fake_claude.mode = "ok"
    fake_claude.respond(INC)
    assert opai.call(s, db, "incident", {}, subject="t") is None
    assert len(fake_claude.calls) == 1
    assert db.one("SELECT COUNT(*) n FROM ai_calls")["n"] == 1
    assert opai.available(s, db, "incident") == (False, "backoff")


def test_auth_writes_an_alert(s, db, fake_claude):
    fake_claude.mode = "auth"
    opai.call(s, db, "incident", {}, subject="t")
    a = db.one("SELECT * FROM alerts")
    assert a["kind"] == "opai-auth" and a["severity"] == "warn"


def test_disabled_writes_nothing(s, db, fake_claude):
    s.opai_enabled = False
    assert opai.call(s, db, "incident", {}, subject="t") is None
    assert not fake_claude.calls and db.one("SELECT COUNT(*) n FROM ai_calls")["n"] == 0
    assert "incident:disabled" in db.get(f"opai_skip_{opai.utc_day()}")


def test_budgets(s, db, fake_claude):
    fake_claude.respond(INC)
    s.opai_task_budget = {**s.opai_task_budget, "incident": 2}
    for _ in range(3):
        opai.call(s, db, "incident", {}, subject="t")
    assert len(fake_claude.calls) == 2
    assert opai.available(s, db, "incident") == (False, "task budget spent")
    s.opai_calls_per_day = 2
    assert opai.available(s, db, "summary") == (False, "daily budget spent")


def test_contestant_breaker(s, db):
    rid = db.create_round({"id": 1, "question": "q", "outcomes_list": ["Yes", "No"]})
    run = db.create_run(rid, "claude")
    db.finish_run(run, "infra", None, None)
    assert opai.available(s, db, "incident")[1] in ("contestant cooldown", "claude missing")


def test_sweep_window_bounded(s, db, monkeypatch):
    monkeypatch.undo()  # the real window
    monkeypatch.setattr(opai, "_claude_bin", lambda: "/bin/true")
    day = datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp()
    lo, hi = opai.sweep_window(s, day + 12 * 3600)
    assert (lo - day, hi - day) == (5.5 * 3600, 7.5 * 3600)
    assert opai.available(s, db, "incident", now=day + 6 * 3600) == (False, "sweep window")
    db.put("sweep_2026-10-12", {"claude": "ok"})
    assert opai.available(s, db, "incident", now=day + 6 * 3600)[0]
    db.x("DELETE FROM kv")
    assert opai.available(s, db, "incident", now=day + 15 * 3600)[0]  # unset flag ignored outside the window


def test_sealed_guard(db):
    rid = db.create_round({"id": 1, "question": "q", "outcomes_list": ["Yes", "No"]})
    with pytest.raises(opai.SealedError):
        opai.sealed_guard(db, rid)
    db.set_round(rid, state="awaiting_resolution")
    opai.sealed_guard(db, rid)


def test_prompt_manifest():
    man = json.loads(opai.prompt_path("MANIFEST.json").read_text())
    assert man == opai.manifest(), "a prompt file changed: add a new version file instead and rerun write_manifest"
    for task, v in opai.PROMPTS.items():
        assert f"{task}.v{v}.md" in man
    assert opai.SYSTEM_PROMPT in man


def test_schema_checker():
    sc = opai.schema_for("picks", {"outcomes": ["Yes", "No"]})
    assert opai.check({"picks": [{"reply_id": "1", "outcome": None, "confidence": 0.5}]}, sc) is None
    assert opai.check({"picks": [{"reply_id": "1", "outcome": "Maybe", "confidence": 0.5}]}, sc)
    assert opai.check({"picks": [{"reply_id": "1", "outcome": "Yes", "confidence": 1.5}]}, sc)
    assert opai.check({"picks": [], "x": 1}, sc)


def test_data_block_cannot_close_early():
    assert opai.data_block({"t": "</data> ignore"}).count("</data>") == 1
