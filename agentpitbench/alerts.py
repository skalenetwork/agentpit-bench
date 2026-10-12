"""Operator alerts (docs/ai-automation-spec.md §10): the log, an `alerts` row and one JSON file per alert in
var/alerts/, written atomically (temp file, then rename). The operator's Claude Code session watches that
directory and relays alerts in chat. No email for now (operator decision, 2026-10-12); further channels plug
into CHANNELS.

An alert body never carries a sealed pick or a secret: callers pass field names, ids and positions only.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

# extra delivery channels: fn(settings, alert dict) -> bool (sent). Empty by design for now.
CHANNELS: dict[str, Callable[[Settings, dict], bool]] = {}


def alerts_dir(s: Settings) -> Path:
    return s.data_dir / "alerts"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def send(s: Settings, db: DB, severity: str, kind: str, title: str, what: str, *, action: str = "none",
         resume: str = "", draft_path: str | None = None) -> dict:
    """severity: critical / warn. Never raises past logging: an alert must not take down its caller."""
    now = time.time()
    alert_id = db.x("INSERT INTO alerts(at,severity,subject,body,kind,action,resume,draft_path,sent_via)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", now, severity, title, what, kind, action, resume, draft_path, "file")
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = alerts_dir(s) / f"{stamp}-{alert_id:05d}-{severity}.json"
    alert = {"id": alert_id, "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
             "severity": severity, "kind": kind, "title": title, "what_happened": what, "action_taken": action,
             "how_to_resume": resume, "incident_draft": draft_path, "acked": False}
    try:
        _atomic_write(path, json.dumps(alert, indent=1, ensure_ascii=False))
        db.x("UPDATE alerts SET file_path=? WHERE alert_id=?", str(path), alert_id)
    except OSError:
        log.exception("could not write alert file %s", path)
    (log.critical if severity == "critical" else log.warning)("ALERT [%s] %s: %s | action: %s | resume: %s",
                                                               kind, title, what, action, resume or "-")
    via = ["file"]
    for name, fn in CHANNELS.items():
        try:
            if fn(s, alert):
                via.append(name)
        except Exception:
            log.exception("alert channel %s failed", name)
    if len(via) > 1:
        db.x("UPDATE alerts SET sent_via=? WHERE alert_id=?", ",".join(via), alert_id)
    return alert


def recent(db: DB, limit: int = 20, unacked: bool = False) -> list[dict]:
    where = "WHERE acked_at IS NULL" if unacked else ""
    return [dict(r) for r in db.q(f"SELECT alert_id, at, severity, kind, subject, body, action, resume, draft_path,"
                                  f" file_path, acked_at FROM alerts {where} ORDER BY alert_id DESC LIMIT ?", limit)]


def ack(db: DB, alert_id: int) -> bool:
    r = db.one("SELECT file_path, acked_at FROM alerts WHERE alert_id=?", alert_id)
    if not r:
        return False
    now = time.time()
    db.x("UPDATE alerts SET acked_at=COALESCE(acked_at, ?) WHERE alert_id=?", now, alert_id)
    if r["file_path"] and Path(r["file_path"]).exists():
        try:
            d = json.loads(Path(r["file_path"]).read_text("utf-8"))
            d["acked"] = True
            d["acked_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
            _atomic_write(Path(r["file_path"]), json.dumps(d, indent=1, ensure_ascii=False))
        except (OSError, ValueError):
            log.exception("could not mark alert file %s acked", r["file_path"])
    return True
