"""The Match of the Day music bed: an original instrumental, synthesised from scratch by this code.

No samples, loops or melodies from anywhere else go in, so the result carries no third-party rights; the
committed file (static/motd/theme.m4a) is dedicated to the public domain under CC0 1.0 (see MUSIC-LICENSE.txt).
Deterministic: the same code always produces the same samples (noise comes from a fixed-seed LCG).

    python -m agentpitbench.music agentpitbench/static/motd/theme.m4a

Pure Python (no numpy): A minor, 112 BPM, i-VI-III-VII (Am F C G) with a soft pad, a plucked arpeggio, a bass line
and light kick/hat, building over the first eight bars and easing off at the end.

Per-concept variations (PRESETS) are the fallback score when no ElevenLabs track exists for a film's concept: the same
synth with a concept's tempo, harmony, groove and a lead voice (whistle for the western, muted horn for noir, ...),
rendered once per concept and cached. `synth()` with no preset is byte-identical to the committed theme.
"""
from __future__ import annotations

import math
import struct
import subprocess
import sys
import wave
from pathlib import Path

RATE = 22050
BPM = 112
BEAT = 60 / BPM
BAR = 4 * BEAT
# chord roots and triads as MIDI notes: Am, F, C, G
CHORDS = [(45, (57, 60, 64)), (41, (53, 57, 60)), (48, (55, 60, 64)), (43, (55, 59, 62))]
ARP = [0, 1, 2, 1, 2, 0, 2, 1]   # triad index per eighth note


def hz(note: int) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


class _Noise:
    """Fixed-seed linear congruential generator: reproducible hats on every machine."""

    def __init__(self, seed: int = 20261012):
        self.x = seed

    def __call__(self) -> float:
        self.x = (1103515245 * self.x + 12345) & 0x7FFFFFFF
        return self.x / 0x3FFFFFFF - 1.0


# Concept presets. chords: (bass root, triad) per bar as MIDI notes; arp: triad index per eighth (None = no arp);
# bass: beat | walk | pulse | drone; drums: pop | brush | tick | march | none; lead: None or a timbre, playing
# `melody` (scale steps over the triad, one per quarter, None = rest); swing: 0..0.33 of an eighth.
DEFAULT = {"bpm": BPM, "chords": CHORDS, "arp": ARP, "bass": "beat", "drums": "pop", "lead": None, "melody": None,
           "swing": 0.0, "pad": 1.0, "seed": 20261012}
PRESETS: dict[str, dict] = {
    "noir": {"bpm": 72, "chords": [(45, (57, 60, 64)), (50, (57, 62, 65)), (52, (56, 59, 62)), (45, (57, 60, 64))],
             "arp": None, "bass": "walk", "drums": "brush", "lead": "horn", "swing": 0.28,
             "melody": [2, None, 1, 0, None, 2, 1, None]},
    "western": {"bpm": 84, "chords": [(40, (52, 55, 59)), (40, (52, 55, 59)), (45, (52, 57, 60)), (47, (54, 59, 63))],
                "arp": [0, 2, 1, 2, 0, 2, 1, 2], "bass": "drone", "drums": "march", "lead": "whistle", "swing": 0.12,
                "melody": [0, None, 2, 1, 2, None, None, 0]},
    "courtroom": {"bpm": 66, "chords": [(38, (50, 53, 57)), (46, (50, 53, 58)), (43, (50, 55, 58)), (45, (49, 52, 57))],
                  "arp": None, "bass": "pulse", "drums": "none", "lead": "strings", "pad": 1.4,
                  "melody": [2, None, None, 1, None, None, 0, None]},
    "heist": {"bpm": 104, "chords": [(40, (55, 59, 62)), (40, (55, 59, 62)), (43, (55, 58, 62)), (45, (55, 57, 60))],
              "arp": [0, None, 2, None, 1, None, 2, 0], "bass": "walk", "drums": "tick", "lead": None, "swing": 0.2},
    "broadcast": {"bpm": 128, "chords": [(48, (60, 64, 67)), (53, (60, 65, 69)), (55, (59, 62, 67)), (48, (60, 64, 67))],
                  "arp": [0, 1, 2, 1, 0, 1, 2, 1], "bass": "beat", "drums": "pop", "lead": "brass",
                  "melody": [0, 1, 2, 2, 1, None, 2, None]},
    "countdown": {"bpm": 120, "chords": [(45, (57, 60, 64)), (45, (57, 60, 64)), (46, (58, 62, 65)), (45, (57, 60, 64))],
                  "arp": None, "bass": "pulse", "drums": "tick", "lead": None, "pad": 0.8},
    "nature_doc": {"bpm": 76, "chords": [(43, (55, 59, 62)), (40, (55, 59, 64)), (36, (55, 60, 64)), (38, (54, 57, 62))],
                   "arp": [0, 1, 2, 1, 2, 1, 0, 1], "bass": "drone", "drums": "none", "lead": "strings", "pad": 1.3,
                   "melody": [2, None, 1, None, 0, None, None, None]},
    "mockumentary": {"bpm": 112, "chords": [(48, (60, 64, 67)), (45, (57, 60, 64)), (41, (57, 60, 65)), (43, (55, 59, 62))],
                     "arp": [0, None, 1, None, 2, None, 1, None], "bass": "beat", "drums": "tick", "lead": "glock",
                     "swing": 0.1, "melody": [2, 1, 0, None, 1, None, 2, None]},
    "mission_control": {"bpm": 96, "chords": [(38, (57, 62, 64)), (41, (57, 60, 65)), (36, (55, 60, 64)), (43, (55, 59, 62))],
                        "arp": [0, 1, 2, 1, 0, 1, 2, 1], "bass": "pulse", "drums": "none", "lead": None, "pad": 1.2},
}


