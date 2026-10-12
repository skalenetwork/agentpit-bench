"""Log excerpts for the operator AI and the ops agent, scrubbed (docs/ai-automation-spec.md §10).

The service log carries picks while a round is still running (`round N: claude bet Yes @ 0.62` at placement),
so every excerpt goes through clean(): lines about a running round, every `bet … @` line and outbox/paused lines
for a running round's posts are dropped at read time; then secrets are redacted.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

from .db import DB

log = logging.getLogger(__name__)

_ROUND = re.compile(r"\bround (\d+)\b", re.I)
_BET = re.compile(r"\bbet\b.*@", re.I)
_POSTKEY = re.compile(r"\[(?:outbox|paused)\]\s*r(\d+)-")


def _secret_values() -> list[str]:
    from .orchestrator import SECRET_PREFIXES
    return sorted({v for k, v in os.environ.items() if k.startswith(SECRET_PREFIXES) and len(v) >= 8},
                  key=len, reverse=True)


def scrub(lines: list[str], running: set[int]) -> list[str]:
    out = []
    for ln in lines:
        if _BET.search(ln):
            continue
        if any(int(m) in running for m in _ROUND.findall(ln)):
            continue
        m = _POSTKEY.search(ln)
        if m and int(m.group(1)) in running:
            continue
        out.append(ln)
    return out


def clean(lines: list[str], db: DB) -> list[str]:
    from .virality import redact
    running = {r["round_id"] for r in db.q("SELECT round_id FROM rounds WHERE state='running'")}
    secrets = _secret_values()
    out = []
    for ln in scrub(lines, running):
        ln = redact(ln)
        for v in secrets:
            ln = ln.replace(v, "[redacted]")
        out.append(ln)
    return out


def journal(since: float, until: float, unit: str = "agentpitbench", limit: int = 400) -> list[str]:
    """Raw service log lines in [since, until] (journalctl --user). Empty when unavailable."""
    try:
        p = subprocess.run(["journalctl", "--user", "-u", unit, "--since", f"@{int(since)}", "--until",
                            f"@{int(until)}", "-o", "short-iso", "--no-pager", "-n", str(limit)],
                           capture_output=True, text=True, timeout=20)
        return p.stdout.splitlines() if p.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        return []


def excerpt(db: DB, at: float, n: int = 40) -> list[str]:
    """Up to n scrubbed lines around `at` (mostly before it)."""
    return clean(journal(at - 900, at + 120), db)[-n:]
