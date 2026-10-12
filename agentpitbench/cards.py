"""Share cards: Jinja HTML templates screenshotted to 1200x675 PNGs with Playwright."""
from __future__ import annotations

import asyncio
import shutil
import re
import logging
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

import json

from .config import AGENT_COLORS, AGENT_NAMES, Settings, AGENT_MODELS
from .emblem import emblem_svg
from .mascots import mascot

log = logging.getLogger(__name__)

W, H = 1200, 675


def shrink_png(path: Path) -> None:
    """Rewrite a rendered card as a 256-colour palette PNG (about a third of Chromium's truecolour bytes).
    Median cut, not octree: it keeps the cards' soft background glows free of visible bands. Best effort:
    the file is left as it is without Pillow (the optional "media" extra), on any error, or if not smaller."""
    try:
        import io
        from PIL import Image
        data = path.read_bytes()
        im = Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO()
        im.quantize(256, method=Image.Quantize.MEDIANCUT).save(buf, "PNG", optimize=True)
        if buf.tell() < len(data):
            path.write_bytes(buf.getvalue())
    except ImportError:
        pass
    except Exception:
        logging.getLogger(__name__).exception("shrinking %s failed; kept as rendered", path)
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


# Match of the Day scene timeline. data-in="<s>" enters at that second (data-fx: up | slam | fade | pop);
# data-count counts up to data-to from data-in over data-cd seconds (data-fmt: signed | time | int), ending
# on the element's own text. The rest state (t = 1e9) is the finished scene.
MOTD_JS = r"""
(function(){
  const q = s => Array.from(document.querySelectorAll(s));
  const ease = x => x <= 0 ? 0 : x >= 1 ? 1 : 1 - Math.pow(1 - x, 3);
  let D = 3;
  const fmt = {
    signed: v => (v >= 0 ? '+' : '−') + Math.round(Math.abs(v)).toLocaleString('en-US'),
    time: v => { const s = Math.round(v), m = Math.floor(s / 60), r = s % 60;
                 return m ? `${m}m ${String(r).padStart(2, '0')}s` : `${r}s`; },
    int: v => Math.round(v).toLocaleString('en-US'),
    cents: v => `${Math.round(v)}¢`};
  q('[data-count]').forEach(el => { el.dataset.final = el.textContent; });
  // v0.5 scripted scenes: 'type' (typewriter, TYPE_CPS characters a second), 'wipe' (left-to-right reveal),
  // a scene-in transition overlay (data-trans: iris | wipe | fade | flash) and the twist's white flash + shake.
  const TYPE_CPS = 45, CAM_S = 2.5;
  q('[data-fx=type]').forEach(el => { el.dataset.full = el.textContent; });
  const typeDur = el => el.dataset.full.length / TYPE_CPS;
  window.__motdDur = d => { D = d; };
  window.__motdEnd = () => {
    let end = 0.6;
    q('[data-in]').forEach(el => { end = Math.max(end, parseFloat(el.dataset.in) + (el.dataset.fx === 'type' ? typeDur(el) : 0) + 0.55); });
    q('[data-count]').forEach(el => { end = Math.max(end, parseFloat(el.dataset.in || 0) + parseFloat(el.dataset.cd || 1) + 0.1); });
    if (q('[data-flash]').length) end = Math.max(end, 1.0);
    if (q('[data-cam]').length) end = Math.max(end, CAM_S);  // the push-in settles after CAM_S (render cost)
    return Math.min(end, D);
  };
  window.__motd = function(t){
    q('[data-in]').forEach(el => {
      const t0 = parseFloat(el.dataset.in), fx = el.dataset.fx || 'up', p = ease((t - t0) / 0.45);
      if (fx === 'type') {
        const full = el.dataset.full, k = Math.max(0, Math.min(1, (t - t0) / typeDur(el)));
        el.style.opacity = t >= t0 ? 1 : 0;
        // keep the line's final layout: typed part visible, the rest transparent
        el.innerHTML = '';
        const a = document.createElement('span'); a.textContent = full.slice(0, Math.round(full.length * k));
        const b = document.createElement('span'); b.textContent = full.slice(Math.round(full.length * k)); b.style.opacity = 0;
        el.append(a, b);
        return;
      }
      el.style.opacity = fx === 'slam' ? Math.min(1, p * 2) : fx === 'wipe' ? (t >= t0 ? 1 : 0) : p;
      el.style.clipPath = fx === 'wipe' && p < 1 ? `inset(0 ${100 * (1 - p)}% 0 0)` : '';
      el.style.transform = p >= 1 ? '' : fx === 'slam' ? `scale(${1.25 - 0.25 * p})` :
        fx === 'pop' ? `scale(${0.6 + 0.4 * p})` : (fx === 'fade' || fx === 'wipe') ? '' :
        fx === 'from-left' ? `translateX(${-700 * (1 - p)}px)` : fx === 'from-right' ? `translateX(${700 * (1 - p)}px)` :
        fx === 'drop' ? `translateY(${-500 * (1 - p)}px) rotate(${-4 * (1 - p)}deg)` : `translateY(${40 * (1 - p)}px)`;
    });
    // losers fall: tip over, sink and grey out
    q('[data-fall]').forEach(el => {
      const p = ease((t - parseFloat(el.dataset.fall)) / 0.6);
      if (p <= 0) return;
      el.style.transform = `translateY(${24 * p}px) rotate(${-9 * p}deg)`;
      el.style.filter = `grayscale(${p}) brightness(${1 - 0.45 * p})`;
    });
    q('[data-meter]').forEach(el => {
      const p = ease((t - parseFloat(el.dataset.in || 0)) / 1.0);
      el.style.width = `${parseFloat(el.dataset.meter) * Math.max(0, p)}%`;
    });
    // camera: a slow push-in on every scene; the twist gets a fast zoom after its hard cut
    q('[data-cam]').forEach(el => {
      if (el.dataset.cam === 'zoom') {
        const p = ease((t - 0.3) / 1.2);
        el.style.transform = `scale(${1 + 0.08 * Math.max(0, p)})`;
      } else {
        el.style.transform = `scale(${1 + 0.035 * ease(Math.min(1, Math.max(0, t / CAM_S)))})`;
      }
    });
    q('[data-trans]').forEach(el => {
      const kind = el.dataset.trans, p = ease(t / (kind === 'flash' ? 0.25 : 0.5));
      const main = document.querySelector('.main');
      el.style.display = p >= 1 ? 'none' : '';
      if (kind === 'iris') { el.style.opacity = 1 - p; main.style.clipPath = p < 1 ? `circle(${p * 120}% at 50% 45%)` : ''; }
      else if (kind === 'wipe') { el.style.clipPath = `inset(0 0 0 ${p * 100}%)`; }
      else if (kind === 'flash') { el.style.background = '#fff'; el.style.opacity = 1 - p; }
      else { el.style.opacity = 1 - p; }
    });
    q('[data-flash]').forEach(el => {
      const at = 0.3; // the twist lands: white flash, then a short shake
      el.style.opacity = t < at ? 0 : Math.max(0, 1 - (t - at) / 0.35);
      const main = document.querySelector('.main'), s = t - at - 0.1;
      if (s > 0 && s < 0.45) main.style.transform += ` translateX(${Math.round(14 * Math.sin(s * 60) * (1 - s / 0.45))}px)`;
    });
    q('[data-count]').forEach(el => {
      const k = ease((t - parseFloat(el.dataset.in || 0)) / parseFloat(el.dataset.cd || 1));
      el.textContent = k >= 1 ? el.dataset.final : fmt[el.dataset.fmt || 'int'](parseFloat(el.dataset.to) * k);
    });
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
env.globals["emblem"] = emblem_svg


def card_state(r: dict | None) -> str | None:
    """Colour state of a round's cards: live (open), final (resolved), grey (void / exhibition), gold (High Stakes)."""
    if not r:
        return None
    if r.get("state") == "void":
        return "grey"
    if r.get("high_stakes"):
        return "gold"
    if r.get("exhibition"):
        return "grey"
    return "final" if r.get("state") == "resolved" else "live"