def preset(concept: str | None) -> dict:
    return {**DEFAULT, **PRESETS.get(concept or "", {}), "seed": DEFAULT["seed"] + sum(map(ord, concept or ""))} \
        if concept in PRESETS else dict(DEFAULT)


def _lead(kind: str, f: float, t: float, dur: float) -> float:
    """One lead note (amplitude before the mix level)."""
    rel = min(1.0, max(0.0, (dur - t) / 0.08))
    if kind == "whistle":   # pure sine, slow vibrato, soft attack
        vib = 1 + 0.006 * math.sin(2 * math.pi * 5.5 * t)
        return min(1.0, t / 0.05) * rel * math.sin(2 * math.pi * f * vib * t)
    if kind == "horn":      # muted trumpet-ish: odd harmonics, breathy attack
        env = min(1.0, t / 0.04) * rel * (0.85 + 0.15 * math.sin(2 * math.pi * 4 * t))
        return env * (math.sin(2 * math.pi * f * t) + 0.45 * math.sin(6 * math.pi * f * t)
                      + 0.2 * math.sin(10 * math.pi * f * t)) / 1.65
    if kind == "brass":     # bright fanfare: many harmonics
        env = min(1.0, t / 0.02) * rel
        return env * sum(math.sin(2 * math.pi * f * k * t) / k for k in range(1, 6)) / 2.3
    if kind == "strings":   # slow bowed swell, chorus
        env = min(1.0, t / 0.35) * rel
        return env * (math.sin(2 * math.pi * f * t) + math.sin(2 * math.pi * f * 1.003 * t)
                      + 0.3 * math.sin(4 * math.pi * f * t)) / 2.3
    if kind == "glock":     # bell: fast decay, inharmonic partial
        env = math.exp(-t * 6) * min(1.0, t / 0.002)
        return env * (math.sin(2 * math.pi * f * t) + 0.4 * math.sin(2 * math.pi * f * 2.76 * t))
    return 0.0


