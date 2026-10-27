"""AI scene images for the posts that carry a card (operator decision 2026-10-27, docs/scene-images.md).

Each reveal and results post gets a bold-cartoon scene starring our own four original robot mascots (one per AI,
the same character sheet in every prompt); our code then composites the headline, the real logos and the picks on
top (cards.CardRenderer.poster, templates/cards/scene_poster.html). The model only ever draws the scene: logos,
text, numbers and picks are never generated.

Safety by construction:
  - the prompt is built from fixed vocabulary tables keyed by the post's situation and the market's category. The
    market's question, outcomes, winner and names never enter it, so it cannot leak a sealed pick or an excluded
    topic's wording, and cannot name a team (no crests, no real people);
  - only post-reveal kinds ("reveal", "results") can be built; anything else raises SceneRefused;
  - a market whose text hits `excluded_keywords` gets no scene at all;
  - every failure (disabled, no key, cap reached, HTTP error, blocked or empty answer) returns None, and the
    publisher then posts today's card unchanged. Posting never waits on or breaks because of an image.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import AGENT_NAMES, Settings
from .db import DB

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"   # tested live 2026-10-27
QUOTA_BACKOFF_S = 900   # after a 429 (no quota / billing off) make no image call for this long
KINDS = ("reveal", "results")          # post-reveal kinds: the only ones a scene may be built for
SHEET_DIR = Path(__file__).resolve().parent / "static" / "scenes"   # the approved cast sheets (operator 2026-10-27)
SHEETS = {"clay": "sheet-clay.jpg"}   # every other style uses sheet-cartoon.jpg
ASPECT = "4:5"                         # the poster is 1080x1350 (X / Bluesky post images)
ORDER = ("claude", "codex", "agy", "grok")   # left to right in every scene; the overlay's logo columns match


class SceneRefused(ValueError):
    """The prompt may not be built (a pre-reveal kind)."""


class SceneError(RuntimeError):
    """The image API failed or returned no image."""


class SceneQuota(SceneError):
    """HTTP 429: the key has no image quota (free tier has none; billing off) or is rate limited. Not retried."""


# ---------- the four robots: one fixed character sheet, original designs (not any brand's logo or product) ----------

# The cast: ONE constant (agent -> character description), so an approved redesign is a text drop-in. Colour mapping
# is fixed: orange = Claude, teal = ChatGPT, blue = Gemini, black = Grok. Distinct silhouettes: round blob, tall slim
# rectangle, wide trapezoid, angular wedge. Original designs, no brand resemblance, no markings on them.
CAST = {
    "claude": "an ORANGE robot, short and round like a bowling ball, with one big round eye-screen, one curly antenna "
              "and stubby legs; thoughtful (the careful analyst)",
    "codex": "a TEAL robot, tall and lanky with a long neck and a flat rounded-rectangle head, standing confidently "
             "with hands on hips (the confident all-rounder)",
    "agy": "a BLUE robot, wide, boxy and low like a tank on short tracks instead of legs, with a square head and small "
           "calm eyes, holding a magnifying glass (the quiet value hunter)",
    "grok": "a BLACK robot, sleek and athletic, with a glowing red visor stripe and spiky fins on its head, cocky, "
            "flipping a coin (the gambler)",
}
NAMES = {"claude": "Prof", "codex": "Slim", "agy": "Tank", "grok": "Lucky"}   # approved running-joke nicknames
NICKNAMES = NAMES                                                             # for the posts writer to import
ROBOTS = {a: (NAMES[a], CAST[a]) for a in CAST}
# art styles, one A/B arm each (ab slot image.style, control cartoon): code -> (style phrase, uses the random palette)
STYLES = {
    "cartoon": ("bold cartoon sports-poster illustration, thick clean outlines, flat bright colours with simple cel "
                "shading", True),
    "clay": ("claymation stop-motion look, handmade plasticine models with visible thumbprints, soft studio "
             "lighting, Aardman-like", True),
    "popart": ("1960s pop-art comic-book style, bold black ink outlines and Ben-Day halftone dots, limited bright "
               "primary colours", False),
    "film3d": ("glossy 3D animated-film still, soft global illumination, rounded appealing shapes, shallow depth "
               "of field", True),
    "screenprint": ("1970s retro screen-print poster, flat overprinted shapes in only orange, teal, navy and cream, "
                    "subtle paper grain", False),
}
STYLE_CORE = "high contrast, big readable shapes that work at phone size, cheerful and family-friendly"
HARD_RULES = (
    "Hard rules: absolutely no text, no letters, no numbers, no digits, no symbols that look like writing, "
    "absolutely no written words, signs, labels or banners with text, not even decorative or pretend lettering, "
    "no scoreboards or screens showing writing. No logos or brand marks of any kind. "
    "No real people, no celebrities, no recognisable faces. No real team crests, kits or trademarks "
    "(generic plain colours are fine). No violence, no injury, no blood, no weapons, nothing scary. "
    "Family-friendly slapstick only.")
COMPOSITION = (
    f"Composition: portrait {ASPECT} poster. The four robots stand in one row across the middle of the frame, "
    "one in each quarter of the width, in this left-to-right order: {order}. Keep all four fully inside the "
    "frame, between 22% and 68% of the height. The top fifth of the image and the bottom third must be simple, "
    "calm and uncluttered (soft sky, floor or plain backdrop with low detail), because text and badges are "
    "added there afterwards.")

# category (cards-side, virality.category) -> settings; a seed picks one. Fixed vocabulary only.
SETTINGS = {
    "Esports": ["a neon-lit gaming arena stage with glowing blank-screened PCs on desks",
                "a futuristic esports stage with light rigs and a cheering crowd silhouette"],
    "Crypto": ["the robots riding a big cartoon rocket past floating plain golden coins, a glowing abstract chart line drawn as a "
               "plain zig-zag shape with no numbers",
               "the robots hanging on a crashing glowing chart line drawn as a plain steep zig-zag shape with no numbers, "
               "floating plain golden coins",
               "a rooftop at night above a bright cartoon city skyline with drifting golden coins"],
    "Sports": ["a bright stadium with a generic soccer (association football) pitch with two soccer goals with nets and a round soccer ball, not an american football field and a cheering crowd of simple shapes",
               "a sunny stadium with plain bleachers, bunting in plain colours and a generic soccer (association football) pitch with two soccer goals with nets and a round soccer ball, not an american football field, floodlit"],
    "Politics": ["a neutral wooden ballot box on a stage with spotlights and balloons (no candidates, no parties, no flags, no signs)",
                 "a grand game-show studio with a checkerboard floor and coloured spotlights"],
    "Economy": ["a bright retro arcade floor with glowing abstract rising and falling lines on blank panels",
                "a playful toy-town marketplace with plain coloured stalls and balloons"],
    "Other": ["a bold game-show stage with spotlights, confetti cannons and a big curtain",
              "a bright carnival midway with striped tents and balloons"],
}
# keyword -> fixed sport vocabulary (used only to pick from this table; the market text is never copied)
SPORT_WORDS = [
    (r"\bnba\b|basketball", "a basketball arena, courtside seats right at the generic plain court with plain hoops"),
    (r"\bnfl\b|american football|super bowl", "a generic american-football field with plain goalposts"),
    (r"\bmlb\b|baseball", "a generic baseball diamond"),
    (r"\bnhl\b|hockey", "a generic ice rink with plain boards"),
    (r"tennis|\batp\b|\bwta\b", "a generic tennis court"),
    (r"\bufc\b|boxing|\bmma\b", "a generic cartoon boxing ring with soft padded ropes, no one hurt"),
    (r"formula 1|\bf1\b|grand prix", "a generic cartoon race track with plain coloured kerbs"),
    (r"premier league|la liga|bundesliga|serie a|ligue 1|champions league|europa league|uefa|\bfifa\b|world cup|"
     r"soccer|football|\bfc\b|\bcf\b|\bafc\b|\bmls\b|copa|euro 20", "a generic soccer (association football) pitch with two soccer goals with nets and a round soccer ball, not an american football field"),
    (r"cricket|\bipl\b", "a generic cricket ground"),
    (r"golf|\bpga\b", "a generic golf green with a flag"),
]
PIT = ("the recurring Pit: a round sandy gladiator-style arena with stone walls, torches and a cheering crowd of simple "
       "shapes, The Boss watching from a balcony above")
EVERYDAY = {   # name: (setting, situations it fits, results only?)
    "courtroom": ("a cartoon courtroom, the robots taking turns at the witness stand making dramatic confessions",
                  {"lone_loser", "nobody", "wipeout", "mixed", "nobody_agrees", "alone"}),
    "detention": ("a school detention classroom with tiny desks and a blank blackboard, the robots squeezed into "
                  "the desks", {"wipeout", "nobody", "lone_loser", "mixed"}),
    "red_carpet": ("a red-carpet premiere with velvet ropes and camera flashes, the robots posing like stars",
                   {"sweep", "lone_winner", "split", "agree"}),
    "hospital": ("a cartoon hospital ward where the robots sit up in beds with bandages and get-well balloons",
                 {"wipeout", "nobody"}),
}
GENRE = {      # original art only: no copyrighted characters, ships, costumes or names
    "heist": ("an original cartoon heist: the robots in striped cat-burglar sweaters sneaking through a vault of "
              "plain gold bars", {"agree", "alone", "sweep", "lone_winner"}),
    "western": ("an original cartoon western main street at high noon, the robots in cowboy hats facing off, "
                "tumbleweed rolling", {"dead_split", "split", "nobody_agrees", "nway", "alone"}),
    "space": ("an original cartoon space-opera starship bridge with glowing consoles and a big window of stars, "
              "the robots as the crew", {"agree", "sweep", "lone_winner", "split", "nway", "dead_split"}),
    "horror": ("a silly, friendly haunted mansion with cartoon ghosts and cobwebs, the robots shivering "
               "(family-friendly, not scary)", {"wipeout", "nobody", "lone_loser"}),
}
REVEAL_ACTIONS = [   # before the result: show the disagreement, never who is right
    "The robots are placing their bets dramatically: slamming stacks of plain chips on the table, sweating, one of them "
    "going all in.",
    "The robots are arguing about the pick, pointing and shouting at each other.",
    "The robots are watching the match on a big couch with popcorn, tense and leaning forward.",
    "The robots are in a training montage: studying blank chart boards, lifting weights, doing push-ups.",
]
COMEDY = ["slips on a banana peel with legs in the air", "gets a cream pie in the face", "is flattened by a falling "
          "anvil and springs back up with a cartoon bump (no injury)"]
DAMAGE = [   # persistent damage by the robot's current losing streak: stage 0 repaired, 1-3 worse
    "",
    "has a small bandage and one patch on its body",
    "has dents, several patches and a bandage on its head",
    "is held together with tape, heavily patched and dented, with a cracked eye-screen",
]
PALETTES = ["hot orange and electric blue", "sunny yellow and deep teal", "magenta and lime", "sky blue and tangerine",
            "red and cream with navy accents", "mint and bright coral", "purple and gold", "turquoise and warm pink"]
LIGHTING = ["golden-hour glow", "bright midday sun", "dramatic stadium floodlights", "night scene with neon glow",
            "soft sunrise haze", "spotlight beams cutting through the dark"]
CAMERAS = ["slightly low hero angle", "straight-on front view", "wide shot with a little tilt", "gentle high angle"]
FLAVOURS = ["retro comic-book halftone shading", "vintage travel-poster flat shapes", "pop-art sticker look",
            "modern mobile-game key-art look", "screen-printed poster look with a grain texture"]
EXTRAS = ["confetti in the air", "a few puffy clouds", "streaks of light", "floating stars", "speed lines in the background",
          "balloons drifting up"]

GLOOMY_EXTRAS = ["a drizzle of rain", "a few grey storm clouds", "a single flickering floodlight", "drifting smoke"]

WIN, LOSE, IDLE = "win", "lose", "idle"
POSES = {
    WIN: ["leaps with both arms raised in joy", "does a victory dance with a huge grin", "pumps a fist triumphantly",
          "sprays fizzy soda over the crowd while confetti flies", "is showered in confetti, popping a champagne-style "
          "soda bottle"],
    LOSE: ["slumps forward with drooping arms and a tiny rain cloud overhead", "flops onto the floor in comic dismay",
           "tumbles backwards with a surprised face", "has smoke puffing from both ears", "falls to pieces into a "
           "harmless heap of springs and bolts", "cries a stream of oil drops", *COMEDY],
    IDLE: ["sits to one side munching an apple, uninvolved"],
}
# situation -> (scene drama sentence, default pose for robots without their own)
DRAMA = {
    "sweep": "A clean sweep: all four robots celebrate together on a shared winners' podium with confetti.",
    "wipeout": "A total wipeout: a big round pit opens in the floor and all four robots tumble comically towards it, "
               "arms flailing, dust clouds and stars.",
    "lone_winner": "One robot is the hero, bathed in a bright spotlight while the other three slump in the shade.",
    "lone_loser": "Three robots celebrate while one robot is alone in the shade, slumped with a tiny rain cloud.",
    "split": "Two robots celebrate while two robots slump: a mixed result.",
    "nobody": "Nobody was right: all four robots slump in a row under one big comic rain cloud.",
    "agree": "The four robots all point the same way in perfect unison, like a synchronised team, with the same "
             "confident grin.",
    "alone": "Three robots stand together nodding and pointing to the right, while one robot stands apart pointing "
             "the other way with arms crossed and a stubborn smirk, a spotlight on it.",
    "dead_split": "Two robots point left and two robots point right, face to face like two teams in a friendly "
                  "tug-of-war around one big rope.",
    "nobody_agrees": "Every robot points a different direction, scratching heads and shrugging, with small plain puffs "
                     "of cloud above their heads.",
    "nway": "The robots split into three camps pointing in different directions, a lively three-way argument "
            "with a tangled rope between them.",
    "mixed": "A mixed result: some robots celebrate and some slump.",
}


def _rng(round_id, kind: str) -> random.Random:
    """Deterministic per (round, post kind): re-rendering gives the same scene, different posts differ."""
    return random.Random(int.from_bytes(hashlib.sha256(f"{round_id}:{kind}".encode()).digest()[:8], "big"))


def category_of(rnd: dict) -> str:
    from .virality import category
    return category(rnd.get("question") or "")


def sport_vocab(question: str) -> str | None:
    q = (question or "").lower()
    return next((v for pat, v in SPORT_WORDS if re.search(pat, q)), None)


def situation(rnd: dict, kind: str) -> str:
    """The drama of the post: results_shape situations for results, split_shape for the reveal."""
    if kind == "reveal":
        from .cards import split_shape
        return split_shape(rnd)[0]
    from .twitter import results_shape
    sit, _ = results_shape(rnd)
    if sit:
        return sit
    winners = [e for e in rnd["entries"] if e.get("won")]
    bettors = [e for e in rnd["entries"] if e.get("outcome")]
    if bettors and not winners:
        return "nobody"
    return "mixed"


def damage_stage(streak: int | None) -> int:
    """Persistent damage 0-3 from a robot's current streak: a win repairs it (stage 0)."""
    n = -(streak or 0)
    return 0 if n < 2 else 1 if n < 4 else 2 if n < 6 else 3


