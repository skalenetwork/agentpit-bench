"""The methodology lock (docs/ai-automation-spec.md §4).

agentpitbench/methodology.json is the single source of truth for everything the methodology page states: the
protected parameters, hashes of the prompts and model table, the market-selection rule and the public
changelog. It changes only through `approve`, called only by `agentpitbench method approve` at a terminal:
draft (a proposal) -> operator approval -> version bump. No model, ops agent or service path writes it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import AGENT_MODELS, Settings

log = logging.getLogger(__name__)

PKG = Path(__file__).resolve().parent
LOCK = PKG / "methodology.json"
I18N_DIR = PKG / "i18n"

# ranges any proposal must respect; other params can only be proposed by the operator
PROPOSAL_BOUNDS = {
    "max_favourite_price": (0.70, 0.92),
    "max_category_share": (0.25, 0.60),
    "daily_round_cap": (3, 10),
    "max_days_to_close": (2, 14),
    "selection.scoring": ("keyword", "model"),
    "selection.rater.prompt_version": None,      # must exist in prompts/
}


class MethodologyError(ValueError):
    pass


def _sha(b: bytes | str) -> str:
    return hashlib.sha256(b.encode("utf-8") if isinstance(b, str) else b).hexdigest()


def load(path: Path | None = None) -> dict:
    return json.loads((path or LOCK).read_text("utf-8"))


def current_hashes(lock: dict) -> dict[str, str]:
    """What the hashed inputs are in the working tree right now."""
    from .forecast import PROMPT
    v = lock["selection"]["rater"]["prompt_version"]
    return {"prompt.txt": _sha((PKG / "prompt.txt").read_bytes()), "sweep_prompt": _sha(PROMPT),
            "rater_prompt": _sha((PKG / "prompts" / f"rate.v{v}.md").read_bytes()),
            "agent_models": _sha(json.dumps(AGENT_MODELS, sort_keys=True))}


def params_sha(lock: dict) -> str:
    return _sha(json.dumps({k: lock[k] for k in ("params", "hashes", "selection")}, sort_keys=True))


def apply(s: Settings, toml_data: dict, path: Path | None = None) -> None:
    """Called by config.load after bench.toml: the lock's params win. A differing protected key in bench.toml
    is logged CRITICAL and ignored."""
    lock = load(path)
    for k, v in lock["params"].items():
        if not hasattr(s, k):
            continue
        if k in toml_data and toml_data[k] != v:
            log.critical("bench.toml sets protected methodology key %s=%r; the lock's %r applies (change it with "
                         "`agentpitbench method propose`)", k, toml_data[k], v)
        setattr(s, k, v)
    s.methodology_version = lock["version"]
    s.selection_scoring = lock["selection"]["scoring"]


def verify(s: Settings | None = None, path: Path | None = None) -> list[str]:
    """Empty when the working tree matches the lock: hashes, the last changelog entry's params_sha and the
    version the running settings report."""
    problems = []
    lock = load(path)
    cur = current_hashes(lock)
    for k, v in lock["hashes"].items():
        if cur.get(k) != v:
            problems.append(f"hash drift: {k}")
    log_ = lock.get("changelog") or []
    if not log_ or log_[-1].get("params_sha") != params_sha(lock):
        problems.append("params_sha of the last changelog entry does not match params + hashes + selection")
    if s is not None:
        if s.methodology_version != lock["version"]:
            problems.append(f"settings report v{s.methodology_version}, lock is v{lock['version']}")
        for k, v in lock["params"].items():
            if hasattr(s, k) and getattr(s, k) != v:
                problems.append(f"setting {k} differs from the lock")
    return problems


def changelog(path: Path | None = None) -> list[tuple[str, str]]:
    """(date, English text), newest first: the shape site.py and discover.py render."""
    return [(e["date"], e["text"]) for e in reversed(load(path)["changelog"])]


# ---------------------------------------------------------------- proposals

def proposals_dir(s: Settings) -> Path:
    return s.data_dir / "proposals"


def _check_change(lock: dict, key: str, value, author: str) -> None:
    if key in PROPOSAL_BOUNDS:
        b = PROPOSAL_BOUNDS[key]
        if key == "selection.scoring":
            if value not in b:
                raise MethodologyError(f"{key} must be one of {b}")
        elif key == "selection.rater.prompt_version":
            if not isinstance(value, int) or not (PKG / "prompts" / f"rate.v{value}.md").exists():
                raise MethodologyError(f"prompts/rate.v{value}.md does not exist")
        elif not (b[0] <= value <= b[1]):
            raise MethodologyError(f"{key}={value} outside {b[0]}..{b[1]}")
        return
    if author != "operator":
        raise MethodologyError(f"{key} can only be proposed by the operator")
    if key not in lock["params"]:
        raise MethodologyError(f"unknown methodology key {key}")


def _coerce(lock: dict, key: str, raw):
    if not isinstance(raw, str):
        return raw
    cur = lock["params"].get(key) if "." not in key else (
        lock["selection"]["scoring"] if key == "selection.scoring" else lock["selection"]["rater"]["prompt_version"])
    if isinstance(cur, bool):
        return raw.lower() in ("1", "true", "yes")
    if isinstance(cur, int):
        return int(raw)
    if isinstance(cur, float):
        return float(raw)
    if isinstance(cur, list):
        return json.loads(raw)
    return raw


def propose(s: Settings, changes: dict, text: str, author: str = "operator", *, rehash: bool = False,
            rationale: str = "", evidence: str = "", output_id: int | None = None, path: Path | None = None) -> dict:
    if author not in ("opai", "ops-agent", "operator"):
        raise MethodologyError("unknown author")
    if rehash and author != "operator":
        raise MethodologyError("--rehash is operator-only")
    if not text.strip():
        raise MethodologyError("a changelog text is required")
    if not changes and not rehash:
        raise MethodologyError("nothing to change")
    lock = load(path)
    changes = {k: _coerce(lock, k, v) for k, v in changes.items()}
    for k, v in changes.items():
        _check_change(lock, k, v, author)
    p = {"id": datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6],
         "created_at": time.time(), "author": author, "changes": changes, "rationale": rationale,
         "evidence": evidence, "changelog_text": text.strip(), "output_id": output_id, "state": "open"}
    if rehash:
        p["hashes"] = current_hashes({**lock, "selection": _apply_changes(lock, changes)["selection"]})
    d = proposals_dir(s)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{p['id']}.json").write_text(json.dumps(p, indent=1, ensure_ascii=False))
    return p


def get_proposal(s: Settings, pid: str) -> dict:
    f = proposals_dir(s) / f"{pid}.json"
    if not f.exists():
        raise MethodologyError(f"no proposal {pid}")
    return json.loads(f.read_text("utf-8"))


def list_proposals(s: Settings) -> list[dict]:
    d = proposals_dir(s)
    return [json.loads(f.read_text("utf-8")) for f in sorted(d.glob("*.json"))] if d.exists() else []


def reject(s: Settings, pid: str) -> dict:
    p = get_proposal(s, pid)
    p["state"] = "rejected"
    (proposals_dir(s) / f"{pid}.json").write_text(json.dumps(p, indent=1, ensure_ascii=False))
    return p


def _apply_changes(lock: dict, changes: dict) -> dict:
    new = json.loads(json.dumps(lock))
    for k, v in changes.items():
        if k == "selection.scoring":
            new["selection"]["scoring"] = v
        elif k == "selection.rater.prompt_version":
            new["selection"]["rater"]["prompt_version"] = v
        else:
            new["params"][k] = v
    return new


def next_version(v: str, major: bool) -> str:
    a, b = (int(x) for x in v.split("."))
    return f"{a + 1}.0" if major else f"{a}.{b + 1}"


def approve(s: Settings, pid: str, approver: str = "operator", *, major: bool = False,
            isatty: Callable[[], bool] | None = None, ask: Callable[[str], str] = input,
            path: Path | None = None, fill: Callable[[list[str]], dict] | None = None) -> dict:
    """The only writer of methodology.json. Refuses without a terminal and an exact typed confirmation."""
    path = path or LOCK
    if not (isatty or sys.stdin.isatty)():
        raise MethodologyError("approve needs an interactive terminal (the operator types the confirmation)")
    p = get_proposal(s, pid)
    if p.get("state") != "open":
        raise MethodologyError(f"proposal {pid} is {p.get('state')}")
    lock = load(path)
    nv = next_version(lock["version"], major)
    if ask(f"Type 'approve v{nv}' to apply proposal {pid} ({p['changelog_text']}): ").strip() != f"approve v{nv}":
        raise MethodologyError("not confirmed")
    for k, v in p["changes"].items():
        _check_change(lock, k, v, p["author"])
    new = _apply_changes(lock, p["changes"])
    if "hashes" in p:
        if current_hashes(new) != p["hashes"]:
            raise MethodologyError("the working tree changed since the rehash proposal; propose again")
        new["hashes"] = p["hashes"]
    elif current_hashes(new) != new["hashes"]:
        raise MethodologyError("hashed files differ from the lock: use `method propose --rehash`")
    new["version"] = nv
    now = datetime.now(timezone.utc)
    new["changelog"].append({"date": now.strftime("%Y-%m-%d"), "version": nv, "text": p["changelog_text"],
                             "approved_by": approver, "approved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "params_sha": params_sha(new)})
    path.write_text(json.dumps(new, indent=1, ensure_ascii=False) + "\n")
    p.update(state="approved", approved_version=nv)
    (proposals_dir(s) / f"{pid}.json").write_text(json.dumps(p, indent=1, ensure_ascii=False))
    (fill or _fill_or_placeholder)([p["changelog_text"]])
    return new


def _fill_or_placeholder(ids: list[str]) -> dict:
    """Translations for new changelog lines: i18n.fill through opai when available; otherwise the English text
    goes into every table (so the page and tests stay complete) and the ids are queued for retry."""
    try:
        from . import i18n
        fill = getattr(i18n, "fill", None)
        if fill:
            return fill(ids=ids)
    except Exception:
        log.exception("i18n fill failed; writing English placeholders")
    return write_english(ids)


def write_english(ids: list[str], i18n_dir: Path | None = None) -> dict:
    d = i18n_dir or I18N_DIR
    n = {}
    for f in sorted(d.glob("*.json")):
        t = json.loads(f.read_text("utf-8"))
        added = [i for i in ids if i not in t]
        for i in added:
            t[i] = i
        if added:
            f.write_text(json.dumps(dict(sorted(t.items())), ensure_ascii=False, indent=0) + "\n")
        n[f.stem] = len(added)
    return n
