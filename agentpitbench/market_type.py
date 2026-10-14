"""Market-mix classifier (methodology v2.2): which kind of market is this?

match            a sports/esports match between named sides ("X vs. Y", "Will X win on <date>?", "...end in a draw?")
crypto_updown    "Bitcoin Up or Down - October 10, 9PM ET"
price_threshold  stock/crypto price ladders ("hit (LOW) $256", "dip to $2,400", "reach $84,000", "above $2,500 on Oct 9")
other            everything else (politics, approval ladders, tweet counts, weather ...)
"""
from __future__ import annotations

import re

_UPDOWN = re.compile(r"\bup or down\b", re.I)
_THRESHOLD = re.compile(r"\bhit\s*\((?:low|high)\)\s*\$|\b(?:dip|drop|fall|reach|rise|climb)s?\s+(?:to\s+)?\$"
                        r"|\b(?:above|below|over|under)\s+\$\s*\d", re.I)
_VS = re.compile(r"\bvs\b\.?", re.I)
_WIN_ON = re.compile(r"\bwin on \d{4}-\d{2}-\d{2}\b|\bend in a draw\b", re.I)


def market_type(question: str | None, event_title: str | None = None) -> str:
    q = question or ""
    if _UPDOWN.search(q):
        return "crypto_updown"
    if _THRESHOLD.search(q):
        return "price_threshold"
    if (event_title and _VS.search(event_title)) or _VS.search(q) or _WIN_ON.search(q):
        return "match"
    return "other"
