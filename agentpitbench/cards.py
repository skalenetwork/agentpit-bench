"""Share cards: Jinja HTML templates screenshotted to 1200x675 PNGs with Playwright."""
from __future__ import annotations

import asyncio
import shutil
import logging
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .config import AGENT_COLORS, AGENT_NAMES, Settings, AGENT_MODELS
from .mascots import mascot

log = logging.getLogger(__name__)

W, H = 1200, 675
UPSET_PRICE = 0.25

env = Environment(loader=PackageLoader("agentpitbench", "templates/cards"), autoescape=select_autoescape())


# Card animation timeline (seconds). The final state is exactly the static card: every element at rest and
# every counted number back to its formatted text.
CLIP_JS = r"""
(function(){
  const q = s => Array.from(document.querySelectorAll(s));
  const ease = x => x <= 0 ? 0 : x >= 1 ? 1 : 1 - Math.pow(1 - x, 3);
  const T = {tag: 0.15, head: 0.55, sub: 1.0, row0: 1.35, gap: 0.42, dur: 0.45, count: 0.9, tail: 0.5};
  const rows = () => q('[data-a=row]');
  const tailAt = () => T.row0 + Math.max(0, rows().length - 1) * T.gap + T.dur + 0.15;
  const fmt = v => (v >= 0 ? '+' : '\u2212') + Math.round(Math.abs(v)).toLocaleString('en-US');
  function set(el, o, dy, sc){ el.style.opacity = o; el.style.transformOrigin = 'left center';
    el.style.transform = (o >= 1 && !dy && sc === 1) ? '' : `translateY(${dy}px) scale(${sc})`; }
  window.__apbEnd = () => Math.max(tailAt() + T.tail, T.row0 + (rows().length - 1) * T.gap + T.count + 0.1);
  window.__apb = function(t){
    q('[data-a=tag]').forEach(el => { const p = ease((t - 0) / T.tag); set(el, p, 0, 1); });
    q('[data-a=head]').forEach(el => { const p = ease((t - 0.15) / 0.4); set(el, p, 0, 1.35 - 0.35 * p); });
    q('[data-a=sub]').forEach(el => { const p = ease((t - 0.6) / 0.4); set(el, p, 8 * (1 - p), 1); });
    rows().forEach((el, i) => {
      const s = T.row0 + i * T.gap, p = ease((t - s) / T.dur);
      set(el, p, -28 * (1 - p), 1);
      el.querySelectorAll('[data-pnl]').forEach(c => {
        const k = ease((t - s) / T.count);
        c.textContent = k >= 1 ? c.dataset.final : fmt(parseFloat(c.dataset.pnl) * k);
      });
    });
    q('[data-a=tail]').forEach(el => { const p = ease((t - tailAt()) / T.tail); set(el, p, 10 * (1 - p), 1); });
  };
})();
"""


def cents(p: float | None) -> str:
    return "—" if p is None else f"{round(p * 100):d}¢"


def payout_x(p: float | None) -> str:
    return "—" if not p else f"{1 / p:.1f}×"


def fmt_time(s: float | None) -> str:
    if s is None:
        return "—"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def signed(v: float | None) -> str:
    if v is None:
        return "—"
    return f"+{v:,.0f}" if v >= 0 else f"−{abs(v):,.0f}"


def is_dark(color: str | None) -> bool:
    """True for near-black team colours (Grok) that vanish on a dark background."""
    c = (color or "").lstrip("#")
    if len(c) != 6:
        return False
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.18


def box_ring(color: str | None) -> str:
    """Inline CSS giving a dark swatch / bar a light outline on the dark card."""
    return "box-shadow:0 0 0 2px #e8ecf7" if is_dark(color) else ""


def edge(color: str | None) -> str:
    """Colour for text, borders and stripes on the dark card: a dark team colour becomes light so it shows."""
    return "#e8ecf7" if is_dark(color) else (color or "#888")


env.filters.update(box_ring=box_ring, edge=edge)
env.filters.update(cents=cents, payout_x=payout_x, fmt_time=fmt_time, signed=signed)
env.globals["mascot"] = mascot


def ranked(entries: list[dict]) -> list[dict]:
    """Higher P&L first; a tie goes to the faster decision; no-bets last."""
    return sorted(entries, key=lambda e: (-(e["pnl"] if e["pnl"] is not None else -1e9),
                                          e["decided_s"] if e["decided_s"] is not None else 1e9))


def is_upset(rnd: dict) -> bool:
    return any(e["won"] and e["avg_price"] is not None and e["avg_price"] <= UPSET_PRICE for e in rnd["entries"])


def _en(msg: str, **kw) -> str:
    return msg.format(**kw)