def synth(seconds: float = 60.0, rate: int = RATE, concept: str | None = None) -> list[float]:
    if concept in PRESETS:
        return _synth_preset(seconds, rate, preset(concept))
    n = int(seconds * rate)
    out = [0.0] * n
    noise = _Noise()

    def add(start: float, dur: float, fn) -> None:
        i0, i1 = int(start * rate), min(n, int((start + dur) * rate))
        for i in range(max(0, i0), i1):
            out[i] += fn((i - i0) / rate)

    bars = int(seconds / BAR) + 1
    total_bars = seconds / BAR
    for b in range(bars):
        t0 = b * BAR
        if t0 >= seconds:
            break
        root, triad = CHORDS[b % 4]
        build = min(1.0, (b + 1) / 8)                      # layers come in over the first eight bars
        ease = max(0.35, min(1.0, (total_bars - b) / 3))   # and ease off in the last bars
        # pad: three detuned triangle-ish voices, slow attack and release
        for k, note in enumerate(triad):
            f1, f2 = hz(note), hz(note) * 1.004

            def pad(t, f1=f1, f2=f2, ph=k * 0.7):
                env = min(1.0, t / 0.6) * min(1.0, (BAR - t) / 0.4)
                s = (math.sin(2 * math.pi * f1 * t + ph) + 0.25 * math.sin(6 * math.pi * f1 * t + ph)
                     + math.sin(2 * math.pi * f2 * t))
                return 0.035 * env * s * ease
            add(t0, BAR, pad)
        # bass: root on each beat, octave on the "and" of 4
        for beat in range(4):
            for half in (0, 1):
                if half and beat != 3:
                    continue
                f = hz(root + (12 if half else 0))

                def bass(t, f=f):
                    env = math.exp(-t * 5)
                    return 0.16 * env * (math.sin(2 * math.pi * f * t) + 0.3 * math.sin(4 * math.pi * f * t)) * ease
                add(t0 + (beat + 0.5 * half) * BEAT, BEAT * 0.9, bass)
        if b >= 2:  # plucked arpeggio in eighths, an octave up
            for j, idx in enumerate(ARP):
                f = hz(triad[idx] + 12)

                def pluck(t, f=f, acc=1.0 if j % 4 == 0 else 0.7):
                    env = math.exp(-t * 9) * min(1.0, t / 0.004)
                    return 0.07 * acc * env * (math.sin(2 * math.pi * f * t) + 0.2 * math.sin(4 * math.pi * f * t)) \
                        * build * ease
                add(t0 + j * BEAT / 2, BEAT / 2, pluck)
        if b >= 4:  # kick on beats, hat on the off-beats
            for beat in range(4):
                def kick(t):
                    f = 50 + 70 * math.exp(-t * 30)
                    return 0.28 * math.exp(-t * 14) * math.sin(2 * math.pi * f * t) * ease
                add(t0 + beat * BEAT, 0.3, kick)

                def hat(t):
                    return 0.035 * math.exp(-t * 60) * noise() * ease
                add(t0 + (beat + 0.5) * BEAT, 0.08, hat)
    peak = max(1e-9, max(abs(v) for v in out))
    fade = int(1.5 * rate)
    for i in range(n - fade, n):   # the loop point: fade to silence
        out[i] *= (n - i) / fade
    return [0.9 * v / peak for v in out]


