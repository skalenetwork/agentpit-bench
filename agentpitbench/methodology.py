"""The methodology lock (docs/ai-automation-spec.md §4).

agentpitbench/methodology.json is the single source of truth for everything the methodology page states: the
protected parameters, hashes of the prompts and model table, the market-selection rule and the public
changelog. Every change goes through one writer, `_commit`: version bump, changelog line, lock rehash. Three
callers reach it:
- `approve`, from `agentpitbench method approve` at a terminal: draft (a proposal) -> operator approval;
- `auto_apply`, from the weekly selection review when `selection_auto_apply` is on (operator decision
  2026-10-12): one market-selection key per change, within PROPOSAL_BOUNDS, at most one per 7 days, a
  changelog line generated from the change (never model text) and the evidence published;
- `revert`, from `agentpitbench method revert <version>` at a terminal: the inverse of an earlier change, as a
  new version.
No model, ops agent or other service path writes it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
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
    """Empty when the working tree matches the lock: hashes, the last changelog entry's params_sha, the
    version the running settings report and no generated change newer than the lock."""
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
        # a generated change's evidence lives in var/, the lock in the checkout: a deploy that resets the
        # checkout without the committed lock would silently undo it
        applied = [f.stem for f in changes_dir(s).glob("*.json")] if changes_dir(s).exists() else []
        newer = [v for v in applied if v.replace(".", "").isdigit() and v.count(".") == 1
                 and _vt(v) > _vt(lock["version"])]
        if newer:
            problems.append(f"lock v{lock['version']} is older than generated change v{max(newer, key=_vt)} "
                            "(deployed without committing methodology.json?)")
        if s.methodology_version != lock["version"]:
            problems.append(f"settings report v{s.methodology_version}, lock is v{lock['version']}")
        for k, v in lock["params"].items():
            if hasattr(s, k) and getattr(s, k) != v:
                problems.append(f"setting {k} differs from the lock")
    return problems


def changelog(path: Path | None = None) -> list[tuple[str, str, dict, str]]:
    """(date, message id, format values, version), newest first: the shape site.py and discover.py render. A
    generated entry (auto-applied or reverted) stores a template id in `text` and its values in `values`, so one
    translated template serves every such change. The version lets the site link a change's evidence file."""
    return [(e["date"], e["text"], e.get("values") or {}, e.get("version") or "")
            for e in reversed(load(path)["changelog"])]


def changelog_anchor(version: str) -> str:
    return f"v{version}"


def changelog_anchors(path: Path | None = None) -> list[str]:
    """The HTML id of each changelog() row, newest first: v<version> on the newest entry of each version
    ('' on older entries that share a version, e.g. the two 1.0 lines), so /methodology/#v1.2 is unique."""
    seen, out = set(), []
    for e in reversed(load(path)["changelog"]):
        v = e.get("version")
        out.append("" if not v or v in seen else changelog_anchor(v))
        seen.add(v)
    return out


def changelog_url(s: Settings, version: str | None = None) -> str:
    """/methodology/#v<version>, or the changelog heading."""
    return f"{s.site_url.rstrip('/')}/methodology/#{changelog_anchor(version) if version else 'changelog'}"


# generated changelog lines: one translated template per change type (i18n ids; site.py marks them with _())
AUTO_TEXT = "Market selection: {param} {old} → {new} (automatic weekly review; evidence: {link})"
REVERT_TEXT = "Market selection: {param} {old} → {new} (operator revert of v{v}; evidence: {link})"
AUTO_BY = "selection-review"     # approved_by of an auto-applied entry
AUTO_KEYS = ("max_favourite_price", "max_category_share", "daily_round_cap", "max_days_to_close",
             "selection.scoring")  # what the weekly review may change (never the rater prompt: that's code)
AUTO_MIN_DAYS = 7


def changes_dir(s: Settings) -> Path:
    """Evidence for every generated change, one JSON per version; the site publishes it at
    /methodology/changes/<version>.json."""
    return s.data_dir / "methodology-changes"


def change_url(s: Settings, version: str) -> str:
    return f"{s.site_url.rstrip('/')}/methodology/changes/{version}.json"


def value_of(lock: dict, key: str):
    if key == "selection.scoring":
        return lock["selection"]["scoring"]
    if key == "selection.rater.prompt_version":
        return lock["selection"]["rater"]["prompt_version"]
    return lock["params"].get(key)


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
    if key not in lock["params"] and not _is_setting(key):  # a new rule may join the lock, but only a real setting
        raise MethodologyError(f"unknown methodology key {key}")


def _is_setting(key: str) -> bool:
    return "." not in key and key in {f.name for f in dataclasses.fields(Settings)}