def pick_setting(rng: random.Random, cat: str, sport: str | None, sit: str, kind: str, gags: dict | None) -> str:
    """One setting from the weighted pool: the market's own world (x3), the Pit (x2), everyday and genre parodies
    that fit the drama (x1 each). Market text is never used: only the category and a sport looked up from keywords."""
    world = [sport] if sport else SETTINGS.get(cat, SETTINGS["Other"])
    pool = [*world * 3, PIT, PIT]
    pool += [t for t, sits in EVERYDAY.values() if sit in sits]
    if (gags or {}).get("dunce") and kind == "results":
        pool.append(EVERYDAY["detention"][0])
    pool += [t for t, sits in GENRE.values() if sit in sits]
    return rng.choice(pool)


def _poses(rnd: dict, kind: str, sit: str, rng: random.Random) -> dict[str, str]:
    """Per robot pose. Results: by who was right (post-reveal). Reveal: by pick group, as labels only."""
    by = {e["agent"]: e for e in rnd["entries"]}
    out = {}
    if kind == "results":
        for a in ORDER:
            e = by.get(a)
            state = IDLE if not e or not e.get("outcome") else (WIN if e.get("won") else LOSE)
            out[a] = rng.choice(POSES[state])
        return out
    return {}


def forbidden_terms(rnd: dict) -> list[str]:
    """Words of the market (question, outcomes, winner) that must never appear in a prompt."""
    words = set()
    for t in [rnd.get("question"), rnd.get("event_title"), rnd.get("winner"), *(rnd.get("outcomes") or []),
              *(e.get("outcome") for e in rnd.get("entries") or [])]:
        for w in re.findall(r"[A-Za-z0-9]{4,}", t or ""):
            words.add(w.lower())
    return sorted(words)


