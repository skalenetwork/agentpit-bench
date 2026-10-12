"""The operator AI ("opai", docs/ai-automation-spec.md §2): one `claude -p` call per task, on its own login,
with no tools, a JSON schema and a code validator. It never blocks a round or a post: every failure returns
None and the caller keeps its rule-based fallback. It is never a contestant and never sees a sealed pick.

Every call that spawns a process writes an `ai_calls` row (the budget counts these). Every output, accepted or
rejected, is tagged in `operator_ai_outputs` with task, model, prompt version, subject and timestamp.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

# task -> active prompt version (prompts/<task>.v<N>.md). `rate` is methodology: its version comes from
# methodology.json (selection.rater.prompt_version), never from here.
PROMPTS = {"rate": 1, "picks": 1, "summary": 1, "report": 1, "incident": 1, "cadence": 1, "i18n": 1,
           "proposal": 1, "translate": 1, "ab": 1}
SYSTEM_PROMPT = "system.v1.md"
LIGHT_TASKS = {"picks"}
CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"
FAILED = ("quota", "auth", "timeout", "invalid", "crash")


class SealedError(RuntimeError):
    """A prompt was about to be built from a round whose picks are still secret."""


@dataclass
class Result:
    data: dict            # validated JSON
    output_id: int        # operator_ai_outputs.output_id
    model: str            # model the CLI reported, else the alias asked for
    call_id: int = 0


# ---------------------------------------------------------------- prompts

def prompt_path(name: str) -> Path:
    return Path(str(resources.files("agentpitbench").joinpath(f"prompts/{name}")))


def prompt_version(task: str) -> int:
    if task == "rate":
        try:
            from . import methodology
            return int(methodology.load()["selection"]["rater"]["prompt_version"])
        except Exception:
            return PROMPTS["rate"]
    return PROMPTS[task]


def prompt_text(task: str, version: int | None = None) -> str:
    return prompt_path(f"{task}.v{version or prompt_version(task)}.md").read_text("utf-8")


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def data_block(payload) -> str:
    """Untrusted material, wrapped so it reads as data. A literal closing tag inside can't end the block."""
    body = json.dumps(payload, ensure_ascii=False, indent=1).replace("</data", "<\\/data")
    return f'<data id="input">\n{body}\n</data>'


# ---------------------------------------------------------------- schemas (stdlib checker, no dependency)

def _obj(props: dict, required: list[str] | None = None, extra=False) -> dict:
    return {"type": "object", "properties": props, "required": list(props) if required is None else required,
            "additionalProperties": extra}


_STR = {"type": "string"}


def _translate_schema(payload: dict) -> dict:
    n = len(payload["texts"])
    arr = {"type": "array", "items": _STR, "minItems": n, "maxItems": n}
    return _obj({lang: arr for lang in payload["langs"]})


def _picks_schema(payload: dict) -> dict:
    return _obj({"picks": {"type": "array", "items": _obj({
        "reply_id": _STR,
        "outcome": {"anyOf": [{"type": "string", "enum": list(payload["outcomes"])}, {"type": "null"}]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}})}})


def _summary_schema(payload: dict) -> dict:
    line = {"type": "string", "maxLength": 200}
    return _obj({"agents": _obj({a["agent"]: _obj({"line1": line, "line2": line}) for a in payload["agents"]},
                                required=[])})


SCHEMAS: dict[str, dict | Callable[[dict], dict]] = {
    "rate": _obj({"ratings": {"type": "array", "items": _obj({
        "market_id": _STR, "score": {"type": "integer", "minimum": 0, "maximum": 100}, "reason": _STR})}}),
    "picks": _picks_schema,
    "summary": _summary_schema,
    "report": _obj({"headline": _STR, "paragraphs": {"type": "array", "items": _STR, "minItems": 2, "maxItems": 4}}),
    "incident": _obj({"title": _STR, "what": _STR, "fix": _STR}),
    "cadence": _obj({"policy": {"type": "object"}, "reason": _STR}),
    "i18n": _obj({"translations": {"type": "object", "additionalProperties": _STR}}),
    "proposal": {"type": "object", "properties": {   # selection_review.validate enforces none-or-one-change
        "none": _STR, "changes": {"type": "object"}, "rationale": _STR, "evidence": _STR, "changelog_text": _STR},
        "required": [], "additionalProperties": False},
    "translate": _translate_schema,
    # A/B challenger templates (ab-testing-spec.md §6): {slot: [{"situations": {...}} | {"text": "..."}]};
    # ab.validate checks each candidate
    "ab": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "object"}}},
}


