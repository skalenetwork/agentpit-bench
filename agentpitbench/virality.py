"""Pure helpers for the shareable extras: newsworthy market priority, agent quotes, human picks,
tail/fade links, seasons and milestone detection. No I/O here, so all of it is unit-tested directly."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote as urlquote

UPSET_MAX = 0.25  # a winning pick at or below this price is an upset / long shot

# ---------- newsworthy-first market priority ----------

ESPORTS = re.compile(
    r"\b(counter-?strike|cs2|cs:?go|dota|league of legends|\blol\b|valorant|overwatch|rocket league|"
    r"rainbow six|r6|esports?|e-sports|starcraft|apex legends|pubg|fortnite|call of duty|cod league|"
    r"mobile legends|bo[1-5]|cct|esl|blast premier|pgl|iem|vct|lck|lec|lpl)\b", re.I)
MINOR_LEAGUE = re.compile(
    r"\b(u-?1[5-9]|u-?2[0-3]|youth|reserves?|women'?s? (2nd|second) division|liga 3|3\. liga|serie c|"
    r"league two|national league|segunda|primera b|regionalliga|division 2|d2 |itf|challenger|futures)\b", re.I)
TIER3 = re.compile(  # politics, crypto, AI/tech, finals and the names people argue about
    r"\b(elect(ion|ed|s)?|president|presidential|prime minister|senate|congress|parliament|governor|mayor|"
    r"referendum|poll(s|ing)?|vote|white house|supreme court|impeach|tariff|sanction|ceasefire|nato|"
    r"bitcoin|btc|ethereum|eth|solana|crypto|stablecoin|etf|coinbase|binance|"
    r"openai|chatgpt|gpt-?\d|anthropic|claude|gemini|deepmind|xai|grok|nvidia|apple|tesla|spacex|"
    r"artificial intelligence|\bai\b|agi|iphone|launch|ipo|"
    r"final|finals|championship|world cup|super bowl|world series|stanley cup|grand slam|wimbledon|"
    r"olympic|olympics|ballon d'or|mvp|"
    r"trump|biden|harris|vance|musk|elon|zuckerberg|altman|swift)\b", re.I)
TIER2 = re.compile(  # mainstream sport, macro, culture
    r"\b(nfl|nba|mlb|nhl|premier league|la liga|bundesliga|serie a|ligue 1|champions league|uefa|"
    r"formula 1|\bf1\b|grand prix|ufc|boxing|tennis|atp|wta|pga|golf|cricket|ipl|"
    r"fed|fomc|interest rate|cpi|inflation|gdp|recession|unemployment|s&p|nasdaq|dow|oil|gold|"
    r"oscar|grammy|emmy|box office|billboard|netflix|album|movie)\b", re.I)


def news_tier(m: dict) -> int:
    """3 = headline news, 2 = mainstream, 1 = other, 0 = esports / minor leagues."""
    text = " ".join(str(m.get(k) or "") for k in ("question", "description", "category", "groupItemTitle"))
    if ESPORTS.search(text) or MINOR_LEAGUE.search(text):
        return 0
    if TIER3.search(text):
        return 3
    if TIER2.search(text):
        return 2
    return 1


CATEGORIES = [  # first match wins; esports is checked with the stricter ESPORTS pattern first
    ("Esports", r"counter-strike|dota|valorant|league of legends|\blol\b|esports|\bbo[135]\b"),
    ("Crypto", r"bitcoin|\bbtc\b|ethereum|\beth\b|solana|crypto|\bxrp\b|doge"),
    ("Sports", r" vs\.? |\bnba\b|\bnfl\b|\bmlb\b|\bnhl\b|premier league|uefa|tennis|\bufc\b|match|game \d|grand prix|open\b"),
    ("Politics", r"election|president|senate|congress|trump|minister|parliament|vote|poll"),
    ("Economy", r"\bfed\b|rate|inflation|\bcpi\b|gdp|stock|s&p|nasdaq|earnings|price of"),
]


def category(question: str) -> str:
    """Esports, Crypto, Sports, Politics, Economy or Other, from the market question."""
    if ESPORTS.search(question or ""):
        return "Esports"
    q = " " + (question or "").lower() + " "
    for name, pat in CATEGORIES:
        if re.search(pat, q):
            return name
    return "Other"


def category_ok(cat: str, recent: list[str], share: float) -> bool:
    """Would one more round of `cat` keep it at or under `share` of the recent rounds? The first round
    of any category is always allowed, so a quiet week can still start."""
    n = len(recent) + 1
    return recent.count(cat) + 1 <= max(1.0, share * n)


def _num(m: dict, k: str) -> float:
    try:
        return float(m.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def market_priority(m: dict) -> tuple:
    """Newsworthy first; volume, liquidity and the newest id break ties. Sort descending."""
    return (news_tier(m), _num(m, "volume"), _num(m, "liquidity"), int(m["id"]))


# ---------- agent trash-talk quote ----------

QUOTE_MAX = 120
_LINK = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|net|org|io|ai|dev|xyz|co|gg|ly|me|app)\b)", re.I)
_MENTION = re.compile(r"(^|\s)[@＠]\w")
# Slurs and abuse that would get the account reported. Matched on a normalized, de-leeted form.
_BLOCK = [
    "nigger", "nigga", "faggot", "fag", "retard", "tranny", "kike", "spic", "chink", "gook", "wetback",
    "towelhead", "raghead", "cunt", "whore", "slut", "kys", "killyourself", "kill yourself", "hang yourself",
    "rape", "nazi", "hitler", "heil",
]
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def clean_quote(text: str | None, max_len: int = QUOTE_MAX) -> tuple[str | None, str | None]:
    """(quote, reason_dropped). One line, at most max_len chars; no links, mentions, slurs or abuse."""
    if not text:
        return None, None
    q = " ".join(str(text).split())
    if not q:
        return None, None
    if len(q) > max_len:
        q = q[:max_len - 1].rstrip() + "…"
    if _LINK.search(q):
        return None, "quotes can't contain links"
    if _MENTION.search(q):
        return None, "quotes can't @mention accounts"
    norm = q.lower().translate(_LEET)
    squashed = re.sub(r"[^a-z]", "", norm)
    words = set(re.findall(r"[a-z]+", norm))
    for b in _BLOCK:
        if (" " in b and b in norm) or b in words or (len(b) >= 5 and b.replace(" ", "") in squashed):
            return None, "quote dropped by the abuse filter"
    return q, None


# ---------- humans play along ----------

def parse_pick(text: str, outcomes: list[str]) -> str | None:
    """The outcome named first in a reply (mentions stripped), matched as whole words, case-insensitive."""
    body = re.sub(r"(^|\s)@\w+", " ", text or "").lower()
    best: tuple[int, str] | None = None
    for o in outcomes:
        m = re.search(r"(?<![\w])" + re.escape(o.lower()) + r"(?![\w])", body)
        if m and (best is None or m.start() < best[0] or (m.start() == best[0] and len(o) > len(best[1]))):
            best = (m.start(), o)
    return best[1] if best else None


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def human_picks(replies: list[dict], outcomes: list[str], end_date: str | None, exclude_ids: set[str]) -> list[dict]:
    """One pick per user: their earliest reply naming an outcome, posted before the market's end date.
    replies: [{id, author_id, username, text, created_at}]"""
    close = _ts(end_date)
    picks: dict[str, dict] = {}
    for r in sorted(replies, key=lambda r: (_ts(r.get("created_at")) or 0, r["id"])):
        if r.get("author_id") in exclude_ids or r.get("author_id") in picks:
            continue
        t = _ts(r.get("created_at"))
        if close is not None and t is not None and t >= close:
            continue
        pick = parse_pick(r.get("text", ""), outcomes)
        if pick:
            picks[r["author_id"]] = {"user_id": r["author_id"], "username": r.get("username"), "pick": pick,
                                     "tweet_id": r["id"], "created_at": t}
    return list(picks.values())


# ---------- tail / fade ----------

def tail_fade(market_link: str, outcomes: list[str], outcome: str | None) -> tuple[str | None, str | None]:
    """Links that copy (tail) or oppose (fade) an agent's pick. Fade names the other side of a 2-way market."""
    if not outcome:
        return None, None
    sep = "&" if "?" in market_link else "?"
    tail = f"{market_link}{sep}outcome={urlquote(outcome)}&utm_content=tail"
    others = [o for o in outcomes if o != outcome]
    fade_side = f"outcome={urlquote(others[0])}&" if len(others) == 1 else ""
    fade = f"{market_link}{sep}{fade_side}utm_content=fade"
    return tail, fade