def assert_sealed(rnd: dict, prompt: str) -> None:
    """Last line of defence: no outcome label, winner or distinctive market word may appear in the prompt."""
    low = prompt.lower()
    for t in [rnd.get("winner"), *(rnd.get("outcomes") or []), *(e.get("outcome") for e in rnd.get("entries") or [])]:
        t = (t or "").strip().lower()
        if len(t) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low):
            raise SceneRefused("the prompt would carry a pick or outcome label")


# ---------- formats and recurring gags ----------

FORMATS = ("scene", "comic", "movie", "trading")
PERSONAS = {"claude": "The Professor", "codex": "Value Hunter", "agy": "The Sprinter", "grok": "The Wild Card"}
PIT_BOSS = ("The Boss (the Pit Boss), a big barrel-chested old brass ringmaster robot with a top hat, a bushy "
            "metal moustache, a whistle on a cord and a clipboard of blank paper, gruff")
DUNCE_STREAK = 2          # a losing streak of this length or more earns the dunce cap
CHAOS = [  # escalating chaos stage by the longest losing streak (stage 0 = a calm scene)
    "",
    "A few wisps of smoke curl up from the floor.",
    "Thick smoke drifts across the scene and small flames flicker at the edges of the set.",
    "The whole set is on fire and smoky, comic chaos everywhere, sparks flying (still friendly cartoon slapstick, "
    "nobody hurt).",
]