def round_banner(r: dict | None) -> str | None:
    """Text of the top ribbon on cards from rounds that don't count in the season statistics."""
    if not r:
        return None
    if r.get("state") == "void":
        return "VOID · NOT SCORED"
    if r.get("high_stakes"):
        return f"HIGH STAKES · {r.get('stake', 500):.0f} TOKENS · EXHIBITION"
    if r.get("exhibition"):
        return "EXHIBITION · NOT IN SEASON STATS"
    return None


def _norm_id(m: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (m or "").lower())


def played_model(agent: str | None, reported: str | None) -> tuple[str, str | None] | None:
    """(display name, reasoning setting or None) of the model a run REPORTED, not today's config: a round played
    on GPT-6 Luna stays GPT-6 Luna after the switch to Astra. The setting is only known (from config) when the
    reported model is the configured one; '(High)'-style suffixes (agy) carry their own. None if not recorded."""
    if not reported:
        return None
    cfg = AGENT_MODELS.get(agent or "")
    m = re.match(r"^(.*?)\s*\((\w+)\)\s*$", reported)
    if m:  # 'Gemini 3.1 Pro (High)'
        return m.group(1), m.group(2).lower()
    n = _norm_id(reported)
    if cfg and (n in (_norm_id(cfg[0]), _norm_id(cfg[1]))
                or re.fullmatch(re.escape(_norm_id(cfg[0])) + r"(build|latest|stable)?", n)):  # 'grok-4.7-build'
        return cfg[1], cfg[2]
    if " " in reported:
        return reported, None
    words = []
    for w in reported.split("-"):
        words.append(w.upper() if w in ("gpt", "o") else w.capitalize())
    name = " ".join(words)
    name = re.sub(r"(GPT) (\d)", r"\1-\2", name)                 # 'GPT 6 Luna' -> 'GPT-6 Luna'
    name = re.sub(r"(\d) (\d)(?=\b)", r"\1.\2", name)            # 'Fable 5 1' -> 'Fable 5.1'
    return name, None