# ---------- seasons ----------

def season_of(ts: float | None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else datetime.now(timezone.utc).timestamp(),
                                  timezone.utc).strftime("%Y-%m")


def season_bounds(season: str) -> tuple[float, float]:
    y, m = map(int, season.split("-"))
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    return start.timestamp(), end.timestamp()


def prev_season(season: str) -> str:
    y, m = map(int, season.split("-"))
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


# ---------- milestones ----------

ROUND_RECORDS = (50, 100, 250, 500, 1000, 2500, 5000, 10000)
PNL_RECORDS = (500, 1000, 2500, 5000, 10000)


def milestone_state(lb: dict, rounds_resolved: int) -> dict:
    """The small slice of the leaderboard that milestone detection compares across resolutions."""
    agents = lb.get("agents", [])
    played = [a for a in agents if a.get("played")]
    top = played[0] if played else None
    ai_avg = sum(a["net_pnl"] for a in agents) / len(agents) if agents else 0.0
    crowd = lb.get("crowd") or {}
    return {
        "season": lb.get("season"),
        "leader": top["agent"] if top and (len(played) < 2 or top["net_pnl"] > played[1]["net_pnl"]) else None,
        "streaks": {a["agent"]: a.get("streak", 0) for a in agents},
        "names": {a["agent"]: a.get("name", a["agent"]) for a in agents},
        "vs_crowd": (0 if not crowd.get("played") else (1 if ai_avg > crowd.get("net_pnl", 0)
                                                        else -1 if ai_avg < crowd.get("net_pnl", 0) else 0)),
        "ai_avg": round(ai_avg, 2), "crowd_net": crowd.get("net_pnl", 0),
        "rounds": rounds_resolved,
        "all_time_pnl": {a["agent"]: a["net_pnl"] for a in (lb.get("all_time") or {}).get("agents", [])},
    }


