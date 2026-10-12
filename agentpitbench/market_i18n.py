"""Market questions and outcome labels, translated for the site (operator addition to the AI-automation spec,
2026-10-12).

Each string is translated once per site language through the operator AI (task "translate": one call per
market covering every language in i18n.CODES, JSON out) and stored in `market_i18n`. The site build only reads
the table and falls back to English; it never calls a model. A validator keeps the exact market wording
auditable: numbers, $ amounts, percentages, tickers, years and proper nouns must survive verbatim (digits may be
the language's own), placeholders stay, length stays sane. Anything that fails stays English.

English pages, JSON-LD, the API, exports and X posts keep the English source.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata

from . import opai
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

MAX_TRIES = 2          # per round; after that its untranslated strings stay English
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"Sept": 9}
WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
# capitalised words that are generic in market titles (translated, not names)
GENERIC = {
    "Will", "Week", "Of", "The", "A", "An", "And", "Or", "In", "On", "At", "By", "For", "To", "From", "Up", "Down",
    "Over", "Under", "Above", "Below", "Between", "Higher", "Lower", "High", "Low", "Hit", "Hits", "Reach", "Win",
    "Wins", "Beat", "Beats", "Close", "Closes", "Price", "Yes", "No", "Other", "Draw", "Tie", "End", "Ends",
    "Before", "After", "Next", "Who", "What", "Which", "How", "When", "Be", "Is", "Are", "Total", "Points", "Goals",
    "Match", "Game", "Games", "Day", "Month", "Year", "Election", "Winner", "Champion", "Final", "Finals", "Round",
    "Most", "Least", "More", "Less", "Than", "Exactly", "Any", "None", "Both", "Either", "Neither", "Team", "Map",
    "Set", "Half", "First", "Second", "Third", "Last", "Top", "Bottom", "Range", "Rate", "Rates", "Cut", "Cuts",
    "Hike", "Hold", "Up or Down", "Not", "Yes/No", "Even", "Odd", "Party", "Seats", "Votes", "Vote", "Market",
    "Daily", "Weekly", "Monthly", "Annual", "Q1", "Q2", "Q3", "Q4", "Inc", "Week of", "Earnings", "Revenue",
    "Score", "Scores", "Kills", "Over/Under", "Spread", "Moneyline", "Handicap", "Series", "Season", "League",
    "Cup", "Championship", "Tournament", "Open", "Grand", "Prix", "Race", "Stage", "Tweets", "Posts", "Post",
}
GENERIC_CAPS = {"LOW", "HIGH", "UP", "DOWN", "YES", "NO", "OVER", "UNDER", "AND", "OR", "THE", "OF", "VS", "TBD",
                "AM", "PM", "UTC", "ET", "EST", "PT"}
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_MONEY = re.compile(r"[$€£]\s?\d+(?:[.,]\d+)*[kKmMbB]?")
_PCT = re.compile(r"(\d+(?:[.,]\d+)?)\s?%")
_TICKER = re.compile(r"(?<![A-Za-z])[A-Z][A-Z0-9]{1,5}(?![A-Za-z])")
_WORD = re.compile(r"[A-Za-z][\w'’.&-]*")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def site_langs() -> list[str]:
    from . import i18n
    return [c for c in i18n.CODES if c != i18n.DEFAULT]


def ascii_digits(text: str) -> str:
    """Any script's decimal digits as 0-9 (Bengali ১২ -> 12), so numbers compare across scripts."""
    return "".join(str(unicodedata.decimal(c)) if c.isdigit() and unicodedata.decimal(c, None) is not None else c
                   for c in text)


def proper_nouns(src: str) -> list[str]:
    """Capitalised words that are names: not the first word, not months/weekdays, not generic title words.
    Names written with internal dots or capitals (Amazon.com, McLaren) count too."""
    out = []
    for i, m in enumerate(_WORD.finditer(src)):
        w = m.group(0).rstrip(".’'")
        if not w or not w[0].isupper() or w.isupper():
            continue
        if i == 0 and w in GENERIC:
            continue
        base = w.rstrip("s") if w.endswith("'s") else w
        if base in GENERIC or base in WEEKDAYS or base in MONTHS or base in MONTH_ABBR:
            continue
        out.append(w)
    return out


