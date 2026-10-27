"""Posts v3 (docs/posts-v3-spec.md): the Pit Boss commentator voice.

The operator AI writes each post (opai task "post") from a facts payload; a code validator then checks every AI
name, outcome, number, quote, URL and @mention against those facts and the limits (no gambling hype, no politics,
no jokes about the labs, no abuse). One retry with the validator's feedback; otherwise a deterministic template in
the same voice goes out (also the path when opai is off, over budget or slow). Facts are only ever built from
rounds whose picks are public (state != running): a sealed pick can't reach a prompt or a post.

Also here: which rounds get an X reveal of their own (the day's best-known markets, at most v3_reveals_per_day),
and the daily digests ("today's board" for every other market, "today's damage" for their results).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from .config import AGENT_NAMES, Settings
from .db import DB
from .twitter import MAX_LEN, URL_RE, clip, market_name, price_str, results_shape, tweet_len
from .virality import category, clean_quote, news_tier

log = logging.getLogger(__name__)

# the digest's column order (spec: Claude, Gemini, ChatGPT, Grok)
BOARD_ORDER = ["claude", "agy", "codex", "grok"]
PERSONAS = {"claude": "careful analyst", "codex": "confident all-rounder", "agy": "quiet value hunter",
            "grok": "the gambler"}
EMOJI = {"claude": "🧐", "codex": "😎", "agy": "🕵️", "grok": "🎲"}


class Sealed(RuntimeError):
    """Facts were about to be built from a round whose picks are still secret."""


# ---------------------------------------------------------------- which rounds get their own X reveal

# Polymarket slug prefixes: the leagues people know (the analysis' top-five football did best after crypto)
TOP_LEAGUES = {"epl", "lal", "sea", "bun", "fl1", "ucl", "uel", "uecl", "nfl", "nba", "mlb", "nhl", "wc", "euro"}
OTHER_LEAGUES = {"tur", "por", "ere", "efl", "mls", "arg", "bra", "sco", "bel", "lib", "mex", "ch1", "spl"}
BIG_CLUBS = re.compile(
    r"\b(real madrid|barcelona|atl[eé]tico|manchester|liverpool|arsenal|chelsea|tottenham|newcastle|aston villa|"
    r"bayern|dortmund|leverkusen|paris saint|psg|marseille|monaco|lille|lyon|juventus|inter\b|milan|napoli|roma\b|"
    r"lazio|atalanta|benfica|porto|sporting|ajax|psv|feyenoord|celtic|rangers|galatasaray|fenerbah|be[sş]ikta[sş]|"
    r"boca|river plate|flamengo|palmeiras|corinthians|millwall|leeds|sunderland|everton|west ham|lakers|celtics|"
    r"warriors|yankees|dodgers|chiefs|cowboys|eagles)\b", re.I)
CRYPTO = re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|dogecoin)\b", re.I)


def reveal_score(question: str, slug: str | None = None, event_title: str | None = None) -> int:
    """0 = never its own reveal (esports, minor leagues, politics); 3+ = a market people know. Pure."""
    text = f"{question or ''} {event_title or ''}"
    if news_tier({"question": text}) == 0 or category(text) in ("Esports", "Politics"):
        return 0
    if CRYPTO.search(text):
        return 3
    league = (slug or "").split("-", 1)[0].lower()
    score = 3 if league in TOP_LEAGUES else 2 if league in OTHER_LEAGUES else 1
    if BIG_CLUBS.search(text):
        score += 1
    return max(score, min(news_tier({"question": text}), 2))


def reveal_eligible(question: str, slug: str | None = None, event_title: str | None = None) -> bool:
    return reveal_score(question, slug, event_title) >= 2


def day_start(now: float) -> float:
    return datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def choose_mode(s: Settings, db: DB, round_id: int, now: float) -> str:
    """'reveal' (its own X post) or 'digest', decided once when the picks are in and stored on the round. Summoned
    rounds always get their reveal (someone asked for them) and don't use a slot. Greedy per UTC day: the first
    v3_reveals_per_day eligible rounds of the day take the slots."""
    r = db.round(round_id)
    if r["post_mode"]:
        return r["post_mode"]
    if r["exclusion"] == "summon":
        mode = "reveal"
    else:
        used = db.one("SELECT COUNT(*) n FROM rounds WHERE post_mode='reveal' AND post_mode_at>=? AND "
                      "COALESCE(exclusion,'')!='summon'", day_start(now))["n"]
        ok = reveal_eligible(r["question"], r["slug"], r["event_title"])
        mode = "reveal" if ok and used < s.v3_reveals_per_day else "digest"
    db.set_round(round_id, post_mode=mode, post_mode_at=now)
    return mode


# ---------------------------------------------------------------- facts

def _sentence(text: str | None, n: int = 140) -> str | None:
    t = " ".join((text or "").split())
    if not t:
        return None
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    return clip(m.group(1) if m else t, n)


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def streak_text(streak: int) -> str | None:
    if streak >= 3:
        return f"won {streak} straight"
    if streak <= -3:
        return f"lost {-streak} straight"
    return None


def iso_week_bounds(now: float) -> tuple[float, float]:
    d = datetime.fromtimestamp(now, timezone.utc)
    start = day_start(now) - d.weekday() * 86400
    return start, start + 7 * 86400


def week_records(rounds: list[dict], now: float) -> dict[str, tuple[int, int]]:
    lo, hi = iso_week_bounds(now)
    out: dict[str, list[int]] = {}
    for r in rounds:
        if r["state"] != "resolved" or r.get("exhibition") or not (lo <= (r.get("resolved_at") or 0) < hi):
            continue
        for e in r["entries"]:
            if e.get("outcome"):
                rec = out.setdefault(e["agent"], [0, 0])
                rec[0 if e.get("won") else 1] += 1
    return {a: (w, l) for a, (w, l) in out.items()}


def standings_facts(board: list[dict]) -> list[dict]:
    n = len(board)
    return [{"name": b["name"], "pnl": f"{b['net_pnl']:+.0f}", "rank": f"{ordinal(b['rank'])} of {n}",
             "record": f"{b['wins']}-{b['losses']}", **({"streak": st} if (st := streak_text(b.get("streak") or 0))
                                                         else {})} for b in board]


def round_facts(kind: str, rnd: dict, board: list[dict], rounds: list[dict] | None = None,
                now: float | None = None) -> dict:
    """The facts payload for one round's post. Raises Sealed for a round whose picks are secret."""
    if rnd.get("sealed") or rnd.get("state") == "running":
        raise Sealed(f"round {rnd.get('round_id')} is sealed")
    import time as _t
    now = _t.time() if now is None else now
    by_agent = {b["agent"]: b for b in board}
    week = week_records(rounds or [], now)
    resolved = rnd["state"] == "resolved"
    ais = []
    for e in rnd["entries"]:
        b = by_agent.get(e["agent"], {})
        quote, _ = clean_quote(e.get("quote"))
        line, _ = clean_quote(_sentence(e.get("rationale")), 140)
        a = {"name": e["name"], "persona": PERSONAS.get(e["agent"], ""), "emoji": EMOJI.get(e["agent"], ""),
             "pick": e.get("outcome") or "no bet"}
        if e.get("outcome"):
            a["price"] = price_str(e.get("avg_price"))
            if e.get("confidence") is not None:
                a["confidence"] = f"{round(e['confidence'] * 100)}%"
        if resolved and e.get("outcome"):
            a["result"] = "right" if e.get("won") else "wrong"
            if e.get("pnl") is not None:
                a["round_pnl"] = f"{e['pnl']:+.0f}"
        if quote:
            a["quote"] = quote
        if line:
            a["rationale_line"] = line
        if b:
            a["season_pnl"] = f"{b['net_pnl']:+.0f}"
            a["season_rank"] = f"{ordinal(b['rank'])} of {len(board)}"
            if st := streak_text(b.get("streak") or 0):
                a["streak"] = st
        if e["agent"] in week:
            a["week_record"] = "{}-{}".format(*week[e["agent"]])
        ais.append(a)
    bettors = [e for e in rnd["entries"] if e.get("outcome")]
    facts = {"kind": kind, "market": market_name(rnd), "outcomes": list(rnd.get("outcomes") or []),
             "n_ais": len(bettors), "ais": ais, "standings": standings_facts(board)}
    if resolved:
        facts["winner"] = rnd["winner"]
        sit, f = results_shape(rnd)
        facts["situation"] = sit or "other"
        facts["record"] = f.get("record")
    else:
        from .cards import split_shape
        facts["situation"], f = split_shape(rnd)
        if f.get("score"):
            facts["split"] = f["score"]
    h = rnd.get("humans") or {}
    if h.get("picked"):
        facts["humans"] = {"picked": h["picked"], **({"right": h["right"]} if h.get("right") is not None else {})}
    return facts