def model_eras(rounds: list[dict], agent: str) -> dict:
    """Which models an agent's record was played on, from each run's model_reported (exported round entries).
    {"current": config display name, "since": first started_at on it or None (not played yet),
     "earlier": [other display names, most rounds first]}. Exhibition rounds and unrecorded models are skipped."""
    cfg = AGENT_MODELS.get(agent)
    current = cfg[1] if cfg else None
    seen: dict[str, list] = {}  # display name -> [rounds, first started_at]
    for r in rounds:
        if r.get("exhibition"):
            continue
        for e in r.get("entries") or []:
            if e.get("agent") != agent:
                continue
            pm = played_model(agent, e.get("model_reported"))
            if pm is None:
                continue
            ts = r.get("started_at") or 0
            x = seen.setdefault(pm[0], [0, ts])
            x[0] += 1
            x[1] = min(x[1], ts)
    earlier = [n for n, _ in sorted(seen.items(), key=lambda kv: -kv[1][0]) if n != current]
    since = seen[current][1] if current in seen else None
    return {"current": current, "since": since if earlier else None, "earlier": earlier}


def model_line(agent: str | None, reported: str | None = None, short: bool = True) -> str:
    """'Fable 5.1 · max' for one agent (its name is already big on the card); the shared line otherwise. With
    `reported` (the run's model_reported) the line names the model that round was actually played on."""
    if reported is not None or agent in AGENT_MODELS:
        pm = played_model(agent, reported) if reported is not None else (AGENT_MODELS[agent][1], AGENT_MODELS[agent][2])
        if pm is None:
            return "model not recorded"
        name, effort = pm
        if short:
            name = name.removeprefix(AGENT_NAMES.get(agent, "") + " ")  # "Claude Fable 5.1" -> "Fable 5.1"
        return f"{name} · {effort}" if effort else name
    return "frontier models · max"


def short_sig(label: str | None) -> str | None:
    """The leaderboard's significance label, shortened for the one-line strip."""
    if not label:
        return None
    return label.replace("significant vs the Crowd (p<0.05)", "significant vs Crowd p<.05")


def start_price(r: dict, outcome: str | None) -> float | None:
    """The market's price for an outcome at the round's start snapshot."""
    outs, prices = r.get("outcomes") or [], r.get("prices_at_start") or []
    if outcome in outs and len(prices) == len(outs):
        return prices[outs.index(outcome)]
    return None


def cents_fine(p: float | None) -> str:
    """Price to the half cent: 61.5¢, 62¢."""
    if p is None:
        return "—"
    v = round(p * 100, 1)
    return f"{v:g}¢"


env.filters.update(cents_fine=cents_fine)


def ranked(entries: list[dict]) -> list[dict]:
    """Higher P&L first; a tie goes to the faster decision; no-bets last."""
    return sorted(entries, key=lambda e: (-(e["pnl"] if e["pnl"] is not None else -1e9),
                                          e["decided_s"] if e["decided_s"] is not None else 1e9))


def is_upset(rnd: dict) -> bool:
    return any(e["won"] and e["avg_price"] is not None and e["avg_price"] <= UPSET_PRICE for e in rnd["entries"])


def _en(msg: str, **kw) -> str:
    return msg.format(**kw)


