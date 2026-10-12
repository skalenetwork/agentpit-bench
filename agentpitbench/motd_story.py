"""Match of the Day v0.5: the scripted film. Each film gets its own concept (a genre with its own look, pacing and
voice), a story written fresh by the operator AI, and one TRUE twist detected from data (twists.py).

Division of labour, which is the credibility rule:
- Code owns every fact about the round. Captions about the round carry numbers only through slots such as
  {claude_price}, filled here from the record; the data panels (picks, reveal, standings) are built by code.
- The writer (opai task "motd_script", `claude -p`, no tools) owns words: the concept, the cold open, the plain-words
  market line, a "who is this" background scene from general knowledge, the characters, the misdirection and the
  dramatization of the twist code chose. `validate` rejects anything that breaks the rules; one retry, then the
  caller falls back to the templated film (motd.script). Posting is never blocked.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import twists as tw
from .cards import results_headline, headline_variant, ranked, start_price
from .config import AGENT_NAMES

TASK = "motd_script"
PROMPT_VERSION = "motd_script.v1"
RECENT_FILMS = 4          # concept and twist types of this many recent films are avoided
CPS = 15.0                # caption reading speed, characters per second (readable on a phone)
MIN_S, MAX_S = 45.0, 60.0

# ---------- concepts: genre, look, pacing ----------
# theme keys drive motd.html: palette, display + caption fonts, the scene transition, caption treatment and the music
# variant (music.VARIANTS). "brief" is what the writer is told about the voice.
CONCEPTS: dict[str, dict] = {
    "noir": {"label": "Noir detective", "brief": "first-person hard-boiled detective voice-over, rain, smoke, short "
             "clipped sentences, the case file of a bet gone wrong",
             "theme": {"bg1": "#07080a", "bg2": "#1b1d22", "ink": "#e9e4d8", "accent": "#d9b44a", "panel": "#15171c",
                       "font": "Special Elite", "cap": "Special Elite", "trans": "iris", "fx": "type",
                       "texture": "grain", "music": "noir"}},
    "nature_doc": {"label": "Nature documentary", "brief": "hushed, reverent wildlife-documentary narrator observing "
                   "four rare creatures in their habitat",
                   "theme": {"bg1": "#0c1f17", "bg2": "#1f3b2a", "ink": "#f3efe2", "accent": "#e6c36a",
                             "panel": "#163024", "font": "Playfair Display", "cap": "Lora", "trans": "fade",
                             "fx": "fade", "texture": "vignette", "music": "nature"}},
    "heist": {"label": "Heist", "brief": "slick heist-movie crew assembling for one job, the plan, the vault, "
              "the double-cross",
              "theme": {"bg1": "#06111f", "bg2": "#0d2a4a", "ink": "#e8f1ff", "accent": "#3fd0ff", "panel": "#0b2340",
                        "font": "Bebas Neue", "cap": "Inter", "trans": "wipe", "fx": "up", "texture": "blueprint",
                        "music": "heist"}},
    "courtroom": {"label": "Courtroom drama", "brief": "a trial: the case, the four witnesses, cross-examination, "
                  "the verdict and the revelation that turns the courtroom",
                  "theme": {"bg1": "#1a0f08", "bg2": "#3b2414", "ink": "#f6ead6", "accent": "#e3a857",
                            "panel": "#2c1b10", "font": "Playfair Display", "cap": "Lora", "trans": "wipe",
                            "fx": "fade", "texture": "vignette", "music": "courtroom"}},
    "broadcast": {"label": "Sports broadcast", "brief": "breathless live sports-commentary booth calling the action "
                  "play by play, with a studio replay at the end",
                  "theme": {"bg1": "#0a0f2c", "bg2": "#18237a", "ink": "#ffffff", "accent": "#ffd400",
                            "panel": "#121a5c", "font": "Bebas Neue", "cap": "Inter", "trans": "wipe", "fx": "slam",
                            "texture": "stripes", "music": "broadcast"}},
    "countdown": {"label": "Thriller countdown", "brief": "ticking-clock thriller, tension rising scene by scene, "
                  "terse present tense",
                  "theme": {"bg1": "#0a0000", "bg2": "#2a0505", "ink": "#ffecec", "accent": "#ff2a2a",
                            "panel": "#220707", "font": "Oswald", "cap": "Inter", "trans": "flash", "fx": "slam",
                            "texture": "scan", "music": "countdown"}},
    "mockumentary": {"label": "Mockumentary", "brief": "deadpan office-style mockumentary, talking heads and "
                     "awkward pauses, dry understatement",
                     "theme": {"bg1": "#f3efe6", "bg2": "#e2dccd", "ink": "#1d1d1d", "accent": "#d2452c",
                               "panel": "#ffffff", "font": "Archivo Black", "cap": "Inter", "trans": "cut",
                               "fx": "fade", "texture": "none", "music": "mockumentary", "light": True}},
    "western": {"label": "Western standoff", "brief": "spaghetti-western showdown at high noon, dust, slow stares, "
                "laconic lines",
                "theme": {"bg1": "#2b1606", "bg2": "#8a4b17", "ink": "#fff1d6", "accent": "#ffb347",
                          "panel": "#3d200a", "font": "Rye", "cap": "Lora", "trans": "iris", "fx": "fade",
                          "texture": "sepia", "music": "western"}},
    "mission_control": {"label": "Space mission control", "brief": "mission control during a launch: call signs, "
                        "checklists, go/no-go, the anomaly",
                        "theme": {"bg1": "#020611", "bg2": "#071a33", "ink": "#d9f3ff", "accent": "#5cf2a0",
                                  "panel": "#06162b", "font": "Share Tech Mono", "cap": "Share Tech Mono",
                                  "trans": "wipe", "fx": "type", "texture": "grid", "music": "mission"}},
}

KINDS = ("cold_open", "title", "market", "background", "character", "misdirect", "result", "twist", "coda")
# minimum and maximum seconds per scene kind (data panels need time beyond the caption)
DUR = {"cold_open": (2.0, 3.0), "title": (2.0, 2.5), "market": (4.0, 6.5), "background": (4.5, 8.0),
       "character": (4.0, 7.0), "misdirect": (2.5, 5.5), "result": (5.0, 7.5), "twist": (5.5, 9.0),
       "coda": (4.5, 7.0)}
MAX_CHARS = {"cold_open": 44, "title": 32, "market": 90, "background": 140, "character": 90, "misdirect": 80,
             "result": 80, "twist": 120, "coda": 70}
BUDGET_CHARS = 650        # every caption together, slots filled: what a 45-60 s film can carry at CPS


SCHEMA = {
    "type": "object",
    "required": ["concept_type", "title", "scenes"],
    "properties": {
        "concept_type": {"type": "string", "enum": list(CONCEPTS)},
        "title": {"type": "string"},
        "background_subject": {"type": "string"},
        "scenes": {"type": "array", "minItems": 8, "maxItems": 16, "items": {
            "type": "object", "required": ["kind", "text"],
            "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                           "agent": {"type": "string"},
                           "text": {"type": "string"}}}},
    },
}

# ---------- slots: the only way a round number reaches a caption ----------

def _date_words(iso: str | None) -> str | None:
    if not iso:
        return None
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{d.day} {d.strftime('%B')}"


def slot_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def slots(rnd: dict, ctx: dict) -> dict[str, str]:
    """{placeholder: caption text}, every value read from the round record (and the transcripts' link counts)."""
    from .twists import cents, pct, secs
    out: dict[str, str] = {}
    for e in rnd["entries"]:
        k = slot_key(e["name"])
        out[f"{k}_pick"] = e.get("outcome") or "no bet"
        if e.get("outcome"):
            out[f"{k}_price"] = cents(e.get("avg_price"))
            out[f"{k}_conf"] = pct(e.get("confidence"))
            out[f"{k}_pnl"] = (f"+{e['pnl']:,.0f}" if e["pnl"] >= 0 else f"−{abs(e['pnl']):,.0f}") if e.get("pnl") is not None else "—"
            out[f"{k}_time"] = secs(e.get("decided_s"))
        src = (ctx.get("sources") or {}).get(e["agent"])
        if src:
            out[f"{k}_links"] = str(src)
    c = rnd.get("crowd") or {}
    if c.get("outcome"):
        out["fav"] = c["outcome"]
        out["fav_price"] = cents(c.get("price"))
        others = [o for o in rnd["outcomes"] if o != c["outcome"]]
        if len(others) == 1:
            out["dog"] = others[0]
            out["dog_price"] = cents(start_price(rnd, others[0]))
    if rnd.get("winner"):
        out["winner"] = rnd["winner"]
    b = [e for e in rnd["entries"] if e.get("outcome")]
    out["n_ai"] = str(len(b))
    out["n_right"] = str(sum(1 for e in b if e.get("won")))
    out["stake"] = f"{rnd.get('stake') or 100:.0f}"
    dw = _date_words(rnd.get("end_date"))
    if dw:
        out["close_date"] = dw
    return out


_SLOT = re.compile(r"\{([a-z0-9_]+)\}")


def fill(text: str, sl: dict[str, str]) -> str:
    return _SLOT.sub(lambda m: sl.get(m.group(1), m.group(0)), text)


# ---------- the writer's input ----------

def payload(rnd: dict, ctx: dict, twist: dict, concepts: list[str], lines: dict[str, list[str]]) -> dict:
    sl = slots(rnd, ctx)
    ents = []
    for e in rnd["entries"]:
        ents.append({"name": e["name"], "pick": e.get("outcome"), "won": e.get("won"),
                     "entry_price": tw.cents(e.get("avg_price")) if e.get("outcome") else None,
                     "confidence": tw.pct(e.get("confidence")) if e.get("outcome") else None,
                     "decided_after": tw.secs(e.get("decided_s")) if e.get("outcome") else None,
                     "rationale": e.get("rationale"), "card_quote": e.get("quote"),
                     "post_match_statement": e.get("statement") if e.get("won") is False else None,
                     "transcript_lines": (lines.get(e["agent"]) or [])[-5:],
                     "no_bet_reason": None if e.get("outcome") else (e.get("exit_reason") or "no bet")})
    return {
        "market_question": rnd["question"], "outcomes": rnd["outcomes"], "winner": rnd.get("winner"),
        "closes": rnd.get("end_date"), "category_hint": ctx.get("category"),
        "start_prices": {o: tw.cents(start_price(rnd, o)) for o in rnd["outcomes"]},
        "agents": ents,
        "twist": {"type": twist["type"], "fact": twist["fact"], "agents": twist["names"]},
        "allowed_concepts": {k: CONCEPTS[k]["label"] + ": " + CONCEPTS[k]["brief"] for k in concepts},
        "slots": sl, "caption_budget_chars": BUDGET_CHARS,
        "max_chars_per_scene": MAX_CHARS,
    }


# ---------- the validator ----------

_NUMWORDS = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|"
    r"billion|percent|per cent|cents?|half|quarter|dozen|twice|thrice|single-digit)\b", re.I)