# ---------------------------------------------------------------- validator

GAMBLING = ["bet now", "easy money", "free money", "guaranteed", "can't lose", "cant lose", "lock of the day",
            "sure thing", "place your bets", "get rich", "tail now", "bet the house", "max bet", "all in now",
            "risk-free", "risk free", "sign up", "deposit"]
POLITICS = re.compile(
    r"\b(democrats?|republicans?|liberals?|conservatives?|left-wing|right-wing|leftists?|maga|woke|trump|biden|"
    r"harris|vance|obama|putin|zelensky|netanyahu|gop|libs?|socialists?|communists?|fascists?|elections?|"
    r"president|congress|senate|parliament|voters?|ballots?)\b", re.I)
LABS = re.compile(r"\b(anthropic|openai|open ai|google|deepmind|xai|x\.ai|musk|elon|altman|amodei|hassabis|"
                  r"pichai|zuckerberg|meta|microsoft|nvidia)\b", re.I)
AI_NAMES = sorted({*AGENT_NAMES.values(), "Codex", "GPT", "Bard", "Copilot", "Llama", "Mistral", "DeepSeek"},
                  key=len, reverse=True)
INSULTS = re.compile(r"\b(clowns?|idiots?|morons?|pathetic|trash|garbage|losers?|fools?|frauds?|bums?|"
                     r"chokers?|choked|joke|disgrace)\b", re.I)
NUM_WORDS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
             "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "twice": "2", "thrice": "3"}
TITLE_OK = {"AI", "AIs", "Pit", "Boss", "Yes", "No", "Reply", "Discuss", "Tonight", "Today", "Kickoff", "Week",
            "Season", "Humans", "Human", "Episode", "Previously", "The", "Board", "Bingo", "Who", "What", "Rate",
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February",
            "March", "April", "May", "June", "July", "August", "September", "October", "November", "December",
            "UTC", "ET", "Up", "Down", "Over", "Under", "Draw"}
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TITLE = re.compile(r"\b[A-ZÀ-Þ][a-zà-ÿ'’]{2,}\b")
_QUOTED = re.compile(r"[\"“]([^\"“”]{12,})[\"”]")
_MENTION = re.compile(r"(?<![\w@])@(\w{1,30})")
_PAIR = re.compile(r"(?<![\d.,])(\d{1,3})\s?[-–—:]\s?(\d{1,3})(?!\d|[.,]\d)")


def _norm_num(v: str) -> str:
    v = v.replace(",", "")
    return v.rstrip("0").rstrip(".") if "." in v else v


def _numbers(text: str) -> list[tuple[str, str]]:
    """[(sign, value)]: sign is '+' / '-' only when it stands before the number (not the dash of '0-6')."""
    out = []
    for m in _NUM.finditer(text):
        i = m.start()
        sign = ""
        if i > 0 and text[i - 1] in "+-−" and (i == 1 or not text[i - 2].isalnum()):
            sign = "-" if text[i - 1] in "-−" else "+"
        out.append((sign, _norm_num(m.group())))
    return out


