"""`agentpitbench i18n fill` (docs/ai-automation-spec.md §12): missing UI translations through the operator AI,
batched: one call covers up to 10 languages (about 300 language-id pairs, so it stays inside the 300 s
timeout), each language validated on its own like tests/test_i18n.py::test_table_complete, merged into
agentpitbench/i18n/<lang>.json. One new id in 39 languages is 4 calls. Never runs inside site.build.

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
BATCH = 150            # ids of one language in one call at most
LANGS_PER_CALL = 10
CELLS = 300            # (language, id) pairs per call at most
SAMPLES = 5            # style samples per language
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


def plan(todo: dict[str, list[str]], langs_per_call: int = LANGS_PER_CALL, cells: int = CELLS,
         batch: int = BATCH) -> list[list[tuple[str, list[str]]]]:
    """The calls to make: each a list of (lang, ids) with at most langs_per_call languages and at most `cells`
    (language, id) pairs (a single larger chunk goes alone), so one call stays well inside the timeout. A
    language with more than `batch` missing ids is split over several calls."""
    calls: list[list[tuple[str, list[str]]]] = []
    for lang, keys in todo.items():
        for i in range(0, len(keys), batch):
            chunk = keys[i:i + batch]
            cur = calls[-1] if calls else None
            if (cur is None or len(cur) >= langs_per_call or any(l == lang for l, _ in cur)
                    or sum(len(k) for _, k in cur) + len(chunk) > cells):
                calls.append([])
            calls[-1].append((lang, chunk))
    return calls


def fill(s: Settings | None = None, db: DB | None = None, *, ids: list[str] | None = None,
         langs: list[str] | None = None, dry: bool = False, d: Path | None = None) -> dict[str, int]:
    """Returns {lang: entries still missing}. Without opai, ids passed in explicitly get English placeholders
    (so tables stay complete) and are queued in kv['i18n_retry']. A call covers several languages (plan());
    each language's answer is validated on its own, and a language missing or rejected in the answer falls
    back alone (placeholder / retry queue) while the rest of that call is kept."""
    d = d or I18N_DIR
    if s is None:
        from . import config
        s = config.load()
    db = db or DB(s.db_path)
    all_ids = i18n.message_ids()
    want = set(ids) if ids else all_ids
    retry = set(db.get("i18n_retry") or [])
    todo = missing(langs, want, d, retry)
    tables = {lang: _load(lang, d) for lang in todo}
    got: dict[str, dict[str, str]] = {lang: {} for lang in todo}
    for call in ([] if dry else plan({lang: keys for lang, keys in todo.items() if keys})):
        payload = {"languages": {lang: {"language_name": i18n.NAMES.get(lang, lang), "ids": keys,
                                        "style_samples": dict([(k, v) for k, v in sorted(tables[lang].items())
                                                               if v != k][:SAMPLES])}
                                 for lang, keys in call},
                   "glossary_keep_untranslated": GLOSSARY}
        res = opai.call(s, db, "i18n", payload, subject="i18n:" + "+".join(dict.fromkeys(l for l, _ in call)),
                        timeout=s.opai_translate_timeout_s)
        if res is None:
            continue
        any_ok = False
        for lang, keys in call:
            out = res.data["translations"].get(lang)
            if not isinstance(out, dict):
                log.warning("i18n output %s has no %s block; that language falls back", res.output_id, lang)
                continue
            ok = {k: v for k, v in out.items() if k in keys and check(k, v) is None}
            got[lang].update(ok)
            any_ok |= bool(ok)
        if any_ok:
            db.x("UPDATE operator_ai_outputs SET public=1 WHERE output_id=?", res.output_id)
    left = {}
    still: set[str] = set()
    for lang, keys in todo.items():
        table = tables[lang]
        if not keys:
            left[lang] = 0
            continue
        unresolved = [k for k in keys if k not in got[lang]]
        if ids and unresolved and not dry:  # explicit ids (changelog, incidents): never leave a gap
            for k in unresolved:
                table.setdefault(k, k)
        still |= set(unresolved) & (set(ids or []) | retry)
        table.update(got[lang])
        if not dry:
            table = {k: v for k, v in table.items() if k in all_ids}  # stale ids go
            _save(lang, table, d)
        left[lang] = len(unresolved)
    if not dry:
        queue = (retry | still) & all_ids
        remaining = missing(None, queue, d, queue)  # every language: an id leaves the queue once done everywhere
        db.put("i18n_retry", sorted({k for v in remaining.values() for k in v}))
    return left
