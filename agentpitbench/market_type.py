"""Market-mix classifier (methodology v2.2): which kind of market is this?

match            a sports/esports match between named sides ("X vs. Y", "Will X win on <date>?", "...end in a draw?")
crypto_updown    "Bitcoin Up or Down - October 10, 9PM ET"
price_threshold  stock/crypto price ladders ("hit (LOW) $256", "dip to $2,400", "reach $84,000", "above $2,500 on Oct 9")
other            everything else (politics, approval ladders, tweet counts, weather ...)

league(slug) is agentpit's league or asset code, the slug's first part ("ucl-lin2-liv-2026-10-14-draw" -> "ucl",
"btc-updown-4h-..." -> "btc"): what the marquee boost (marquee_leagues) matches. fixture(slug) is the slug up to the
match date ("lal-get-ray-2026-10-19-draw" -> "lal-get-ray-2026-10-19"): one key for every market of one match
(one_round_per_fixture); None for slugs without a date (crypto, prices, politics ...).
"""
from __future__ import annotations

import re

_FIXTURE = re.compile(r"^([a-z0-9]+(?:-[a-z0-9]+)*?-\d{4}-\d{2}-\d{2})(?=-|$)")

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


def league(slug: str | None) -> str:
    return (slug or "").split("-", 1)[0].lower()


def fixture(slug: str | None) -> str | None:
    m = _FIXTURE.match((slug or "").lower())
    return m.group(1) if m else None