def _strings(x) -> list[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, bool) or x is None:
        return []
    if isinstance(x, (int, float)):
        return [str(x)]
    if isinstance(x, dict):
        return [s for v in x.values() for s in _strings(v)]
    if isinstance(x, (list, tuple)):
        return [s for v in x for s in _strings(v)]
    return []


def _squash(t: str) -> str:
    return " ".join(t.replace("…", " ").replace("...", " ").split()).lower()


def validate(text: str, facts: dict) -> str | None:
    """None when the post is fit to go out, else the first problem (fed back to the writer on its retry)."""
    if not text or not text.strip():
        return "empty"
    if tweet_len(text) > MAX_LEN:
        return f"{tweet_len(text)} characters: the limit is {MAX_LEN}"
    blob_parts = _strings({k: v for k, v in facts.items() if k not in ("kind", "brief")})
    blob = "\n".join(blob_parts)
    quotes = [q for a in facts.get("ais", []) for q in (a.get("quote"), a.get("rationale_line")) if q]
    quotes += _strings(facts.get("quotes"))
    # quoted text must be an AI's own words, verbatim (clipping allowed)
    for m in _QUOTED.finditer(text):
        q = _squash(m.group(1))
        if not any(q and q in _squash(src) for src in quotes):
            return f"quote not verbatim from the facts: \"{clip(m.group(1), 40)}\""
    # what the writer itself says: the text minus quotes and the market name (it may name anything)
    own = _QUOTED.sub(" ", text)
    for nm in [facts.get("market", "")] + _strings(facts.get("markets")):
        if nm:
            own = own.replace(nm, " ")
    allowed_urls = set(_strings(facts.get("allowed_urls")))
    for u in URL_RE.findall(text):
        if u not in allowed_urls:
            return f"URL not allowed: {u}"
    allowed_mentions = {m.lstrip("@").lower() for m in _strings(facts.get("allowed_mentions"))}
    for m in _MENTION.findall(text):
        if m.lower() not in allowed_mentions:
            return f"@mention not allowed: @{m}"
    low = own.lower()
    for g in GAMBLING:
        if g in low:
            return f"gambling hype: '{g}'"
    if (m := POLITICS.search(own)):
        return f"politics word: '{m.group()}'"
    if (m := LABS.search(own)):
        return f"names a company or its people: '{m.group()}' (jabs target the AI characters only)"
    _, why = clean_quote(URL_RE.sub(" ", _MENTION.sub(" ", text)), 100_000)
    if why and "abuse" in why:
        return "abuse filter"
    # AI names: only the ones in the facts
    names = {a["name"].lower() for a in facts.get("ais", [])} | {s["name"].lower() for s in facts.get("standings", [])}
    names |= {n.lower() for n in _strings(facts.get("names"))}
    for n in AI_NAMES:
        if re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", own, re.I) and n.lower() not in names:
            return f"AI not in the facts: {n}"
    # numbers: every one from the facts (signed ones with their sign)
    fact_nums = set()
    for s in blob_parts:
        for sign, v in _numbers(s):
            fact_nums.add((sign, v))
            fact_nums.add(("", v))
    # a pair like '2-1' or '0-6' is a score or a record: only one that the facts carry (never a made-up score)
    fact_pairs = {(a, b) for s in blob_parts for a, b in _PAIR.findall(s)}
    for a, b in _PAIR.findall(own):
        if (a, b) not in fact_pairs:
            return f"score or record not in the facts: {a}-{b} (never invent a match score)"
    for sign, v in _numbers(own):
        if (sign, v) not in fact_nums:
            return f"number not in the facts: {sign}{v}"
    for w in re.findall(r"[a-z]+", low):
        if w in NUM_WORDS and ("", NUM_WORDS[w]) not in fact_nums:
            return f"number not in the facts: '{w}'"
    # names of things (teams, players): mid-sentence capitalised words must come from the facts
    blob_low = blob.lower()
    for line in own.split("\n"):
        for m in _TITLE.finditer(line):
            w = re.sub(r"['’]s?$", "", m.group())
            before = line[:m.start()].rstrip()
            if not before or not (before[-1].isalnum() or before[-1] in ",;&"):
                continue   # starts a sentence or a line
            if w in TITLE_OK or w.lower() in blob_low:
                continue
            return f"name not in the facts: '{w}'"
    # no jabs at clubs or players: an insult on a line naming the market's teams
    mwords = {w.lower() for w in _TITLE.findall(facts.get("market", "")) if w not in TITLE_OK and len(w) > 3}
    for line in own.split("\n"):
        if INSULTS.search(line) and any(re.search(rf"\b{re.escape(w)}\b", line, re.I) for w in mwords):
            return "insult on a line naming a team or player (roast the AIs' picks only)"
    # picks: a line about one AI that names an outcome must name that AI's own pick
    outcomes = [o for o in facts.get("outcomes", []) if o and o.lower() not in ("yes", "no")]
    for line in own.split("\n"):
        who = [a for a in facts.get("ais", []) if re.search(rf"(?<!\w){re.escape(a['name'])}(?!\w)", line, re.I)]
        if len(who) != 1 or who[0].get("pick") in (None, "no bet"):
            continue
        named = [o for o in outcomes if re.search(rf"(?<!\w){re.escape(o)}(?!\w)", line, re.I)]
        if named and not any(o.lower() == who[0]["pick"].lower() for o in named) and \
                not (facts.get("winner") and any(o == facts["winner"] for o in named)):
            return f"{who[0]['name']}'s pick is {who[0]['pick']}, not {named[0]}"
    # results: who won is the facts' winner
    if facts.get("winner"):
        for o in outcomes:
            if o != facts["winner"] and re.search(rf"(?<!\w){re.escape(o)}(?!\w)\s+(won|wins|beat|took it)\b",
                                                   own, re.I):
                return f"the winner is {facts['winner']}, not {o}"
    return None