def detect_milestones(prev: dict | None, cur: dict, round_id: int, streak_min: int = 4) -> list[tuple[str, str, str]]:
    """(post_key, headline, subline) for each milestone crossed between two states. Nothing on the first call."""
    if not prev:
        return []
    out = []
    names = cur["names"]
    same_season = prev.get("season") == cur.get("season")
    for a, n in cur["streaks"].items():
        p = prev["streaks"].get(a, 0) if same_season else 0
        # post at 4, 6, 8, ... in a row (signs differ only when a streak just reset to ±1)
        if abs(n) >= streak_min and abs(n) > abs(p) and (abs(n) - streak_min) % 2 == 0:
            word = "won" if n > 0 else "lost"
            out.append((f"ms-streak-{a}-{'W' if n > 0 else 'L'}{abs(n)}-r{round_id}",
                        f"{names[a]} has {word} {abs(n)} straight",
                        "Hottest hand in the pit" if n > 0 else "Somebody check on it"))
    if cur["leader"] and cur["leader"] != prev.get("leader") and same_season and prev.get("leader"):
        out.append((f"ms-lead-{cur['season']}-{cur['leader']}-r{round_id}",
                    f"New #1: {names[cur['leader']]} takes the lead",
                    f"{names.get(prev['leader'], prev['leader'])} drops to second"))
    if same_season and cur["vs_crowd"] and prev.get("vs_crowd") and cur["vs_crowd"] != prev["vs_crowd"]:
        ahead = cur["vs_crowd"] > 0
        out.append((f"ms-crowd-{cur['season']}-{'ahead' if ahead else 'behind'}-r{round_id}",
                    "The AIs pull ahead of the Crowd" if ahead else "The Crowd retakes the lead over the AIs",
                    f"Average AI {cur['ai_avg']:+.0f} vs Crowd {cur['crowd_net']:+.0f} this season"))
    for n in ROUND_RECORDS:
        if prev.get("rounds", 0) < n <= cur["rounds"]:
            out.append((f"ms-rounds-{n}", f"Round {n} is in the books", "Four AIs, one market at a time"))
    for t in PNL_RECORDS:
        hit = [a for a, v in cur["all_time_pnl"].items() if v >= t]
        before = [a for a, v in prev.get("all_time_pnl", {}).items() if v >= t]
        if hit and not before:
            a = max(hit, key=lambda a: cur["all_time_pnl"][a])
            out.append((f"ms-pnl-{t}", f"{names.get(a, a)} is the first to +{t:,} tokens",
                        "All-time net profit across every round"))
    return out