def tickers(src: str) -> list[str]:
    return [t for t in _TICKER.findall(src) if t not in GENERIC_CAPS and not t.isdigit()]


def _norm_num(n: str) -> str:
    return n.replace(",", ".")


def check(src: str, out: str, lang: str) -> str | None:
    """None if the translation keeps everything that makes the market exact; else why not."""
    if not isinstance(out, str) or not out.strip():
        return "empty"
    if "\n" in out.strip() or "<" in out or "http" in out.lower():
        return "markup, link or line break"
    o = ascii_digits(out)
    if sorted(_PLACEHOLDER.findall(src)) != sorted(_PLACEHOLDER.findall(o)):
        return "placeholders changed"
    lo, hi = (0.25, 3.5) if len(src) > 12 else (0.0, 30 / max(len(src), 1))
    if not (lo * len(src) <= len(out) <= max(hi * len(src), 30)):
        return "length"
    src_nums = [_norm_num(n) for n in _NUM.findall(src)]
    out_nums = [_norm_num(n) for n in _NUM.findall(o)]
    for n in src_nums:
        if n not in out_nums:
            return f"number {n} missing"
    month_nums = {str(i + 1) for i, m in enumerate(MONTHS) if re.search(rf"\b{m}\b", src)}
    month_nums |= {str(v) for k, v in MONTH_ABBR.items() if re.search(rf"\b{k}\b", src)}
    month_nums |= {f"{int(x):02d}" for x in month_nums}
    for n in out_nums:
        if n not in src_nums and n not in month_nums:
            return f"number {n} added"
    for m in _MONEY.findall(src):
        if m not in o and m.replace(" ", "") not in o.replace(" ", ""):
            return f"amount {m} changed"
    for p in _PCT.findall(src):
        if not re.search(rf"(?:{re.escape(p)}\s?%|%\s?{re.escape(p)})", o.replace(",", ".") if "," not in p else o):
            return f"percentage {p}% changed"
    for t in tickers(src):
        if not re.search(rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])", o):
            return f"ticker {t} changed"
    for w in proper_nouns(src):
        if w not in o:
            return f"name {w} changed"
    return None


# ---------------------------------------------------------------- storage

def table(db: DB) -> dict[str, dict[str, str]]:
    """{lang: {English source: translation}} for the site build."""
    out: dict[str, dict[str, str]] = {}
    for r in db.q("SELECT source_text, lang, text FROM market_i18n"):
        out.setdefault(r["lang"], {})[r["source_text"]] = r["text"]
    return out


def round_texts(row) -> list[str]:
    import json
    texts = [row["question"], *json.loads(row["outcomes"])]
    return list(dict.fromkeys(t for t in texts if t and t.strip()))


def missing(db: DB, texts: list[str], langs: list[str]) -> dict[str, list[str]]:
    """{text: [langs without a translation]} for the texts that lack any."""
    have: dict[str, set[str]] = {}
    for t in texts:
        have[t] = {r["lang"] for r in db.q("SELECT lang FROM market_i18n WHERE source_text=?", t)}
    return {t: [l for l in langs if l not in have[t]] for t in texts if any(l not in have[t] for l in langs)}


def store(db: DB, src: str, lang: str, text: str, model: str, version: int, output_id: int) -> None:
    db.x("INSERT OR IGNORE INTO market_i18n(source_text,lang,text,model,prompt_version,created_at,output_id)"
         " VALUES(?,?,?,?,?,?,?)", src, lang, " ".join(text.split()), model, f"translate.v{version}", time.time(),
         output_id)


# ---------------------------------------------------------------- translating