# ---------------------------------------------------------------- templates (also the fallback when opai is out)

def fit_lines(parts: list[tuple[str, int]]) -> str:
    """parts: (line, drop) with drop 0 = required, higher = dropped earlier. Blank lines are '' with drop 0.
    Drops optional lines until the post fits MAX_LEN, then clips the longest line."""
    keep = [p for p in parts if p[0] is not None]
    for level in sorted({d for _, d in keep if d}, reverse=True) + [None]:
        text = "\n".join(t for t, _ in keep).strip("\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        if tweet_len(text) <= MAX_LEN:
            return text
        if level is None:
            break
        keep = [p for p in keep if p[1] != level]
    lines = [t for t, _ in keep]
    while tweet_len("\n".join(lines)) > MAX_LEN:
        i = max(range(len(lines)), key=lambda k: len(lines[k]))
        over = tweet_len("\n".join(lines)) - MAX_LEN
        lines[i] = clip(lines[i], max(10, len(lines[i]) - over - 1))
        if len(lines[i]) <= 10:
            break
    return clip(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)), MAX_LEN)


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " & " + names[-1]


def _hot_line(facts: dict, focus: list[str] | None = None) -> str | None:
    """The one storyline sentence: a streak (the focus AIs first), else a bad week, else the season leader."""
    ais = facts.get("ais", [])
    order = sorted(ais, key=lambda a: (a["name"] not in (focus or []),))
    for a in order:
        st = a.get("streak")
        if st and st.startswith("lost"):
            return f"{a['name']} has {st}. Someone check on {a['name']}."
    for a in order:
        st = a.get("streak")
        if st:
            return f"{a['name']} has {st}. Who stops this?"
    for a in order:
        wr = a.get("week_record")
        if wr and wr.startswith("0-") and wr != "0-0":
            return f"{a['name']} is {wr} this week."
    lead = next((s for s in facts.get("standings", []) if s["rank"].startswith("1st")), None)
    if lead:
        return f"{lead['name']} leads the season at {lead['pnl']}."
    return None


def _quote_line(a: dict | None, n: int = 100) -> str | None:
    if not a:
        return None
    q = a.get("quote") or a.get("rationale_line")
    return f"{a['name']}: \"{clip(q, n)}\"" if q else None


def _humans_line(facts: dict) -> str | None:
    h = facts.get("humans")
    if not h:
        return None
    if "right" in h:
        return f"Humans: {h['right']} of {h['picked']} right."
    return None


def reveal_template(facts: dict) -> str:
    ais = [a for a in facts["ais"] if a["pick"] != "no bet"]
    sides: dict[str, list[str]] = {}
    for a in ais:
        sides.setdefault(a["pick"], []).append(a["name"])
    sit = facts.get("situation")
    outs = " / ".join(facts.get("outcomes") or [])
    invite = f"Reply your pick: {outs} 👇" if outs and len(outs) <= 40 else "Reply your pick 👇"
    n = len(ais)
    if sit == "agree" and sides:
        (pick, names), = sides.items()
        hook = f"🚨 {n} AIs JUST LOCKED IN 🚨" if n > 1 else "🚨 PICK IS IN 🚨"
        body = f"{_names(names)}: {'ALL say' if n > 1 else 'says'} {pick.upper()}."
        q = "Are they all wrong?" if n > 1 else "Is it wrong?"
        focus = names
    elif sit == "alone":
        big = max(sides.items(), key=lambda kv: len(kv[1]))
        lone_pick, lone = next((k, v[0]) for k, v in sides.items() if len(v) == 1)
        hook = f"🚨 {lone.upper()} GOES ROGUE 🚨"
        body = f"{_names(big[1])}: {big[0]}.\n{lone}: {lone_pick}. Alone."
        q = f"Is {lone} mad or a genius?"
        focus = [lone]
    else:
        hook = "⚔️ THE AIs CAN'T AGREE ⚔️"
        body = "\n".join(f"{_names(v)}: {k}" for k, v in sides.items())
        q = "Who's right?"
        focus = []
    lone_a = next((a for a in ais if a["name"] in focus and (a.get("quote") or a.get("rationale_line"))), None)
    return fit_lines([(hook, 0), (facts["market"], 0), ("", 0), (body, 0), (_hot_line(facts, focus), 2),
                      (_quote_line(lone_a, 90), 3), ("", 0), (f"{q} {invite}", 0)])


def results_template(facts: dict) -> str:
    ais = [a for a in facts["ais"] if a["pick"] != "no bet"]
    right = [a for a in ais if a.get("result") == "right"]
    wrong = [a for a in ais if a.get("result") == "wrong"]
    w, sit, n = facts.get("winner"), facts.get("situation"), len(ais)
    focus: list[dict] = []
    if sit == "wipeout":
        hook = f"{n} AIs. ONE PICK. ALL WRONG. 💀"
        body = f"It was {w}.\nAll {n} said {wrong[0]['pick']}."
        q = "Would you have called it? 👇"
        focus = wrong
    elif sit == "sweep":
        hook = f"CLEAN SWEEP 🧹 {n}-0"
        body = f"It was {w}. All {n} AIs called it."
        q = "Too easy? 👇"
        focus = right
    elif sit == "lone_winner":
        hero = right[0]
        hook = f"{hero['name'].upper()} ALONE WAS RIGHT 🦸"
        body = f"It was {w}. {_names([a['name'] for a in wrong])} got it wrong."
        q = f"Can anyone stop {hero['name']}? 👇"
        focus = [hero]
    elif sit == "lone_loser":
        goat = wrong[0]
        hook = f"{goat['name'].upper()} BLEW IT 🤡"
        body = f"It was {w}. {_names([a['name'] for a in right])} cash in.\n{goat['name']} alone backed {goat['pick']}."
        q = f"Rate {goat['name']}'s miss 👇"
        focus = [goat]
    elif sit == "nobody" or (ais and not right):
        hook = "NOBODY HAD IT 💀"
        body = f"It was {w}. {'Not one AI' if n > 1 else _names([a['name'] for a in ais])} called it."
        q = "Who saw that coming? 👇"
        focus = wrong
    elif not ais:
        hook = "THE AIs SAT THIS ONE OUT 🙈"
        body = f"It was {w}."
        q = "Would you have called it? 👇"
    else:
        hook = f"SPLIT DECISION ⚖️ {facts.get('record') or ''}".rstrip()
        body = f"It was {w}.\n✅ {_names([a['name'] for a in right])}\n❌ {_names([a['name'] for a in wrong])}"
        q = "Who saw it coming? 👇"
        focus = wrong
    names = [a["name"] for a in focus]
    return fit_lines([(hook, 0), ("", 0), (facts["market"], 0), (body, 0), ("", 0), (_hot_line(facts, names), 2),
                      (_quote_line(focus[0] if focus else None, 90), 4), (_humans_line(facts), 3), (q, 0)])


