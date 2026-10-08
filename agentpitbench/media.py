"""Video for X: leaderboard-race frames and encoding (MP4 via ffmpeg, else animated GIF via Pillow)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .config import AGENT_COLORS, AGENT_NAMES

log = logging.getLogger(__name__)

ROW = 72


def ffmpeg_exe() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # optional dependency: a static ffmpeg in the venv
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def encode(frames: list[Path], out_base: Path, fps: int = 12, allow_gif: bool = True) -> Path | None:
    """MP4 (H.264, yuv420p, even size) when ffmpeg exists; else a 600px GIF when Pillow exists; else None."""
    if not frames:
        return None
    exe = ffmpeg_exe()
    if exe:
        out = out_base.with_suffix(".mp4")
        cmd = [exe, "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(frames[0].parent / "f%05d.png"),
               "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "23", "-movflags", "+faststart", str(out)]
        try:
            subprocess.run(cmd, check=True, timeout=600)
            return out
        except Exception:
            log.exception("ffmpeg encode failed")
    if not allow_gif:
        return None
    try:
        from PIL import Image
    except ImportError:
        log.warning("no ffmpeg and no Pillow: cannot encode %s", out_base.name)
        return None
    step = max(1, len(frames) // 150)  # keep the GIF under X's 15 MB limit
    imgs = [Image.open(f).convert("RGB").resize((600, 338)) for f in frames[::step]]
    out = out_base.with_suffix(".gif")
    imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=int(1000 * step / fps), loop=0, optimize=True)
    return out


def race_contexts(rounds: list[dict], agents: list[str], week: str, sub: int = 6, hold: int = 24) -> list[dict]:
    """Frame contexts for the race template: cumulative P&L per agent (and the Crowd) across the week's rounds,
    values and bar positions interpolated between rounds. rounds: resolved, oldest first."""
    keys = list(agents) + ["crowd"]
    steps = [{k: 0.0 for k in keys}]
    for r in rounds:
        nxt = dict(steps[-1])
        for e in r["entries"]:
            if e["agent"] in nxt:
                nxt[e["agent"]] += e.get("pnl") or 0
        nxt["crowd"] += (r.get("crowd") or {}).get("pnl") or 0
        steps.append(nxt)
    vals = [v for st in steps for v in st.values()]
    lo, hi = min(0.0, *vals), max(0.0, *vals)
    span = (hi - lo) or 1.0
    zero = 8 + (-lo) / span * 78

    def ranks(st):
        order = sorted(keys, key=lambda k: -st[k])
        return {k: i for i, k in enumerate(order)}

    ctxs = []
    total = len(rounds)
    for i in range(1, len(steps)):
        a, b = steps[i - 1], steps[i]
        ra, rb = ranks(a), ranks(b)
        for j in range(1, sub + 1):
            t = j / sub
            ctxs.append(_frame(keys, {k: a[k] + (b[k] - a[k]) * t for k in keys},
                               {k: (ra[k] + (rb[k] - ra[k]) * t) * ROW for k in keys}, zero, span, week, i, total))
    last = steps[-1]
    rl = ranks(last)
    final = _frame(keys, last, {k: rl[k] * ROW for k in keys}, zero, span, week, total, total)
    return (ctxs or [final]) + [final] * hold


def _frame(keys, values, ys, zero, span, week, step, total) -> dict:
    rows = []
    for k in keys:
        v = values[k]
        w = abs(v) / span * 78
        left = zero if v >= 0 else zero - w
        label = left + w + 1 if v >= 0 else max(0.0, left - 11)
        rows.append({"key": k, "name": "The Crowd" if k == "crowd" else AGENT_NAMES.get(k, k),
                     "color": AGENT_COLORS.get(k, "#8a93a8"), "value": round(v, 1), "y": round(ys[k], 1),
                     "left": round(left, 2), "width": round(w, 2), "label_left": round(label, 2)})
    return {"rows": rows, "zero_pct": round(zero, 2), "week": week, "step": step, "total": total}


def replay_contexts(rnd: dict, entry: dict, lines: list[str], seconds: int = 24, fps: int = 10) -> list[dict]:
    """Frames scrolling the reasoning upward so the bet line ends in view."""
    est = sum(35 * (len(ln) // 75 + 1) + 10 for ln in lines)
    start, end = 380, min(0, 300 - est)  # end with the bet line clear of the bottom fade
    n = seconds * fps
    hold = 2 * fps
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        out.append({"r": rnd, "e": entry, "lines": lines, "offset": round(start + (end - start) * t)})
    return out + [out[-1]] * hold
