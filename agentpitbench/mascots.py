"""Original mascots, one per contestant plus the Crowd. Plain inline SVG in the team colour; no vendor logos
or trademarks. Claude is the Scholar (round, spectacles, book), Codex the Robot (boxy, antenna, visor),
Gemini the Twins (two linked blobs), Grok the Visitor (tall head, big eyes), the Crowd three dashed figures."""
from __future__ import annotations

from markupsafe import Markup

from .config import AGENT_COLORS

INK = "#0b1020"
LIGHT = "#e8ecf7"


def _dark(color: str) -> bool:
    c = color.lstrip("#")
    if len(c) != 6:
        return False
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.18


def _svg(body: str, size: int, label: str) -> Markup:
    return Markup(f'<svg class="mascot" width="{size}" height="{size}" viewBox="0 0 100 100" role="img" '
                  f'aria-label="{label}" xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def _eyes(y: float, dx: float = 12, r: float = 5, pupil: str = INK, white: str = "#fff") -> str:
    return (f'<circle cx="{50 - dx}" cy="{y}" r="{r + 2}" fill="{white}"/><circle cx="{50 + dx}" cy="{y}" r="{r + 2}" fill="{white}"/>'
            f'<circle cx="{50 - dx + 1}" cy="{y + 1}" r="{r - 1}" fill="{pupil}"/><circle cx="{50 + dx + 1}" cy="{y + 1}" r="{r - 1}" fill="{pupil}"/>')


def scholar(c: str, edge: str) -> str:
    return (f'<ellipse cx="50" cy="92" rx="26" ry="5" fill="#000" opacity=".25"/>'
            f'<circle cx="50" cy="52" r="36" fill="{c}" stroke="{edge}" stroke-width="3"/>'
            f'<circle cx="38" cy="46" r="10" fill="#fff" stroke="{INK}" stroke-width="3"/>'
            f'<circle cx="62" cy="46" r="10" fill="#fff" stroke="{INK}" stroke-width="3"/>'
            f'<line x1="48" y1="46" x2="52" y2="46" stroke="{INK}" stroke-width="3"/>'
            f'<circle cx="39" cy="47" r="4" fill="{INK}"/><circle cx="63" cy="47" r="4" fill="{INK}"/>'
            f'<path d="M40 64 Q50 71 60 64" stroke="{INK}" stroke-width="3" fill="none" stroke-linecap="round"/>'
            f'<rect x="56" y="70" width="26" height="18" rx="2" fill="#fff" stroke="{INK}" stroke-width="2.5" transform="rotate(-12 69 79)"/>'
            f'<line x1="69" y1="70" x2="69" y2="88" stroke="{INK}" stroke-width="2" transform="rotate(-12 69 79)"/>')


def robot(c: str, edge: str) -> str:
    return (f'<ellipse cx="50" cy="93" rx="28" ry="5" fill="#000" opacity=".25"/>'
            f'<line x1="50" y1="8" x2="50" y2="22" stroke="{edge}" stroke-width="4"/><circle cx="50" cy="8" r="5" fill="#ffd34d"/>'
            f'<rect x="18" y="22" width="64" height="56" rx="12" fill="{c}" stroke="{edge}" stroke-width="3"/>'
            f'<rect x="26" y="34" width="48" height="20" rx="10" fill="{INK}"/>'
            f'<rect x="33" y="40" width="10" height="8" rx="3" fill="#7CFFCB"/><rect x="57" y="40" width="10" height="8" rx="3" fill="#7CFFCB"/>'
            f'<rect x="36" y="62" width="28" height="6" rx="3" fill="{INK}" opacity=".55"/>'
            f'<rect x="10" y="40" width="8" height="18" rx="3" fill="{edge}"/><rect x="82" y="40" width="8" height="18" rx="3" fill="{edge}"/>'
            f'<rect x="30" y="78" width="12" height="10" rx="3" fill="{c}" stroke="{edge}" stroke-width="2"/>'
            f'<rect x="58" y="78" width="12" height="10" rx="3" fill="{c}" stroke="{edge}" stroke-width="2"/>')


def twins(c: str, edge: str) -> str:
    return (f'<ellipse cx="50" cy="93" rx="34" ry="5" fill="#000" opacity=".25"/>'
            f'<path d="M30 30 Q50 18 70 30" stroke="{edge}" stroke-width="4" fill="none"/>'
            f'<circle cx="32" cy="58" r="24" fill="{c}" stroke="{edge}" stroke-width="3"/>'
            f'<circle cx="68" cy="52" r="24" fill="{c}" stroke="{edge}" stroke-width="3" opacity=".92"/>'
            f'<circle cx="25" cy="55" r="5" fill="#fff"/><circle cx="39" cy="55" r="5" fill="#fff"/>'
            f'<circle cx="26" cy="56" r="2.6" fill="{INK}"/><circle cx="40" cy="56" r="2.6" fill="{INK}"/>'
            f'<circle cx="61" cy="49" r="5" fill="#fff"/><circle cx="75" cy="49" r="5" fill="#fff"/>'
            f'<circle cx="62" cy="50" r="2.6" fill="{INK}"/><circle cx="76" cy="50" r="2.6" fill="{INK}"/>'
            f'<path d="M26 67 Q32 72 38 67" stroke="{INK}" stroke-width="2.5" fill="none" stroke-linecap="round"/>'
            f'<path d="M62 61 Q68 66 74 61" stroke="{INK}" stroke-width="2.5" fill="none" stroke-linecap="round"/>'
            f'<path d="M30 14 l2 5 5 1 -4 3 1 5 -4-3 -4 3 1-5 -4-3 5-1z" fill="#ffd34d"/>'
            f'<path d="M70 12 l2 5 5 1 -4 3 1 5 -4-3 -4 3 1-5 -4-3 5-1z" fill="#ffd34d"/>')


def visitor(c: str, edge: str) -> str:
    return (f'<ellipse cx="50" cy="94" rx="24" ry="5" fill="#000" opacity=".25"/>'
            f'<path d="M50 6 C78 6 86 34 80 54 C74 76 62 88 50 88 C38 88 26 76 20 54 C14 34 22 6 50 6Z" '
            f'fill="{c}" stroke="{edge}" stroke-width="3"/>'
            f'<ellipse cx="36" cy="46" rx="11" ry="15" fill="#fff" transform="rotate(-18 36 46)"/>'
            f'<ellipse cx="64" cy="46" rx="11" ry="15" fill="#fff" transform="rotate(18 64 46)"/>'
            f'<ellipse cx="38" cy="49" rx="6" ry="9" fill="{INK}" transform="rotate(-18 38 49)"/>'
            f'<ellipse cx="62" cy="49" rx="6" ry="9" fill="{INK}" transform="rotate(18 62 49)"/>'
            f'<circle cx="35" cy="45" r="2.4" fill="#fff"/><circle cx="59" cy="45" r="2.4" fill="#fff"/>'
            f'<path d="M43 72 Q50 76 57 72" stroke="{edge}" stroke-width="3" fill="none" stroke-linecap="round"/>'
            f'<circle cx="50" cy="20" r="3" fill="{edge}"/>')


def crowd(c: str = "#8a93a8", edge: str = "#8a93a8") -> str:
    def fig(x, y, s):
        return (f'<g transform="translate({x} {y}) scale({s})" fill="none" stroke="{edge}" stroke-width="3" '
                f'stroke-dasharray="5 4"><circle cx="0" cy="-22" r="10"/><path d="M-16 14 C-16 -6 16 -6 16 14 Z"/></g>')
    return fig(24, 66, 0.95) + fig(76, 66, 0.95) + fig(50, 60, 1.2)


DESIGNS = {"claude": scholar, "codex": robot, "agy": twins, "grok": visitor}


def mascot(agent: str, size: int = 96, color: str | None = None) -> Markup:
    """Inline SVG for an agent ('crowd' for the Crowd). Near-black colours get a light outline."""
    if agent == "crowd":
        return _svg(crowd(), size, "The Crowd")
    c = color or AGENT_COLORS.get(agent, "#888")
    edge = LIGHT if _dark(c) else INK
    design = DESIGNS.get(agent, scholar)
    return _svg(design(c, edge), size, agent)


def sprite(agents: list[str]) -> Markup:
    """One hidden <svg> of <symbol>s, so a page can show many small mascots for the cost of one each."""
    parts = []
    for a in [*agents, "crowd"]:
        if a == "crowd":
            body = crowd()
        else:
            c = AGENT_COLORS.get(a, "#888")
            body = DESIGNS.get(a, scholar)(c, LIGHT if _dark(c) else INK)
        parts.append(f'<symbol id="m-{a}" viewBox="0 0 100 100">{body}</symbol>')
    return Markup('<svg width="0" height="0" style="position:absolute" aria-hidden="true">' + "".join(parts) + "</svg>")


def use(agent: str, size: int = 22, label: str = "") -> Markup:
    """A small mascot from the page's sprite."""
    lab = f' role="img" aria-label="{label}"' if label else ' aria-hidden="true"'
    return Markup(f'<svg class="mi" width="{size}" height="{size}"{lab}><use href="#m-{agent}"/></svg>')


def svg_file(agent: str, size: int = 512) -> str:
    """Standalone SVG document for the press kit download."""
    return str(mascot(agent, size)).replace('class="mascot" ', "")
