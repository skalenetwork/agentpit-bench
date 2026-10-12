"""Match of the Day v0.5 sound: a narrator voice, a score per concept, a sting on the twist and a genre effect.

Engines (pluggable, never blocking; every failure falls back and the film still renders):
- narrator: ElevenLabs text-to-speech (`eleven_v4`, audio tags for delivery) when ELEVENLABS_API_KEY is set
  (secrets.env, as every other key), else none (captions only). A local engine can be added behind `narrate`.
- score: an ElevenLabs Music track from a small per-concept library (generated once per prompt, cached and reused),
  else the procedural CC0 bed (static/motd/theme.m4a).
- sting and genre effect: ElevenLabs Sound Effects, cached per prompt, else silence.

Everything is cached by a hash of (engine, model, voice, settings, text or prompt) under var/audio_cache/, so a
re-render never re-bills. Credits reported by the API (`character-cost` header) are logged per film.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io"
TTS_MODEL = "eleven_v4"
MUSIC_MODEL = "music_v1"

# Stock ElevenLabs voices per concept (hard-coded: the key may not list voices). tag = the v4 audio tag that sets
# the delivery for every line; settings = voice_settings sent with each request.
VOICES: dict[str, dict] = {
    "noir": {"id": "nPczCjzI2devNBz1zQrb", "name": "Brian", "tag": "[low, weary, gravelly]"},
    "nature_doc": {"id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "tag": "[hushed, reverent]"},
    "heist": {"id": "cjVigY5qzO86Huf0OWal", "name": "Eric", "tag": "[smooth, confident]"},
    "courtroom": {"id": "onwK4e9ZLuTAKqWW03F9", "name": "Daniel", "tag": "[measured, solemn]"},
    "broadcast": {"id": "TX3LPaxmHKxFdv7VOQHJ", "name": "Liam", "tag": "[excited, fast]"},
    "countdown": {"id": "N2lVS1w4EtoT3dr4eOWO", "name": "Callum", "tag": "[tense, urgent]"},
    "mockumentary": {"id": "cgSgspJ2msm6clMCkdW9", "name": "Jessica", "tag": "[deadpan]"},
    "western": {"id": "2EiwWnXFnvU5JabPnv8n", "name": "Clyde", "tag": "[slow, gravelly drawl]"},
    "mission_control": {"id": "CwhRBWXzGAHq8TQ4Fs17", "name": "Roger", "tag": "[calm, clipped]"},
}
FALLBACK_VOICE = {"id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "tag": ""}
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}

# Score prompts per concept (instrumental). A library of `variants` tracks per concept is generated on demand and
# rotated by round id, so a month of films costs at most len(CONCEPTS) * variants music generations.
MUSIC: dict[str, str] = {
    "noir": "smoky film noir jazz, slow brushed drums, muted trumpet and saxophone, upright bass, rain-soaked tension",
    "nature_doc": "gentle nature documentary strings and piano, wonder, slow build, warm and hopeful",
    "heist": "slick heist movie groove, ticking hi-hats, funky bass, tension stabs, cool and propulsive",
    "courtroom": "courtroom drama underscore, solemn strings, low piano, building suspense",
    "broadcast": "big sports broadcast theme, brass fanfare, driving drums, stadium energy",
    "countdown": "thriller countdown score, pulsing synth bass, ticking clock percussion, rising tension",
    "mockumentary": "quirky deadpan mockumentary ukulele and glockenspiel, light and awkward",
    "western": "spaghetti western standoff, twangy guitar, whistling, slow tension, distant trumpet",
    "mission_control": "space mission control ambient synths, steady pulse, wonder and tension",
}
STING: dict[str, str] = {
    "noir": "dramatic film noir brass stab with a cymbal swell",
    "nature_doc": "sudden orchestral string hit, surprise",
    "heist": "vault door slams with a deep cinematic impact",
    "courtroom": "a wooden gavel strikes hard, once, with a dramatic orchestral hit",
    "broadcast": "stadium crowd gasps and roars, air horn",
    "countdown": "deep cinematic boom with a clock stopping",
    "mockumentary": "record scratch then silence",
    "western": "ricocheting gunshot echo in a canyon with a guitar twang",
    "mission_control": "alarm klaxon blip then a deep sub boom",
}
MUSIC_VARIANTS = 2
MUSIC_LEN_S = 62


def key() -> str | None:
    return os.environ.get("ELEVENLABS_API_KEY") or None


def cache_dir(s) -> Path:
    d = Path(s.data_dir) / "audio_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _post(path: str, body: dict, timeout: int = 180) -> tuple[bytes | None, int]:
    """(audio bytes or None, credits reported by the API)."""
    k = key()
    if not k:
        return None, 0
    req = urllib.request.Request(API + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"xi-api-key": k, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cost = r.headers.get("character-cost") or "0"
            return r.read(), int(float(cost)) if cost.replace(".", "", 1).isdigit() else 0
    except urllib.error.HTTPError as e:
        log.warning("ElevenLabs %s: HTTP %s %s", path.split("?")[0], e.code, e.read()[:200])
    except Exception as e:  # network, timeout
        log.warning("ElevenLabs %s failed: %s", path.split("?")[0], e)
    return None, 0


# ---------- spoken form: the same slot values as the captions, read aloud ----------

def speakable(text: str) -> str:
    """Caption text as the narrator should say it: 79¢ -> 79 cents, 74% -> 74 percent, −100 -> minus 100,
    1m 33s -> 1 minute 33 seconds. Numbers stay the slot values; only the symbols become words."""
    t = text.replace("“", '"').replace("”", '"')
    t = re.sub(r"(\d+(?:\.\d+)?)¢", r"\1 cents", t)
    t = re.sub(r"(\d+(?:\.\d+)?)%", r"\1 percent", t)
    t = re.sub(r"[−-](\d)", r"minus \1", t)
    t = re.sub(r"\+(\d)", r"plus \1", t)
    t = re.sub(r"\b1m (\d+)s\b", r"1 minute \1 seconds", t)
    t = re.sub(r"\b(\d+)m (\d+)s\b", r"\1 minutes \2 seconds", t)
    t = re.sub(r"\b(\d+)s\b", r"\1 seconds", t)
    return t


# ---------- engines ----------

def narrate_line(s, text: str, concept: str, voice: dict | None = None) -> tuple[Path | None, int, dict]:
    """One line through the narrator. (mp3 path or None, credits, voice record). A concept voice that fails
    (e.g. retired from the stock library) falls back to FALLBACK_VOICE once."""
    v = voice or VOICES.get(concept, FALLBACK_VOICE)
    spoken = f"{v['tag']} {speakable(text)}".strip()
    rec = {"engine": "elevenlabs", "model": TTS_MODEL, "voice_id": v["id"], "voice": v["name"],
           "settings": VOICE_SETTINGS, "tag": v["tag"]}
    p = cache_dir(s) / f"tts-{_hash(rec, spoken)}.mp3"
    if p.exists() and p.stat().st_size > 0:
        return p, 0, rec
    audio, cost = _post(f"/v1/text-to-speech/{v['id']}?output_format=mp3_44100_128",
                        {"text": spoken, "model_id": TTS_MODEL, "voice_settings": VOICE_SETTINGS})
    if not audio:
        if v["id"] != FALLBACK_VOICE["id"]:
            return narrate_line(s, text, concept, {**FALLBACK_VOICE, "tag": v["tag"]})
        return None, 0, rec
    p.write_bytes(audio)
    return p, cost, rec


def score(s, concept: str, round_id: int) -> tuple[Path | None, int, dict]:
    """A library track for the concept, rotated by round id. Generated once per (prompt, variant), then reused."""
    prompt = MUSIC.get(concept)
    if not prompt:
        return None, 0, {}
    variant = round_id % MUSIC_VARIANTS
    rec = {"engine": "elevenlabs-music", "model": MUSIC_MODEL, "prompt": prompt, "variant": variant,
           "length_ms": MUSIC_LEN_S * 1000}
    p = cache_dir(s) / f"music-{concept}-{_hash(rec)}.mp3"
    if p.exists() and p.stat().st_size > 0:
        return p, 0, rec
    audio, cost = _post("/v1/music?output_format=mp3_44100_128",
                        {"prompt": f"{prompt}. Instrumental, no vocals. Variation {variant + 1}.",
                         "music_length_ms": MUSIC_LEN_S * 1000, "model_id": MUSIC_MODEL,
                         "force_instrumental": True}, timeout=600)
    if not audio:
        return None, 0, rec
    p.write_bytes(audio)
    return p, cost, rec


def effect(s, prompt: str, seconds: float = 2.0) -> tuple[Path | None, int]:
    rec = {"engine": "elevenlabs-sfx", "prompt": prompt, "seconds": seconds}
    p = cache_dir(s) / f"sfx-{_hash(rec)}.mp3"
    if p.exists() and p.stat().st_size > 0:
        return p, 0
    audio, cost = _post("/v1/sound-generation", {"text": prompt, "duration_seconds": seconds})
    if not audio:
        return None, 0
    p.write_bytes(audio)
    return p, cost


# ---------- the film's soundtrack ----------

LEAD_S = 0.3      # the narrator starts this long after a scene's cut
TAIL_S = 0.45     # and the scene holds this long after the line ends
MAX_TEMPO = 1.12  # the fastest the narrator may be sped up (atempo keeps pitch)


def voice_scenes(s, scenes: list[dict], concept: str, max_s: float = 60.0) -> dict:
    """Narrate every scene's caption and stretch scenes so each line fits (scene dur >= lead + line + tail).
    If the voiced film would pass max_s, lines are sped up (atempo, at most 1.2x). Mutates scenes[*]['voice'] and
    ['dur']. Returns the audio record for film.json; {} when no narrator is available."""
    from .media import duration
    if not key() or not getattr(s, "motd_narrator", True):
        return {}
    credits, rec, n_chars = 0, None, 0
    orig = [x["dur"] for x in scenes]
    for sc in scenes:
        text = sc.get("caption") or ""
        if sc["kind"] == "s_title":
            text = sc.get("title") or text
        if not text.strip():
            continue
        p, c, rec = narrate_line(s, text, concept)
        credits += c
        n_chars += len(speakable(text))
        if p is None:
            log.warning("narrator failed on a line; the film keeps captions only")
            for x in scenes:
                x.pop("voice", None)
            return {}
        sc["voice"] = {"path": str(p), "len": duration(p) or 0.0}
    # floor of each scene: its voiced line (or a short visual beat when silent)
    def floor(x, tempo):
        return LEAD_S + x["voice"]["len"] / tempo + TAIL_S if x.get("voice") else min(x["dur"], 2.5)

    tempo = 1.0
    if sum(floor(x, 1.0) for x in scenes) > max_s:  # the lines alone overrun: speak a little faster
        spoken = sum(x["voice"]["len"] for x in scenes if x.get("voice"))
        over = sum(floor(x, 1.0) for x in scenes) - max_s
        tempo = min(MAX_TEMPO, spoken / max(1.0, spoken - over))
    for x in scenes:
        if x.get("voice"):
            x["voice"]["tempo"] = round(tempo, 3)
            x["dur"] = max(x["dur"], floor(x, tempo))
    total = sum(x["dur"] for x in scenes)
    if total > max_s:  # trim visual holds beyond each line, proportionally, down to the floors
        slack = sum(x["dur"] - floor(x, tempo) for x in scenes)
        cut = min(total - max_s, slack)
        for x in scenes:
            x["dur"] -= cut * (x["dur"] - floor(x, tempo)) / slack if slack else 0
    for x in scenes:
        x["dur"] = round(x["dur"], 3)
    if sum(x["dur"] for x in scenes) > max_s + 0.5:  # the 60 s cap wins over the voice: captions only
        log.warning("narration does not fit %.0f s; the film keeps captions only", max_s)
        for x, d in zip(scenes, orig):
            x["dur"] = d
            x.pop("voice", None)
        return {}
    return {"narrator": rec, "chars": n_chars, "credits": credits, "tempo": round(tempo, 3),
            "voiced_s": round(sum(x["dur"] for x in scenes), 2)}


def soundtrack(s, scenes: list[dict], concept: str, round_id: int, out: Path) -> tuple[Path | None, dict]:
    """Mix narration (ducking the score), the score, the twist sting and one genre effect into one AAC file
    the length of the film. (path or None, record)."""
    from .media import ffmpeg_exe
    exe = ffmpeg_exe()
    total = sum(x["dur"] for x in scenes)
    music, mc, mrec = score(s, concept, round_id) if key() else (None, 0, {})
    if music is None:
        from .motd import MUSIC as BED
        music, mrec = BED, {"engine": "procedural", "file": "theme.m4a"}
    starts, t = [], 0.0
    for x in scenes:
        starts.append(t)
        t += x["dur"]
    sting, sc_ = (None, 0)
    if key() and STING.get(concept):
        sting, sc_ = effect(s, STING[concept], 2.0)
    twist_at = next((st + 0.3 for st, x in zip(starts, scenes) if x["kind"] == "s_twist"), None)
    inputs = ["-stream_loop", "-1", "-i", str(music)]
    chains, nar = [], []
    idx = 1
    for st, x in zip(starts, scenes):
        v = x.get("voice")
        if not v:
            continue
        inputs += ["-i", v["path"]]
        ms = int((st + LEAD_S) * 1000)
        tempo = f",atempo={v['tempo']}" if v.get("tempo", 1.0) != 1.0 else ""
        chains.append(f"[{idx}:a]aresample=44100,aformat=channel_layouts=stereo{tempo},adelay={ms}|{ms}[n{idx}]")
        nar.append(f"[n{idx}]")
        idx += 1
    fx = []
    if sting and twist_at is not None:
        inputs += ["-i", str(sting)]
        ms = int(twist_at * 1000)
        chains.append(f"[{idx}:a]aresample=44100,aformat=channel_layouts=stereo,volume=0.9,adelay={ms}|{ms}[fx{idx}]")
        fx.append(f"[fx{idx}]")
        idx += 1
    bed = (f"[0:a]aresample=44100,aformat=channel_layouts=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
           f"volume={0.55 if nar else 0.8},afade=t=in:st=0:d=0.5,afade=t=out:st={max(0.0, total - 2.0):.3f}:d=2[bed]")
    chains.append(bed)
    if nar:
        chains.append(f"{''.join(nar)}amix=inputs={len(nar)}:normalize=0,apad=whole_dur={total:.3f}[voice]")
        chains.append("[voice]asplit=2[v1][v2]")
        chains.append("[bed][v1]sidechaincompress=threshold=0.04:ratio=8:attack=15:release=350[duck]")
        mix_in = ["[duck]", "[v2]", *fx]
    else:
        mix_in = ["[bed]", *fx]
    chains.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:normalize=0:duration=first,"
                  f"atrim=0:{total:.3f},alimiter=limit=0.95[out]")
    cmd = [exe, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(chains), "-map", "[out]",
           "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}", str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=600)
    except Exception:
        log.exception("soundtrack mix failed")
        return None, {}
    return out, {"score": mrec, "score_credits": mc, "sting": STING.get(concept) if sting else None,
                 "sting_credits": sc_}


def mux(video: Path, audio: Path, out: Path) -> Path | None:
    from .media import ffmpeg_exe
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0",
           "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=600)
        return out
    except Exception:
        log.exception("ffmpeg mux failed")
        return None