def payload_for(db: DB, texts: list[str], langs: list[str] | None = None) -> dict | None:
    langs = langs or site_langs()
    need = missing(db, texts, langs)
    if not need:
        return None
    want_langs = [l for l in langs if any(l in v for v in need.values())]
    return {"texts": list(need), "langs": want_langs}


def verdicts(payload: dict, data: dict) -> dict[str, dict[str, str | None]]:
    """{lang: {source: reject reason or None}}"""
    out: dict[str, dict[str, str | None]] = {}
    for lang in payload["langs"]:
        outs = data.get(lang) or []
        out[lang] = {src: (check(src, outs[i], lang) if i < len(outs) else "missing")
                     for i, src in enumerate(payload["texts"])}
    return out


def apply(db: DB, payload: dict, res: opai.Result) -> dict:
    v = verdicts(payload, res.data)
    ok = rejected = 0
    reasons = []
    for lang, per in v.items():
        for i, (src, why) in enumerate(per.items()):
            if why is None:
                store(db, src, lang, res.data[lang][i], res.model, opai.prompt_version("translate"), res.output_id)
                ok += 1
            else:
                rejected += 1
                reasons.append(f"{lang}:{why}")
    if ok:
        db.x("UPDATE operator_ai_outputs SET public=1 WHERE output_id=?", res.output_id)
    if reasons:
        db.x("UPDATE operator_ai_outputs SET reject_reason=? WHERE output_id=?", "; ".join(reasons)[:2000],
             res.output_id)
    return {"stored": ok, "rejected": rejected, "reasons": reasons}


async def translate_texts(s: Settings, db: DB, texts: list[str], subject: str) -> dict:
    payload = payload_for(db, texts)
    if payload is None:
        return {"stored": 0, "rejected": 0, "reasons": [], "skipped": "complete"}
    res = await opai.acall(s, db, "translate", payload, subject=subject)
    if res is None:
        return {"stored": 0, "rejected": 0, "reasons": [], "skipped": "opai unavailable or failed"}
    return apply(db, payload, res)


def due(db: DB, limit: int = 50) -> list[int]:
    """Non-void rounds, newest first, whose question or outcomes lack a translation in some site language and
    that have not used up their tries."""
    langs = site_langs()
    out = []
    for r in db.q("SELECT round_id, question, outcomes FROM rounds WHERE state!='void' ORDER BY round_id DESC"):
        if (db.get(f"mt_tries_{r['round_id']}") or 0) >= MAX_TRIES:
            continue
        if missing(db, round_texts(r), langs):
            out.append(r["round_id"])
            if len(out) >= limit:
                break
    return out


async def translate_round(s: Settings, db: DB, round_id: int) -> dict:
    r = db.round(round_id)
    db.put(f"mt_tries_{round_id}", (db.get(f"mt_tries_{round_id}") or 0) + 1)
    return await translate_texts(s, db, round_texts(r), f"round:{round_id}")


async def atick(s: Settings, db: DB, limit: int = 1) -> list[dict]:
    """Ops tick: new rounds first, then the backfill, newest first; at most `limit` calls, within the
    'translate' task budget. Never on the site-build path."""
    out = []
    for rid in due(db, limit):
        ok, why = opai.available(s, db, "translate")
        if not ok:
            opai._skip(db, "translate", why)
            break
        out.append({"round_id": rid, **await translate_round(s, db, rid)})
    return out


def tick(s: Settings, db: DB, limit: int = 5) -> list[dict]:
    return asyncio.run(atick(s, db, limit))


def coverage(db: DB) -> dict:
    langs = site_langs()
    rounds = db.q("SELECT round_id, question, outcomes FROM rounds WHERE state!='void' ORDER BY round_id DESC")
    full = sum(1 for r in rounds if not missing(db, round_texts(r), langs))
    return {"languages": len(langs), "rounds": len(rounds), "fully_translated": full,
            "strings": db.one("SELECT COUNT(*) n FROM market_i18n")["n"], "due": due(db, 1000)[:20]}