_PUTDOWN = re.compile(
    r"\b(stupid|dumb|idiot\w*|moron\w*|fool(s|ish)?|clueless|pathetic|embarrass\w*|humiliat\w*|clown\w*|trash|"
    r"garbage|loser|losers|choked?|incompetent|useless|brain-?dead|brainless|witless|dimwit\w*|dense|inept|"
    r"hopeless|laughable|joke of|dunce|halfwit\w*|nitwit\w*|imbecil\w*)\b", re.I)
# playful rivalry between the AIs, never at the labs (operator, 2026-10-12): the makers are not characters
_LABS = re.compile(r"\b(openai|anthropic|google|deepmind|xai|x\.ai|microsoft|meta|altman|musk|amodei|hassabis|"
                   r"pichai|nadella)\b", re.I)
_SIGNIF = re.compile(
    r"\b(statistically|significan\w*|proves?|proven|proof|smartest|best ai|better than the crowd|beats? the crowd|"
    r"superior|always wins|never loses|guaranteed|the best forecaster|outperform\w*)\b", re.I)
_WON = re.compile(r"\b(won|cashed in|was right|got it right|got paid|collected|rides? off richer)\b", re.I)
_LOST = re.compile(r"\b(lost|was wrong|got it wrong|busted|went bust)\b", re.I)
AFTER = ("result", "twist", "coda")  # win/loss wording is checked once the result is known