HEADLINE_VARIANTS = ("fun", "plain")
_NUM = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def headline_variant(round_id: int) -> str:
    """A/B tone of a round's results headline, alternating by round so both run under the same conditions."""
    return HEADLINE_VARIANTS[round_id % 2]


def results_headline(rnd: dict, _=_en, variant: str = "fun") -> str:
    """`_` translates a message id with {placeholders}; the site passes one per language, cards stay English.
    variant 'fun' is the sportscaster tone, 'plain' the sober one (English only, cards and tweets)."""
    plain = variant == "plain"
    if rnd["state"] == "void":
        return _("Market voided")
    bettors = [e for e in rnd["entries"] if e["outcome"]]
    winners = [e for e in rnd["entries"] if e["won"]]
    n = _NUM.get(len(bettors), str(len(bettors)))
    if not winners:
        return f"All {n} wrong" if plain else _("Everyone got rekt")
    if len(winners) == len(rnd["entries"]) and len(winners) > 1:
        return f"All {n} right" if plain else _("Clean sweep")
    crowd_lost = (rnd.get("crowd") or {}).get("won") is False
    if len(winners) == 1:
        w = winners[0]
        if w["avg_price"] is not None and w["avg_price"] <= UPSET_PRICE:
            return (f"{w['name']} wins at {cents(w['avg_price'])}" if plain else
                    _("Upset! {name} cashes at {price}", name=w["name"], price=cents(w["avg_price"])))
        if crowd_lost:
            return f"{w['name']} right, the crowd wrong" if plain else _("{name} beats the crowd", name=w["name"])
        if len(bettors) > 1:
            return f"Only {w['name']} right" if plain else _("{name} takes it alone", name=w["name"])
        return f"{w['name']} right" if plain else _("{name} takes it", name=w["name"])
    names = " & ".join(e["name"] for e in winners)
    if plain:
        return f"{names} right" + (", the crowd wrong" if crowd_lost else "")
    return _("{names} beat the crowd", names=names) if crowd_lost else _("{names} take it", names=names)


def split_headline(rnd: dict, _=_en) -> tuple[str, str]:
    """(kind, headline): kind is 'split' or 'agree'. Agents with no bet are left out of the count."""
    sit, f = split_shape(rnd)
    if sit == "agree":
        return "agree", _("They all agree")
    if sit == "alone":
        return "split", _("{score}: {name} goes alone", score=f["score"], name=f["lone"])
    if sit == "dead_split":
        return "split", _("{score}: dead split", score=f["score"])
    if sit == "nobody_agrees":
        return "split", _("{score}: nobody agrees", score=f["score"])
    return "split", _("{score}: {n}-way split", score=f["score"], n=f["n"])