def _coerce(lock: dict, key: str, raw):
    if not isinstance(raw, str):
        return raw
    if "." not in key and key not in lock["params"] and _is_setting(key):  # new key: typed like its Settings default
        cur = getattr(Settings(), key)
    else:
        cur = None
    cur = cur if cur is not None else lock["params"].get(key) if "." not in key else (
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


def reject(s: Settings, pid: str, reason: str = "") -> dict:
    p = get_proposal(s, pid)
    p["state"] = "rejected"
    if reason:
        p["reject_reason"] = reason
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


def _vt(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def next_version(v: str, major: bool) -> str:
    a, b = (int(x) for x in v.split("."))
    return f"{a + 1}.0" if major else f"{a}.{b + 1}"


def approve(s: Settings, pid: str, approver: str = "operator", *, major: bool = False, snapshot: bool = False,
            isatty: Callable[[], bool] | None = None, ask: Callable[[str], str] = input,
            path: Path | None = None, fill: Callable[[list[str]], dict] | None = None,
            standings: Callable[[], dict] | None = None) -> dict:
    """The operator's path. Refuses without a terminal and an exact typed confirmation. With `snapshot`
    (`--snapshot-standings`), the standings as they stand are frozen as the version's evidence file
    (/methodology/changes/<version>.json; docs/simple-scoring-spec.md §2), written before the lock and removed
    if the lock write fails."""
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
    commit = lambda: _commit(s, lock, p["changes"], p["author"], nv,  # noqa: E731
                             {"text": p["changelog_text"], "approved_by": approver}, path=path, hashes=p.get("hashes"))
    if snapshot:
        ev = {"version": nv, "previous_version": lock["version"], "approved_by": approver,
              "changelog": p["changelog_text"], **(standings or (lambda: _standings(s)))()}
        new = _with_evidence(s, nv, ev, commit)
    else:
        new = commit()
    p.update(state="approved", approved_version=nv)
    _write_proposal(s, p)
    (fill or _fill_or_placeholder)([p["changelog_text"]])
    return new


def _standings(s: Settings) -> dict:
    from .db import DB
    from .exports import standings_snapshot
    return standings_snapshot(s, DB(s.db_path))


def _with_evidence(s: Settings, nv: str, ev: dict, commit: Callable[[], dict]) -> dict:
    """Write-then-commit-or-unlink: the evidence file first (the changelog line links it), then the lock; a
    failed lock write removes the evidence again, so no version's evidence outlives a refused change."""
    f = changes_dir(s) / f"{nv}.json"
    _atomic(f, json.dumps(ev, indent=1, ensure_ascii=False))
    try:
        return commit()
    except BaseException:
        f.unlink(missing_ok=True)
        raise


def _write_proposal(s: Settings, p: dict) -> None:
    _atomic(proposals_dir(s) / f"{p['id']}.json", json.dumps(p, indent=1, ensure_ascii=False))


def _atomic(path: Path, text: str) -> None:
    """Temp file, then rename: a crash mid-write must never leave a half-written lock behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _commit(s: Settings, lock: dict, changes: dict, author: str, nv: str, entry: dict, *, path: Path,
            hashes: dict | None = None, now: float | None = None) -> dict:
    """The single writer of the lock (approve, auto_apply and revert all end here): bounds re-checked, the
    hashed inputs must still match (or a rehash proposal's hashes are adopted), version bump, changelog line
    with the new params_sha, atomic write. The running settings follow at once, so new rounds use the new
    rules and the drift detector sees a consistent lock."""
    for k, v in changes.items():
        _check_change(lock, k, v, author)
    new = _apply_changes(lock, changes)
    if hashes is not None:
        if current_hashes(new) != hashes:
            raise MethodologyError("the working tree changed since the rehash proposal; propose again")
        new["hashes"] = hashes
    elif current_hashes(new) != new["hashes"]:
        raise MethodologyError("hashed files differ from the lock: use `method propose --rehash`")
    new["version"] = nv
    now = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    new["changelog"].append({"date": now.strftime("%Y-%m-%d"), "version": nv, **entry,
                             "approved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "params_sha": params_sha(new)})
    _atomic(path, json.dumps(new, indent=1, ensure_ascii=False) + "\n")
    apply(s, {}, path)
    return new


def _fmt(v) -> str:
    return str(v)


def _generated(s: Settings, lock: dict, key: str, new_value, author: str, template: str, approved_by: str,
               record: dict, path: Path, fill: Callable[[list[str]], dict] | None, now: float,
               extra: dict | None = None) -> dict:
    """A change whose changelog line is generated from the change itself: the evidence file is written
    first (the line links to it), then the lock."""
    nv = next_version(lock["version"], False)
    old = value_of(lock, key)
    values = {"param": key, "old": _fmt(old), "new": _fmt(new_value), "link": change_url(s, nv), **(extra or {})}
    ev = {"version": nv, "previous_version": lock["version"], "applied_at": now, "approved_by": approved_by,
          "change": {"key": key, "old": old, "new": new_value}, "changelog": template.format(**values), **record}
    new = _with_evidence(s, nv, ev, lambda: _commit(
        s, lock, {key: new_value}, author, nv, {"text": template, "values": values, "approved_by": approved_by,
                                                "change": {"key": key, "old": old, "new": new_value}},
        path=path, now=now))
    if any(template not in json.loads(f.read_text("utf-8")) for f in I18N_DIR.glob("*.json")):
        (fill or _fill_or_placeholder)([template])   # the shipped tables already hold both templates
    return new


def last_auto(lock: dict) -> dict | None:
    return next((e for e in reversed(lock["changelog"]) if e.get("approved_by") == AUTO_BY), None)


def auto_apply(s: Settings, pid: str, *, path: Path | None = None, now: float | None = None,
               fill: Callable[[list[str]], dict] | None = None) -> dict:
    """The weekly selection review's own path (selection_auto_apply). Everything is re-checked here rather
    than trusted from the caller: the flag, an opai proposal still open, exactly one change, a market-selection
    key, PROPOSAL_BOUNDS, no rehash, and at most one auto-applied change per 7 days (calendar days of the
    lock's own changelog, so a weekly review a few minutes early isn't refused)."""
    path = path or LOCK
    now = time.time() if now is None else now
    if not s.selection_auto_apply:
        raise MethodologyError("selection_auto_apply is off")
    p = get_proposal(s, pid)
    if p.get("state") != "open" or p.get("author") != "opai":
        raise MethodologyError(f"proposal {pid} is not an open opai proposal")
    if "hashes" in p:
        raise MethodologyError("a rehash is never auto-applied")
    if not isinstance(p.get("changes"), dict) or len(p["changes"]) != 1:
        raise MethodologyError("auto-apply takes exactly one change")
    (key, value), = p["changes"].items()
    if key not in AUTO_KEYS:
        raise MethodologyError(f"{key} is not auto-applicable")
    lock = load(path)
    _check_change(lock, key, value, "opai")         # PROPOSAL_BOUNDS, again (and once more in _commit)
    if value == value_of(lock, key):
        raise MethodologyError(f"{key} is already {value}")
    last = last_auto(lock)
    today = datetime.fromtimestamp(now, timezone.utc).date()
    if last and (today - date.fromisoformat(last["date"])).days < AUTO_MIN_DAYS:
        raise MethodologyError(f"rate limit: v{last['version']} was auto-applied on {last['date']} "
                               f"(at most one change per {AUTO_MIN_DAYS} days)")
    record = {"proposal": {k: p.get(k) for k in ("id", "created_at", "author", "rationale", "evidence",
                                                  "changelog_text", "output_id")},
              "rule": f"weekly selection review, auto-applied within {PROPOSAL_BOUNDS[key]}; at most one change "
                      f"per {AUTO_MIN_DAYS} days; takes effect for rounds created after the version bump"}
    new = _generated(s, lock, key, value, "opai", AUTO_TEXT, AUTO_BY, record, path, fill, now)
    p.update(state="applied", approved_version=new["version"], applied_by=AUTO_BY)
    _write_proposal(s, p)
    return new


def revert(s: Settings, version: str, *, isatty: Callable[[], bool] | None = None,
           ask: Callable[[str], str] = input, path: Path | None = None, now: float | None = None,
           fill: Callable[[list[str]], dict] | None = None) -> dict:
    """`method revert <version>`: the inverse of a generated change (auto-applied or an earlier revert),
    applied as a new version. Refused when a later version changed the same key again."""
    path = path or LOCK
    now = time.time() if now is None else now
    if not (isatty or sys.stdin.isatty)():
        raise MethodologyError("revert needs an interactive terminal (the operator types the confirmation)")
    lock = load(path)
    entry = next((e for e in lock["changelog"] if e.get("version") == version), None)
    if entry is None or not entry.get("change"):
        raise MethodologyError(f"v{version} is not a generated single-key change; use method propose")
    key, old = entry["change"]["key"], entry["change"]["old"]
    if value_of(lock, key) != entry["change"]["new"]:
        raise MethodologyError(f"{key} is {value_of(lock, key)} now, not v{version}'s {entry['change']['new']}: "
                               "it changed again since; revert the latest change instead")
    nv = next_version(lock["version"], False)
    if ask(f"Type 'revert v{version}' to set {key} back to {old} as v{nv}: ").strip() != f"revert v{version}":
        raise MethodologyError("not confirmed")
    record = {"reverts": version, "reverted_change": entry["change"],
              "reverted_evidence": change_url(s, version),
              "rule": "operator revert (agentpitbench method revert); takes effect for rounds created after "
                      "the version bump"}
    return _generated(s, lock, key, old, "operator", REVERT_TEXT, "operator", record, path, fill, now,
                      extra={"v": version})


def _fill_or_placeholder(ids: list[str]) -> dict:
    """Translations for new changelog lines: i18n.fill through opai when available; otherwise the English text
    goes into every table (so the page and tests stay complete) and the ids are queued for retry."""
    try:
        from .i18n_fill import fill
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