def gags_from_board(board: list[dict] | None, prev_leader: str | None = None) -> dict:
    """Recurring visual gags from the leaderboard rows (db.leaderboard / exports): the dunce (the agent on the
    longest losing streak of DUNCE_STREAK+), the #1 agent holding the trophy (snatched when the lead just
    changed), the chaos stage 0-3 from the longest losing streak, and per-agent stats for the trading card."""
    board = [b for b in board or [] if b.get("agent") in ORDER]
    g = {"pit_boss": True, "dunce": None, "leader": None, "prev_leader": None, "lead_changed": False, "chaos": 0,
         "stats": {b["agent"]: {k: b.get(k) for k in ("wins", "losses", "win_rate", "net_pnl", "streak", "rank")}
                   for b in board}}
    if not board:
        return g
    worst = min(board, key=lambda b: b.get("streak") or 0)
    ws = -(worst.get("streak") or 0)
    if ws >= DUNCE_STREAK:
        g["dunce"] = worst["agent"]
    g["chaos"] = 0 if ws < 2 else 1 if ws < 4 else 2 if ws < 6 else 3
    top = next((b for b in board if b.get("rank") == 1 and (b.get("wins") or b.get("losses"))), None)
    if top:
        g["leader"] = top["agent"]
        if prev_leader in ORDER and prev_leader != top["agent"]:
            g.update(lead_changed=True, prev_leader=prev_leader)
    return g


def featured_agent(rnd: dict, kind: str, sit: str, gags: dict | None) -> tuple[str | None, bool]:
    """(the agent a trading card features, rare): the lone winner is RARE, the lone loser a plain card."""
    for e in rnd["entries"]:
        if kind == "results" and sit == "lone_winner" and e.get("won"):
            return e["agent"], True
        if kind == "results" and sit == "lone_loser" and e.get("outcome") and not e.get("won"):
            return e["agent"], False
    if kind == "reveal" and sit == "alone":
        from .cards import split_shape
        lone = split_shape(rnd)[1].get("lone")
        for e in rnd["entries"]:
            if e.get("name") == lone:
                return e["agent"], False
    return None, False


def choose_format(rnd: dict, kind: str, gags: dict | None = None, allowed=FORMATS) -> str:
    """The post's format, by situation (deterministic per round and kind): wipeouts and upsets lean to the comic
    strip, marquee markets to the movie poster, a lone winner to a RARE trading card, the rest a single scene."""
    from .cards import is_upset
    from .virality import news_tier
    rng = _rng(rnd.get("round_id"), kind + ":format")
    sit = situation(rnd, kind)
    pick = "scene"
    marquee = news_tier({"question": rnd.get("question") or ""}) == 3
    if kind == "results" and (sit in ("wipeout", "nobody") or is_upset(rnd)) and rng.random() < 0.6:
        pick = "comic"
    elif kind == "results" and sit == "lone_winner" and rng.random() < 0.5:
        pick = "trading"
    elif marquee and rng.random() < 0.5:
        pick = "movie"
    elif sit in ("dead_split", "nobody_agrees") and rng.random() < 0.3:
        pick = "comic"
    if pick == "trading" and not featured_agent(rnd, kind, sit, gags)[0]:
        pick = "scene"
    return pick if pick in allowed else "scene"


def _gag_text(gags: dict | None, fmt: str) -> list[str]:
    g = gags or {}
    out = []
    if g.get("pit_boss", True) and fmt != "trading":
        out.append(f"In the right-hand edge of the frame at about 20-30% of the height, small and partly cropped, "
                   f"stands {PIT_BOSS}.")
    if g.get("dunce") in ROBOTS and fmt != "trading":
        out.append(f"{ROBOTS[g['dunce']][0]} wears a tall pointed blank paper dunce cap, looking sheepish.")
    if g.get("leader") in ROBOTS and fmt != "trading":
        t = f"{ROBOTS[g['leader']][0]} holds up a shiny golden trophy cup (plain, no engraving)."
        if g.get("lead_changed") and g.get("prev_leader") in ROBOTS:
            t += (f" {ROBOTS[g['prev_leader']][0]} is cheekily snatching at the trophy, trying to grab it away "
                  f"(playful tug, nobody hurt).")
        out.append(t)
    if CHAOS[min(3, max(0, int(g.get("chaos") or 0)))]:
        out.append(CHAOS[min(3, max(0, int(g.get("chaos") or 0)))])
    return out