def schema_for(task: str, payload: dict) -> dict:
    sc = SCHEMAS[task]
    return sc(payload) if callable(sc) else sc


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def check(value, schema: dict, path: str = "$") -> str | None:
    """The JSON Schema subset the task schemas use. Returns the first violation, or None."""
    if "anyOf" in schema:
        return None if any(check(value, s, path) is None for s in schema["anyOf"]) else f"{path}: no anyOf match"
    t = schema.get("type")
    if t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{path}: not an integer"
    elif t == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            return f"{path}: not a number"
    elif t and not isinstance(value, _TYPES[t]):
        return f"{path}: not {t}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: not one of the allowed values"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path}: below minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path}: above maximum"
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        return f"{path}: too long"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return f"{path}: wrong number of items"
        for i, v in enumerate(value):
            if "items" in schema and (err := check(v, schema["items"], f"{path}[{i}]")):
                return err
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                return f"{path}: missing {k}"
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                if err := check(v, props[k], f"{path}.{k}"):
                    return err
            elif extra is False:
                return f"{path}: unexpected key {k}"
            elif isinstance(extra, dict) and (err := check(v, extra, f"{path}.{k}")):
                return err
    return None


# ---------------------------------------------------------------- state: pause, sealed rounds, availability

def paused(s: Settings, db: DB, what: str) -> bool:
    """Posting or new rounds paused: by an anomaly or the operator (kv 'pause'), or, for posting, by the static
    posting_paused flag. The pause stays until the operator resumes it."""
    if what == "posting" and s.posting_paused:
        return True
    return bool((db.get("pause") or {}).get(what))


def sealed_guard(db: DB, round_id: int) -> None:
    r = db.one("SELECT state FROM rounds WHERE round_id=?", round_id)
    if r is None or r["state"] == "running":
        raise SealedError(f"round {round_id} is still running (or unknown): its picks are sealed")


def utc_day(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime("%Y-%m-%d")


def sweep_window(s: Settings, now: float) -> tuple[float, float]:
    d = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    start = d + s.sweep_hour_utc * 3600 - s.opai_sweep_guard_min * 60
    hours = math.ceil(s.sweep_timeout_s * max(1, len(s.agents)) / 3600)
    return start, d + (s.sweep_hour_utc + hours) * 3600 + s.opai_sweep_guard_min * 60


def calls_today(db: DB, task: str | None = None, day: str | None = None) -> int:
    day = day or utc_day()
    if task:
        return db.one("SELECT COUNT(*) n FROM ai_calls WHERE day=? AND task=?", day, task)["n"]
    return db.one("SELECT COUNT(*) n FROM ai_calls WHERE day=?", day)["n"]


def available(s: Settings, db: DB, task: str, now: float | None = None) -> tuple[bool, str]:
    """(ok, reason) without calling. Contestants come first: opai shares the Claude contestant's quota."""
    now = time.time() if now is None else now
    if task == "ab":  # the A/B generator keeps its own switch (ab_gen_enabled), shares everything else
        if not getattr(s, "ab_gen_enabled", False):
            return False, "disabled"
    elif not s.opai_enabled:
        return False, "disabled"
    elif task not in s.opai_tasks:
        return False, "task disabled"
    if task not in PROMPTS:
        return False, "unknown task"
    if not _claude_bin():
        return False, "claude missing"
    if (db.get("opai_backoff_until") or 0) > now:
        return False, "backoff"
    last = db.last_infra_failure()
    if last and now - last < s.infra_cooldown_s:
        return False, "contestant cooldown"
    lo, hi = sweep_window(s, now)
    if lo <= now < hi and not db.get(f"sweep_{utc_day(now)}"):
        return False, "sweep window"
    day = utc_day(now)
    if calls_today(db, None, day) >= s.opai_calls_per_day:
        return False, "daily budget spent"
    if calls_today(db, task, day) >= int(s.opai_task_budget.get(task, 0)):
        return False, "task budget spent"
    return True, "ok"


def _skip(db: DB, task: str, reason: str) -> None:
    """No process, no ai_calls row: logged once per (task, reason, UTC day)."""
    key = f"opai_skip_{utc_day()}"
    seen = db.get(key) or []
    tag = f"{task}:{reason}"
    if tag not in seen:
        db.put(key, [*seen, tag])
        log.info("opai %s skipped: %s (fallback used)", task, reason)


def _claude_bin() -> str | None:
    return shutil.which("claude")


# ---------------------------------------------------------------- the call

_lock = threading.Lock()
_asem: asyncio.Semaphore | None = None


def child_env(s: Settings, config_dir: str | None = None) -> dict:
    from .orchestrator import ENV_ALLOW, SECRET_PREFIXES
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW and not k.startswith(SECRET_PREFIXES)}
    home = os.path.expanduser("~")
    env.update(HOME=home, PATH=f"{CHILD_PATH}:{home}/.local/bin")
    cdir = config_dir if config_dir is not None else s.opai_config_dir
    if cdir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(cdir)
    return env


