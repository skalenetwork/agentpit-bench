"""The Match of the Day music bed: an original instrumental, synthesised from scratch by this code.

No samples, loops or melodies from anywhere else go in, so the result carries no third-party rights; the
committed file (static/motd/theme.m4a) is dedicated to the public domain under CC0 1.0 (see MUSIC-LICENSE.txt).
Deterministic: the same code always produces the same samples (noise comes from a fixed-seed LCG).

    python -m agentpitbench.music agentpitbench/static/motd/theme.m4a

Pure Python (no numpy): A minor, 112 BPM, i-VI-III-VII (Am F C G) with a soft pad, a plucked arpeggio, a bass line
and light kick/hat, building over the first eight bars and easing off at the end.
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


def synth(seconds: float = 60.0, rate: int = RATE) -> list[float]:
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


def write_wav(samples: list[float], path: Path, rate: int = RATE) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767)) for v in samples))
    return path


def build(out: Path, seconds: float = 60.0) -> Path:
    """Synthesise and encode to AAC (m4a) with the bundled ffmpeg; a .wav target skips the encode."""
    from .media import ffmpeg_exe
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".wav":
        return write_wav(synth(seconds), out)
    wav = write_wav(synth(seconds), out.with_suffix(".tmp.wav"))
    try:
        subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "64k",
                        "-movflags", "+faststart", str(out)], check=True, timeout=120)
    finally:
        wav.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "theme.m4a")))
