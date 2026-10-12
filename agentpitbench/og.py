"""Link-preview images (Open Graph / X cards), made at site build.

Every preview is 1200x630. Share cards are 1200x675, so they are scaled to 1120x630 and centred with side bars
(letterboxed, never cropped: cropping would cut the credibility strip). The standings card, the agent cards and
the branded page cards are rendered here from the leaderboard and the emblem.

site.build runs after every bet, so an image is rendered only when its input changes: the input hash (the
rendered card HTML, or the source card's bytes) is kept in a manifest, and the same hash is the ?v= cache key
that makes X, LinkedIn and Facebook refetch a changed preview. Everything here is best effort: on any failure
a page falls back to the committed static/og-default.jpg, and the site build goes on. That includes a missing
font: a branded card whose title has a character no installed font covers is not rendered (it would show empty
boxes), so its page keeps the default image.

Previews are JPEG (quality 82, about 30-80 KB; the truecolour PNGs were about 250-400 KB): X, Facebook, LinkedIn,
WhatsApp and Telegram all take JPEG og:images, and JPEG keeps the soft background glows smooth where a palette
PNG of the same size bands. The 1200x675 cards themselves stay PNG (palette, cards.shrink_png): they are
posted and downloaded as images.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import shutil
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from . import cards
from .config import AGENT_COLORS, AGENT_MODELS, AGENT_NAMES, Settings
from .emblem import emblem_svg
from .mascots import mascot

log = logging.getLogger(__name__)

W, H = 1200, 630
ENABLED = True          # tests switch the browser renders off (see tests/conftest.py); letterboxing still runs
STATIC = Path(__file__).resolve().parent / "static"
DEFAULT = "og-default.jpg"
OG_EXT = "-og.jpg"
JPEG_Q = 82
OG_MAX = 80_000      # bytes; a busier preview is encoded a notch lower
BG = (11, 16, 32)       # #0b1020, the cards' background: the letterbox bars blend into the card

env = Environment(loader=PackageLoader("agentpitbench", "templates/cards"), autoescape=select_autoescape())
env.filters.update(signed=cards.signed, edge=cards.edge, box_ring=cards.box_ring)
env.globals.update(mascot=mascot, emblem=emblem_svg)


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()[:12]


def page_html(title: str, lang: str = "en", rtl: bool = False, sub: str | None = None, host: str = "agentpitbench.org") -> str:
    """The branded card: emblem, page title (translated), the site address. Rendered straight at 1200x630."""
    return env.get_template("page.html").render(title=title, lang=lang, rtl=rtl, sub=sub, host=host)


def _needs_font(ch: str) -> bool:
    """Characters worth checking: not ASCII, not a space, mark-free punctuation of the General Punctuation block
    (the browser takes those from any font)."""
    o = ord(ch)
    return o > 0x7F and not ch.isspace() and not 0x2000 <= o <= 0x206F and unicodedata.category(ch) not in ("Cf", "Cc")


_covered: dict[int, bool] = {}


def _fc_has(cps: list[int]) -> bool:
    """True when one installed font has all these code points (fontconfig, the fonts Chromium uses on Linux)."""
    r = subprocess.run(["fc-list", ":charset=" + " ".join(f"{c:x}" for c in cps), "family"],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0 and bool(r.stdout.strip())


def missing_glyphs(texts: list[str]) -> set[str]:
    """The characters of texts that no installed font can draw. Empty when fontconfig is not there to ask:
    then nothing is known to be missing, and rendering goes ahead as before."""
    want = sorted({ord(c) for t in texts for c in t if _needs_font(c)} - _covered.keys())
    if want and shutil.which("fc-list"):
        blocks: dict[int, list[int]] = {}
        for c in want:
            blocks.setdefault(c >> 8, []).append(c)
        for cps in blocks.values():
            try:
                if _fc_has(cps):       # one query per 256-code-point block; per character only when that fails
                    _covered.update(dict.fromkeys(cps, True))
                else:
                    _covered.update({c: _fc_has([c]) for c in cps})
            except (OSError, subprocess.SubprocessError):
                log.exception("font check failed")
                return set()
    return {chr(c) for t in texts for c in map(ord, t) if not _covered.get(c, True)}


@dataclass
class Job:
    out: Path
    hash: str
    html: str | None = None     # render this HTML at (width, height)
    width: int = W
    height: int = H
    src: Path | None = None     # letterbox this card
    card: Path | None = None    # the HTML is a 1200x675 card: screenshot it here, then letterbox it to out
    text: str | None = None     # text in the card that must be drawable (branded page titles)


class Previews:
    """Collects the previews a build needs, renders the changed ones in one browser session, hands out URLs."""

    def __init__(self, s: Settings):
        self.s = s
        self.base = s.site_url.rstrip("/")
        self.manifest_path = s.data_dir / "og-manifest.json"  # not under cards/: that folder is published
        try:
            self.manifest: dict[str, str] = json.loads(self.manifest_path.read_text())
        except (OSError, ValueError):
            self.manifest = {}
        self.jobs: list[Job] = []
        self.urls: dict[str, str] = {}     # site-relative path -> absolute URL with ?v=
        self.default_url = f"{self.base}/{DEFAULT}?v={digest((STATIC / DEFAULT).read_bytes())}"

    def _rel(self, p: Path) -> str:
        return "cards/" + p.relative_to(self.s.cards_dir).as_posix()

    def _want(self, out: Path, h: str, **job) -> str:
        """Queue out unless it exists with this input hash; returns its site-relative path."""
        rel = self._rel(out)
        self.urls[rel] = f"{self.base}/{rel}?v={h}"
        old = out.with_suffix(".png")  # the preview's PNG from before previews were JPEG
        if old.exists():
            old.unlink(missing_ok=True)
            self.manifest.pop(self._rel(old), None)
        if self.manifest.get(rel) != h or not out.exists():
            self.jobs.append(Job(out=out, hash=h, **job))
        return rel

    # --- requests ---

    def letterbox(self, card_rel: str) -> str:
        """Preview of an existing card (cards/<round>/results.png -> cards/<round>/results-og.jpg)."""
        src = self.s.cards_dir / card_rel.removeprefix("cards/")
        out = src.with_name(src.stem + OG_EXT)
        return self._want(out, digest(src.read_bytes()), src=src)

    def card(self, name: str, html: str) -> tuple[str, str]:
        """A 1200x675 card rendered from html plus its letterboxed preview: (card path, preview path)."""
        out = self.s.cards_dir / f"{name}.png"
        og = out.with_name(out.stem + OG_EXT)
        h = digest(html)
        rel = self._rel(out)
        self.urls[rel] = f"{self.base}/{rel}?v={h}"
        return rel, self._want(og, h, html=html, width=cards.W, height=cards.H, card=out)

    def page(self, name: str, title: str, lang: str, rtl: bool) -> str:
        html = page_html(title, lang, rtl, host=self._host())
        return self._want(self.s.cards_dir / "pages" / f"{name}-{lang}{OG_EXT}", digest(html), html=html, text=title)

    def _host(self) -> str:
        return self.s.site_url.replace("https://", "").replace("http://", "").rstrip("/")

    def url(self, rel: str | None) -> str:
        """Absolute preview URL with its cache key; the default image if that preview could not be made."""
        if rel and rel in self.urls and (self.s.cards_dir / rel.removeprefix("cards/")).exists():
            return self.urls[rel]
        return self.default_url

    # --- rendering ---

    def run(self) -> None:
        """Render every queued preview. Never raises: a failed preview leaves its page on the default image."""
        letterbox_only = [j for j in self.jobs if j.src]
        browser = [j for j in self.jobs if j.html] if ENABLED else []
        if browser:
            browser = [j for j in browser if self._drawable(j)]
        for j in letterbox_only:
            try:
                letterbox_file(j.src, j.out)
                self.manifest[self._rel(j.out)] = j.hash
            except Exception:
                log.exception("preview %s failed", j.out)
        if browser:
            try:
                _in_thread(self._render(browser))
            except Exception:
                log.exception("preview renders failed; pages keep the default image")
        if letterbox_only or browser:
            try:
                self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
                self.manifest_path.write_text(json.dumps(self.manifest, indent=0, sort_keys=True))
            except OSError:
                log.exception("preview manifest not saved")

    def _drawable(self, j: Job) -> bool:
        """False when the card's text has characters no installed font has: its old image is removed and the
        page uses the default preview rather than one with empty boxes."""
        try:
            missing = missing_glyphs([j.text]) if j.text else set()
        except Exception:
            log.exception("font check of %s failed", j.out)
            missing = set()
        if not missing:
            return True
        log.warning("preview %s skipped: no installed font has %s", j.out, "".join(sorted(missing)))
        self.manifest.pop(self._rel(j.out), None)
        j.out.unlink(missing_ok=True)
        return False

    async def _render(self, jobs: list[Job]) -> None:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                pages = {}
                for j in jobs:
                    size = (j.width, j.height)
                    if size not in pages:
                        pages[size] = await b.new_page(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1)
                    page = pages[size]
                    try:
                        target = j.card or j.out
                        target.parent.mkdir(parents=True, exist_ok=True)
                        await page.set_content(j.html, wait_until="load")
                        await page.evaluate("document.fonts && document.fonts.ready")
                        clip = {"x": 0, "y": 0, "width": j.width, "height": j.height}
                        if j.card:
                            await page.screenshot(path=str(target), clip=clip)
                            cards.shrink_png(j.card)
                            letterbox_file(j.card, j.out)
                        else:
                            j.out.write_bytes(await _jpeg_shot(page, clip))
                        self.manifest[self._rel(j.out)] = j.hash
                    except Exception:
                        log.exception("preview %s failed", j.out)
            finally:
                await b.close()


def _in_thread(coro):
    """Run a coroutine to completion from sync code, whether or not the caller has an event loop running."""
    with ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


def letterbox_bytes(png: bytes) -> bytes:
    """A 1200x675 card scaled to 1120x630 and centred on a 1200x630 canvas (any size fits the same way)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    scale = min(W / im.width, H / im.height)
    size = (round(im.width * scale), round(im.height * scale))
    canvas = Image.new("RGB", (W, H), BG)
    canvas.paste(im.resize(size, Image.LANCZOS), ((W - size[0]) // 2, (H - size[1]) // 2))
    return jpeg_bytes(canvas)


def jpeg_bytes(im) -> bytes:
    """The preview as JPEG, at JPEG_Q unless that is over OG_MAX bytes (a busy card): then a notch lower."""
    im = im.convert("RGB")
    for q in (JPEG_Q, JPEG_Q - 6, JPEG_Q - 12):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
        if buf.tell() <= OG_MAX:
            break
    return buf.getvalue()


async def _jpeg_shot(page, clip: dict) -> bytes:
    """A screenshot as a preview JPEG: Pillow's encoder when there is Pillow, else Chromium's."""
    try:
        from PIL import Image
    except ImportError:
        return await page.screenshot(clip=clip, type="jpeg", quality=JPEG_Q)
    return jpeg_bytes(Image.open(io.BytesIO(await page.screenshot(clip=clip))))


def letterbox_file(src: Path, out: Path) -> None:
    try:
        data = letterbox_bytes(src.read_bytes())
    except ImportError:  # no Pillow (the optional "media" extra): let Chromium scale it instead
        data = _in_thread(_letterbox_browser(src.read_bytes()))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)


async def _letterbox_browser(png: bytes) -> bytes:
    from playwright.async_api import async_playwright
    src = "data:image/png;base64," + base64.b64encode(png).decode()
    html = (f'<body style="margin:0;background:#0b1020;width:{W}px;height:{H}px;display:flex;align-items:center;'
            f'justify-content:center"><img src="{src}" style="max-width:{W}px;max-height:{H}px"></body>')
    async with async_playwright() as pw:
        b = await pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = await b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            await page.set_content(html, wait_until="load")
            return await page.screenshot(clip={"x": 0, "y": 0, "width": W, "height": H}, type="jpeg", quality=JPEG_Q)
        finally:
            await b.close()


# ---------- card contexts ----------

def _plain_ctx(s: Settings, url_path: str, sig: str | None, agent: str | None = None) -> dict:
    host = s.site_url.replace("https://", "").replace("http://", "").rstrip("/")
    return {"r": None, "board": [], "colors": AGENT_COLORS, "names": AGENT_NAMES, "models": AGENT_MODELS,
            "site": host, "state": None, "banner": None, "url": f"{host}/{url_path}".rstrip("/"),
            "model_line": cards.model_line(agent) if agent else None, "sig": cards.short_sig(sig),
            "mver": s.methodology_version}


def standings_html(s: Settings, lb: dict) -> str | None:
    """Rank, mascot, name, forecast accuracy with its CI, betting record; the Crowd row; n and significance."""
    fc = lb.get("forecast") or {}
    board = {b["agent"]: b for b in lb.get("agents") or []}
    acc = {a["agent"]: a for a in fc.get("agents") or []}
    if not board and not acc:
        return None
    agents = [a for a in s.agents if a in board or a in acc]
    # ranked by forecast accuracy (the headline metric); agents without a scored forecast after, by profit
    agents.sort(key=lambda a: (acc.get(a, {}).get("brier") is None, acc.get(a, {}).get("brier") or 0,
                               -(board.get(a, {}).get("net_pnl") or 0)))
    rows = [{"agent": a, "name": AGENT_NAMES.get(a, a), "color": AGENT_COLORS.get(a, "#888"),
             "rank": i + 1, "fc": acc.get(a), "bet": board.get(a)} for i, a in enumerate(agents)]
    sig = lb.get("significance") or {}
    ctx = _plain_ctx(s, "leaderboard/", sig.get("multi_label") or sig.get("label"))
    return env.get_template("standings.html").render(
        **ctx, rows=rows, crowd_fc=fc.get("crowd"), crowd_bet=lb.get("crowd"), season=lb.get("season") or "",
        markets=fc.get("markets_resolved") or 0)


def agent_html(s: Settings, lb: dict, agent: str, eras: dict | None = None) -> str | None:
    """Mascot, name, model and reasoning setting, forecast accuracy with its CI, W-L, profit, current streak."""
    fc = next((a for a in (lb.get("forecast") or {}).get("agents") or [] if a["agent"] == agent), None)
    row = next((b for b in lb.get("agents") or [] if b["agent"] == agent), None)
    sig = lb.get("significance") or {}
    ctx = _plain_ctx(s, f"agent/{agent}/", (sig.get("agent_labels") or {}).get(agent) or sig.get("label"), agent)
    ctx["model_line"] = None  # the model is the card's own subtitle; the strip keeps the n and significance
    model = AGENT_MODELS.get(agent)
    effort = {"max": "max reasoning", "xhigh": "extra-high reasoning", "high": "high reasoning"}
    return env.get_template("agent.html").render(
        **ctx, agent=agent, name=AGENT_NAMES.get(agent, agent), color=AGENT_COLORS.get(agent, "#888"),
        model=(f"{model[1]} · {effort.get(model[2], model[2])}" if model else "")
        + (f" (earlier rounds on {', '.join(eras['earlier'])})" if eras and eras.get("earlier") else ""), fc=fc, row=row,
        n_agents=len((lb.get("forecast") or {}).get("agents") or []))


def render_default(out: Path = STATIC / DEFAULT) -> Path:
    """Regenerate the committed fallback preview (run by hand after a brand change)."""
    html = page_html("Four frontier AIs bet on real prediction markets", sub="Claude · ChatGPT · Gemini · Grok")

    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = await b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
                await page.set_content(html, wait_until="load")
                await page.evaluate("document.fonts && document.fonts.ready")
                out.write_bytes(await _jpeg_shot(page, {"x": 0, "y": 0, "width": W, "height": H}))
            finally:
                await b.close()
    _in_thread(go())
    return out