def results_headline(rnd: dict, _=_en) -> str:
    """`_` translates a message id with {placeholders}; the site passes one per language, cards stay English."""
    if rnd["state"] == "void":
        return _("Market voided")
    bettors = [e for e in rnd["entries"] if e["outcome"]]
    winners = [e for e in rnd["entries"] if e["won"]]
    if not winners:
        return _("Everyone got rekt")
    if len(winners) == len(rnd["entries"]) and len(winners) > 1:
        return _("Clean sweep")
    crowd_lost = (rnd.get("crowd") or {}).get("won") is False
    if len(winners) == 1:
        w = winners[0]
        if w["avg_price"] is not None and w["avg_price"] <= UPSET_PRICE:
            return _("Upset! {name} cashes at {price}", name=w["name"], price=cents(w["avg_price"]))
        if crowd_lost:
            return _("{name} beats the crowd", name=w["name"])
        if len(bettors) > 1:
            return _("{name} takes it alone", name=w["name"])
        return _("{name} takes it", name=w["name"])
    names = " & ".join(e["name"] for e in winners)
    return _("{names} beat the crowd", names=names) if crowd_lost else _("{names} take it", names=names)


def split_headline(rnd: dict, _=_en) -> tuple[str, str]:
    """(kind, headline): kind is 'split' or 'agree'. Agents with no bet are left out of the count."""
    picks: dict[str, list[str]] = {}
    for e in rnd["entries"]:
        if e["outcome"]:
            picks.setdefault(e["outcome"], []).append(e["name"])
    if len(picks) <= 1:
        return "agree", _("They all agree")
    groups = sorted(picks.values(), key=len, reverse=True)
    sizes = [len(g) for g in groups]
    score = "–".join(map(str, sizes))
    if len(groups) == 2 and sizes[1] == 1 and sizes[0] > 1:
        return "split", _("{score}: {name} goes alone", score=score, name=groups[1][0])
    if len(set(sizes)) == 1:
        return "split", (_("{score}: dead split", score=score) if len(groups) == 2
                         else _("{score}: nobody agrees", score=score))
    return "split", _("{score}: {n}-way split", score=score, n=len(groups))


def entry_status(e: dict) -> str:
    if e["outcome"]:
        return "bet"
    return "timeout" if e.get("exit_reason") == "timeout" else "nobet"


