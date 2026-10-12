"""The AgentpitBench emblem: a poker-chip ring in the four team colours around an upward chart tick, on the
brand yellow. One mark for the X avatar, card footers, the site favicon and the badge."""
from __future__ import annotations

import math

from markupsafe import Markup

from .config import AGENT_COLORS

YELLOW = "#E8FF3A"
INK = "#0b1020"


def emblem_svg(size: int = 800, round_: bool = False) -> Markup:
    """Standalone SVG. round_=True clips it to a circle (how X shows the avatar), for small inline use."""
    cx = cy = 400
    r_out, r_in = 330, 205
    cols = [AGENT_COLORS["claude"], AGENT_COLORS["codex"], AGENT_COLORS["grok"], AGENT_COLORS["agy"]]  # TL, TR, BR, BL

    def pt(r: float, a: float) -> str:
        return f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}"

    parts = []
    for i, col in enumerate(cols):
        a0, a1 = math.radians(-180 + 90 * i + 2), math.radians(-90 + 90 * i - 2)
        parts.append(f'<path d="M{pt(r_in, a0)} L{pt(r_out, a0)} A{r_out},{r_out} 0 0 1 {pt(r_out, a1)} '
                     f'L{pt(r_in, a1)} A{r_in},{r_in} 0 0 0 {pt(r_in, a0)} Z" fill="{col}"/>')
    notches = "".join(f'<rect x="{cx - 14}" y="{cy - r_out - 2}" width="28" height="44" rx="6" fill="#fff" '
                      f'transform="rotate({k * 30 + 15} {cx} {cy})"/>' for k in range(12))
    tick = ('<polyline points="300,450 360,390 405,425 500,320" fill="none" stroke="#fff" stroke-width="34" '
            'stroke-linecap="round" stroke-linejoin="round"/><polygon points="512,308 452,318 502,368" fill="#fff"/>')
    clip = '<clipPath id="apb-c"><circle cx="400" cy="400" r="400"/></clipPath>' if round_ else ""
    g_open, g_close = ('<g clip-path="url(#apb-c)">', "</g>") if round_ else ("", "")
    return Markup(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800" width="{size}" height="{size}" '
        f'role="img" aria-label="AgentpitBench">{clip}{g_open}<rect width="800" height="800" fill="{YELLOW}"/>'
        f'{"".join(parts)}{notches}<circle cx="{cx}" cy="{cy}" r="{r_in - 8}" fill="{INK}"/>{tick}{g_close}</svg>')