def split_shape(rnd: dict) -> tuple[str, dict[str, str]]:
    """(situation, facts) behind the reveal headline, for the A/B hook templates (docs/ab-testing-spec.md
    section 3). situation: agree / alone / dead_split / nobody_agrees / nway; facts: score, lone, n."""
    picks: dict[str, list[str]] = {}
    for e in rnd["entries"]:
        if e["outcome"]:
            picks.setdefault(e["outcome"], []).append(e["name"])
    if len(picks) <= 1:
        return "agree", {}
    groups = sorted(picks.values(), key=len, reverse=True)
    sizes = [len(g) for g in groups]
    facts = {"score": "–".join(map(str, sizes)), "n": str(len(groups))}
    if len(groups) == 2 and sizes[1] == 1 and sizes[0] > 1:
        return "alone", {**facts, "lone": groups[1][0]}
    if len(set(sizes)) == 1:
        return ("dead_split" if len(groups) == 2 else "nobody_agrees"), facts
    return "nway", facts


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
        # "n=12 · not yet significant": set by the publisher from the latest export before split/results renders
        self.significance: str | None = None

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
        shrink_png(out)
        return out

    def _dir(self, rnd: dict) -> Path:
        return self.s.cards_dir / str(rnd["round_id"])

    @property
    def sig(self) -> str | None:
        """The significance line; set by the publisher, else read from the latest export."""
        if self.significance:
            return self.significance
        try:
            lb = json.loads((self.s.export_dir / "leaderboard.json").read_text())
            return (lb.get("significance") or {}).get("label")
        except (OSError, ValueError):
            return None

    @property
    def host(self) -> str:
        return self.s.site_url.replace("https://", "").replace("http://", "").rstrip("/")

    def _sig_for(self, agent: str | None) -> str | None:
        """Single-agent cards carry that agent's own verdict (with direction); others the multi-agent line."""
        try:
            sig = json.loads((self.s.export_dir / "leaderboard.json").read_text()).get("significance") or {}
        except (OSError, ValueError):
            sig = {}
        if agent and (sig.get("agent_labels") or {}).get(agent):
            return sig["agent_labels"][agent]
        return sig.get("multi_label") or self.sig

    def _strip(self, url_path: str, agent: str | None = None, reported: str | None = None) -> dict:
        """The credibility strip every card carries: model and reasoning, n and significance, methodology, URL.
        `reported` = the run's model_reported, so a re-rendered old round names the model it was played on."""
        url, sig = f"{self.host}/{url_path}".rstrip("/"), short_sig(self._sig_for(agent))
        ml = model_line(agent, reported) if agent and reported else model_line(agent)
        # one line at 26px holds ~107 condensed characters (with the URL); past that, multi-agent cards drop the model detail
        if agent is None and len(f"{ml} · {sig or ''} · Methodology v{self.s.methodology_version} {url}") > 107:
            ml = None
        return {"url": url, "model_line": ml, "sig": sig,
                "mver": self.s.methodology_version}

    def _ctx(self, rnd: dict, board: list[dict] | None, agent: str | None = None) -> dict:
        return {"r": rnd, "board": board or [], "colors": AGENT_COLORS, "names": AGENT_NAMES, "models": AGENT_MODELS,
                "site": self.host, "state": card_state(rnd), "banner": round_banner(rnd),
                **self._strip(f"round/{rnd['round_id']}", agent,
                              next((e.get("model_reported") for e in rnd.get("entries") or []
                                    if e.get("agent") == agent), None) if agent else None)}

    async def decision(self, rnd: dict, entry: dict, board: list[dict] | None = None) -> Path:
        row = next((b for b in board or [] if b["agent"] == entry["agent"]), {})
        streak = row.get("streak", 0)
        form = "fire" if streak >= 3 else "tilt" if streak <= -3 else None
        mkt = start_price(rnd, entry.get("outcome"))
        conf = entry.get("confidence")
        # bets although it rates the pick below the market; a point of slack keeps rounding noise from flagging
        against = mkt is not None and conf is not None and conf <= mkt - 0.01
        html = env.get_template("decision.html").render(
            **self._ctx(rnd, board, entry["agent"]), e=entry, status=entry_status(entry), form=form,
            streak=abs(streak), mkt=mkt, against=against)
        return await self.render_html(html, self._dir(rnd) / f"decision-{entry['agent']}.png")

    async def split(self, rnd: dict, board: list[dict] | None = None, headline: str | None = None,
                    grudge: dict | None = None) -> Path:
        kind, default = split_headline(rnd)
        html = env.get_template("split.html").render(**self._ctx(rnd, board), kind=kind,
                                                     headline=headline or default, grudge=grudge)
        return await self.render_html(html, self._dir(rnd) / "split.png")

    def _results_html(self, rnd, board, crowd, cast, top_humans, variant) -> str:
        return env.get_template("results.html").render(
            **self._ctx(rnd, board), ranked=ranked(rnd["entries"]),
            headline=results_headline(rnd, variant=variant or "fun"),
            upset=is_upset(rnd), crowd=crowd, cast=cast, top_humans=top_humans or [])

    async def results(self, rnd: dict, board: list[dict] | None = None, crowd: dict | None = None,
                      cast: str | None = None, top_humans: list[dict] | None = None, variant: str | None = None) -> Path:
        html = self._results_html(rnd, board, crowd, cast, top_humans, variant)
        return await self.render_html(html, self._dir(rnd) / "results.png")

    def _plain(self, url_path: str = "leaderboard/", agent: str | None = None, **kw) -> dict:
        return {"r": None, "board": [], "colors": AGENT_COLORS, "names": AGENT_NAMES, "models": AGENT_MODELS,
                "site": self.host, "state": None, "banner": None, "agent": agent, **self._strip(url_path, agent), **kw}

    async def trophy(self, season: str, board: list[dict], crowd: dict | None) -> Path:
        ctx = self._plain(season=season, champ=board[0], crowd=crowd)
        ctx["board"] = board
        html = env.get_template("trophy.html").render(**ctx)
        return await self.render_html(html, self.s.cards_dir / "seasons" / f"{season}.png")

    async def milestone(self, key: str, headline: str, subline: str, agent: str | None, season: str) -> Path:
        html = env.get_template("milestone.html").render(**self._plain("leaderboard/", agent, headline=headline,
                                                                        subline=subline, season=season))
        return await self.render_html(html, self.s.cards_dir / "milestones" / f"{key}.png")

    async def human_winner(self, rnd: dict, human: dict, beaten: list[str]) -> Path:
        html = env.get_template("human.html").render(**self._ctx(rnd, None), h=human, beaten=beaten)
        safe = "".join(ch for ch in (human.get("username") or human["user_id"]) if ch.isalnum() or ch in "_-")
        return await self.render_html(html, self.s.cards_dir / "humans" / str(rnd["round_id"]) / f"{safe}.png")

    async def awards(self, week: str, awards: list[dict]) -> Path:
        html = env.get_template("awards.html").render(**self._plain(week=week, awards=awards))
        return await self.render_html(html, self.s.cards_dir / "awards" / f"{week}.png")

    async def contrarian(self, week: str, calls: list[dict]) -> Path:
        html = env.get_template("contrarian.html").render(**self._plain("contrarian/", week=week, calls=calls))
        return await self.render_html(html, self.s.cards_dir / "contrarian" / f"{week}.png")

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
                           cast: str | None = None, top_humans: list[dict] | None = None,
                           variant: str | None = None) -> Path | None:
        html = self._results_html(rnd, board, crowd, cast, top_humans, variant)
        return await self.clip(html, self._dir(rnd) / "results")

    async def sequence(self, scenes: list[dict], out_base: Path, width: int, height: int, fps: int = 15,
                       fmt: str = "tall") -> Path | None:
        """Match of the Day: render a scene list (motd.script) to one silent MP4 at width x height. Each scene is
        motd.html animated by MOTD_JS; frames after it comes to rest are copies of its rest frame. The browser
        lock is held per scene, so other cards can render between scenes."""
        from . import media
        if not media.ffmpeg_exe():
            return None
        frames_dir = out_base.parent / f"{out_base.name}-frames"
        shutil.rmtree(frames_dir, ignore_errors=True)
        frames_dir.mkdir(parents=True, exist_ok=True)
        clip = {"x": 0, "y": 0, "width": width, "height": height}
        n = 0
        async with self._lock:
            browser = await self._ensure()
            page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        try:
            for i, sc in enumerate(scenes):
                strip = self._strip(f"round/{sc['round_id']}", sc.get("agent"))
                tpl = env.get_template("motd_story.html" if sc["kind"].startswith("s_") else "motd.html")
                html = tpl.render(sc=sc, fmt=fmt, w=width, h=height, idx=i, count=len(scenes),
                                  colors=AGENT_COLORS, names=AGENT_NAMES, **strip,
                                  sig_line=sc.get("sig") or strip["sig"],
                                  model=sc.get("model_line") or strip["model_line"])
                total = max(1, int(round(sc["dur"] * fps)))
                async with self._lock:
                    await page.set_content(html, wait_until="load")
                    await page.evaluate("document.fonts && document.fonts.ready")
                    await page.add_script_tag(content=MOTD_JS)
                    await page.evaluate("d => window.__motdDur(d)", sc["dur"])
                    end = await page.evaluate("window.__motdEnd()")
                    moving = min(total - 1, int(end * fps) + 1)
                    for k in range(moving):
                        await page.evaluate("t => window.__motd(t)", k / fps)
                        await page.screenshot(path=str(frames_dir / f"f{n + k:05d}.png"), clip=clip)
                    rest = frames_dir / f"f{n + moving:05d}.png"
                    await page.evaluate("window.__motd(1e9)")
                    await page.screenshot(path=str(rest), clip=clip)
                for k in range(moving + 1, total):
                    shutil.copyfile(rest, frames_dir / f"f{n + k:05d}.png")
                n += total
        finally:
            await page.close()
        frames = sorted(frames_dir.glob("f*.png"))
        try:
            out = await asyncio.to_thread(media.encode, frames, out_base, fps, False)
        finally:
            shutil.rmtree(frames_dir, ignore_errors=True)
        return out

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
                    rnd = ctx.get("r")
                    base = (self._ctx(rnd, None, (ctx.get("e") or {}).get("agent")) if rnd else self._plain())
                    await page.set_content(tpl.render(**{**base, **ctx}), wait_until="load")
                    path = out_dir / f"f{i:05d}.png"
                    await page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": W, "height": H})
                    paths.append(path)
            finally:
                await page.close()
        return paths