def build_prompt(rnd: dict, kind: str, has_refs: bool = False, fmt: str = "scene",
                 gags: dict | None = None, style: str = "cartoon", movie: dict | None = None) -> str:
    """The prompt for one post in a format (FORMATS) with the recurring gags (gags_from_board). Raises SceneRefused
    for a kind that is not post-reveal. No text is ever requested: titles, stats and picks are our overlay."""
    if kind not in KINDS:
        raise SceneRefused(f"no scene for a {kind!r} post (only post-reveal kinds: {KINDS})")
    if fmt not in FORMATS:
        raise SceneRefused(f"unknown format {fmt!r}")
    if style not in STYLES:
        raise SceneRefused(f"unknown style {style!r}")
    rng = _rng(rnd.get("round_id"), kind)
    sit = situation(rnd, kind)
    gloomy = kind == "results" and sit in ("wipeout", "nobody")   # a defeat: no trophy, no confetti
    cat = category_of(rnd)
    sport = sport_vocab(rnd.get("question") or "") if cat == "Sports" else None
    setting = pick_setting(rng, cat, sport, sit, kind, gags)
    poses = _poses(rnd, kind, sit, rng)
    order = ", ".join(ROBOTS[a][0] for a in ORDER)
    sheet = "; ".join(f"{ROBOTS[a][0]}: {ROBOTS[a][1]}" for a in ORDER)
    drama = DRAMA.get(sit, DRAMA["mixed"])
    phrase, palette = STYLES[style]
    parts = [f"{phrase}, {STYLE_CORE}, " + (f"palette of {rng.choice(PALETTES)}, " if palette else "")
             + f"{rng.choice(LIGHTING)}, {rng.choice(CAMERAS)}.",
             f"Setting: {setting}, with {rng.choice(GLOOMY_EXTRAS if gloomy else EXTRAS)}."]
    if movie:   # the market's movie (operator 2026-10-27): every post is a new frame from the same film
        parts[1] = (f"This image is a new frame from the movie '{movie['title']}'. Its poster is the first attached image: "
                    f"keep the poster's world, setting, lighting, colour palette and art style exactly, but compose a new "
                    f"moment. The movie: {movie['visual']}")
    if fmt == "trading":
        who, rare = featured_agent(rnd, kind, sit, gags)
        if not who:
            raise SceneRefused("a trading card needs a featured agent")
        name, desc = ROBOTS[who]
        mood = (rng.choice(POSES[WIN]) if rare or any(e["agent"] == who and e.get("won") for e in rnd["entries"])
                else rng.choice(POSES[LOSE]))
        parts += [f"Collectible trading-card illustration of ONE original cartoon robot mascot, {name}: {desc}. "
                  "Always drawn exactly like this, no logos or markings on it.",
                  f"{name} {mood}, large and centred, shown from the knees up, simple bright background of the setting."
                  + (" Sparkles and rainbow foil glints in the air." if rare else "")]
        comp = ("Composition: portrait 4:5. The robot fills the upper two thirds; the top eighth is simple background; "
                "the bottom third is a plain, calm, uncluttered gradient because stats and text are added there "
                "afterwards.")
    else:
        parts.append("Characters: four original cartoon robot mascots, always drawn exactly like this: " + sheet + ". "
                     "Same proportions and colours every time; they have no logos or markings on them.")
        if fmt == "comic":
            parts.append(
                "Format: a 3-panel comic strip with thick white panel borders, panels stacked top to bottom with equal "
                "gutters, no speech bubbles and no captions. Panel 1: the four robots at a table each placing a blank "
                "face-down card, thinking. Panel 2: the big moment of the match in the setting, robots watching "
                "tensely. Panel 3 (the reaction): " + drama)
        elif fmt == "movie":
            parts.append("Format: dramatic cinematic movie-poster key art, heroic low lighting, lens flare, the four "
                         "robots shoulder to shoulder as the cast. The mood: " + drama)
        else:
            parts.append("Scene: " + drama)
        if kind == "reveal":
            parts.append(rng.choice(REVEAL_ACTIONS) + " Whatever they do, the scene must show how the robots are "
                         "divided or united, and must not show who is right.")
        stats = (gags or {}).get("stats") or {}
        dmg = [f"{ROBOTS[a][0]} {DAMAGE[damage_stage(stats[a].get('streak'))]}" for a in ORDER
               if a in stats and damage_stage(stats[a].get("streak"))]
        if dmg and kind == "results":
            parts.append("Visible damage that stays until a win: " + "; ".join(dmg) + ". The other robots look shiny "
                         "and freshly repaired.")
        if poses:
            parts.append("Poses, left to right: " + "; ".join(f"{ROBOTS[a][0]} {poses[a]}" for a in ORDER) + ".")
        parts += _gag_text({**(gags or {}), "leader": None} if gloomy else gags, fmt)
        if gloomy:
            parts.append("This is a defeat for every robot: no confetti, balloons, trophies or celebration anywhere.")
        comp = COMPOSITION.format(order=order)
        if fmt == "comic":
            comp = (f"Composition: portrait {ASPECT}. The three panels fill the frame between 18% and 70% of its height (the top 18% is plain banner space), with the robots "
                    f"in each panel in this left-to-right order: {order}. The bottom 30% below the panels is plain, "
                    "dark, calm and uncluttered because text and badges are added there afterwards. "
                    "")
        elif fmt == "movie":
            comp += " Keep the top 28% as a moody, simple sky or backdrop for a title."
    if has_refs and movie:
        parts.append("The second attached image is the character sheet of the four robots and The Boss: match their "
                     "designs exactly and do not copy its plain background.")
    elif has_refs:
        parts.append("The attached reference image is the character sheet of the four robots and The Boss: match their designs "
                     "exactly and do not copy their backgrounds.")
    parts += [comp, HARD_RULES]
    prompt = " ".join(parts)
    assert_sealed(rnd, prompt)
    return prompt


def poster_hook(rnd: dict, kind: str) -> str:
    """The poster's one big line, poster type (the card upper-cases it): '0-4. ALL FOUR WRONG.'"""
    from .cards import _NUM, results_headline, split_headline
    if kind == "reveal":
        k, h = split_headline(rnd)
        return h.upper() + ("" if h.endswith((".", "!")) else ".")
    from .twitter import results_shape
    sit, f = results_shape(rnd)
    n = int(f.get("n", 0)) if f else 0
    w = _NUM.get(n, str(n)).upper()
    if sit == "sweep":
        return f"{n}-0. ALL {w} RIGHT."
    if sit == "wipeout":
        return f"0-{n}. ALL {w} WRONG."
    if sit == "lone_winner":
        return f"{f['lone'].upper()} ALONE. 1-{n - 1}."
    if sit == "lone_loser":
        return f"{f['lone'].upper()} ALONE WAS WRONG. {n - 1}-1."
    if sit == "split":
        wins = int(f["record"].split("-")[0])
        return f"{wins}-{n - wins}. SPLIT DECISION."
    if sit == "nobody":
        return f"NOBODY GOT IT. 0-{n}."
    return results_headline(rnd, variant="plain").upper() + "."


_DATE_TAIL = re.compile(r"\s+(on|by|before)\s+(\d{4}-\d{2}-\d{2}|[A-Z][a-z]+ \d{1,2}(,? \d{4})?)\??$")