def _synth_preset(seconds: float, rate: int, p: dict) -> list[float]:
    """The concept variant of the theme: same building blocks, the preset's tempo, harmony, groove and lead."""
    n = int(seconds * rate)
    out = [0.0] * n
    noise = _Noise(p["seed"])
    beat = 60 / p["bpm"]
    bar = 4 * beat
    chords, arp = p["chords"], p["arp"]

    def add(start: float, dur: float, fn) -> None:
        i0, i1 = int(start * rate), min(n, int((start + dur) * rate))
        for i in range(max(0, i0), i1):
            out[i] += fn((i - i0) / rate)

    def eighth(j: int) -> float:  # swung eighths: the off-beat lands late
        return (j // 2) * beat + (j % 2) * beat * (0.5 + p["swing"])

    total_bars = seconds / bar
    for b in range(int(total_bars) + 1):
        t0 = b * bar
        if t0 >= seconds:
            break
        root, triad = chords[b % len(chords)]
        build = min(1.0, (b + 1) / 6)
        ease = max(0.35, min(1.0, (total_bars - b) / 3))
        for k, note in enumerate(triad):   # pad
            f1, f2 = hz(note), hz(note) * 1.004

            def pad(t, f1=f1, f2=f2, ph=k * 0.7):
                env = min(1.0, t / 0.8) * min(1.0, (bar - t) / 0.5)
                return 0.032 * p["pad"] * env * ease * (math.sin(2 * math.pi * f1 * t + ph)
                                                        + math.sin(2 * math.pi * f2 * t))
            add(t0, bar, pad)
        style = p["bass"]
        if style == "drone":
            f = hz(root)
            add(t0, bar, lambda t, f=f: 0.09 * ease * min(1.0, t / 0.3) * min(1.0, (bar - t) / 0.3)
                * (math.sin(2 * math.pi * f * t) + 0.25 * math.sin(4 * math.pi * f * t)))
        else:
            steps = {"beat": [0, 0, 0, 0], "walk": [0, 4, 7, 9 if b % 2 else 5], "pulse": [0] * 8}[style]
            dur = bar / len(steps)
            for j, st in enumerate(steps):
                f = hz(root + st)
                add(t0 + j * dur, dur * 0.9, lambda t, f=f: 0.15 * math.exp(-t * (8 if style == "pulse" else 4))
                    * (math.sin(2 * math.pi * f * t) + 0.3 * math.sin(4 * math.pi * f * t)) * ease)
        if arp and b >= 1:
            for j, idx in enumerate(arp):
                if idx is None:
                    continue
                f = hz(triad[idx] + 12)
                add(t0 + eighth(j), beat / 2, lambda t, f=f, acc=1.0 if j % 4 == 0 else 0.7:
                    0.06 * acc * math.exp(-t * 9) * min(1.0, t / 0.004) * math.sin(2 * math.pi * f * t) * build * ease)
        if p["lead"] and p["melody"] and b >= 2 and b % 4 in (2, 3, 0):
            mel = p["melody"]
            q = bar / 4 if len(mel) <= 4 else bar / len(mel) * 2
            for j, idx in enumerate(mel[b % 2 * (len(mel) // 2):][:len(mel) // 2] if len(mel) > 4 else mel):
                if idx is None:
                    continue
                f = hz(triad[idx] + 12)
                add(t0 + j * q, q * 0.95, lambda t, f=f, d=q * 0.95: 0.11 * _lead(p["lead"], f, t, d) * ease)
        drums = p["drums"]
        if drums != "none" and b >= 2:
            for bt in range(4):
                if drums in ("pop", "march"):
                    if drums == "pop" or bt % 2 == 0:
                        add(t0 + bt * beat, 0.3, lambda t: 0.26 * math.exp(-t * 14)
                            * math.sin(2 * math.pi * (50 + 70 * math.exp(-t * 30)) * t) * ease)
                    if drums == "march" or bt % 2:
                        add(t0 + bt * beat + (beat / 2 if drums == "pop" else 0), 0.12,
                            lambda t: 0.05 * math.exp(-t * 35) * noise() * ease)
                elif drums == "brush":
                    add(t0 + bt * beat, beat * 0.9, lambda t: 0.018 * math.exp(-t * 6) * noise() * ease)
                elif drums == "tick":
                    for h in (0, 1):
                        add(t0 + bt * beat + h * beat / 2, 0.03, lambda t: 0.04 * math.exp(-t * 120)
                            * math.sin(2 * math.pi * 3200 * t) * ease)
    peak = max(1e-9, max(abs(v) for v in out))
    fade = int(1.5 * rate)
    for i in range(n - fade, n):
        out[i] *= (n - i) / fade
    return [0.9 * v / peak for v in out]


def write_wav(samples: list[float], path: Path, rate: int = RATE) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767)) for v in samples))
    return path


def build(out: Path, seconds: float = 60.0, concept: str | None = None) -> Path:
    """Synthesise and encode to AAC (m4a) with the bundled ffmpeg; a .wav target skips the encode."""
    from .media import ffmpeg_exe
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".wav":
        return write_wav(synth(seconds, concept=concept), out)
    wav = write_wav(synth(seconds, concept=concept), out.with_suffix(".tmp.wav"))
    try:
        subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "64k",
                        "-movflags", "+faststart", str(out)], check=True, timeout=120)
    finally:
        wav.unlink(missing_ok=True)
    return out


PRESET_VERSION = 1   # bump when PRESETS or _synth_preset change, so cached concept beds are re-rendered


def concept_bed(cache: Path, concept: str | None, seconds: float = 62.0) -> Path | None:
    """The concept's procedural score, rendered once and cached (a few seconds of pure Python); None for an unknown
    concept (the caller uses the committed theme)."""
    if concept not in PRESETS:
        return None
    p = Path(cache) / f"proc-{concept}-v{PRESET_VERSION}-{int(seconds)}s.m4a"
    if p.exists() and p.stat().st_size > 0:
        return p
    tmp = p.with_name(p.stem + ".part.m4a")
    build(tmp, seconds, concept)
    tmp.replace(p)
    return p


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "theme.m4a")))