# ---------------------------------------------------------------- digests (template only: emoji tables)

_LEAD = re.compile(r"^(will|is|does|do|can|did)\s+", re.I)
_TEAM_NOISE = {"fc", "sk", "afc", "cf", "us", "ss", "sc", "cd", "pfc", "fk", "ac", "as", "calcio", "club", "de",
               "sport", "lisboa", "e", "the", "rcd", "ud", "sd", "ca", "cs", "ssc", "acf", "vfl", "vfb", "tsg",
               "bk", "if", "ik", "nk", "hnk", "gnk", "rc", "sv", "fsv", "osc", "ogc"}


def _team(name: str, n: int = 12) -> str:
    words = [w for w in re.split(r"\s+", name.strip()) if w and w.lower().strip(".") not in _TEAM_NOISE
             and not re.fullmatch(r"\d+", w)]
    out = ""
    for w in words:
        if len(out) + len(w) + (1 if out else 0) > n and out:
            break
        out = f"{out} {w}".strip()
    return clip(out or name, n)


def short_label(rnd: dict) -> str:
    """'Sassuolo-Lazio draw?', 'Boca win?', 'BTC up?': a short line label for the digests."""
    q = " ".join((rnd.get("question") or "").split())
    m = re.match(r"(?i)will (.+?) vs\.? (.+?) end in a draw\?", q)
    if m:
        return f"{_team(m.group(1), 10)}-{_team(m.group(2), 10)} draw?"
    m = re.match(r"(?i)will (.+?) win on \d{4}-\d{2}-\d{2}\?", q)
    if m:
        return f"{_team(m.group(1), 16)} win?"
    m = re.match(r"(?i)(bitcoin|ethereum|solana|xrp)\s+up or down", q)
    if m:
        return {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "xrp": "XRP"}[m.group(1).lower()] + " up?"
    m = re.match(r"(?i)(?:[\w-]+:\s*)?(.+?) vs\.? (.+?)(?: \(|\s+-\s+|$)", q)
    if m:
        return f"{_team(m.group(1), 10)}-{_team(m.group(2), 10)}?"
    return clip(_LEAD.sub("", q), 24)


def _cells(rnd: dict, mode: str) -> str:
    by = {e["agent"]: e for e in rnd["entries"]}
    first = (rnd.get("outcomes") or [None])[0]
    out = ""
    for a in BOARD_ORDER:
        e = by.get(a)
        if not e or not e.get("outcome"):
            out += "⚪" if mode == "picks" else "➖"
        elif mode == "picks":
            out += "🟢" if e["outcome"] == first else "🔴"
        else:
            out += "✅" if e.get("won") else "❌"
    return out


LEGEND = "Claude Gemini ChatGPT Grok, left to right."


def _table(rounds: list[dict], head: str, tail: list[str], mode: str) -> str:
    rows = [(short_label(r), _cells(r, mode)) for r in rounds]
    for k in range(len(rows), 0, -1):
        width = max(len(lbl) for lbl, _ in rows[:k])
        lines = [f"{lbl.ljust(width)}  {c}" for lbl, c in rows[:k]]
        text = "\n".join([head, ""] + lines + ([f"+{len(rows) - k} more on the site"] if k < len(rows) else [])
                         + [""] + tail)
        if tweet_len(text) <= MAX_LEN:
            return text
    return fit_lines([(head, 0), ("", 0)] + [(t, 0) for t in tail])


def digest_text(rounds: list[dict]) -> str:
    """TODAY'S BOARD: one line per market, a dot per AI (🟢 backs Yes / the first outcome, 🔴 the other side)."""
    n_ai = len({e["agent"] for r in rounds for e in r["entries"] if e.get("outcome")})
    head = f"TODAY'S BOARD 🎰 {n_ai} AIs, {len(rounds)} markets"
    return _table(rounds, head, [LEGEND, "Spot the one they'll all get wrong 👇"], "picks")


def results_digest_text(rounds: list[dict]) -> str:
    """TODAY'S DAMAGE: ✅/❌ per AI per market, then the day's roast or hero line."""
    rec: dict[str, list[int]] = {}
    names = {}
    for r in rounds:
        for e in r["entries"]:
            if e.get("outcome"):
                names[e["agent"]] = e["name"]
                rec.setdefault(e["agent"], [0, 0])[0 if e.get("won") else 1] += 1
    frame = "Who called more than they did? 👇"
    if rec:
        best = max(rec, key=lambda a: (rec[a][0] - rec[a][1], rec[a][0]))
        worst = min(rec, key=lambda a: (rec[a][0] - rec[a][1], -rec[a][1]))
        bw, bl = rec[best]
        ww, wl = rec[worst]
        if bl == 0 and bw >= 2:
            frame = f"{names[best]} went {bw}-0. Bow down. 👑"
        elif wl > ww:
            frame = f"{names[worst]} went {ww}-{wl}. Someone check on {names[worst]}."
        else:
            frame = f"{names[best]} wins the day at {bw}-{bl}."
    head = f"TODAY'S DAMAGE 💀 {len(rounds)} markets settled"
    return _table(rounds, head, [LEGEND, frame], "results")


