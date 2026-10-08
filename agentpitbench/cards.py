"""Share cards: Jinja HTML templates screenshotted to 1200x675 PNGs with Playwright."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .config import AGENT_COLORS, AGENT_NAMES, Settings

log = logging.getLogger(__name__)

W, H = 1200, 675
UPSET_PRICE = 0.25

env = Environment(loader=PackageLoader("agentpitbench", "templates/cards"), autoescape=select_autoescape())


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


def ranked(entries: list[dict]) -> list[dict]:
    """Higher P&L first; a tie goes to the faster decision; no-bets last."""
    return sorted(entries, key=lambda e: (-(e["pnl"] if e["pnl"] is not None else -1e9),
                                          e["decided_s"] if e["decided_s"] is not None else 1e9))


def is_upset(rnd: dict) -> bool:
    return any(e["won"] and e["avg_price"] is not None and e["avg_price"] <= UPSET_PRICE for e in rnd["entries"])


def results_headline(rnd: dict) -> str:
    if rnd["state"] == "void":
        return "Market voided"
    bettors = [e for e in rnd["entries"] if e["outcome"]]
    winners = [e for e in rnd["entries"] if e["won"]]
    if not winners:
        return "Everyone got rekt"
    if len(winners) == len(rnd["entries"]) and len(winners) > 1:
        return "Clean sweep"
    if len(winners) == 1:
        w = winners[0]
        if w["avg_price"] is not None and w["avg_price"] <= UPSET_PRICE:
            return f"Upset! {w['name']} cashes at {cents(w['avg_price'])}"
        if len(bettors) > 1:
            return f"{w['name']} takes it alone"
        return f"{w['name']} takes it"
    names = " & ".join(e["name"] for e in winners)
    return f"{names} take it"


def split_headline(rnd: dict) -> tuple[str, str]:
    """(kind, headline): kind is 'split' or 'agree'. Agents with no bet are left out of the count."""
    picks: dict[str, list[str]] = {}
    for e in rnd["entries"]:
        if e["outcome"]:
            picks.setdefault(e["outcome"], []).append(e["name"])
    if len(picks) <= 1:
        return "agree", "They all agree"
    groups = sorted(picks.values(), key=len, reverse=True)
    sizes = [len(g) for g in groups]
    score = "–".join(map(str, sizes))
    if len(groups) == 2 and sizes[1] == 1 and sizes[0] > 1:
        return "split", f"{score}: {groups[1][0]} goes alone"
    if len(set(sizes)) == 1:
        return "split", f"{score}: dead split" if len(groups) == 2 else f"{score}: nobody agrees"
    return "split", f"{score}: {len(groups)}-way split"


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

    async def render_html(self, html: str, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            browser = await self._ensure()
            page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            try:
                await page.set_content(html, wait_until="load")
                await page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": W, "height": H})
            finally:
                await page.close()
        return out

    def _dir(self, rnd: dict) -> Path:
        return self.s.cards_dir / str(rnd["round_id"])

    def _ctx(self, rnd: dict, board: list[dict] | None) -> dict:
        return {"r": rnd, "board": board or [], "colors": AGENT_COLORS, "names": AGENT_NAMES,
                "site": self.s.site_url.replace("https://", "")}

    async def decision(self, rnd: dict, entry: dict, board: list[dict] | None = None) -> Path:
        html = env.get_template("decision.html").render(
            **self._ctx(rnd, board), e=entry, status=entry_status(entry))
        return await self.render_html(html, self._dir(rnd) / f"decision-{entry['agent']}.png")

    async def split(self, rnd: dict, board: list[dict] | None = None) -> Path:
        kind, headline = split_headline(rnd)
        html = env.get_template("split.html").render(**self._ctx(rnd, board), kind=kind, headline=headline)
        return await self.render_html(html, self._dir(rnd) / "split.png")

    async def results(self, rnd: dict, board: list[dict] | None = None) -> Path:
        html = env.get_template("results.html").render(
            **self._ctx(rnd, board), ranked=ranked(rnd["entries"]), headline=results_headline(rnd),
            upset=is_upset(rnd))
        return await self.render_html(html, self._dir(rnd) / "results.png")