def _sentences(t: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?…])\s+|\n", t) if x.strip()]


def durations(scenes: list[dict], sl: dict[str, str], quotes: dict[str, str | None]) -> list[float]:
    """Seconds per scene: caption reading time at CPS plus one second, inside the kind's bounds. Character
    scenes add reading time for the card quote shown in the panel (quotes: agent name -> quote)."""
    out = []
    for s in scenes:
        lo, hi = DUR[s["kind"]]
        n = len(fill(s["text"], sl))
        extra = 0.0
        if s["kind"] == "character":
            q = quotes.get(s.get("agent") or "") or ""
            extra = min(2.5, len(q) / (CPS * 1.6))
        out.append(round(min(hi + extra, max(lo, n / CPS + 0.8 + extra)), 2))
    return out


def validate(data: dict, pl: dict) -> str | None:
    """None if the script may be filmed, else the first reason it may not."""
    sl = pl["slots"]
    scenes = data.get("scenes") or []
    if data.get("concept_type") not in pl["allowed_concepts"]:
        return f"concept_type must be one of {sorted(pl['allowed_concepts'])}"
    kinds = [s.get("kind") for s in scenes]
    if kinds[:1] != ["cold_open"]:
        return "the first scene must be the cold_open"
    for k in ("title", "market", "background", "result", "twist", "coda"):
        if kinds.count(k) != 1:
            return f"exactly one {k} scene is required (found {kinds.count(k)})"
    if kinds[-1] != "coda":
        return "the last scene must be the coda"
    if not kinds.index("result") < kinds.index("twist"):
        return "the twist comes after the result"
    if "misdirect" not in kinds[:kinds.index("result")]:
        return "at least one misdirect scene is required before the result"
    names = [a["name"] for a in pl["agents"]]
    chars = [s.get("agent") for s in scenes if s.get("kind") == "character"]
    if sorted(chars) != sorted(names):
        return f"one character scene per agent, agent field exactly one of {names} (got {chars})"
    if not kinds.index("market") < min(i for i, k in enumerate(kinds) if k == "character") < kinds.index("result"):
        return "order: market before the characters, characters before the result"
    pick = {a["name"]: a["pick"] for a in pl["agents"]}
    won = {a["name"]: a["won"] for a in pl["agents"]}
    outcomes = pl["outcomes"]
    title = data.get("title") or ""
    if re.search(r"\d", title) or _NUMWORDS.search(title) or len(title) > MAX_CHARS["title"]:
        return "the title must be short and contain no numbers"
    for i, s in enumerate(scenes):
        t = s.get("text") or ""
        k = s["kind"]
        if not t.strip():
            return f"scene {i} ({k}) is empty"
        for m in _SLOT.findall(t):
            if m not in sl:
                return f"scene {i} ({k}): unknown slot {{{m}}}"
        if re.search(r"[{}]", _SLOT.sub("", t)):
            return f"scene {i} ({k}): stray brace"
        bare = _SLOT.sub("", t)
        if len(fill(t, sl)) > MAX_CHARS[k]:
            return f"scene {i} ({k}) is too long ({len(fill(t, sl))} > {MAX_CHARS[k]} characters)"
        if k == "background":
            for num in re.findall(r"\d[\d,]*(?:\.\d+)?", bare):
                if not (re.fullmatch(r"\d{4}s?", num) and 1850 <= int(num[:4]) <= datetime.now(timezone.utc).year):
                    return f"background: only plausible years may be written as digits (got {num})"
            if re.search(r"[%$€£¢]", bare):
                return "background: no prices or percentages"
        else:
            if re.search(r"\d", bare):
                return f"scene {i} ({k}): digits must come from slots, not text"
            m = _NUMWORDS.search(bare)
            if m:
                return f"scene {i} ({k}): number word '{m.group(0)}' (round numbers come from slots only)"
        m = _PUTDOWN.search(bare)
        if m:
            return f"scene {i} ({k}): put-down '{m.group(0)}'"
        m = _LABS.search(bare)  # the background may need one (a market about a lab); captions about AIs may not
        if m and k != "background" and not _LABS.search(pl.get("market_question") or ""):
            return f"scene {i} ({k}): names a lab or its people ('{m.group(0)}'); the AIs are the characters"
        m = _SIGNIF.search(bare)
        if m:
            return f"scene {i} ({k}): significance or superiority claim '{m.group(0)}'"
        for sent in _sentences(fill(t, sl)):
            who = [n for n in names if re.search(r"\b" + re.escape(n) + r"\b", sent)]
            if len(who) != 1 and not (k == "character" and len(who) == 0):
                continue
            n = who[0] if who else s.get("agent")
            if not n:
                continue
            for o in outcomes:
                said = re.search(r"(?<![\w])" + re.escape(o) + r"(?![\w])", sent)
                if said and o != pick.get(n) and pick.get(n) and not re.search(r"\b(not|against|n't|over)\b", sent):
                    return f"scene {i} ({k}): '{sent}' ties {n} to {o}, but {n} picked {pick[n]}"
            if k in AFTER and won.get(n) is False and _WON.search(sent) \
                    and not _LOST.search(sent):
                return f"scene {i} ({k}): '{sent}' says {n} won, but {n} lost"
            if k in AFTER and won.get(n) is True and _LOST.search(sent) \
                    and not _WON.search(sent):
                return f"scene {i} ({k}): '{sent}' says {n} lost, but {n} won"
        if k == "character" and s.get("agent") not in pick:
            return f"scene {i}: unknown agent {s.get('agent')}"
    tw_scene = next(s for s in scenes if s["kind"] == "twist")
    tw_names = pl["twist"]["agents"]
    if tw_names and not any(re.search(r"\b" + re.escape(n) + r"\b", fill(tw_scene["text"], sl)) for n in tw_names):
        return f"the twist scene must name {' or '.join(tw_names)} (the twist is: {pl['twist']['fact']})"
    chars = sum(len(fill(s.get("text") or "", sl)) for s in scenes)
    if chars > BUDGET_CHARS:
        return f"too long: {chars} caption characters in total, the budget is {BUDGET_CHARS}; cut words"
    total = sum(durations(scenes, sl, {a["name"]: a.get("card_quote") for a in pl["agents"]}))
    if total > MAX_S:
        return f"too long: about {total:.0f} s at reading speed; cut words (the film must fit {MAX_S:.0f} s)"
    return None