class CardRenderer:
    """One shared Chromium, reused across renders."""

    def __init__(self, s: Settings):
        self.s = s
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(args=["--no-sandbox"])
        return self._browser

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def render_html(self, html: str, out: Path, width: int = W, height: int = H) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            browser = await self._ensure()
            page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            try:
                await page.set_content(html, wait_until="load")
                await page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": width, "height": height})
            finally:
                await page.close()
        return out

    def _dir(self, rnd: dict) -> Path:
        return self.s.cards_dir / str(rnd["round_id"])

    def _ctx(self, rnd: dict, board: list[dict] | None) -> dict:
        return {"r": rnd, "board": board or [], "colors": AGENT_COLORS, "names": AGENT_NAMES, "models": AGENT_MODELS,
                "site": self.s.site_url.replace("https://", "")}

    async def decision(self, rnd: dict, entry: dict, board: list[dict] | None = None) -> Path:
        row = next((b for b in board or [] if b["agent"] == entry["agent"]), {})
        streak = row.get("streak", 0)
        form = "fire" if streak >= 3 else "tilt" if streak <= -3 else None
        html = env.get_template("decision.html").render(
            **self._ctx(rnd, board), e=entry, status=entry_status(entry), form=form, streak=abs(streak))
        return await self.render_html(html, self._dir(rnd) / f"decision-{entry['agent']}.png")

    async def split(self, rnd: dict, board: list[dict] | None = None, headline: str | None = None,
                    grudge: dict | None = None) -> Path:
        kind, default = split_headline(rnd)
        html = env.get_template("split.html").render(**self._ctx(rnd, board), kind=kind,
                                                     headline=headline or default, grudge=grudge)
        return await self.render_html(html, self._dir(rnd) / "split.png")

    async def results(self, rnd: dict, board: list[dict] | None = None, crowd: dict | None = None,
                      cast: str | None = None, top_humans: list[dict] | None = None) -> Path:
        html = env.get_template("results.html").render(
            **self._ctx(rnd, board), ranked=ranked(rnd["entries"]), headline=results_headline(rnd),
            upset=is_upset(rnd), crowd=crowd, cast=cast, top_humans=top_humans or [])
        return await self.render_html(html, self._dir(rnd) / "results.png")

    def _plain(self, **kw) -> dict:
        return {"r": None, "board": [], "colors": AGENT_COLORS, "names": AGENT_NAMES,
                "site": self.s.site_url.replace("https://", ""), **kw}

    async def trophy(self, season: str, board: list[dict], crowd: dict | None) -> Path:
        ctx = self._plain(season=season, champ=board[0], crowd=crowd)
        ctx["board"] = board
        html = env.get_template("trophy.html").render(**ctx)
        return await self.render_html(html, self.s.cards_dir / "seasons" / f"{season}.png")

    async def milestone(self, key: str, headline: str, subline: str, agent: str | None, season: str) -> Path:
        html = env.get_template("milestone.html").render(**self._plain(headline=headline, subline=subline,
                                                                        agent=agent, season=season))
        return await self.render_html(html, self.s.cards_dir / "milestones" / f"{key}.png")

    async def human_winner(self, rnd: dict, human: dict, beaten: list[str]) -> Path:
        html = env.get_template("human.html").render(**self._ctx(rnd, None), h=human, beaten=beaten)
        safe = "".join(ch for ch in (human.get("username") or human["user_id"]) if ch.isalnum() or ch in "_-")
        return await self.render_html(html, self.s.cards_dir / "humans" / str(rnd["round_id"]) / f"{safe}.png")

    async def awards(self, week: str, awards: list[dict]) -> Path:
        html = env.get_template("awards.html").render(**self._plain(week=week, awards=awards))
        return await self.render_html(html, self.s.cards_dir / "awards" / f"{week}.png")

    async def banner(self, board: list[dict], crowd: dict | None, season: str) -> Path:
        ctx = self._plain(crowd=crowd, season=season)
        ctx["board"] = board
        html = env.get_template("banner.html").render(**ctx)
        return await self.render_html(html, self.s.cards_dir / "banner.png", 1500, 500)

    async def clip(self, html: str, out_base: Path, fps: int = 12, seconds: float = 6.0) -> Path | None:
        """Animate a finished card (split/results) into a short MP4 whose last frame equals the static card.
        Elements marked data-a=tag|head|sub|row|tail enter in that order; data-pnl numbers count up.
        Returns None (callers fall back to the PNG) if anything fails or no ffmpeg is available."""
        from . import media
        if not media.ffmpeg_exe():
            return None
        frames_dir = out_base.parent / f"{out_base.name}-frames"
        shutil.rmtree(frames_dir, ignore_errors=True)
        frames_dir.mkdir(parents=True, exist_ok=True)
        total = int(round(seconds * fps))
        async with self._lock:
            browser = await self._ensure()
            page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            try:
                await page.set_content(html, wait_until="load")
                await page.evaluate("document.fonts && document.fonts.ready")
                await page.add_script_tag(content=CLIP_JS)
                end = await page.evaluate("window.__apbEnd()")
                moving = min(total - 1, int(end * fps) + 1)   # frames before the card comes to rest
                for i in range(moving):
                    await page.evaluate("t => window.__apb(t)", i / fps)
                    await page.screenshot(path=str(frames_dir / f"f{i:05d}.png"),
                                          clip={"x": 0, "y": 0, "width": W, "height": H})
                rest = frames_dir / f"f{moving:05d}.png"
                await page.evaluate("window.__apb(1e9)")      # the final frame: identical to the static card
                await page.screenshot(path=str(rest), clip={"x": 0, "y": 0, "width": W, "height": H})
                for i in range(moving + 1, total):
                    shutil.copyfile(rest, frames_dir / f"f{i:05d}.png")
            finally:
                await page.close()
        frames = sorted(frames_dir.glob("f*.png"))
        try:
            out = await asyncio.to_thread(media.encode, frames, out_base, fps, False)
        finally:
            shutil.rmtree(frames_dir, ignore_errors=True)
        return out

    async def split_clip(self, rnd: dict, board: list[dict] | None = None, headline: str | None = None,
                         grudge: dict | None = None) -> Path | None:
        kind, default = split_headline(rnd)
        html = env.get_template("split.html").render(**self._ctx(rnd, board), kind=kind,
                                                     headline=headline or default, grudge=grudge)
        return await self.clip(html, self._dir(rnd) / "split")

    async def results_clip(self, rnd: dict, board: list[dict] | None = None, crowd: dict | None = None,
                           cast: str | None = None, top_humans: list[dict] | None = None) -> Path | None:
        html = env.get_template("results.html").render(
            **self._ctx(rnd, board), ranked=ranked(rnd["entries"]), headline=results_headline(rnd),
            upset=is_upset(rnd), crowd=crowd, cast=cast, top_humans=top_humans or [])
        return await self.clip(html, self._dir(rnd) / "results")

    async def frames(self, template: str, contexts: list[dict], out_dir: Path) -> list[Path]:
        """Render one PNG per context (video frames), reusing a single page."""
        out_dir.mkdir(parents=True, exist_ok=True)
        tpl = env.get_template(template)
        paths = []
        async with self._lock:
            browser = await self._ensure()
            page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            try:
                for i, ctx in enumerate(contexts):
                    await page.set_content(tpl.render(**self._plain(**ctx)), wait_until="load")
                    path = out_dir / f"f{i:05d}.png"
                    await page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": W, "height": H})
                    paths.append(path)
            finally:
                await page.close()
        return paths
