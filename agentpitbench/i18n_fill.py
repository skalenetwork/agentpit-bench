"""`agentpitbench i18n fill` (docs/ai-automation-spec.md §12): missing UI translations through the operator AI,
one call per language (up to 150 ids), validated like tests/test_i18n.py::test_table_complete, merged into
agentpitbench/i18n/<lang>.json. Never runs inside site.build.

An id counts as missing when it is absent, or when its value equals the English key and the id is queued in
kv['i18n_retry'] (the placeholder written while opai was unavailable). Many ids are legitimately identical in
other languages ("Agent", "Date"), so equality alone never counts.
"""
from __future__ import annotations

import json
import logging
import string
from pathlib import Path

from . import i18n, opai
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

I18N_DIR = Path(__file__).resolve().parent / "i18n"
BATCH = 150
GLOSSARY = ["Claude", "ChatGPT", "Gemini", "Grok", "Brier", "AgentpitBench", "agentpit", "X", "Polymarket", "apUSD"]


def fields(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def check(key: str, val) -> str | None:
    if not isinstance(val, str) or not val.strip():
        return "empty"
    try:
        if fields(val) != fields(key):
            return "placeholders differ"
    except ValueError:
        return "broken braces"
    if val.count("<b>") != key.count("<b>") or val.count("</b>") != key.count("</b>"):
        return "<b> count differs"
    return None


def _load(lang: str, d: Path) -> dict[str, str]:
    f = d / f"{lang}.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else {}


def _save(lang: str, table: dict[str, str], d: Path) -> None:
    (d / f"{lang}.json").write_text(json.dumps(dict(sorted(table.items())), ensure_ascii=False, indent=0) + "\n")


def missing(langs: list[str] | None = None, ids: set[str] | None = None, d: Path | None = None,
            retry: set[str] | None = None) -> dict[str, list[str]]:
    d = d or I18N_DIR
    ids = ids if ids is not None else i18n.message_ids()
    retry = retry or set()
    out = {}
    for lang in langs or [c for c in i18n.CODES if c != i18n.DEFAULT]:
        t = _load(lang, d)
        out[lang] = sorted(k for k in ids if k not in t or (t[k] == k and k in retry))
    return out


def fill(s: Settings | None = None, db: DB | None = None, *, ids: list[str] | None = None,
         langs: list[str] | None = None, dry: bool = False, d: Path | None = None) -> dict[str, int]:
    """Returns {lang: entries still missing}. Without opai, ids passed in explicitly get English placeholders
    (so tables stay complete) and are queued in kv['i18n_retry']."""
    d = d or I18N_DIR
    if s is None:
        from . import config
        s = config.load()
    db = db or DB(s.db_path)
    all_ids = i18n.message_ids()
    want = set(ids) if ids else all_ids
    retry = set(db.get("i18n_retry") or [])
    todo = missing(langs, want, d, retry)
    left = {}
    still: set[str] = set()
    for lang, keys in todo.items():
        table = _load(lang, d)
        if not keys:
            left[lang] = 0
            continue
        got = {}
        for i in range(0, len(keys), BATCH):
            batch = keys[i:i + BATCH]
            samples = dict(list((k, v) for k, v in sorted(table.items()) if v != k)[:10])
            payload = {"target_language": lang, "language_name": i18n.NAMES.get(lang, lang), "ids": batch,
                       "glossary_keep_untranslated": GLOSSARY, "style_samples": samples}
            res = opai.call(s, db, "i18n", payload, subject=f"i18n:{lang}") if not dry else None
            if res is None:
                break
            ok = {k: v for k, v in res.data["translations"].items() if k in batch and check(k, v) is None}
            got.update(ok)
            if ok:
                db.x("UPDATE operator_ai_outputs SET public=1 WHERE output_id=?", res.output_id)
        unresolved = [k for k in keys if k not in got]
        if ids and unresolved and not dry:  # explicit ids (changelog, incidents): never leave a gap
            for k in unresolved:
                table.setdefault(k, k)
        still |= set(unresolved) & (set(ids or []) | retry)
        table.update(got)
        if not dry:
            table = {k: v for k, v in table.items() if k in all_ids}  # stale ids go
            _save(lang, table, d)
        left[lang] = len(unresolved)
    if not dry:
        queue = (retry | still) & all_ids
        remaining = missing(None, queue, d, queue)  # every language: an id leaves the queue once done everywhere
        db.put("i18n_retry", sorted({k for v in remaining.values() for k in v}))
    return left
