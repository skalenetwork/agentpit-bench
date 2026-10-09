"""Static site: built only from the exports (rounds.json, leaderboard.json, bets.csv), deployed to gh-pages.

Pages use relative links, so the site works under /agentpit-bench/ on GitHub Pages and from a local folder.
Absolute URLs (Open Graph images, feeds) use s.site_url.

Every page exists in each language of i18n.LANGS: English at the root, the others under /<code>/. Data, cards,
transcripts, feeds, the badge and the widget are shared and stay at the root. On a first visit to an English
page, a tiny inline script sends the browser to its own language if we have it; the picker's choice sticks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape

from jinja2 import Environment, PackageLoader, pass_context, select_autoescape

from .cards import cents, is_dark, fmt_time, is_upset, payout_x, ranked, results_headline, signed, split_headline
from .config import AGENT_COLORS, AGENT_NAMES, Settings, AGENT_MODELS
from .i18n import CODES, LANGS, NAMES, Translator
from .mascots import sprite, svg_file, use as mascot_use
from .virality import CATEGORIES, category, commentary  # noqa: F401  (CATEGORIES: category names the site translates)

log = logging.getLogger(__name__)

DEFAULT_REMOTE = "git@github.com:skalenetwork/agentpit-bench.git"
PAGES_BRANCH = "gh-pages"

env = Environment(loader=PackageLoader("agentpitbench", "templates/site"), autoescape=select_autoescape())


@pass_context
def _date(ctx, ts) -> str:
    """'Oct 08, 2026' in English; ISO dates elsewhere, which every locale reads."""
    if not ts:
        return "—"
    fmt = "%b %d, %Y" if ctx.get("lang", "en") == "en" else "%Y-%m-%d"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime(fmt)


def _pnl_class(v) -> str:
    return "zero" if not v else ("pos" if v > 0 else "neg")


env.filters.update(cents=cents, payout_x=payout_x, fmt_time=fmt_time, signed=signed, date=_date,
                   pnl_class=_pnl_class, dk=lambda c: "dk" if is_dark(c) else "",
                   dkb=lambda c: "dkb" if is_dark(c) else "", urlq=lambda v: quote(str(v), safe=""))



def _newest(folder: Path, pattern: str, exts: set[str] | None = None) -> str | None:
    """Site-relative path of the newest matching file under cards/, or None."""
    if not folder.exists():
        return None
    files = [p for p in folder.glob(pattern) if p.is_file() and (exts is None or p.suffix in exts)]
    if not files:
        return None
    best = max(files, key=lambda p: (p.stat().st_mtime, p.name))
    return "cards/" + best.relative_to(folder.parent).as_posix()


def tweet_url(tweet_id: str | None) -> str | None:
    if not tweet_id or str(tweet_id).startswith("dry-"):
        return None
    return f"https://x.com/AgentpitBench/status/{tweet_id}"


def share_url(text: str, url: str) -> str:
    return "https://x.com/intent/tweet?" + urlencode({"text": text, "url": url})


# ---------- derived stats ----------

def _flat(rounds: list[dict]):
    for r in rounds:
        for e in r["entries"]:
            yield r, e


def card_for(s: Settings, r: dict) -> str | None:
    """Site-relative path of the best share card for a round."""
    d = s.cards_dir / str(r["round_id"])
    for name in ["results.png", "split.png", *(f"decision-{e['agent']}.png" for e in r["entries"])]:
        if (d / name).exists():
            return f"cards/{r['round_id']}/{name}"
    return None


def head_to_head(rounds: list[dict], agents: list[str]) -> list[dict]:
    """For each pair: resolved rounds where both bet on different sides, and who was right."""
    out = []
    for i, a in enumerate(agents):
        for b in agents[i + 1:]:
            aw = bw = 0
            for r in rounds:
                if r["state"] != "resolved":
                    continue
                ea = next((e for e in r["entries"] if e["agent"] == a), None)
                eb = next((e for e in r["entries"] if e["agent"] == b), None)
                if ea and eb and ea["outcome"] and eb["outcome"] and ea["outcome"] != eb["outcome"]:
                    aw += bool(ea["won"])
                    bw += bool(eb["won"])
            out.append({"a": a, "b": b, "a_name": AGENT_NAMES.get(a, a), "b_name": AGENT_NAMES.get(b, b),
                        "a_wins": aw, "b_wins": bw})
    return out


def biggest_upset(rounds: list[dict]) -> tuple[dict, dict] | None:
    wins = [(r, e) for r, e in _flat(rounds) if e["won"] and e["avg_price"]]
    return min(wins, key=lambda re_: re_[1]["avg_price"]) if wins else None


def shame(rounds: list[dict], limit: int = 50) -> list[tuple[dict, dict]]:
    """Most confident wrong calls: confidence first, then how expensive the losing side was."""
    wrong = [(r, e) for r, e in _flat(rounds) if e["won"] is False and e["outcome"]]
    wrong.sort(key=lambda re_: (-(re_[1]["confidence"] or 0), -(re_[1]["avg_price"] or 0)))
    return wrong[:limit]


def agent_stats(rounds: list[dict], agent: str) -> dict:
    picks = [(r, e) for r, e in _flat(rounds) if e["agent"] == agent]
    resolved = sorted([(r, e) for r, e in picks if r["state"] == "resolved"], key=lambda x: x[0]["resolved_at"] or 0)
    longest = cur = 0
    for _, e in resolved:
        cur = cur + 1 if e["won"] else 0
        longest = max(longest, cur)
    cats: dict[str, list[int]] = {}
    for r, e in resolved:
        c = cats.setdefault(category(r["question"]), [0, 0])
        c[0] += bool(e["won"])
        c[1] += 1
    by_cat = [{"name": k, "wins": v[0], "played": v[1], "rate": v[0] / v[1]} for k, v in sorted(cats.items())]
    upsets = [(r, e) for r, e in resolved if e["won"] and e["avg_price"]]
    return {"picks": picks, "longest_streak": longest, "by_category": by_cat,
            "upset": min(upsets, key=lambda x: x[1]["avg_price"]) if upsets else None}


def best_worst(rounds: list[dict], n: int = 5):
    settled = [(r, e) for r, e in _flat(rounds) if e["pnl"] is not None and e["outcome"]]
    best = sorted(settled, key=lambda x: -x[1]["pnl"])[:n]
    worst = sorted(settled, key=lambda x: x[1]["pnl"])[:n]
    return best, [w for w in worst if w[1]["pnl"] < 0]


CROWD = "crowd"


def pnl_chart(series: dict[str, list], width: int = 640, height: int = 220, crowd: list | None = None,
              _=Translator("en")) -> str:
    """Inline SVG line chart of cumulative P&L per agent, x = resolved rounds in order. No JS.
    The Crowd baseline, when given, is drawn as a grey dashed reference line."""
    series = {**series, CROWD: crowd} if crowd else series
    if not any(series.values()):
        return ""
    n = max(len(v) for v in series.values())
    vals = [p[1] for v in series.values() for p in v]
    lo, hi = min(0, *vals), max(0, *vals)
    if hi == lo:
        hi += 1
    pad = 40

    def xy(i, v):
        return (pad + i / n * (width - pad - 8), 8 + (hi - v) / (hi - lo) * (height - 30))

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(_("Total profit/loss by round"))}">']
    zy = xy(0, 0)[1]
    parts.append(f'<line x1="{pad}" x2="{width - 8}" y1="{zy:.1f}" y2="{zy:.1f}" class="axis"/>')
    parts.append(f'<text x="2" y="14" class="lbl">{signed(hi)}</text><text x="2" y="{height - 22}" class="lbl">{signed(lo)}</text>')
    for agent, pts in series.items():
        if not pts:
            continue
        line = [xy(0, 0)] + [xy(i + 1, v) for i, (_, v) in enumerate(pts)]
        d = " ".join(f"{x:.1f},{y:.1f}" for x, y in line)
        if agent == CROWD:
            parts.append(f'<polyline points="{d}" fill="none" stroke="#8a93a8" stroke-width="2" stroke-dasharray="6 4"/>')
            continue
        c = AGENT_COLORS.get(agent, "#888")
        cls = ' class="dkl"' if is_dark(c) else ""
        parts.append(f'<polyline{cls} points="{d}" fill="none" stroke="{c}" stroke-width="2.5"/>')
        x, y = line[-1]
        parts.append(f'<circle{cls} cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{c}"/>')
    parts.append(f'<text x="{pad}" y="{height - 6}" class="lbl">{escape(_("start"))}</text>'
                 f'<text x="{width - 8}" y="{height - 6}" class="lbl" text-anchor="end">{escape(_("round {n}", n=n))}</text></svg>')
    return "".join(parts)


def badge_svg(board: list[dict], crowd: dict | None = None) -> str:
    """Shields-style badge: 'AgentpitBench | Claude 12-8 · Codex 10-10 · Gemini 9-11'."""
    label = "AgentpitBench"
    value = " · ".join(f"{b['name']} {b['wins']}-{b['losses']}" for b in board) or "no results yet"
    if board and crowd and crowd.get("played"):
        value += f" · Crowd {crowd['wins']}-{crowd['losses']}"
    lw, vw = 7 * len(label) + 12, int(6.6 * len(value)) + 14
    w = lw + vw
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{escape(label)}: {escape(value)}">'
        f'<title>{escape(label)}: {escape(value)}</title>'
        f'<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r"><rect width="{w}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#0b1020"/><rect x="{lw}" width="{vw}" height="20" fill="#D97757"/>'
        f'<rect width="{w}" height="20" fill="url(#s)"/></g>'
        f'<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{lw / 2}" y="14">{escape(label)}</text><text x="{lw + vw / 2}" y="14">{escape(value)}</text></g></svg>'
    )


def feeds(s: Settings, rounds: list[dict]) -> tuple[str, str]:
    done = [r for r in rounds if r["state"] in ("resolved", "void")]
    done.sort(key=lambda r: -(r["resolved_at"] or 0))
    done = done[:50]
    base = s.site_url.rstrip("/")
    items, jitems = [], []
    for r in done:
        url = f"{base}/round/{r['round_id']}/"
        title = f"{results_headline(r)}: {r['question']}"
        winners = ", ".join(e["name"] for e in r["entries"] if e["won"]) or "nobody"
        summary = (f"Resolved {r['winner']}. Winners: {winners}." if r["state"] == "resolved" else "Market voided.")
        summary += " " + "; ".join(f"{e['name']}: {e['outcome'] or 'no bet'} ({signed(e['pnl']) if e['outcome'] else '—'})"
                                   for e in r["entries"])
        items.append(f"<item><title>{escape(title)}</title><link>{url}</link><guid>{url}</guid>"
                     f"<pubDate>{formatdate(r['resolved_at'] or r['started_at'], usegmt=True)}</pubDate>"
                     f"<description>{escape(summary)}</description></item>")
        jitems.append({"id": url, "url": url, "title": title, "content_text": summary,
                       "date_published": datetime.fromtimestamp(r["resolved_at"] or r["started_at"], timezone.utc).isoformat()})
    rss = ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
           f"<title>AgentpitBench results</title><link>{base}/</link>"
           f"<description>{escape(' vs '.join(AGENT_NAMES.get(a, a) for a in s.agents))} bet on prediction markets.</description>"
           + "".join(items) + "</channel></rss>")
    jf = {"version": "https://jsonfeed.org/version/1.1", "title": "AgentpitBench results",
          "home_page_url": base + "/", "feed_url": base + "/feed.json", "items": jitems}
    return rss, json.dumps(jf, indent=1)


def model_line(agent: str, _) -> str:
    """'Fable 5.1 · max reasoning' from config.AGENT_MODELS, so a model swap is one edit there."""
    if agent not in AGENT_MODELS:
        return ""
    _id, name, effort = AGENT_MODELS[agent]
    label = {"max": _("max reasoning"), "xhigh": _("extra-high reasoning"),
             "high": _("high reasoning")}.get(effort, effort)
    return f"{name} · {label}"


# ---------- build ----------

_build_lock = threading.Lock()  # builds run in worker threads; two at once would share the .new folder


def build(s: Settings) -> Path:
    """Render the whole site into s.site_dir from the exports. Atomic: builds aside, then swaps."""
    with _build_lock:
        return _build(s)


def _build(s: Settings) -> Path:
    exp = s.export_dir
    rounds = json.loads((exp / "rounds.json").read_text()) if (exp / "rounds.json").exists() else []
    lb = json.loads((exp / "leaderboard.json").read_text()) if (exp / "leaderboard.json").exists() else {
        "agents": [], "series": {}, "season": "", "updated_at": time.time()}
    board = lb["agents"]
    out = s.site_dir.with_name(s.site_dir.name + ".new")
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    base = s.site_url.rstrip("/")
    live = [r for r in rounds if r["state"] in ("running", "awaiting_resolution")]
    past = [r for r in rounds if r["state"] in ("resolved", "void")]
    past.sort(key=lambda r: -(r["resolved_at"] or 0))
    names = [AGENT_NAMES.get(a, a) for a in s.agents]
    vs = " vs ".join(names)
    best, worst = best_worst(rounds)
    h2h = head_to_head(rounds, s.agents)
    splits = [r for r in rounds if r["split"]]
    prompt = (Path(__file__).parent / "prompt.txt").read_text()
    upset, wrong = biggest_upset(rounds), shame(rounds)
    stats = {a: agent_stats(rounds, a) for a in s.agents}
    cards = {r["round_id"]: card_for(s, r) for r in rounds}
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    weekly = {"race": _newest(s.cards_dir / "race", "race-*.*", {".mp4", ".gif", ".png"}),
              "awards": _newest(s.cards_dir / "awards", "*.png")}
    seasons = sorted(((p.stem, f"cards/seasons/{p.name}") for p in (s.cards_dir / "seasons").glob("*.png")),
                     reverse=True) if (s.cards_dir / "seasons").exists() else []
    press_stats = {"rounds": sum(1 for r in rounds if r["state"] == "resolved"),
                   "bets": sum(1 for r in rounds for e in r["entries"] if e["outcome"])}
    latest_cards = [c for c in (card_for(s, r) for r in past) if c][:6]

    for lang in CODES:
        _ = Translator(lang)
        roster = (_("{a} and {b}", a=", ".join(names[:-1]), b=names[-1]) if len(names) > 1
                  else (names[0] if names else ""))
        common = {"vs": vs, "roster": roster, "s": s, "board": board, "lb": lb, "colors": AGENT_COLORS,
                  "names": AGENT_NAMES, "base": base, "api_url": s.api_url, "built": built,
                  "share_url": share_url, "tweet_url": tweet_url,
                  "headline": lambda r, _=_: results_headline(r, _),
                  "split_headline": lambda r, _=_: split_headline(r, _),
                  "is_upset": is_upset, "ranked": ranked, "category": category,
                  "_": _, "_h": _.html, "lang": lang, "rtl": _.rtl, "langs": LANGS, "lang_codes": CODES,
                  "lang_name": NAMES[lang], "mascot_sprite": sprite(s.agents), "mi": mascot_use,
                  "commentary": commentary, "model_line": lambda a, _=_: model_line(a, _)}
        prefix = _.prefix()

        def page(path: str, tpl: str, **ctx) -> None:
            depth = (prefix + path).count("/")
            root = "../" * depth                     # site root: shared assets, data, feeds
            rel = path.removesuffix("index.html")    # this page, relative to a language root
            ctx.setdefault("canonical", f"{base}/{prefix}{rel}")
            ctx.setdefault("og_image", f"{base}/og.png" if (s.cards_dir / "og.png").exists() else None)
            html = env.get_template(tpl).render(**common, root=root, home=root + prefix, rel=rel, **ctx)
            f = out / prefix / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(html)

        site_name = " · AgentpitBench"
        page("index.html", "index.html", live=live, past=past[:30], upset=upset,
             title=_("AgentpitBench: {vs} bet on prediction markets", vs=vs))
        page("leaderboard/index.html", "leaderboard.html",
             chart=pnl_chart(lb.get("series", {}), crowd=(lb.get("crowd") or {}).get("series"), _=_),
             best=best, worst=worst, h2h=h2h, weekly=weekly, seasons=seasons, title=_("Leaderboard") + site_name)
        for r in rounds:
            card = cards[r["round_id"]]
            page(f"round/{r['round_id']}/index.html", "round.html", r=r, card=card,
                 og_image=f"{base}/{card}" if card else None,
                 title=_("Round {n}", n=r["round_id"]) + f": {r['question']}")
        for a in s.agents:
            page(f"agent/{a}/index.html", "agent.html", agent=a, name=AGENT_NAMES.get(a, a),
                 row=next((b for b in board if b["agent"] == a), None), st=stats[a],
                 cats=(lb.get("by_category") or {}).get(a, []),
                 title=AGENT_NAMES.get(a, a) + site_name)
        page("splits/index.html", "splits.html", splits=splits, h2h=h2h, title=_("Split decisions") + site_name)
        page("hall-of-shame/index.html", "shame.html", wrong=wrong, title=_("Hall of shame") + site_name)
        page("data/index.html", "data.html", prompt=prompt, title=_("Data & method") + site_name)
        page("press/index.html", "press.html", stats=press_stats, latest_cards=latest_cards,
             title=_("Press kit") + site_name)
        if lang == "en":  # embedded elsewhere; one shared copy
            page("widget/index.html", "widget.html", title="AgentpitBench standings")
            page("embed/index.html", "embed.html", title="AgentpitBench leaderboard")

    (out / "badge.svg").write_text(badge_svg(board, lb.get("crowd")))
    kit = out / "press" / "mascots"
    kit.mkdir(parents=True, exist_ok=True)
    for a in [*s.agents, "crowd"]:
        (kit / f"{a}.svg").write_text(svg_file(a))
    widget_src = Path(__file__).parent / "widget" / "agentpit-widget.js"
    if widget_src.exists():  # the agentpit.dev market-page widget, served next to the standings widget
        shutil.copy2(widget_src, out / "widget" / "agentpit-widget.js")
    rss, jf = feeds(s, rounds)
    (out / "feed.xml").write_text(rss)
    (out / "feed.json").write_text(jf)
    (out / ".nojekyll").write_text("")
    d = out / "data"
    for name in ("rounds.json", "leaderboard.json", "bets.csv"):
        if (exp / name).exists():
            shutil.copy2(exp / name, d / name)
    if s.cards_dir.exists():
        shutil.copytree(s.cards_dir, out / "cards")
    for r in rounds:
        for e in r["entries"]:
            if e["transcript"]:
                src = s.transcripts_dir / str(r["round_id"]) / f"{e['agent']}.txt"
                if src.exists():
                    dst = out / e["transcript"]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
    old = s.site_dir.with_name(s.site_dir.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if s.site_dir.exists():
        s.site_dir.rename(old)
    out.rename(s.site_dir)
    shutil.rmtree(old, ignore_errors=True)
    return s.site_dir


# ---------- deploy ----------

def _git(cwd: Path, *args, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, capture_output=True, text=True, timeout=180)


def _remote_url() -> str:
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=Path(__file__).resolve().parent.parent,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or DEFAULT_REMOTE
    except Exception:
        return DEFAULT_REMOTE


def _git_env(s: Settings) -> dict:
    """GH_PAGES_DEPLOY_KEY is a private-key path or the key itself; without it, the user's ssh setup is used."""
    env = dict(os.environ)
    key = os.environ.get("GH_PAGES_DEPLOY_KEY")
    if key:
        path = Path(key).expanduser()
        if not path.exists():
            path = s.data_dir / ".deploy_key"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(key.strip() + "\n")
            path.chmod(0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    return env


def deploy(s: Settings) -> bool:
    """Commit s.site_dir to gh-pages in a private clone under s.data_dir and push. Never touches the main tree."""
    if s.dry_run:
        log.info("[dry-run] site built at %s; deploy skipped", s.site_dir)
        return False
    repo = s.data_dir / "gh-pages"
    env = _git_env(s)
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", _remote_url())
        if _git(repo, "fetch", "-q", "--depth", "1", "origin", PAGES_BRANCH, env=env, check=False).returncode == 0:
            _git(repo, "checkout", "-q", "-B", PAGES_BRANCH, "FETCH_HEAD")
        else:
            _git(repo, "checkout", "-q", "--orphan", PAGES_BRANCH)
        _git(repo, "config", "user.name", "AgentpitBench bot")
        _git(repo, "config", "user.email", "agentpitbench@users.noreply.github.com")
    for p in repo.iterdir():
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(s.site_dir, repo, dirs_exist_ok=True)
    _git(repo, "add", "-A")
    if _git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        log.info("site unchanged; nothing to deploy")
        return False
    _git(repo, "commit", "-q", "-m", f"site: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    r = _git(repo, "push", "-q", "origin", f"HEAD:{PAGES_BRANCH}", env=env, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"gh-pages push failed: {r.stderr.strip()[:300]}")
    log.info("site deployed to %s", PAGES_BRANCH)
    return True


class SiteDeployer:
    """request() coalesces events within deploy_debounce_s into one build + deploy. Never raises to callers."""

    def __init__(self, s: Settings):
        self.s = s
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._failures = 0
        self.builds = 0

    def request(self) -> None:
        self._event.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while self._event.is_set():
            await asyncio.sleep(self.s.deploy_debounce_s)
            self._event.clear()
            await self._once()

    async def _once(self, retry: bool = True) -> None:
        try:
            await asyncio.to_thread(build, self.s)
            self.builds += 1
            await asyncio.to_thread(deploy, self.s)
            self._failures = 0
        except Exception:
            self._failures += 1
            log.exception("site build/deploy failed (%d in a row)", self._failures)
            if retry:
                await asyncio.sleep(min(600, 30 * 2 ** min(self._failures, 5)))
                self._event.set()

    async def flush(self) -> None:
        """Build and deploy now if anything is pending (used on shutdown)."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._event.is_set():
            self._event.clear()
            await self._once(retry=False)

    async def close(self) -> None:
        await self.flush()
