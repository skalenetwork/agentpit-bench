"""Operator AI ("opai"): MINIMAL SHIM with the ai-automation spec's signature (docs/ai-automation-spec.md section 2).

The full runner (ai_calls budget table, quota backoff, contestant breaker, per-task allocations) is being built on
another branch and replaces this file. Until then this shim gives Match of the Day (`task="motd_script"`) the same
call shape and the same safety rules that matter for it:

- `claude -p` with no tools, no MCP servers, no session persistence, JSON output and a JSON schema; argv only,
  the prompt on stdin, an empty temporary cwd, a minimal environment. It never runs inside a contestant sandbox.
- `available()` refuses while any round is running (its picks are sealed and the Claude contestant shares the
  quota) and inside the daily sweep window, so a script call never competes with a contestant.
- Every answer, accepted or rejected, is stored in `operator_ai_outputs` with model, prompt version, timestamp,
  subject and public=0. Any failure returns None and the caller uses its fallback.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"
SWEEP_GUARD_MIN = 30


@dataclass
class Result:
    data: dict            # validated JSON
    output_id: int        # operator_ai_outputs.output_id
    model: str            # model actually reported (result JSON "modelUsage" key), else the alias asked for


@dataclass
class Task:
    prompt: str                                   # file name under prompts/, e.g. "motd_script.v1.md"
    schema: dict                                  # JSON schema passed to --json-schema
    validate: Callable[[dict, dict], str | None]  # (data, payload) -> reject reason or None
    model: str = "fable"                          # CLI alias; overridden by settings when given


TASKS: dict[str, Task] = {}


def register(name: str, task: Task) -> None:
    TASKS[name] = task


class SealedError(RuntimeError):
    pass


def sealed_guard(db, round_id: int) -> None:
    r = db.one("SELECT state FROM rounds WHERE round_id=?", round_id)
    if r is not None and r["state"] == "running":
        raise SealedError(f"round {round_id} is running: its picks are sealed")


def _in_sweep_window(s, db, now: float) -> bool:
    t = datetime.fromtimestamp(now, timezone.utc)
    day = t.strftime("%Y-%m-%d")
    start = t.replace(hour=s.sweep_hour_utc, minute=0, second=0, microsecond=0).timestamp() - SWEEP_GUARD_MIN * 60
    hours = math.ceil(s.sweep_timeout_s * len(s.agents) / 3600)
    end = start + SWEEP_GUARD_MIN * 60 + hours * 3600 + SWEEP_GUARD_MIN * 60
    if not start <= now < end:
        return False
    return not db.get(f"sweep_{day}")  # the sweep finished early: the window closes


def available(s, db, task: str, now: float | None = None) -> tuple[bool, str]:
    """(ok, reason) without calling: claude on PATH, the task known, no round running, outside the sweep window."""
    if task not in TASKS:
        return False, f"unknown task {task}"
    if not shutil.which(getattr(s, "opai_claude_bin", "claude") or "claude"):
        return False, "claude CLI not found"
    if db.one("SELECT 1 FROM rounds WHERE state='running' LIMIT 1") is not None:
        return False, "a round is running"
    if _in_sweep_window(s, db, now if now is not None else time.time()):
        return False, "inside the forecast sweep window"
    return True, "ok"


def _env(s) -> dict:
    from .orchestrator import ENV_ALLOW
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW}
    env["HOME"] = os.environ.get("HOME", str(Path.home()))
    env["PATH"] = f"/usr/local/bin:/usr/bin:/bin:{Path.home() / '.local' / 'bin'}"
    cfg = getattr(s, "opai_config_dir", "") or ""
    if cfg and Path(cfg).expanduser().is_dir():
        env["CLAUDE_CONFIG_DIR"] = str(Path(cfg).expanduser())
    return env


def _run_cli(s, task: Task, prompt: str, model: str) -> tuple[dict | None, str, str]:
    """(data, model_reported, error). Overridden in tests via `runner`."""
    argv = [getattr(s, "opai_claude_bin", "claude") or "claude", "-p", "--model", model, "--output-format", "json",
            "--json-schema", json.dumps(task.schema), "--tools", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence",
            "--safe-mode"]
    cwd = tempfile.mkdtemp(prefix="opai-")
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, env=_env(s), cwd=cwd,
                           timeout=getattr(s, "opai_timeout_s", 300))
    except subprocess.TimeoutExpired:
        return None, model, "timeout"
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        return None, model, f"crash: exit {p.returncode}: {(p.stderr or p.stdout)[:300]}"
    reported = next(iter(out.get("modelUsage") or {}), None) or model
    if out.get("is_error"):
        return None, reported, f"error: {str(out.get('result'))[:300]}"
    data = out.get("structured_output")
    if data is None:
        try:
            data = json.loads(out.get("result") or "")
        except ValueError:
            return None, reported, "invalid: result is not JSON"
    return (data if isinstance(data, dict) else None), reported, ("" if isinstance(data, dict) else "invalid: not an object")


runner = _run_cli  # tests replace this with a fake


def _store(db, task_name: str, version: str, model: str, data, accepted: bool, reason: str | None,
           subject: str) -> int:
    return db.x("INSERT INTO operator_ai_outputs(purpose,model,prompt_version,created_at,content,accepted,"
                "reject_reason,subject,public) VALUES(?,?,?,?,?,?,?,?,0)",
                task_name, model, version, time.time(), json.dumps(data, ensure_ascii=False), int(accepted),
                reason, subject)


def call(s, db, task: str, payload: dict, *, subject: str) -> Result | None:
    """Run one task. The prompt is prompts/<task file> followed by the payload as JSON. Returns None on any
    failure (unavailable, CLI error, schema or validator rejection); the caller falls back."""
    ok, why = available(s, db, task)
    if not ok:
        log.info("opai %s (%s): unavailable: %s", task, subject, why)
        return None
    t = TASKS[task]
    text = (PROMPTS / t.prompt).read_text()
    version = t.prompt.rsplit(".", 1)[0]  # 'motd_script.v1'
    prompt = text + "\n\n## Input (JSON)\n\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    model = getattr(s, f"opai_model_{task}", None) or t.model
    t0 = time.time()
    try:
        data, reported, err = runner(s, t, prompt, model)
    except Exception as e:  # never let the writer break the caller
        log.exception("opai %s: runner failed", task)
        data, reported, err = None, model, f"crash: {e}"
    log.info("opai %s (%s): %s in %.0fs, sha %s", task, subject, err or "ok", time.time() - t0,
             hashlib.sha256(prompt.encode()).hexdigest()[:12])
    if data is None:
        _store(db, task, version, reported, {"error": err}, False, err[:300], subject)
        return None
    reason = t.validate(data, payload)
    oid = _store(db, task, version, reported, data, reason is None, reason, subject)
    if reason:
        log.warning("opai %s (%s): rejected: %s", task, subject, reason)
        return None
    return Result(data=data, output_id=oid, model=reported)


def last_rejection(db, task: str, subject: str) -> str | None:
    r = db.one("SELECT reject_reason FROM operator_ai_outputs WHERE purpose=? AND subject=? ORDER BY output_id DESC"
               " LIMIT 1", task, subject)
    return r["reject_reason"] if r else None