def argv(s: Settings, task: str, schema: dict, model: str | None = None) -> list[str]:
    system = prompt_path(SYSTEM_PROMPT).read_text("utf-8").strip()
    return [_claude_bin() or "claude", "-p", "--model", model or model_for(s, task), "--output-format", "json",
            "--json-schema", json.dumps(schema), "--tools", "", "--strict-mcp-config", "--mcp-config",
            '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence", "--safe-mode",
            "--system-prompt", system]


def model_for(s: Settings, task: str) -> str:
    if task == "rate":
        try:
            from . import methodology
            return methodology.load()["selection"]["rater"]["model"]
        except Exception:
            return s.opai_model
    if task == "ab":
        return getattr(s, "ab_gen_model", s.opai_model)
    return s.opai_model_light if task in LIGHT_TASKS else s.opai_model


def _redact(text: str) -> str:
    from .virality import redact
    return redact(text or "")[:300]


def _parse(stdout: str) -> tuple[dict | None, str | None, bool]:
    """(data, reported model, is_error) from `--output-format json`."""
    try:
        obj = json.loads(stdout.strip().splitlines()[-1] if stdout.strip() else "")
    except (ValueError, IndexError):
        try:
            obj = json.loads(stdout)
        except ValueError:
            return None, None, False
    if not isinstance(obj, dict):
        return None, None, False
    model = next(iter(obj.get("modelUsage") or {}), None) or obj.get("model")
    data = obj.get("structured_output")
    if data is None and isinstance(obj.get("result"), str):
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", obj["result"].strip())
        try:
            data = json.loads(txt)
        except ValueError:
            data = None
    return (data if isinstance(data, dict) else None), model, bool(obj.get("is_error"))


def tag(db: DB, task: str, model: str, version: int, content, accepted: bool, reason: str | None,
        call_id: int | None, subject: str | None, public: bool = False) -> int:
    return db.x("INSERT INTO operator_ai_outputs(purpose,model,prompt_version,created_at,content,accepted,"
                "reject_reason,call_id,subject,public) VALUES(?,?,?,?,?,?,?,?,?,?)",
                task, model, f"{task}.v{version}", time.time(), json.dumps(content, ensure_ascii=False),
                int(accepted), reason, call_id, subject, int(public))


def call(s: Settings, db: DB, task: str, payload: dict, *, subject: str,
         validate: Callable[[dict], str | None] | None = None, force: bool = False,
         config_dir: str | None = None, timeout: float | None = None) -> Result | None:
    """One opai call. None on any failure (the caller falls back). force=True skips only the enabled/task
    switches (for `opai try`); budgets, backoff, the contestant breaker and the sweep window still apply."""
    ok, reason = available(s, db, task) if not force else _forced_available(s, db, task)
    if not ok:
        _skip(db, task, reason)
        return None
    version = prompt_version(task)
    schema = schema_for(task, payload)
    ptext = prompt_text(task, version)
    stdin = f"{ptext.strip()}\n\n{data_block(payload)}\n"
    alias = model_for(s, task)
    with _lock:
        start = time.time()
        call_id = db.x("INSERT INTO ai_calls(task,subject,day,started_at,model_alias,prompt_name,prompt_version,"
                       "prompt_sha,input_sha,status) VALUES(?,?,?,?,?,?,?,?,?,'running')",
                       task, subject, utc_day(start), start, alias, task, version, sha(ptext), sha(stdin))
        timeout = timeout or {"translate": s.opai_translate_timeout_s,
                              "ab": getattr(s, "ab_gen_timeout_s", s.opai_timeout_s)}.get(task, s.opai_timeout_s)
        status, detail, data, model = _run(s, db, task, schema, stdin, alias, config_dir, timeout)
        if status == "ok":
            err = check(data, schema) or (validate(data) if validate else None)
            if err:
                status, detail = "invalid", err
        db.x("UPDATE ai_calls SET status=?, detail=?, duration_s=?, model_reported=? WHERE call_id=?",
             status, _redact(detail or "") or None, time.time() - start, model, call_id)
    if data is not None:
        oid = tag(db, task, model or alias, version, data, status == "ok", None if status == "ok" else detail,
                  call_id, subject)
    else:
        oid = 0
    if status != "ok":
        log.warning("opai %s (%s) failed: %s %s", task, subject, status, _redact(detail or "")[:120])
        return None
    return Result(data=data, output_id=oid, model=model or alias, call_id=call_id)


