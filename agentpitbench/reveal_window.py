"""When the reveal goes out on X (docs/virality-2-spec.md F1): in the event's live window, T-3h .. T-15min, where T is
the kickoff. The agents' decisions are sealed and public on the site at once; only the X post waits. A reveal is held,
never dropped: its due time is stored on the round (rounds.reveal_due_at) so a restart resumes it.

T, in order: the kickoff stated in the agentpit event description ('scheduled for October 12 at 8:15PM ET'); the market's
end when the description gives only the date ('scheduled for October 10, 2026': football markets end at kickoff, e.g.
Aston Villa-Brentford 14:00 UTC); 12:00 UTC on the date of a 'Will X win on <date>?' market; the market's end for
'Up or Down ... ET' markets. Markets with
no kickoff (stock thresholds, crypto, counts) go out in the weekday window 13:30-20:00 UTC instead."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BEFORE_S = 3 * 3600        # the window opens 3 h before kickoff (most pre-match tweeting, Öztürkcan et al. 2019)
LAST_S = 15 * 60           # and closes 15 min before it
DAY_OPEN = (13, 30)        # no-kickoff markets: the US session plus the European afternoon, weekdays
DAY_CLOSE = (20, 0)

_KICKOFF = re.compile(r"scheduled for (?:\w+day,? )?([A-Z][a-z]+\.? \d{1,2})(?:st|nd|rd|th)?(?:,? (\d{4}))?,?"
                      r" (?:at )?(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?\s*(?:ET|EST|EDT)\b")
_DATE_ONLY = re.compile(r"scheduled for (?:\w+day,? )?[A-Z][a-z]+\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}(?![^\n.]{0,12}\d:\d)")
_WIN_ON = re.compile(r"\bwin on (\d{4}-\d{2}-\d{2})\b")
_UP_DOWN = re.compile(r"\bUp or Down\b", re.I)


def _ts(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_kickoff(description: str | None, ref: float) -> float | None:
    """The kickoff stated in an event description, as a unix time (ET, DST-aware). A missing year is the one that
    puts the date closest to ref (the market's end, or now)."""
    m = _KICKOFF.search(description or "")
    if not m:
        return None
    day, year, hh, mm, ap = m.groups()
    hour = int(hh) % 12 + (12 if ap.lower() == "p" else 0)
    ref_year = datetime.fromtimestamp(ref, timezone.utc).year
    best = None
    for y in ([int(year)] if year else [ref_year - 1, ref_year, ref_year + 1]):
        for fmt in ("%B %d %Y", "%b %d %Y", "%b. %d %Y"):
            try:
                d = datetime.strptime(f"{day} {y}", fmt)
                break
            except ValueError:
                d = None
        if d is None:
            continue
        t = d.replace(hour=hour, minute=int(mm or 0), tzinfo=ET).timestamp()
        if best is None or abs(t - ref) < abs(best - ref):
            best = t
    return best


def kickoff(question: str, event_start: float | None, end_date: str | None, description: str | None = None) -> float | None:
    """T for a round: the parsed event start, else the fallbacks, else None (no kickoff exists)."""
    if event_start:
        return event_start
    if description and _DATE_ONLY.search(description) and _ts(end_date):
        return _ts(end_date)
    m = _WIN_ON.search(question or "")
    if m:
        return datetime.fromisoformat(m.group(1)).replace(hour=12, tzinfo=timezone.utc).timestamp()
    if _UP_DOWN.search(question or ""):
        return _ts(end_date)
    return None


def _next_day_slot(now: float) -> float:
    """now if it is inside the weekday window, else the next window opening."""
    d = datetime.fromtimestamp(now, timezone.utc)
    for i in range(8):
        day = (d + timedelta(days=i)).date()
        if day.weekday() >= 5:
            continue
        o = datetime(day.year, day.month, day.day, *DAY_OPEN, tzinfo=timezone.utc).timestamp()
        c = datetime(day.year, day.month, day.day, *DAY_CLOSE, tzinfo=timezone.utc).timestamp()
        if now < o:
            return o
        if now < c:
            return now
    return now


def due_at(question: str, event_start: float | None, end_date: str | None, now: float,
           description: str | None = None) -> float:
    """When the reveal should post: now when it is inside the window or the window has passed (a late market posts at
    once), else the window opening. Never later than 15 min before the market ends."""
    end = _ts(end_date)
    t = kickoff(question, event_start, end_date, description)
    due = max(now, t - BEFORE_S) if t else _next_day_slot(now)
    if end is not None and due > end - LAST_S:
        due = max(now, end - LAST_S)
    return due


def start_ok(question: str, end_date: str | None, description: str | None, now: float,
             min_h: float = 3, max_h: float = 12) -> bool:
    """F1b (docs/virality-2-spec.md): may a round on this market start now? Only when its event is min_h..max_h hours
    away (T from the market's own description, which carries the 'scheduled for ...' text, then kickoff()'s
    fallbacks). Markets with no kickoff start inside the weekday window 13:30-20:00 UTC, where their reveal posts at
    once. On a weekend, a no-kickoff market that ends before Monday's window may use the same hours, or it never runs."""
    end = _ts(end_date)
    t = kickoff(question, parse_kickoff(description, end or now), end_date, description)
    if t:
        return t - max_h * 3600 <= now <= t - min_h * 3600
    slot = _next_day_slot(now)
    if slot == now:
        return True
    d = datetime.fromtimestamp(now, timezone.utc)
    in_hours = DAY_OPEN <= (d.hour, d.minute) < DAY_CLOSE
    return d.weekday() >= 5 and in_hours and end is not None and end < slot