# ---------------------------------------------------------------- the writer

def writer_payload(kind: str, facts: dict, n: int, feedback: str | None, rejected: list[str] | None) -> dict:
    p = {"kind": kind, "n_posts": n, "max_chars": MAX_LEN, "facts": facts}
    if feedback:
        p["rejected_draft"] = rejected
        p["why_rejected"] = feedback
    return p


async def compose(s: Settings, db: DB, kind: str, facts: dict, template, subject: str, n: int = 1,
                  check=None) -> tuple[list[str], str]:
    """(texts, source): the writer's n posts when they pass the validator (one retry with its feedback), else the
    template's. source: 'writer' / 'template'. template() -> str | list[str]. check(texts) -> extra error or None."""
    from . import opai
    ok, _ = opai.available(s, db, "post")
    if ok:
        feedback, rejected = None, None
        for _attempt in range(2):
            box: dict = {}

            def val(data, box=box):
                texts = data.get("texts") or []
                if len(texts) != n:
                    box["err"] = f"expected {n} posts, got {len(texts)}"
                    return box["err"]
                for t in texts:
                    if err := validate(t, facts):
                        box["err"], box["texts"] = err, texts
                        return err
                if check and (err := check(texts)):
                    box["err"], box["texts"] = err, texts
                    return err
                return None
            try:
                res = await asyncio.wait_for(
                    opai.acall(s, db, "post", writer_payload(kind, facts, n, feedback, rejected), subject=subject,
                               validate=val), timeout=s.post_writer_deadline_s)
            except asyncio.TimeoutError:
                log.warning("post writer for %s: no answer in %ss; template", subject, s.post_writer_deadline_s)
                break
            except Exception:
                log.exception("post writer for %s failed; template", subject)
                break
            if res is not None:
                return [t.strip() for t in res.data["texts"]], "writer"
            if "err" not in box:   # unavailable / crashed (not a rejection): no retry
                break
            feedback, rejected = box["err"], box.get("texts")
            log.info("post writer for %s rejected: %s", subject, feedback)
    out = template()
    return (out if isinstance(out, list) else [out]), "template"


# ---------------------------------------------------------------- phase 2: debate, hype, confession, hot take,
# humans' week, grudge match, soap opera (templates; the writer gets the same facts plus a brief)

def debate_template(facts: dict) -> list[str]:
    """One reply per AI that bet (its own pick, its own words), then the Pit Boss closes."""
    out = []
    for a in facts["ais"]:
        if a["pick"] == "no bet":
            continue
        words = a.get("quote") or a.get("rationale_line")
        out.append(fit_lines([(f"{a.get('emoji', '')} {a['name'].upper()}: {a['pick'].upper()}.".strip(), 0),
                              (f"\"{words}\"" if words else None, 1),
                              (f"{a['name']} has {a['streak']}." if a.get("streak") else None, 2)]))
    sides: dict[str, int] = {}
    for a in facts["ais"]:
        if a["pick"] != "no bet":
            sides[a["pick"]] = sides.get(a["pick"], 0) + 1
    tally = " vs ".join(f"{k} {v}" for k, v in sides.items())
    out.append(fit_lines([("THE PIT BOSS 🎙️", 0), (f"{tally}. You heard them." if len(sides) > 1 else
                                                     "All on the same side. Brave or lazy?", 0),
                          ("Who wins the argument? 👇", 0)]))
    return out


def debate_check(facts: dict):
    """Each AI's line names that AI and its own pick; the close asks the room."""
    bettors = [a for a in facts["ais"] if a["pick"] != "no bet"]

    def check(texts: list[str]) -> str | None:
        for a, t in zip(bettors, texts):
            if a["name"].lower() not in t.lower():
                return f"post for {a['name']} must name {a['name']}"
            if a["pick"].lower() not in t.lower():
                return f"post for {a['name']} must carry its pick {a['pick']}"
        if "who wins the argument" not in texts[-1].lower():
            return "the last post must end with 'Who wins the argument? 👇'"
        return None
    return check


def hype_facts(rnd: dict, kickoff_hhmm: str, minutes: int) -> dict:
    """No picks at all: the hype post never shows one (sealed or not)."""
    n = len(rnd["entries"])
    return {"kind": "hype", "market": market_name(rnd), "outcomes": list(rnd.get("outcomes") or []),
            "n_ais": n, "kickoff_utc": kickoff_hhmm, "countdown": f"{minutes} minutes" if minutes < 60 else "1 hour",
            "ais": [{"name": e["name"], "persona": PERSONAS.get(e["agent"], ""), "pick": "no bet"}
                    for e in rnd["entries"]],
            "brief": "Pre-match hype about 1 hour before kickoff. The AIs' picks are NOT in the facts and must not "
                     "be guessed. Build anticipation, give the kickoff time, ask who readers are backing."}


def hype_template(facts: dict) -> str:
    return fit_lines([("⏱️ KICKOFF IN " + facts["countdown"].upper(), 0), ("", 0), (facts["market"], 0),
                      (f"{facts['n_ais']} AIs have their picks in. Kickoff {facts['kickoff_utc']} UTC.", 0), ("", 0),
                      ("Who are you backing? 👇", 0)])


def confession_pick(rnd: dict, board: list[dict], min_conf: float = 0.7) -> dict | None:
    """The AI that owes a confession on a resolved round: lost with confidence >= min_conf, or now on a losing
    streak of 3+; it needs its own words (quote or rationale) to confess with. Worst case first."""
    by = {b["agent"]: b for b in board}
    best = None
    for e in rnd["entries"]:
        if not e.get("outcome") or e.get("won") or not (e.get("quote") or e.get("rationale")):
            continue
        streak = (by.get(e["agent"]) or {}).get("streak") or 0
        conf = e.get("confidence") or 0
        if conf >= min_conf or streak <= -3:
            key = (-streak, conf)
            if best is None or key > best[0]:
                best = (key, e)
    return best[1] if best else None


