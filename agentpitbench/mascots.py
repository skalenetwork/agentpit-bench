"""Agent identity marks: each contestant is shown by its product's official logo (Claude, ChatGPT, Gemini, Grok),
the Crowd by our own three dashed figures. The logo files in static/logos/ are the brands' own files, byte for
byte (sources, hashes and the brand rules we follow: docs/brand-assets.md). They are never recoloured, cropped,
stretched or redrawn: only scaled uniformly, with clear space around them.

One asset map (LOGOS) drives everything:
  - cards, OG images and films (fixed dark backgrounds): mascot() -> the logo centred on a neutral dark plate,
    the dark-background variant, embedded as a data URI so the headless renderer needs no files;
  - the site (dark by default, with a light switch): sprite() puts each logo file once per page in a hidden
    <symbol> (both theme variants, switched by the --lgd/--lgl properties base.html sets per theme); use() shows it.
The module keeps its old name and API (mascot, sprite, use), so the templates barely changed."""
from __future__ import annotations

import base64
from functools import cache
from pathlib import Path

from markupsafe import Markup, escape

from .config import AGENT_NAMES

LOGO_DIR = Path(__file__).resolve().parent / "static" / "logos"
WEB_DIR = "logos"   # where site.py publishes LOGO_DIR, relative to the site root

# agent -> (file for dark backgrounds, file for light backgrounds); one file when the brand ships one colourway
LOGOS: dict[str, tuple[str, str]] = {
    "claude": ("claude-spark-clay.svg", "claude-spark-clay.svg"),        # Anthropic press kit: Claude Spark, Clay
    "codex": ("openai-blossom-white.svg", "openai-blossom-black.svg"),   # OpenAI brand page: Blossom, white / black
    "agy": ("gemini-sparkle.png", "gemini-sparkle.png"),                 # Google: the Gemini app's own spark icon
    "grok": ("grok-logomark-light.svg", "grok-logomark-dark.svg"),       # xAI brand page: Grok logomark, light / dark
}
DISCLAIMER = "Product names and logos belong to their owners; AgentpitBench is not affiliated with or endorsed by them."

PLATE = "#161d33"        # neutral plate behind a logo on cards and films (never the team colour)
PLATE_EDGE = "#2c3658"
CLEAR = 0.22             # clear space on each side of the logo, as a share of the plate


def _mime(name: str) -> str:
    return "image/svg+xml" if name.endswith(".svg") else "image/png"


@cache
def data_uri(name: str) -> str:
    return f"data:{_mime(name)};base64," + base64.b64encode((LOGO_DIR / name).read_bytes()).decode()


def _svg(body: str, size: int, label: str) -> Markup:
    return Markup(f'<svg class="mascot" width="{size}" height="{size}" viewBox="0 0 100 100" role="img" '
                  f'aria-label="{escape(label)}" xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def crowd(c: str = "#8a93a8", edge: str = "#8a93a8") -> str:
    def fig(x, y, s):
        return (f'<g transform="translate({x} {y}) scale({s})" fill="none" stroke="{edge}" stroke-width="3" '
                f'stroke-dasharray="5 4"><circle cx="0" cy="-22" r="10"/><path d="M-16 14 C-16 -6 16 -6 16 14 Z"/></g>')
    return fig(24, 66, 0.95) + fig(76, 66, 0.95) + fig(50, 60, 1.2)


def plate(agent: str) -> str:
    """SVG body (100x100): the agent's logo, dark-background variant, centred on the neutral plate. The logo goes
    in as an <image> of the untouched file, so it is only scaled (uniformly) and never restyled by page CSS."""
    lo, span = 100 * CLEAR, 100 * (1 - 2 * CLEAR)
    return (f'<circle cx="50" cy="50" r="48" fill="{PLATE}" stroke="{PLATE_EDGE}" stroke-width="2"/>'
            f'<image href="{data_uri(LOGOS[agent][0])}" x="{lo:g}" y="{lo:g}" width="{span:g}" height="{span:g}" '
            f'preserveAspectRatio="xMidYMid meet"/>')


def mascot(agent: str, size: int = 96, color: str | None = None) -> Markup:
    """Inline SVG for cards and films: the agent's logo on a plate ('crowd' for the Crowd). `color` is unused
    (logos are never recoloured) and kept for the callers."""
    if agent == "crowd":
        return _svg(crowd(), size, "The Crowd")
    if agent not in LOGOS:
        return _svg(f'<circle cx="50" cy="50" r="48" fill="{PLATE}" stroke="{PLATE_EDGE}" stroke-width="2"/>', size, agent)
    return _svg(plate(agent), size, AGENT_NAMES.get(agent, agent))


def sprite(agents: list[str], root: str = "") -> Markup:
    """One hidden <svg> of <symbol>s, so a page shows many small marks for the cost of one each: per agent its
    logo file (an <image> per theme variant; the page's theme sets --lgd/--lgl, the display of the dark/light
    variant: custom properties inherit into <use> clones, selectors like ':root[data-theme=dark] .x' do not),
    plus the Crowd. `root` is the page's path to the site root, where /logos/ is published."""
    parts = []
    for a in agents:
        if a not in LOGOS:
            continue
        dark, light = LOGOS[a]
        imgs = ([(dark, "")] if dark == light else
                [(dark, ' style="display:var(--lgd,none)"'), (light, ' style="display:var(--lgl,inline)"')])
        body = "".join(f'<image{c} href="{root}{WEB_DIR}/{f}" width="100" height="100"/>' for f, c in imgs)
        parts.append(f'<symbol id="m-{a}" viewBox="0 0 100 100">{body}</symbol>')
    parts.append(f'<symbol id="m-crowd" viewBox="0 0 100 100">{crowd()}</symbol>')
    return Markup('<svg width="0" height="0" style="position:absolute" aria-hidden="true">' + "".join(parts) + "</svg>")


def use(agent: str, size: int = 22, label: str = "") -> Markup:
    """A small mark next to a name on the site: the agent's logo (or the Crowd) from the page's sprite."""
    lab = f' role="img" aria-label="{escape(label)}"' if label else ' aria-hidden="true"'
    return Markup(f'<svg class="mi" width="{size}" height="{size}"{lab}><use href="#m-{agent}"/></svg>')