# ---------- the scene list for the renderer ----------

def build_scenes(data: dict, rnd: dict, ctx: dict, twist: dict, lb: dict, site: str,
                 variant: str | None = None) -> list[dict]:
    """The writer's scenes with captions filled from slots and the code-built data panels attached."""
    from .motd import _banner, _cents, _pct, _pct1, _sig, _signed, full_model_line, standings
    sl = slots(rnd, ctx)
    concept = data["concept_type"]
    theme = CONCEPTS[concept]["theme"]
    durs = durations(data["scenes"], sl, {e["name"]: e.get("quote") for e in rnd["entries"]})
    by_name = {e["name"]: e for e in rnd["entries"]}
    url = f"{site}/round/{rnd['round_id']}"
    crowd = rnd.get("crowd") or {}
    out = []
    n_chars = sum(1 for s in data["scenes"] if s["kind"] == "character")
    ci = 0
    for s, dur in zip(data["scenes"], durs):
        k = s["kind"]
        e = by_name.get(s.get("agent") or "") if k == "character" else None
        agent = e["agent"] if e else None
        sc = {"kind": "s_" + k, "dur": dur, "agent": agent, "sig": _sig(lb, agent),
              "model_line": full_model_line(agent), "caption": fill(s["text"], sl), "concept": concept, "theme": theme}
        if k == "cold_open":
            sc["mascots"] = [x["agent"] for x in rnd["entries"]]
        elif k == "title":
            sc["title"] = data.get("title") or ""
            sc["genre"] = CONCEPTS[concept]["label"]
        elif k == "market":
            sc["question"] = rnd["question"]
            sc["prices"] = [{"outcome": o, "price": _cents(start_price(rnd, o))} for o in rnd["outcomes"]]
            sc["close"] = (rnd.get("end_date") or "")[:10]
        elif k == "background":
            sc["subject"] = (data.get("background_subject") or "").strip()[:40]
        elif k == "character" and e:
            ci += 1
            mkt = start_price(rnd, e["outcome"]) if e.get("outcome") else None
            sc.update(name=e["name"], outcome=e.get("outcome"), entry=_cents(e.get("avg_price")), mkt=_pct1(mkt),
                      conf=_pct(e.get("confidence")), quote=(e.get("quote") or "").strip() or None,
                      decided=e.get("decided_s"), n=ci, of=n_chars,
                      why=None if e.get("outcome") else (e.get("exit_reason") or "no bet"))
        elif k == "result":
            sc["winner"] = rnd.get("winner")
            sc["headline"] = results_headline(rnd, variant=variant or rnd.get("headline_variant")
                                              or headline_variant(rnd["round_id"]))
            sc["rows"] = [{"agent": x["agent"], "name": x["name"], "outcome": x.get("outcome"),
                           "entry": _cents(x.get("avg_price")), "pnl": x.get("pnl"),
                           "pnl_s": _signed(x.get("pnl")) if x.get("outcome") else "—", "won": bool(x.get("won"))}
                          for x in ranked(rnd["entries"])]
            sc["crowd"] = ({"outcome": crowd.get("outcome"), "price": _cents(crowd.get("price")),
                            "pnl": _signed(crowd.get("pnl")), "won": crowd.get("won")} if crowd.get("outcome") else None)
        elif k == "twist":
            sc["fact"] = twist["fact"]
            sc["twist_agents"] = twist["agents"]
        elif k == "coda":
            sc["standings"] = standings(lb)
            sc["asof"] = (datetime.fromtimestamp(lb["updated_at"], timezone.utc).strftime("%Y-%m-%d")
                          if lb.get("updated_at") else None)
            sc["cta"] = "Pick against the AIs: reply to the next round's reveal post on @agentpitbench."
        out.append(sc)
    for sc in out:
        sc.update(round_id=rnd["round_id"], url=url, exhibition=bool(rnd.get("exhibition")), banner=_banner(rnd))
    return fit(out)


