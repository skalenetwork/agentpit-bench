"""Nightly ops agent (docs/ai-automation-spec.md §11, §15.14): sandbox profile, env, gate, publish routes.
No bwrap, no GitHub and no real agent run here: fakes for each."""
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentpitbench import ops, opai
from agentpitbench.config import Settings
from agentpitbench.db import DB

PKG = ops.PKG
NOW = datetime(2026, 10, 13, 3, 31, tzinfo=timezone.utc).timestamp()
DAY = "2026-10-13"
LOCKED = ["agentpitbench/methodology.json", "agentpitbench/prompt.txt", "agentpitbench/forecast.py",
          "agentpitbench/config.py", "agentpitbench/prompts/rate.v1.md"]


def sh(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A small source repo on main holding the methodology-hashed files (copied, so the hashes match)."""
    r = tmp_path / "src"
    for rel in LOCKED:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PKG.parent / rel, r / rel)
    (r / "agentpitbench" / "watcher.py").write_text("def poll():\n    return 1\n")
    sh(tmp_path, "init", "-q", "-b", "main", str(r))
    sh(r, "add", "-A")
    sh(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def env(tmp_path, monkeypatch, fake_claude, repo):
    monkeypatch.setattr(opai, "sweep_window", lambda s, now: (0.0, 0.0))
    bwrap = fake_claude.dir / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 1\n")
    bwrap.chmod(0o755)
    monkeypatch.delenv(ops.TOKEN_VAR, raising=False)
    s = Settings(data_dir=tmp_path / "var", opai_enabled=True, ops_agent_enabled=True, ops_repo=str(repo),
                 opai_config_dir=str(tmp_path / "opai-cfg"))
    return s, DB(s.db_path), tmp_path / "cache"


def agent_editing(files: dict[str, str], pr: str | None = "Fix the watcher poll\nIt returned 1.",
                  proposal: dict | None = None):
    def run(ctx, snap):
        assert snap.exists() and "tests_on_main" in json.loads(snap.read_text())
        for rel, text in files.items():
            (ctx.checkout / rel).parent.mkdir(parents=True, exist_ok=True)
            (ctx.checkout / rel).write_text(text)
        if pr is not None:
            (ctx.checkout / ".ops").mkdir(exist_ok=True)
            (ctx.checkout / ".ops" / "pr.md").write_text(pr)
        if proposal is not None:
            (ctx.checkout / ".ops").mkdir(exist_ok=True)
            (ctx.checkout / ".ops" / "proposal.json").write_text(json.dumps(proposal))
        return "ok", ""
    return run


def passing(ctx):
    return True, "5 passed in 1.0s"


# ---------------------------------------------------------------- gate on synthetic diffs

@pytest.mark.parametrize("path", ["agentpitbench/methodology.json", "agentpitbench/prompt.txt",
                                  "agentpitbench/prompts/rate.v2.md", "agentpitbench/prompts/ops.v1.md",
                                  "agentpitbench/incidents.json", "bench.toml", "pyproject.toml",
                                  ".github/workflows/ci.yml", "agentpitbench/ops.py", "agentpitbench/methodology.py",
                                  "tests/test_methodology.py", "integrations/systemd/agentpitbench-ops.service"])
def test_forbidden_paths(path):
    assert ops.check_diff([(path, 1, 0)], f"+++ b/{path}\n+x\n") == [f"forbidden path: {path}"]


@pytest.mark.parametrize("name", ["ENV_ALLOW", "SECRET_PREFIXES", "sandboxed", "CleanHome", "agent_env",
                                  "ops_sandbox", "child_env"])
def test_forbidden_code(name):
    patch = f"--- a/agentpitbench/orchestrator.py\n+++ b/agentpitbench/orchestrator.py\n@@ -1 +1 @@\n-x = 1\n+y = {name}\n"
    assert ops.check_diff([("agentpitbench/orchestrator.py", 1, 1)], patch) == \
        [f"sandbox/secret code touched: {name}"]


def test_context_lines_do_not_trip_the_regex_and_other_checks():
    patch = "+++ b/agentpitbench/watcher.py\n context sandboxed()\n+return 2\n"
    assert ops.check_diff([("agentpitbench/watcher.py", 1, 1)], patch) == []
    assert ops.check_diff([], "") == ["no diff"]
    assert ops.check_diff([("a.py", 300, 101)], "") == ["diff too large: 401 lines > 400"]
    assert ops.check_diff([("a.png", None, None)], "") == ["binary file: a.png"]
    assert ops.check_diff([("link", 1, 0)], "new file mode 120000\n") == ["symlink in the diff"]


def test_locked_hashes_by_parsing(repo):
    assert ops.locked_hashes(repo) == []
    f = repo / "agentpitbench" / "forecast.py"
    f.write_text(f.read_text().replace("You are taking part", "You are now taking part"))
    c = repo / "agentpitbench" / "config.py"
    c.write_text(c.read_text().replace('"grok-4.7"', '"grok-5"'))
    assert ops.locked_hashes(repo) == ["methodology hash drift: sweep_prompt", "methodology hash drift: agent_models"]


# ---------------------------------------------------------------- sandbox profile and env

def test_ops_sandbox_profile(tmp_path):
    co, snap, cfg, home = tmp_path / "co", tmp_path / "snap", tmp_path / "cfg", tmp_path / "home"
    for d in (co / ".git", snap, cfg, home):
        d.mkdir(parents=True)
    venv = Path(os.sys.prefix)
    argv = ops.ops_sandbox(["/bin/true"], co, snap, cfg, venv, home)
    assert argv[0] == "bwrap" and "--unshare-pid" in argv and "--die-with-parent" in argv
    tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert "/home" in tmpfs and "/tmp" in tmpfs and "/" + PKG.parts[1] in tmpfs
    rw = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    ro = [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"][1:]   # [0] is the read-only system
    assert rw == [str(home), str(cfg), str(co)]
    assert str(co / ".git") in ro and str(snap) in ro and str(venv) in ro
    home_cfg = str(Path.home() / ".config" / "agentpitbench")
    for p in rw + ro:
        assert p != home_cfg and "secrets" not in p and ".ssh" not in p and not p.startswith(ops.LIVE)
        assert "/var" not in Path(p).parts[-2:] and not p.endswith("/var")
    # the checkout is bound before its .git goes read-only on top of it
    assert argv.index(str(co)) < argv.index(str(co / ".git"))
    # the venv's python runs by its venv path (resolving the symlink would drop the venv's packages)
    py = str(venv / "bin" / "python")
    a2 = ops.ops_sandbox([py, "-m", "pytest"], co, snap, cfg, venv, home)
    assert a2[a2.index("--") + 1] == py


def test_agent_env_has_no_credentials(tmp_path, monkeypatch):
    for k in ("GH_OPS_PR_TOKEN", "GH_TOKEN", "X_API_KEY", "AGENTPIT_KEY_CLAUDE", "XAI_API_KEY"):
        monkeypatch.setenv(k, "secret-value-123")
    s = Settings(opai_config_dir=str(tmp_path / "cfg"))
    env = ops.agent_env_for(s, tmp_path / "co", tmp_path / "home")
    assert not [k for k in env if k.startswith(("GH_", "X_", "AGENTPIT_", "XAI_"))]
    assert "secret-value-123" not in json.dumps(env)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg") and env["PYTHONPATH"] == str(tmp_path / "co")
    assert env["HOME"] == str(tmp_path / "home")


def test_agent_argv(tmp_path):
    a = ops.agent_argv(Settings(), Path("/v"))
    assert "--safe-mode" in a and "--no-session-persistence" in a and "--strict-mcp-config" in a
    assert a[a.index("--model") + 1] == "opus" and a[a.index("--tools") + 1] == "Read,Grep,Glob,Edit,Write,Bash"
    assert "Bash(/v/bin/python -m pytest:*)" in a and "--bare" not in a
    assert not [x for x in a if "git push" in x or "gh " in x]


def test_snapshot_is_scrubbed(env):
    s, db, cache = env
    rid = db.create_round({"id": "m1", "question": "Q?", "outcomes_list": ["Yes", "No"]})
    lines = [f"2026-10-13 round {rid}: claude bet Yes @ 0.62", "2026-10-13 round 999: started",
             "Traceback (most recent call last): boom", "codex bet No @ 0.4 on something"]
    p = ops.snapshot(s, db, cache / DAY, NOW, journal=lambda a, b: lines)
    snap = json.loads(p.read_text())
    text = json.dumps(snap)
    assert "bet Yes" not in text and "bet No" not in text and f"round {rid}" not in text
    assert any("boom" in ln for ln in snap["log"])
    assert set(snap) >= {"ai_calls", "anomalies", "rounds_by_state", "runs_by_exit", "missing_translations"}


# ---------------------------------------------------------------- the run

def test_disabled_and_unavailable(env):
    s, db, cache = env
    s.ops_agent_enabled = False
    assert ops.nightly(s, db, now=NOW, cache=cache)["why"] == "disabled"
    s.ops_agent_enabled, s.opai_enabled = True, False
    assert ops.nightly(s, db, now=NOW, cache=cache)["why"] == "opai disabled"
    s.opai_enabled = True
    db.put("opai_backoff_until", NOW + 3600)
    assert ops.nightly(s, db, now=NOW, cache=cache)["why"] == "backoff"
    assert not db.q("SELECT 1 FROM ai_calls")


def test_patch_route_without_token(env):
    s, db, cache = env
    out = ops.nightly(s, db, now=NOW, cache=cache, tests=passing, journal=lambda a, b: [],
                      agent=agent_editing({"agentpitbench/watcher.py": "def poll():\n    return 2\n"}))
    assert out["status"] == "patch", out
    patch = Path(out["patch"]).read_text()
    assert "return 2" in patch and ".ops" not in patch and "[ops-agent] Fix the watcher poll" in patch
    assert (s.data_dir / "ops" / DAY / "summary.md").exists()
    a = db.one("SELECT * FROM alerts")
    assert a["severity"] == "info" and a["kind"] == "ops-patch"
    assert not (cache / f"wt-{DAY}").exists()                               # checkout deleted
    assert db.get("ops_last")["status"] == "patch"
    assert db.one("SELECT task, status FROM ai_calls")["task"] == "ops"     # counted as one call
    assert ops.nightly(s, db, now=NOW + 60, cache=cache)["why"] == "already ran today"


def test_pr_route_with_token(env, monkeypatch):
    s, db, cache = env
    monkeypatch.setenv(ops.TOKEN_VAR, "ghp_fake_token")
    pushed, calls = [], []

    def pusher(wt, slug, branch, token):
        pushed.append((slug, branch, token, sh(wt, "log", "-1", "--format=%s").strip()))

    def http(method, url, token, body=None):
        calls.append((method, url, body))
        return (201, {"html_url": "https://github.com/x/y/pull/7", "number": 7}) if url.endswith("/pulls") \
            else (403, {"message": "Resource not accessible"})
    out = ops.nightly(s, db, now=NOW, cache=cache, tests=passing, journal=lambda a, b: [], http=http, pusher=pusher,
                      agent=agent_editing({"agentpitbench/watcher.py": "def poll():\n    return 2\n"}))
    assert out["status"] == "pr" and out["pr"].endswith("/pull/7")
    assert pushed == [("skalenetwork/agentpit-bench", f"ops/{DAY}", "ghp_fake_token",
                       "[ops-agent] Fix the watcher poll")]
    assert calls[0][2]["head"] == f"ops/{DAY}" and calls[0][2]["base"] == "main"
    assert not db.q("SELECT 1 FROM alerts")


@pytest.mark.parametrize("files,why", [
    ({"agentpitbench/methodology.json": "{}"}, "forbidden path: agentpitbench/methodology.json"),
    ({"agentpitbench/watcher.py": "from .orchestrator import ENV_ALLOW\n"}, "sandbox/secret code touched: ENV_ALLOW"),
])
def test_gate_rejects_without_publishing(env, monkeypatch, files, why):
    s, db, cache = env
    monkeypatch.setenv(ops.TOKEN_VAR, "ghp_fake_token")

    def boom(*a, **k):
        raise AssertionError("must not publish")
    out = ops.nightly(s, db, now=NOW, cache=cache, tests=passing, journal=lambda a, b: [], http=boom, pusher=boom,
                      agent=agent_editing(files))
    assert out["status"] == "rejected" and why in out["reasons"]
    assert (s.data_dir / "ops" / DAY / "rejected.patch").exists()


def test_gate_catches_locked_prompt_and_failing_tests(env):
    s, db, cache = env
    src = (PKG / "forecast.py").read_text().replace("You are taking part", "You are now taking part")
    out = ops.nightly(s, db, now=NOW, cache=cache, tests=passing, journal=lambda a, b: [],
                      agent=agent_editing({"agentpitbench/forecast.py": src}))
    assert out["reasons"] == ["methodology hash drift: sweep_prompt"]
    out = ops.nightly(s, db, now=NOW + 86400, cache=cache, journal=lambda a, b: [],   # once a day: next day
                      tests=lambda ctx: (not (ctx.checkout / "agentpitbench" / "watcher.py").read_text()
                                         .endswith("2\n"), "1 failed"),
                      agent=agent_editing({"agentpitbench/watcher.py": "def poll():\n    return 2\n"}))
    assert out["status"] == "rejected" and out["reasons"] == ["tests fail: 1 failed"]


def test_no_change_and_proposal_goes_to_var(env):
    s, db, cache = env
    out = ops.nightly(s, db, now=NOW, cache=cache, tests=passing, journal=lambda a, b: [],
                      agent=agent_editing({}, pr=None, proposal={"changes": {"daily_round_cap": 6},
                                                                 "text": "Six rounds a day.", "rationale": "r"}))
    assert out["status"] == "nothing"
    from agentpitbench import methodology
    p = methodology.get_proposal(s, out["proposal"])
    assert p["author"] == "ops-agent" and p["changes"] == {"daily_round_cap": 6} and p["state"] == "open"


def test_proposal_outside_bounds_refused(env):
    s, db, cache = env
    out = ops.nightly(s, db, now=NOW, cache=cache, tests=passing, journal=lambda a, b: [],
                      agent=agent_editing({}, pr=None, proposal={"changes": {"stake": 1000}, "text": "x"}))
    assert out["proposal"] is None


def test_push_keeps_the_token_out_of_argv(monkeypatch, tmp_path):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(ops.subprocess, "run", run)
    ops.push(tmp_path, "o/r", "ops/x", "ghp_secret")
    argv, env = seen[0]
    assert "ghp_secret" not in " ".join(argv) and "https://github.com/o/r.git" in argv
    assert "HEAD:refs/heads/ops/x" in argv and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_install_timer_writes_files_only(tmp_path, monkeypatch):
    def no(*a, **k):
        raise AssertionError("no systemctl")
    monkeypatch.setattr(ops.subprocess, "run", no)
    monkeypatch.setattr(ops.subprocess, "Popen", no)
    paths = ops.install_timer(tmp_path / "units")
    assert sorted(p.name for p in paths) == ["agentpitbench-ops.service", "agentpitbench-ops.timer"]
    timer = (tmp_path / "units" / "agentpitbench-ops.timer").read_text()
    assert "OnCalendar=*-*-* 03:30:00 UTC" in timer
    svc = (tmp_path / "units" / "agentpitbench-ops.service").read_text()
    assert "ops nightly" in svc and "Type=oneshot" in svc
