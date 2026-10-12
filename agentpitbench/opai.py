"""Operator AI runner: MINIMAL SHIM. The AI-automation build (docs/ai-automation-spec.md section 2) replaces this
module with the full runner (ai_calls rows, a separate login dir, contestant-priority checks, schemas). Only the
signature A/B uses is kept stable: `call` / `acall(s, db, task, payload, *, subject) -> Result | None`.

payload: {"prompt": str, "model": alias, "timeout_s": int, "raw_dir": path}. The prompt goes on stdin; the model
can't use any tool (`--tools ""`), no MCP server, no slash command. Every spawn counts against the task's daily
budget (kv opai_calls_<task>_<day>), before the call, so a crash can't spend the budget twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

SHIM = True
CLAUDE = "claude"


@dataclass
class Result:
    data: dict            # the parsed JSON answer
    output_id: int | None  # operator_ai_outputs row (None: the shim leaves tagging to the caller)
    model: str            # model reported by the CLI's json output, else the alias asked for


def task_budget(s: Settings, task: str) -> int:
    return {"ab": s.ab_gen_max_calls_per_day}.get(task, 0)


def _day(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), timezone.utc).strftime("%Y-%m-%d")


def available(s: Settings, db: DB, task: str, now: float | None = None, need_cli: bool = True) -> tuple[bool, str]:
    if need_cli and not shutil.which(CLAUDE):
        return False, "claude not on PATH"
    used = db.get(f"opai_calls_{task}_{_day(now)}", 0)
    if used >= task_budget(s, task):
        return False, f"daily budget spent ({used}/{task_budget(s, task)})"
    return True, ""


def argv(model: str) -> list[str]:
    return [CLAUDE, "-p", "--output-format", "json", "--model", model, "--tools", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
            "--no-session-persistence"]


def parse(stdout: str, alias: str) -> tuple[dict | None, str]:
    """(answer JSON, model) from `claude -p --output-format json`; the answer is the JSON object in `result`."""
    try:
        d = json.loads(stdout)
    except ValueError:
        return None, alias
    if isinstance(d, list):   # some versions print the event list
        d = next((x for x in reversed(d) if isinstance(x, dict) and x.get("type") == "result"), {})
    model = next(iter(d.get("modelUsage") or {}), None) or d.get("model") or alias
    if d.get("is_error"):
        return None, model
    if isinstance(d.get("structured_output"), dict):
        return d["structured_output"], model
    text = d.get("result") or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None, model
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return None, model
    return (out if isinstance(out, dict) else None), model


async def _run(cmd: list[str], stdin: str, cwd: Path, timeout: float) -> tuple[int | None, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTPIT_KEY_", "X_", "CF_"))}
    p = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                             start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(p.communicate(stdin.encode()), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(p.pid, 9)
        except ProcessLookupError:
            pass
        await p.wait()
        return None, "timeout"
    return p.returncode, out.decode(errors="replace")


async def acall(s: Settings, db: DB, task: str, payload: dict, *, subject: str, runner=None,
                now: float | None = None) -> Result | None:
    """One call, or None on any failure (unavailable, timeout, non-zero exit, unparseable answer)."""
    ok, why = available(s, db, task, now, need_cli=runner is None)
    if not ok:
        log.info("opai %s (%s) unavailable: %s", task, subject, why)
        return None
    k = f"opai_calls_{task}_{_day(now)}"
    db.put(k, db.get(k, 0) + 1)
    raw_dir = Path(payload.get("raw_dir") or s.data_dir / "opai" / task)
    raw_dir.mkdir(parents=True, exist_ok=True)
    alias = payload.get("model") or "sonnet"
    rc, out = await (runner or _run)(argv(alias), payload["prompt"], raw_dir, payload.get("timeout_s", 180))
    (raw_dir / f"out-{int(time.time())}.txt").write_text(out or "")
    if rc != 0:
        log.warning("opai %s (%s) failed: rc=%s %s", task, subject, rc, (out or "")[-300:])
        return None
    data, model = parse(out, alias)
    if data is None:
        log.warning("opai %s (%s): no JSON answer", task, subject)
        return None
    return Result(data, None, model)


def call(s: Settings, db: DB, task: str, payload: dict, *, subject: str, runner=None) -> Result | None:
    return asyncio.run(acall(s, db, task, payload, subject=subject, runner=runner))