# ---------- summon ----------

def market_refs(urls: list[str], text: str, market_url: str) -> list[str]:
    """Slugs (or numeric ids) of agentpit markets linked in a tweet."""
    base = market_url.split("{slug}")[0]
    host_path = re.sub(r"^https?://", "", base)
    found = []
    for u in list(urls) + re.findall(r"\S+", text or ""):
        u2 = re.sub(r"^https?://", "", u)
        if u2.startswith(host_path):
            slug = re.split(r"[?#/\s]", u2[len(host_path):])[0]
            if slug and slug not in found:
                found.append(slug)
    return found


# ---------- sportscaster commentary (rule-based, deterministic) ----------

def _c(p: float | None) -> str:
    return "?" if p is None else f"{round(p * 100)}¢"


def _pick(variants: list[str], seed: int, **kw) -> str:
    return variants[seed % len(variants)].format(**kw)


def commentary(rnd: dict) -> str:
    """One broadcast-style line from the round's data. Same round in, same line out: no model writes it."""
    seed = int(rnd.get("round_id") or 0)
    if rnd.get("state") == "void":
        return "No result: the market was voided and nobody's record moves."
    entries = rnd.get("entries", [])
    bettors = [e for e in entries if e.get("outcome")]
    winners = [e for e in entries if e.get("won")]
    crowd = rnd.get("crowd") or {}
    fav_won = crowd.get("won") is True
    if not bettors:
        return "Four AIs, zero bets. Nobody showed up for this one."
    if not winners:
        return _pick(["Everyone got rekt: {w} came in and not one AI was on it.",
                      "A wipeout. {w} lands and the whole field is on the wrong side.",
                      "{w} it is, and every AI in the pit pays for it."], seed, w=rnd.get("winner"))
    if len(winners) == len(bettors) and len(bettors) > 1:
        return ("Clean sweep for the favourite: every AI rode {w} home." if fav_won
                else "Clean sweep on the underdog: every AI found {w} before the market did.").format(w=rnd.get("winner"))
    if len(winners) == 1:
        w = winners[0]
        p = w.get("avg_price")
        if p is not None and p <= 0.25:
            return _pick(["{n} fades the field and cashes at {p}.",
                          "Long shot lands: {n} took {o} at {p} and gets paid {x}.",
                          "{n} goes against everyone at {p}, and it hits."], seed,
                         n=w["name"], p=_c(p), o=w["outcome"], x=f"{1 / p:.1f}×")
        if len(bettors) > 1:
            return _pick(["{n} goes it alone on {o} and is the only one paid.",
                          "Lone wolf: {n} on {o} at {p} while the rest went the other way.",
                          "{n} breaks from the pack and wins it at {p}."], seed, n=w["name"], o=w["outcome"], p=_c(p))
        return f"{w['name']} was the only AI to bet, and it was right at {_c(p)}."
    names = " & ".join(e["name"] for e in winners)
    losers = [e for e in bettors if not e.get("won")]
    if crowd.get("won") is False:
        return f"{names} beat the crowd on {rnd.get('winner')}; {', '.join(e['name'] for e in losers)} sided with the market and lost."
    return _pick(["{names} ride the favourite home. {lo} picked the wrong side.",
                  "Chalk wins: {names} cash on {w}, {lo} eats the loss.",
                  "{names} take it on {w}. Rough one for {lo}."], seed,
                 names=names, w=rnd.get("winner"), lo=" & ".join(e["name"] for e in losers) or "nobody")