def _forced_available(s: Settings, db: DB, task: str) -> tuple[bool, str]:
    s2 = dataclasses.replace(s, opai_enabled=True, opai_tasks=[*s.opai_tasks, task],
                             **({"ab_gen_enabled": True} if hasattr(s, "ab_gen_enabled") else {}))
    return available(s2, db, task)


def _run(s: Settings, db: DB, task: str, schema: dict, stdin: str, alias: str, config_dir: str | None,
         timeout: float) -> tuple[str, str | None, dict | None, str | None]:
    cwd = tempfile.mkdtemp(prefix="opai-")
    try:
        p = subprocess.Popen(argv(s, task, schema, alias), cwd=cwd, env=child_env(s, config_dir),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, text=True)
        try:
            out, err = p.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.communicate()
            return "timeout", f"over {timeout:.0f}s", None, None
    except OSError as e:
        return "crash", str(e), None, None
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    data, model, is_error = _parse(out)
    if p.returncode != 0 or is_error or data is None:
        from .orchestrator import infra_failure
        kind = infra_failure((out or "") + "\n" + (err or ""))
        if kind in ("quota", "auth"):
            db.put("opai_backoff_until", time.time() + s.opai_backoff_s)
            if kind == "auth":
                try:
                    from . import alerts
                    alerts.send(s, db, "warn", "opai-auth", "opai login failed",
                                "The operator AI's Claude login was rejected; opai backs off and every task uses "
                                "its fallback.", action="opai backed off for "
                                f"{s.opai_backoff_s / 3600:.0f} h",
                                resume=f"CLAUDE_CONFIG_DIR={s.opai_config_dir} claude /login")
                except Exception:
                    log.exception("auth alert failed")
            return kind, (err or out)[-300:], None, model
        if p.returncode != 0 or is_error:
            return "crash", (err or out)[-300:], None, model
        return "invalid", "no JSON object in the result", None, model
    return "ok", None, data, model


async def acall(s: Settings, db: DB, task: str, payload: dict, *, subject: str,
                validate: Callable[[dict], str | None] | None = None) -> Result | None:
    """Async wrapper: one opai call at a time per process. Never awaited inside Round.run."""
    global _asem
    if _asem is None:
        _asem = asyncio.Semaphore(1)
    ok, reason = available(s, db, task)
    if not ok:  # cheap path: no thread hop
        _skip(db, task, reason)
        return None
    async with _asem:
        return await asyncio.to_thread(call, s, db, task, payload, subject=subject, validate=validate)


# ---------------------------------------------------------------- status

def status(s: Settings, db: DB) -> dict:
    day = utc_day()
    used = {r["task"]: r["n"] for r in db.q("SELECT task, COUNT(*) n FROM ai_calls WHERE day=? GROUP BY task", day)}
    last = [dict(r) for r in db.q("SELECT call_id, task, subject, status, duration_s, model_reported, started_at"
                                  " FROM ai_calls ORDER BY call_id DESC LIMIT 20")]
    infra = db.last_infra_failure()
    return {"enabled": s.opai_enabled, "tasks": s.opai_tasks, "day": day,
            "calls_today": sum(used.values()), "calls_per_day": s.opai_calls_per_day,
            "budget": {t: f"{used.get(t, 0)}/{n}" for t, n in s.opai_task_budget.items()},
            "backoff_until": db.get("opai_backoff_until"),
            "contestant_breaker": bool(infra and time.time() - infra < s.infra_cooldown_s),
            "skips_today": db.get(f"opai_skip_{day}") or [], "last_calls": last}


def manifest() -> dict[str, str]:
    """prompts/*.md -> sha256. MANIFEST.json is committed; a test fails if a file changes without a new
    version file, so every version that ever produced an output stays in the repo."""
    return {p.name: sha(p.read_bytes()) for p in sorted(prompt_path("").glob("*.md"))}


def write_manifest() -> Path:
    path = prompt_path("MANIFEST.json")
    path.write_text(json.dumps(manifest(), indent=1, sort_keys=True) + "\n")
    return path