def fit(scenes: list[dict]) -> list[dict]:
    """45-60 s: short films hold the coda, then the twist, then the result longer; long ones trim characters."""
    total = sum(s["dur"] for s in scenes)
    for kind, cap in (("s_coda", 10.0), ("s_twist", 10.0), ("s_result", 9.0), ("s_background", 9.0),
                      ("s_coda", 30.0)):
        if total >= MIN_S + 0.5:
            break
        s = next((x for x in scenes if x["kind"] == kind), None)
        if s:
            add = min(MIN_S + 0.5 - total, max(0.0, cap - s["dur"]))
            s["dur"] += add
            total += add
    if total > MAX_S:
        ch = [s for s in scenes if s["kind"] in ("s_character", "s_misdirect", "s_background")]
        room = sum(s["dur"] - 3.0 for s in ch)
        cut = min(total - MAX_S, room)
        for s in ch:
            s["dur"] -= cut * (s["dur"] - 3.0) / room if room else 0
    for s in scenes:
        s["dur"] = round(s["dur"], 3)
    return scenes


def script_text(data: dict, sl: dict[str, str]) -> str:
    """The filled script as plain text (review and the film record)."""
    lines = [f"[{data['concept_type']}] {data.get('title', '')}"]
    for s in data["scenes"]:
        who = f" ({s['agent']})" if s.get("agent") else ""
        lines.append(f"{s['kind']}{who}: {fill(s['text'], sl)}")
    return "\n".join(lines)


def recent(db, n: int = RECENT_FILMS) -> tuple[list[str], list[str]]:
    """(concept types, twist types) of the last n accepted scripts."""
    rows = db.q("SELECT content FROM operator_ai_outputs WHERE purpose=? AND accepted=1 ORDER BY output_id DESC "
                "LIMIT ?", TASK, n)
    concepts, twists_ = [], []
    for r in rows:
        try:
            d = json.loads(r["content"])
        except ValueError:
            continue
        concepts.append(d.get("concept_type"))
        twists_.append((d.get("_twist") or {}).get("type"))
    return [c for c in concepts if c], [t for t in twists_ if t]


def register() -> None:
    from . import opai
    opai.register(TASK, opai.Task(prompt=f"{PROMPT_VERSION}.md", schema=SCHEMA,
                                  validate=lambda d, p: validate(d, p)))


__all__ = ["CONCEPTS", "SCHEMA", "slots", "fill", "payload", "validate", "build_scenes", "durations",
           "recent", "register", "script_text", "AGENT_NAMES"]