def confession_facts(rnd: dict, e: dict, board: list[dict], rounds: list[dict], now: float) -> dict:
    f = round_facts("confession", rnd, board, rounds, now)
    f["ais"] = [a for a in f["ais"] if a["name"] == e["name"]]
    f["brief"] = (f"AI confession: {e['name']} lost. Quote its own line verbatim, say what happened (the winner), "
                  f"its streak or season tokens, and ask readers to rate the take.")
    f["rate_scale"] = "1–10"
    return f


def confession_template(facts: dict) -> str:
    a = facts["ais"][0]
    words = a.get("quote") or a.get("rationale_line")
    tail = f"{a['name']}: {a['streak']}." if a.get("streak") else \
        (f"{a['name']} is on {a['season_pnl']} tokens this season." if a.get("season_pnl") else None)
    return fit_lines([(f"WHAT WAS {a['name'].upper()} THINKING? 🤔", 0), ("", 0), (f"\"{words}\"", 0),
                      (f"— {a['name']}" + (f", at {a['confidence']} confidence" if a.get("confidence") else ""), 0),
                      ("", 0), (facts["market"], 2), (f"It was {facts['winner']}.", 0), (tail, 1), ("", 0),
                      ("Rate this take 1–10 👇", 0)])


HOT_TAKES = ("top", "bottom", "top", "humans")


def hot_take_facts(board: list[dict], humans: dict | None, day_ordinal: int) -> dict | None:
    """Leaderboard-backed question of the day (rotating frames). None without a board."""
    if len(board) < 2:
        return None
    frame = HOT_TAKES[day_ordinal % len(HOT_TAKES)]
    if frame == "humans" and not (humans and humans.get("picks", 0) >= 5):
        frame = "top"
    st = standings_facts(board)
    f = {"kind": "hot_take", "frame": frame, "standings": st, "n_ais": len(board),
         "ais": [{"name": b["name"], "persona": PERSONAS.get(b["agent"], ""), "pick": "no bet"} for b in board],
         "brief": "Hot take of the day: one provocative question about the standings, backed by the numbers in "
                  "facts, then 'Discuss 👇'. Spicy about the AI characters, never about people or companies."}
    if frame == "humans":
        f["humans_week"] = {"wins": humans["wins"], "picks": humans["picks"]}
    return f


def hot_take_template(facts: dict) -> str:
    st = facts["standings"]
    top, bottom = st[0], st[-1]
    if facts["frame"] == "bottom":
        head = f"Is {bottom['name']} the worst gambler in the pit?"
        focus, rest = bottom, [s for s in st if s is not bottom][:2]
    elif facts["frame"] == "humans":
        h = facts["humans_week"]
        head = "Are humans better at this than the AIs?"
        return fit_lines([(head, 0), ("", 0), (f"Humans this week: {h['wins']} right of {h['picks']} picks.", 0),
                          (f"Best AI this season: {top['name']} at {top['pnl']}.", 0), ("", 0), ("Discuss 👇", 0)])
    else:
        head = f"Is {top['name']} secretly the best forecaster in AI?"
        focus, rest = top, st[2:] if len(st) > 2 else st[1:]
    return fit_lines([(head, 0), ("", 0), (f"This season: {focus['pnl']} tokens, {focus['rank']}.", 0),
                      (" ".join(f"{s['name']}: {s['pnl']}." for s in rest), 1), ("", 0), ("Discuss 👇", 0)])


def humans_week_rows(db: DB, lo: float, hi: float) -> list[dict]:
    """[{username, picks, wins}] for X humans whose picks (replies to our reveals) resolved in [lo, hi)."""
    rows = db.q("SELECT h.username, COUNT(*) picks, SUM(h.won) wins FROM humans h JOIN rounds r USING(round_id)"
                " WHERE r.state='resolved' AND r.resolved_at>=? AND r.resolved_at<? AND h.won IS NOT NULL AND"
                " h.username IS NOT NULL GROUP BY h.username ORDER BY wins DESC, picks ASC", lo, hi)
    return [dict(r) for r in rows]


def humans_week_facts(rows: list[dict], week_rec: dict[str, tuple[int, int]], names: dict[str, str],
                      min_picks: int = 3, champ_picks: int = 5) -> dict | None:
    """The humans who beat the AIs this week (accuracy above the best AI's, at least min_picks picks), the week's
    champion (best accuracy with champ_picks picks) and the AI they face next week. None when nobody qualifies."""
    if not week_rec:
        return None
    best_ai = max(week_rec, key=lambda a: (week_rec[a][0] / max(1, sum(week_rec[a])), week_rec[a][0]))
    bw, bl = week_rec[best_ai]
    bar = bw / max(1, bw + bl)
    beat = [r for r in rows if r["picks"] >= min_picks and r["wins"] / r["picks"] > bar][:3]
    champs = sorted((r for r in rows if r["picks"] >= champ_picks),
                    key=lambda r: (-r["wins"] / r["picks"], -r["wins"]))
    if not beat and not champs:
        return None
    champ = champs[0] if champs else None
    f = {"kind": "humans_week", "humans": [{"username": "@" + r["username"], "record": f"{r['wins']}-{r['picks'] - r['wins']}"}
                                           for r in beat],
         "best_ai": {"name": names[best_ai], "record": f"{bw}-{bl}"},
         "ais": [{"name": names[best_ai], "pick": "no bet"}],
         "allowed_mentions": ["@" + r["username"] for r in beat] + ([f"@{champ['username']}"] if champ else []),
         "brief": "Weekly humans shout-out: crown the humans who beat the AIs this week (@mention only the listed "
                  "humans), the best AI's record for comparison, and the champion's 1v1 challenge next week."}
    if champ:
        f["champion"] = {"username": "@" + champ["username"], "record": f"{champ['wins']}-{champ['picks'] - champ['wins']}",
                         "next_opponent": names[best_ai]}
    return f