def short_question(q: str) -> str:
    """'Will Vissel Kobe win on 2026-10-27?' -> 'Will Vissel Kobe win?': the date is noise on an image."""
    q = " ".join((q or "").split())
    s = _DATE_TAIL.sub("", q.rstrip("?")).strip()
    return (s + "?") if q.endswith("?") else s


def poster_claim(rnd: dict, kind: str) -> dict | None:
    """The poster's headline when an AI said something quotable: the AI's own confident claim, verbatim, against
    what happened. A stranger who sees only the image gets the joke: 'Claude: "Kobe are not leaving Korea with three
    points." -> WRONG. It was Yes.' Results: a confident loser when the story is a defeat (wipeout, lone loser,
    nobody, split), else a confident winner. Reveal (picks already public): the boldest claim. None without a quote."""
    from .virality import clean_quote
    from .twitter import results_shape
    bettors = [e for e in rnd.get("entries") or [] if e.get("outcome")]
    quoted = [(e, q) for e in bettors if (q := clean_quote(e.get("quote"), 120)[0])]
    if not quoted:
        return None
    conf = lambda eq: (eq[0].get("confidence") or 0)
    if kind == "results":
        if rnd.get("state") != "resolved":
            return None
        sit, _ = results_shape(rnd)
        losers = [x for x in quoted if not x[0].get("won")]
        winners = [x for x in quoted if x[0].get("won")]
        pool = (losers or winners) if sit in ("wipeout", "lone_loser", "nobody", "split", "mixed") else (winners or losers)
        e, q = max(pool, key=conf)
        won = bool(e.get("won"))
        stamp, color = ("CALLED IT", "#22c55e") if won else ("WRONG", "#ff4d6d")
        verdict = f"It was {rnd.get('winner')}."
    else:
        e, q = max(quoted, key=conf)
        stamp, color, verdict = "LOCKED IN", "#ffd34d", f"{e['name']} picked {e['outcome']}."
    return {"agent": e["agent"], "name": e["name"], "quote": q, "stamp": stamp, "stamp_color": color,
            "verdict": verdict, "question": short_question(rnd.get("question") or "")}


def blocked_by_keywords(s: Settings, rnd: dict) -> bool:
    text = f" {rnd.get('question', '')} {rnd.get('event_title', '')} ".lower()
    return any(k in text for k in s.excluded_keywords)


# ---------- the image API (Gemini Interactions API; docs checked 2026-10-27) ----------