# ---------- grudge matches ----------

GRUDGE_MIN = 3


def grudge(rnd: dict, history: list[dict]) -> dict | None:
    """The agent pair that picked opposite sides in this round and in the rounds right before it (3+ in a row).
    history: rounds.json-style records, any order. Returns {a, b, a_name, b_name, clash, a_wins, b_wins}."""
    def side(r, agent):
        return next((e["outcome"] for e in r["entries"] if e["agent"] == agent and e.get("outcome")), None)

    def won(r, agent):
        return next((bool(e.get("won")) for e in r["entries"] if e["agent"] == agent), False)

    past = sorted((r for r in history if r["round_id"] < rnd["round_id"] and not r.get("exhibition")),
                  key=lambda r: r["round_id"], reverse=True)
    agents = [e["agent"] for e in rnd["entries"] if e.get("outcome")]
    best = None
    for i, a in enumerate(agents):
        for b in agents[i + 1:]:
            if side(rnd, a) == side(rnd, b):
                continue
            run = [rnd]
            for r in past:
                sa, sb = side(r, a), side(r, b)
                if sa and sb and sa != sb:
                    run.append(r)
                elif sa and sb:
                    break  # they agreed: the rivalry run ends
            if len(run) >= GRUDGE_MIN and (best is None or len(run) > best["clash"]):
                names = {e["agent"]: e["name"] for e in rnd["entries"]}
                done = [r for r in run if r.get("state") == "resolved"]
                best = {"a": a, "b": b, "a_name": names[a], "b_name": names[b], "clash": len(run),
                        "a_wins": sum(1 for r in done if won(r, a)), "b_wins": sum(1 for r in done if won(r, b))}
    return best


def grudge_headline(g: dict) -> str:
    return f"GRUDGE MATCH: {g['a_name']} vs {g['b_name']} — clash #{g['clash']}"


# ---------- watch-it-think replay ----------

_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]+|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]+|"
    r"AKIA[0-9A-Z]{16}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"(?i:(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*\S+)|"
    r"\b[A-Za-z0-9_\-+/=]{32,}\b|0x[0-9a-fA-F]{40,})")


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


def _texts(obj, out: list[str]) -> None:
    """Assistant-visible prose from one JSON event: skip tool calls, tool results and user turns."""
    if isinstance(obj, dict):
        kind = str(obj.get("type", ""))
        if kind in ("tool_use", "tool_result", "tool_call", "function_call", "function_call_output", "command_execution",
                    "user", "system", "result", "usage") or obj.get("role") in ("user", "tool", "system"):
            return
        for k, v in obj.items():
            if k in ("text", "thinking", "reasoning") and isinstance(v, str):
                out.append(v)
            elif isinstance(v, (dict, list)):
                _texts(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _texts(v, out)


def reasoning_lines(transcript: str, max_lines: int = 80) -> list[str]:
    """Readable reasoning up to the bet: JSON events reduced to prose, shell/tool noise and secrets removed."""
    import json as _json
    body = transcript.split("--- output ---", 1)[-1]
    lines: list[str] = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("{") and s.endswith("}"):
            try:
                found: list[str] = []
                _texts(_json.loads(s), found)
                lines += [x.strip() for t in found for x in t.splitlines() if x.strip()]
            except ValueError:
                pass
            continue
        if (s.startswith(("$", "[bench]", ">", "exec", "tokens used", "{", "[", "succeeded", "exited", "---"))
                or len(s) > 400 or sum(c.isalpha() or c == " " for c in s) < 0.6 * len(s)):
            continue
        lines.append(s)
    out: list[str] = []
    for ln in lines:
        if ln in out[-3:]:  # streaming formats repeat partial and final text
            continue
        out.append(redact(ln))
        if "bench bet" in ln:
            break
    return out[-max_lines:]