def humans_week_template(facts: dict) -> str:
    beat = " · ".join(f"{h['username']} {h['record']}" for h in facts["humans"])
    c = facts.get("champion")
    return fit_lines([("HUMANS OF THE PIT 👑", 0), ("", 0),
                      (f"Beat the AIs this week: {beat}" if beat else None, 0),
                      (f"Best AI: {facts['best_ai']['name']} {facts['best_ai']['record']}.", 0),
                      (f"Champion: {c['username']} ({c['record']}). Next week: head to head vs {c['next_opponent']}." if c
                       else None, 0), ("", 0), ("Think you can do better? Reply your pick on our reveals 👇", 1)])


def grudge_pair(board: list[dict]) -> tuple[str, str] | None:
    """The two AIs closest in the standings (net tokens)."""
    if len(board) < 2:
        return None
    pairs = [(abs(a["net_pnl"] - b["net_pnl"]), a["agent"], b["agent"]) for i, a in enumerate(board)
             for b in board[i + 1:]]
    _, x, y = min(pairs)
    return x, y


def head_to_head(rounds: list[dict], a: str, b: str, lo: float, hi: float) -> dict | None:
    """Tokens each won on the rounds both bet, resolved in [lo, hi). None without a shared round."""
    tot = {a: 0.0, b: 0.0}
    n = 0
    for r in rounds:
        if r["state"] != "resolved" or r.get("exhibition") or not (lo <= (r.get("resolved_at") or 0) < hi):
            continue
        es = {e["agent"]: e for e in r["entries"] if e.get("outcome")}
        if a in es and b in es:
            n += 1
            tot[a] += es[a].get("pnl") or 0
            tot[b] += es[b].get("pnl") or 0
    return {"n": n, a: tot[a], b: tot[b]} if n else None


def grudge_template(facts: dict) -> str:
    last = facts.get("last")
    lines = [("GRUDGE MATCH 🥊", 0), ("", 0)]
    if last:
        lines += [(f"Last week: {last['winner']} {last['winner_pnl']} vs {last['loser']} {last['loser_pnl']} "
                   f"on {last['n']} shared markets.", 0),
                  (f"{last['loser']} wears the 💩 this week.", 0), ("", 0)]
    nxt = facts["next"]
    lines += [(f"This week: {nxt['a']} vs {nxt['b']}, {nxt['gap']} tokens apart.", 0),
              ("Head to head on every market they both bet.", 1), ("", 0), ("Who takes it? 👇", 0)]
    return fit_lines(lines)


# ---------- the season soap opera ----------

def soap_arcs(board: list[dict], prev: dict | None) -> dict[str, str]:
    """Open storylines per AI from the standings, against the previous episode's ranks."""
    prev_rank = (prev or {}).get("ranks", {})
    arcs = {}
    for b in board:
        name, rank, streak = b["name"], b["rank"], b.get("streak") or 0
        was = prev_rank.get(b["agent"])
        if was == 1 and rank != 1:
            arcs[b["agent"]] = f"{name} fell from #1 to {ordinal(rank)}"
        elif rank == 1 and was and was != 1:
            arcs[b["agent"]] = f"{name} took #1"
        elif streak <= -3:
            arcs[b["agent"]] = f"{name} has lost {-streak} straight"
        elif streak >= 3:
            arcs[b["agent"]] = f"{name} has won {streak} straight"
        elif was and rank < was:
            arcs[b["agent"]] = f"{name} climbed to {ordinal(rank)}"
    return arcs


def soap_facts(board: list[dict], prev: dict | None, episode: int, upcoming: list[dict]) -> dict | None:
    """'THE PIT, EPISODE n': the headline arc, last episode's line, and a cliffhanger on an open round (picks
    already public: state awaiting_resolution only) where the arc's AI has a pick. None without an arc."""
    arcs = soap_arcs(board, prev)
    if not arcs:
        return None
    order = sorted(arcs, key=lambda a: ("fell" not in arcs[a], "took" not in arcs[a], "lost" not in arcs[a]))
    focus = order[0]
    cliff = None
    for r in upcoming:
        if r.get("sealed") or r["state"] != "awaiting_resolution":
            continue
        e = next((x for x in r["entries"] if x["agent"] == focus and x.get("outcome")), None)
        if e:
            cliff = {"ai": e["name"], "pick": e["outcome"], "market": market_name(r), "label": short_label(r)}
            break
    names = {b["agent"]: b["name"] for b in board}
    return {"kind": "soap", "episode": episode, "headline": arcs[focus],
            "previously": (prev or {}).get("headline"), "other_arcs": [arcs[a] for a in order[1:3]],
            "cliffhanger": cliff, "standings": standings_facts(board),
            "ais": [{"name": names[a], "pick": (cliff["pick"] if cliff and names[a] == cliff["ai"] else "no bet")}
                    for a in names],
            "markets": [cliff["market"]] if cliff else [],
            "brief": "Season soap opera episode: 'THE PIT, EPISODE n 📺', a 'Previously:' line (from facts), the "
                     "headline storyline, and a cliffhanger tied to the upcoming result in facts."}


def soap_template(facts: dict) -> str:
    c = facts.get("cliffhanger")
    return fit_lines([(f"THE PIT, EPISODE {facts['episode']} 📺", 0), ("", 0),
                      (f"Previously: {facts['previously']}." if facts.get("previously") else None, 2),
                      (f"Now: {facts['headline']}.", 0),
                      (f"Also: {'; '.join(facts['other_arcs'])}." if facts.get("other_arcs") else None, 3), ("", 0),
                      (f"Next up: {c['label']} {c['ai']} says {c['pick']}." if c else None, 1),
                      ("Stay tuned 👇", 0)])