def extract_image(body: dict) -> bytes | None:
    """The first image in a response. Interactions API: steps[type=model_output].content[type=image].data (base64);
    also accepts the older generateContent shape (candidates[].content.parts[].inlineData.data)."""
    def dec(v):
        try:
            b = base64.b64decode(v, validate=False)
        except Exception:
            return None
        return b if b[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xdb", b"\xff\xd8\xff\xee") or b[:4] == b"RIFF" \
            or b[:3] == b"\xff\xd8\xff" else None
    for st in body.get("steps") or []:
        if st.get("type") == "model_output":
            for c in st.get("content") or []:
                if c.get("type") == "image" and isinstance(c.get("data"), str) and (b := dec(c["data"])):
                    return b
    for cand in body.get("candidates") or []:
        for p in (cand.get("content") or {}).get("parts") or []:
            d = p.get("inlineData") or p.get("inline_data") or {}
            if isinstance(d.get("data"), str) and (b := dec(d["data"])):
                return b
    for key in ("output_image", "outputImage"):
        o = body.get(key)
        if isinstance(o, dict) and isinstance(o.get("data"), str) and (b := dec(o["data"])):
            return b
    return None


def _image_part(p: Path) -> dict:
    data = p.read_bytes()
    mime = "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}


def _ref_inputs(style: str = "cartoon") -> list[dict]:
    """The approved cast sheet as a generateContent image part (clay style: the clay sheet), so the four robots and
    The Boss keep their designs from post to post. Empty when the file is missing."""
    p = SHEET_DIR / SHEETS.get(style, "sheet-cartoon.jpg")
    if not p.exists():
        return []
    mime = "image/png" if p.suffix == ".png" else "image/jpeg"
    return [{"inlineData": {"mimeType": mime, "data": base64.b64encode(p.read_bytes()).decode()}}]


TEXT_URL = API_URL
# operator 2026-10-27: pick the genre and the director first, then write the pitch
GENRES = ["comedy", "action", "thriller", "heist", "western", "science fiction", "fantasy adventure", "horror comedy",
          "musical", "romantic comedy", "sports drama", "film noir", "spy caper", "disaster movie", "martial arts",
          "space opera", "mystery whodunit", "war epic", "coming-of-age", "mockumentary"]
DIRECTORS_FILE = SHEET_DIR / "directors.json"     # 1000 well-known film directors, all eras and countries


def directors() -> list[str]:
    try:
        return json.loads(DIRECTORS_FILE.read_text())
    except (OSError, ValueError):
        return ["Wes Anderson"]


def pick_genre_director(round_id, recent: list[dict] | None = None) -> tuple[str, str]:
    """Step 0, before any writing: a genre (of 20) and a director (of 1000) for this market's movie. Seeded by the
    round; avoids the genres of the last 5 movies and every director already used."""
    rng = random.Random(f"movie:{round_id}")
    recent = recent or []
    used_g = {m.get("genre") for m in recent[-5:]}
    used_d = {m.get("director") for m in recent}
    genres = [g for g in GENRES if g not in used_g] or GENRES
    names = [d for d in directors() if d not in used_d] or directors()
    return rng.choice(genres), rng.choice(names)


MOVIE_PITCH = """You pitch fun, family-friendly movies. Write a movie idea about the subject of this prediction market.
Genre: {genre}. Write it the way {director} would make it: their signature tone, themes, storytelling and visual style.
Invent whatever characters and world fit the subject best (people, animals, creatures, anything).
Market: {question}
Event: {event}
Rules: no real people; in "visual" never name real teams, clubs, players, brands or trademarks (describe them generically,
e.g. "a club in red and white"); no written words, signs or logos in the image; do not say how the market ends.
Return JSON only, with exactly these keys:
{{"title": "a catchy movie title, at most 6 words",
 "idea": "one paragraph of 60-90 words: the premise, the characters, the stakes, a twist",
 "visual": "one or two sentences describing the world, the main characters and the mood of the film for an illustrator"}}"""


def movie_pitch_prompt(rnd: dict, genre: str = "comedy", director: str = "Wes Anderson") -> str:
    return MOVIE_PITCH.format(genre=genre, director=director, question=" ".join((rnd.get("question") or "").split()),
                              event=" ".join((rnd.get("event_title") or "").split()) or "-")


def movie_poster_prompt(rnd: dict, movie: dict, style: str) -> str:
    phrase, _ = STYLES[style]
    prompt = " ".join([
        f"{phrase}, {STYLE_CORE}.",
        f"A dramatic movie poster key-art illustration for a {movie.get('genre', '')} film called '{movie['title']}'. "
        f"{movie['visual']}",
        f"Composition: portrait {ASPECT}; the main characters in the middle; the top 30% and the bottom 30% darker and "
        "simpler because titles and badges are added afterwards.",
        HARD_RULES])
    assert_sealed(rnd, prompt)   # the poster never carries an outcome label
    return prompt


MOVIE_REVEAL_MOMENT = ("the tense moment just before the decisive event: everything is still undecided, both sides "
                       "hopeful, and the outcome is not shown")


def movie_frame_prompt(rnd: dict, kind: str, movie: dict, style: str) -> str:
    """Step 3: a new still from the market's movie, starting from its poster (the attached image). Reveal frames are
    sealed (no outcome); results frames show the real ending."""
    moment, ending = MOVIE_REVEAL_MOMENT, ""
    if kind == "results" and rnd.get("winner"):   # the market's real ending, told in the movie's own world
        moment = "the movie's ending, the big emotional climax"
        ending = (f" In the story, the answer to the question '{short_question(rnd.get('question') or '')}' turned out "
                  f"to be: {rnd['winner']}. Show exactly that ending, in pictures only: whoever that answer favours "
                  f"celebrates wildly, whoever it goes against is crushed. Make it unmistakable at a glance.")
    phrase, _ = STYLES[style]
    prompt = " ".join([
        f"{phrase}, {STYLE_CORE}.",
        f"A new still from the movie '{movie['title']}'. The attached image is its poster: keep the poster's world, "
        f"characters, art style and colour palette exactly, but compose a new scene. The movie: {movie['idea']}",
        f"This still shows {moment}.{ending}",
        f"Composition: portrait {ASPECT}; the action in the middle band; the top 35% and the bottom 30% darker and "
        "simpler because a quote, titles and badges are added there afterwards.",
        HARD_RULES])
    if kind != "results":
        assert_sealed(rnd, prompt)
    return prompt


class TextClient:
    """One JSON text request (generateContent, responseMimeType JSON) for the movie pitch."""

    def __init__(self, s: Settings, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.key, self.transport = s, api_key, transport

    async def json(self, prompt: str) -> dict:
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0}}
        async with httpx.AsyncClient(timeout=self.s.scene_images_timeout_s, transport=self.transport) as c:
            r = await c.post(TEXT_URL.format(model=self.s.scene_text_model), json=body, headers={"x-goog-api-key": self.key})
        if r.status_code == 429:
            raise SceneQuota("HTTP 429 on the movie pitch")
        if r.status_code >= 400:
            raise SceneError(f"pitch HTTP {r.status_code}: {r.text[:200]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        return json.loads("".join(p.get("text", "") for p in parts))


def check_pitch(d: dict) -> dict:
    """title / idea / visual present and sane; raises SceneRefused otherwise."""
    if not isinstance(d, dict):
        raise SceneRefused("pitch is not an object")
    out = {}
    for k, lo, hi in (("title", 2, 60), ("idea", 120, 900), ("visual", 20, 500)):
        v = " ".join(str(d.get(k) or "").split())
        if not lo <= len(v) <= hi:
            raise SceneRefused(f"pitch {k} has {len(v)} chars")
        out[k] = v
    return out


class SceneClient:
    """One image request (generateContent, IMAGE only, 4:5), one retry on a transport error, timeout or 5xx.
    A 429 is never retried: SceneQuota."""

    def __init__(self, s: Settings, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.key, self.transport = s, api_key, transport

    async def generate(self, prompt: str, refs: list[dict] | None = None) -> bytes:
        body = {"contents": [{"parts": [*(refs or []), {"text": prompt}]}],
                "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ASPECT}}}
        url = API_URL.format(model=self.s.scene_images_model)
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=self.s.scene_images_timeout_s, transport=self.transport) as c:
                    r = await c.post(url, json=body, headers={"x-goog-api-key": self.key})
                if r.status_code == 429:
                    raise SceneQuota("HTTP 429 (no image quota: billing not enabled, or rate limited)")
                if r.status_code >= 500:
                    last = SceneError(f"HTTP {r.status_code}")
                    continue
                if r.status_code >= 400:
                    raise SceneError(f"HTTP {r.status_code}: {r.text[:200]}")
                img = extract_image(r.json())
                if img is None:
                    raise SceneError("no image in the response (blocked or empty)")
                return img
            except (httpx.TransportError, ValueError) as e:   # timeout, connection error, bad JSON
                last = e
        raise SceneError(f"failed after a retry: {last!r}")


# ---------- the service the publisher uses: enabled / key / cap / cache / cost log, never raises ----------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class SceneService:
    quota_until = 0.0      # class-wide: after a 429 every post keeps its plain card until this time

    def __init__(self, s: Settings, db: DB, transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.db, self.transport = s, db, transport

    @property
    def dir(self) -> Path:
        return self.s.data_dir / "scenes"

    def path(self, post_key: str) -> Path:
        return self.dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', post_key)}.png"

    def used_today(self) -> int:
        return self.db.one("SELECT COUNT(*) n FROM scene_images WHERE day=? AND status!='quota'", _today())["n"]

    def spent_today(self) -> float:
        return self.db.one("SELECT COALESCE(SUM(cost_usd),0) c FROM scene_images WHERE day=?", _today())["c"]

    def fmt_of(self, post_key: str) -> str | None:
        """The format a cached scene was generated in (the overlay must match it)."""
        try:
            f = self.path(post_key).with_suffix(".fmt").read_text().strip()
            return f if f in FORMATS else None
        except OSError:
            return None

    async def scene(self, rnd: dict, kind: str, post_key: str, fmt: str = "scene",
                    gags: dict | None = None, style: str = "cartoon") -> Path | None:
        """The scene PNG for a post (format `fmt`, recurring `gags`), or None (the caller posts today's card)."""
        try:
            return await self._scene(rnd, kind, post_key, fmt, gags, style)
        except SceneRefused as e:
            log.warning("%s: scene refused (%s); plain card", post_key, e)
            return None
        except Exception:
            log.exception("%s: scene image failed; using the plain card", post_key)
            return None

    async def _scene(self, rnd: dict, kind: str, post_key: str, fmt: str, gags: dict | None,
                     style: str = "cartoon") -> Path | None:
        if not self.s.scene_images_enabled:
            return None
        build_prompt(rnd, kind, fmt=fmt, gags=gags, style=style)    # a pre-reveal kind raises before anything else, even with the cache warm
        out = self.path(post_key)
        if out.exists():
            return out
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            log.info("%s: no GEMINI_API_KEY; plain card", post_key)
            return None
        if blocked_by_keywords(self.s, rnd):
            log.info("%s: market hits excluded_keywords; no scene", post_key)
            return None
        if time.time() < SceneService.quota_until:
            return None
        if self.used_today() >= self.s.scene_images_per_day:
            log.info("%s: scene cap (%s a day) reached; plain card", post_key, self.s.scene_images_per_day)
            return None
        refs = _ref_inputs(style) if self.s.scene_images_references else []
        movie = await self.movie(rnd, self.s.scene_movie_style or style, key) if self.s.scene_movie else None
        if movie:   # step 3: the post image is a new still from the market's movie, starting from its poster
            style, fmt = movie["style"], "scene"
            refs = [_image_part(self.movie_poster(rnd["round_id"]))]
            prompt = movie_frame_prompt(rnd, kind, movie, style)
        else:
            prompt = build_prompt(rnd, kind, has_refs=bool(refs), fmt=fmt, gags=gags, style=style)
        made = await self._make(post_key, prompt, refs, out, key)
        if made:
            made.with_suffix(".fmt").write_text(fmt)
        return made

    def movie_meta(self, round_id) -> Path:
        return self.dir / f"movie-r{round_id}.json"

    def movie_poster(self, round_id) -> Path:
        return self.dir / f"movie-r{round_id}.png"

    def recent_movies(self, n: int = 200) -> list[dict]:
        """The latest movies (oldest first), for genre/director variety."""
        out = []
        for p in sorted(self.dir.glob("movie-r*.json"), key=lambda p: p.stat().st_mtime)[-n:]:
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                pass
        return out

    def movie_of(self, round_id) -> dict | None:
        """The market's movie (title, idea, visual, style) once its poster exists, else None."""
        try:
            m = json.loads(self.movie_meta(round_id).read_text())
            return m if self.movie_poster(round_id).exists() else None
        except (OSError, ValueError):
            return None

    async def movie(self, rnd: dict, style: str, key: str) -> dict | None:
        """Steps 1 and 2 of the operator's flow (2026-10-27), once per market: a one-paragraph movie idea about the
        market's subject (Gemini text), then that movie's poster (Gemini image, the cast sheet as reference). Every
        post image of the market then starts from the poster. None on any failure: the post falls back to a plain
        scene."""
        rid = rnd.get("round_id")
        have = self.movie_of(rid)
        if have:
            return have
        genre, director = pick_genre_director(rid, self.recent_movies())
        try:
            pitch = check_pitch(await asyncio.wait_for(TextClient(self.s, key, self.transport).json(
                movie_pitch_prompt(rnd, genre, director)),
                                                       timeout=self.s.scene_images_timeout_s + 10))
            refs = []
            pitch = {**pitch, "genre": genre, "director": director}
            prompt = movie_poster_prompt(rnd, pitch, style)
        except SceneQuota:
            SceneService.quota_until = time.time() + QUOTA_BACKOFF_S
            return None
        except Exception as e:
            log.warning("round %s: movie pitch failed (%r); plain scene", rid, e)
            return None
        if not await self._make(f"movie-r{rid}", prompt, refs, self.movie_poster(rid), key):
            return None
        meta = {**pitch, "style": style, "round_id": rid, "created_at": time.time()}
        self.movie_meta(rid).write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        log.info("round %s: movie '%s' (%s, %s, %s)", rid, pitch["title"], genre, director, style)
        return meta

    async def _make(self, post_key: str, prompt: str, refs: list[dict], out: Path, key: str) -> Path | None:
        row = self.db.x("INSERT INTO scene_images(post_key,day,model,status,cost_usd,created_at,prompt) "
                        "VALUES(?,?,?,?,0,?,?)", post_key, _today(), self.s.scene_images_model, "pending", time.time(),
                        prompt)
        try:
            img = await asyncio.wait_for(SceneClient(self.s, key, self.transport).generate(prompt, refs),
                                         timeout=self.s.scene_images_timeout_s * 2 + 10)
        except SceneQuota as e:   # expected until billing is enabled: quiet, and no more calls for a while
            SceneService.quota_until = time.time() + QUOTA_BACKOFF_S
            self.db.x("UPDATE scene_images SET status='quota', detail=? WHERE id=?", str(e)[:300], row)
            log.info("%s: scene images unavailable (%s); plain card, retrying in %d min", post_key, e,
                     QUOTA_BACKOFF_S // 60)
            return None
        except Exception as e:
            self.db.x("UPDATE scene_images SET status='failed', detail=? WHERE id=?", repr(e)[:300], row)
            log.warning("%s: scene generation failed (%r); plain card", post_key, e)
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(img)
        tmp.replace(out)
        self.db.x("UPDATE scene_images SET status='ok', cost_usd=? WHERE id=?", self.s.scene_image_cost_usd, row)
        log.info("%s: scene image ok (%d bytes)", post_key, len(img))
        return out
