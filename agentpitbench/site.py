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
from dataclasses import dataclass
from urllib.parse import urlparse, quote, urlencode
from xml.sax.saxutils import escape

from jinja2 import Environment, PackageLoader, pass_context, select_autoescape

from . import og
from .cards import cents, is_dark, fmt_time, is_upset, payout_x, ranked, results_headline, signed, split_headline
from . import discover, methodology
from .emblem import emblem_svg
from .config import AGENT_COLORS, AGENT_NAMES, Settings, AGENT_MODELS
from .i18n import CODES, LANGS, NAMES, Translator
from .mascots import sprite, svg_file, use as mascot_use
from .twitter import clip, results_hook
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
    files = [p for p in folder.glob(pattern) if p.is_file() and (exts is None or p.suffix in exts)
             and not p.stem.endswith("-og")]  # link previews live next to their cards
    if not files:
        return None
    best = max(files, key=lambda p: (p.stat().st_mtime, p.name))
    return "cards/" + best.relative_to(folder.parent).as_posix()


def tweet_url(tweet_id: str | None) -> str | None:
    if not tweet_id or str(tweet_id).startswith("dry-"):
        return None
    return f"https://x.com/AgentpitBench/status/{tweet_id}"


# ---------- sharing ----------

# Open Graph locale of each site language (Facebook's list; Arabic is ar_AR there)
OG_LOCALES = {"en": "en_US", "zh": "zh_CN", "hi": "hi_IN", "es": "es_ES", "ar": "ar_AR", "fr": "fr_FR", "bn": "bn_IN",
              "pt": "pt_BR", "ru": "ru_RU", "ur": "ur_PK", "id": "id_ID", "de": "de_DE", "ja": "ja_JP", "mr": "mr_IN",
              "te": "te_IN", "tr": "tr_TR", "ta": "ta_IN", "vi": "vi_VN", "ko": "ko_KR", "ha": "ha_NG"}


@dataclass
class ShareText:
    """One share string per page, cut per network. flex is the part clipped first (a long question or outcome
    label), so the picks in tail survive; head and tail are only clipped when there is no other way."""
    head: str
    flex: str = ""
    tail: str = ""

    @property
    def full(self) -> str:
        return " ".join((self.head + self.flex + self.tail).split())

    def fit(self, n: int) -> str:
        if len(self.full) <= n:
            return self.full
        room = n - len(" ".join((self.head + self.tail).split()))
        if self.flex and room >= 12:
            return " ".join((self.head + clip(self.flex, room) + self.tail).split())
        return clip(self.full, n)


def flex_text(_, msg: str, flex: str, value: str, **kw) -> ShareText:
    """A translated share string whose {flex} placeholder is the part to clip first."""
    # msg arrives already translated (the call site wraps the literal in _() so the extractor sees it); this second
    # call only formats it, isolating the values in right-to-left pages
    head, _sep, tail = _(msg, **{flex: "\x00"}, **kw).partition("\x00")
    return ShareText(head, value, tail)


def utm(url: str, source: str, page_type: str, medium: str = "share") -> str:
    """Attribution on every shared link. utm_content carries a share- prefix: results tweets already use
    utm_content for their A/B headline variants."""
    return url + ("&" if "?" in url else "?") + urlencode(
        {"utm_source": source, "utm_medium": medium, "utm_campaign": "site", "utm_content": f"share-{page_type}"})


def share_bar(text: "ShareText | str", title: str, url: str, page_type: str, _) -> dict:
    """The share bar's links, built here so they work with JavaScript off. Lengths per network: X 280 with the
    link counted as 23, Bluesky 300 including the link, Telegram and WhatsApp ~200 before the link, Reddit
    titles 300."""
    t = text if isinstance(text, ShareText) else ShareText(text)

    def q(params: dict) -> str:
        return urlencode(params, quote_via=quote)

    def u(net: str) -> str:
        return utm(url, net, page_type)
    bsky = u("bluesky")
    links = [
        ("x", "X", "https://x.com/intent/tweet?" + q({"text": t.fit(280 - 24), "url": u("x")})),
        ("linkedin", "LinkedIn", "https://www.linkedin.com/sharing/share-offsite/?" + q({"url": u("linkedin")})),
        ("facebook", "Facebook", "https://www.facebook.com/sharer/sharer.php?" + q({"u": u("facebook")})),
        ("reddit", "Reddit", "https://www.reddit.com/submit?" + q({"url": u("reddit"), "title": t.fit(300)})),
        ("whatsapp", "WhatsApp", "https://wa.me/?" + q({"text": f"{t.fit(200)} {u('whatsapp')}"})),
        ("telegram", "Telegram", "https://t.me/share/url?" + q({"url": u("telegram"), "text": t.fit(200)})),
        ("bluesky", "Bluesky", "https://bsky.app/intent/compose?" + q({"text": f"{t.fit(300 - len(bsky) - 1)} {bsky}"})),
        ("email", _("Email"), "mailto:?" + q({"subject": title, "body": f"{t.full}\n\n{u('email')}"})),
    ]
    return {"links": links, "copy_url": utm(url, "copy", page_type), "native_url": utm(url, "native", page_type),
            "text": t.fit(200), "title": title}


def page_type(path: str) -> str:
    """utm_content of a page: its first folder (round, agent, ...), 'home' for the front page."""
    first = path.split("/", 1)[0]
    return "home" if first == "index.html" else first


def round_share(r: dict, _, vs: str) -> ShareText:
    """Resolved: the results tweet's own hook, so the two never drift. Open: the question and every pick
    (or, while the picks are sealed, that the AIs are still betting)."""
    if r["state"] in ("resolved", "void"):
        return ShareText(results_hook(r))
    if r.get("sealed") or not any(e.get("outcome") for e in r["entries"]):
        return flex_text(_, _("{question}: {vs} are placing their bets. Who's right?"), "question", r["question"], vs=vs)
    picks = ", ".join(f"{e['name']} {e['outcome'] or _('no bet')}" for e in r["entries"])
    return flex_text(_, _("{question}: {picks}. Who's right?"), "question", r["question"], picks=picks)


def leaderboard_share(lb: dict, _) -> str:
    fc = lb.get("forecast") or {}
    scored = [a for a in fc.get("agents") or [] if a.get("brier") is not None]
    if scored and fc.get("markets_resolved"):
        lead = min(scored, key=lambda a: a["brier"])
        return _("Which AI forecasts best? {leader} leads on accuracy after {n} markets.", leader=lead["name"],
                 n=fc["markets_resolved"])
    return _("Which AI is smartest with money?")


def agent_share(lb: dict, agent: str, name: str, row: dict | None, _) -> str:
    fc = next((a for a in (lb.get("forecast") or {}).get("agents") or [] if a["agent"] == agent), None)
    w, l, p = (row or {}).get("wins", 0), (row or {}).get("losses", 0), signed((row or {}).get("net_pnl", 0))
    if fc and fc.get("brier") is not None:
        return _("{name} on AgentpitBench: {wins}-{losses}, {profit} tokens, forecast accuracy {score}.",
                 name=name, wins=w, losses=l, profit=p, score=f"{fc['brier']:.3f}")
    return _("{name} on AgentpitBench: {wins}-{losses}, {profit} tokens.", name=name, wins=w, losses=l, profit=p)


def report_headline(rep: dict, _) -> str | None:
    """A weekly report's headline, from its numbers: the best forecaster, else the best bettor."""
    fc = [a for a in rep.get("forecast") or [] if a.get("brier") is not None]
    if fc:
        a = min(fc, key=lambda x: x["brier"])
        return _("State of the AIs, {week}: {name} forecast best (Brier {score}).", week=rep["week"], name=a["name"],
                 score=f"{a['brier']:.3f}")
    if rep.get("rounds") and rep.get("betting"):
        b = max(rep["betting"], key=lambda x: x["profit"])
        return _("State of the AIs, {week}: {name} led the betting, {wins}-{losses}, {profit} tokens.",
                 week=rep["week"], name=b["name"], wins=b["wins"], losses=b["losses"], profit=signed(b["profit"]))
    return None


def contrarian_share(calls: list[dict], _) -> str | None:
    done = [c for c in calls if c.get("right") is not None]
    if not done:
        return None
    return _("When the AIs bet against the market: {right} of {total} contrarian calls right so far.",
             right=sum(1 for c in done if c["right"]), total=len(done))


SHARE_BAR = 'data-share-bar'  # every public page carries it (checked at build)
OG_REQUIRED = ('property="og:title"', 'property="og:description"', 'property="og:image"', SHARE_BAR)
NO_SHARE = ("widget/", "embed/")  # embedded elsewhere: no bar, no preview


def check_pages(out: Path) -> None:
    """Acceptance check: every public page has a title, description and image preview, and the share bar."""
    bad = []
    for f in out.rglob("*.html"):
        rel = f.relative_to(out).as_posix()
        if rel.startswith(("cards/", "data/", "transcripts/")) or any(f"{x}" in rel for x in NO_SHARE):
            continue
        html = f.read_text()
        missing = [m for m in OG_REQUIRED if m not in html]
        if missing:
            bad.append(f"{rel}: {', '.join(missing)}")
    if bad:
        raise RuntimeError(f"{len(bad)} page(s) lack link previews or the share bar: " + "; ".join(bad[:10]))


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


def badge_svg(board: list[dict], crowd: dict | None = None, sig: str | None = None) -> str:
    """Shields-style badge: 'AgentpitBench | Claude 12-8 · ChatGPT 10-10 · Gemini 9-11'."""
    label = "AgentpitBench"
    value = " · ".join(f"{b['name']} {b['wins']}-{b['losses']}" for b in board) or "no results yet"
    if board and crowd and crowd.get("played"):
        value += f" · Crowd {crowd['wins']}-{crowd['losses']}"
    if sig:
        value += f" · {sig}"
    lw, vw = 7 * len(label) + 12 + 18, int(6.6 * len(value)) + 14  # +18: the emblem left of the label
    w = lw + vw
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{escape(label)}: {escape(value)}">'
        f'<title>{escape(label)}: {escape(value)}</title>'
        f'<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r"><rect width="{w}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#0b1020"/><rect x="{lw}" width="{vw}" height="20" fill="#D97757"/>'
        f'<rect width="{w}" height="20" fill="url(#s)"/></g>'
        f'<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{(lw + 18) / 2}" y="14">{escape(label)}</text><text x="{lw + vw / 2}" y="14">{escape(value)}</text></g>'
        f'{str(emblem_svg(14, True)).replace("<svg ", "<svg x=\"3\" y=\"3\" ", 1)}</svg>'
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


def _(msg: str) -> str:
    """Marks module-level message ids for translation (the extractor looks for calls to this name);
    pages translate them at render time with the page's Translator."""
    return msg


# Methodology changelog: (date, message id). Newest first; every rule change during Season 1 gets an entry.
# It lives in methodology.json (the methodology lock) and changes only through `agentpitbench method approve`.
CHANGELOG = methodology.changelog()

# Public incident log: (date, title id, what happened id, resolution id). Newest first.
INCIDENTS = [
    ("2026-10-09", _("Rounds 2–4 voided"),
     _("The agent sandbox (bubblewrap) could not create user namespaces when the bench ran as a systemd user service (Ubuntu's AppArmor restriction). Every agent failed at launch, and 12 forfeit posts went out on X."),
     _("The posts were deleted and the rounds voided. Fix: an AppArmor profile for bubblewrap, a sandbox self-check before every batch, and automatic voiding of rounds hit by infrastructure failures.")),
]

EXCLUSION_REASONS = {
    "pre-season": _("pre-season round (earlier settings)"),
    "high-stakes": _("High-Stakes Friday showcase"),
    "summon": _("summoned by a follower"),
    "infra": _("voided by an infrastructure failure"),
}

# Branded link-preview cards: page key -> title (message id), rendered per language
BRANDED = {"methodology": _("Methodology"), "independence": _("Independence & conflicts"), "incidents": _("Incidents"),
           "press": _("Press kit"), "data": _("Data & method"), "splits": _("Split decisions"),
           "hall-of-shame": _("Hall of shame"), "reports": _("Weekly reports"), "contrarian": _("Contrarian calls")}

REASONING = {"max": "max reasoning", "xhigh": "extra-high reasoning", "high": "high reasoning"}


def model_line(agent: str, _) -> str:
    """'Fable 5.1 · max reasoning' from config.AGENT_MODELS, so a model swap is one edit there."""
    if agent not in AGENT_MODELS:
        return ""
    _id, name, effort = AGENT_MODELS[agent]
    label = {"max": _("max reasoning"), "xhigh": _("extra-high reasoning"),
             "high": _("high reasoning")}.get(effort, effort)
    return f"{name} · {label}"


def ci_bar(rows: list[dict], crowd: dict | None, width: int = 640) -> str:
    """Inline SVG: each agent's mean Brier score with its 95% CI, the Crowd as a dashed reference line.
    Lower is better, so better forecasters sit further left."""
    pts = [r for r in rows if r.get("brier") is not None and r.get("ci")]
    if not pts:
        return ""
    vals = [v for r in pts for v in r["ci"]] + ([crowd["brier"]] if crowd and crowd.get("brier") is not None else [])
    lo, hi = max(0.0, min(vals) - 0.02), min(1.0, max(vals) + 0.02)
    pad_l, pad_r, row_h = 110, 16, 30
    h = len(pts) * row_h + 34

    def x(v):
        return pad_l + (v - lo) / ((hi - lo) or 1) * (width - pad_l - pad_r)
    parts = [f'<svg viewBox="0 0 {width} {h}" class="chart" role="img" aria-label="Brier score with 95% CI">']
    if crowd and crowd.get("brier") is not None:
        cx = x(crowd["brier"])
        parts.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="4" y2="{h - 24}" stroke="#8a93a8" stroke-dasharray="5 4"/>')
    for i, r in enumerate(pts):
        y = 18 + i * row_h
        c = AGENT_COLORS.get(r["agent"], "#888")
        cls = ' class="dkl"' if is_dark(c) else ""
        parts.append(f'<text x="4" y="{y + 4}" class="lbl" style="font-size:13px">{escape(r["name"])}</text>')
        parts.append(f'<line{cls} x1="{x(r["ci"][0]):.1f}" x2="{x(r["ci"][1]):.1f}" y1="{y}" y2="{y}" stroke="{c}" stroke-width="3"/>')
        parts.append(f'<circle{cls} cx="{x(r["brier"]):.1f}" cy="{y}" r="5" fill="{c}"/>')
    parts.append(f'<text x="{pad_l}" y="{h - 6}" class="lbl">{lo:.2f}</text>'
                 f'<text x="{width - pad_r}" y="{h - 6}" class="lbl" text-anchor="end">{hi:.2f}</text></svg>')
    return "".join(parts)


def sweep_prompt_example(s: Settings) -> str:
    from .forecast import PROMPT
    return PROMPT.format(minutes=round(s.sweep_timeout_s / 60), n=s.sweep_markets,
                         lines="<market id> | <question> | <outcome> <price>, ... | <close date>\n...")


# ---------- build ----------

_build_lock = threading.Lock()  # builds run in worker threads; two at once would share the .new folder


def _db_extras(s: Settings) -> dict:
    """Read-only DB extras for the pages: published incidents (newest first) and market-title translations.
    The build never calls a model: a missing translation shows the English source."""
    out = {"incidents": [], "mt": {}}
    if not s.db_path.exists():
        return out
    from .db import DB as _DB
    db = _DB(s.db_path)
    try:
        from .anomaly import published_incidents
        out["incidents"] = published_incidents(db)
    except Exception:
        log.exception("published incidents unavailable")
    try:
        from .market_i18n import table as mt_table
        out["mt"] = mt_table(db)
    except Exception:
        log.exception("market translations unavailable")
    return out


def build(s: Settings) -> Path:
    """Render the whole site into s.site_dir from the exports. Atomic: builds aside, then swaps."""
    with _build_lock:
        return _build(s)


def _build(s: Settings) -> Path:
    exp = s.export_dir
    rounds = json.loads((exp / "rounds.json").read_text()) if (exp / "rounds.json").exists() else []
    all_rounds = list(rounds)  # the API keeps void rounds, flagged; only the pages hide them
    if s.site_hide_void:  # temporary while operations stabilise: void rounds get no page, row or feed item
        rounds = [r for r in rounds if r.get("state") != "void"]
    reports = json.loads((exp / "reports.json").read_text()) if (exp / "reports.json").exists() else []
    lb = json.loads((exp / "leaderboard.json").read_text()) if (exp / "leaderboard.json").exists() else {
        "agents": [], "series": {}, "season": "", "updated_at": time.time()}
    board = lb["agents"]
    metrics = json.loads((exp / "metrics.json").read_text()) if (exp / "metrics.json").exists() else {}
    sitemap: dict[str, float] = {}  # English page rel path -> lastmod
    as_of = discover.as_of_ts(lb)
    out = s.site_dir.with_name(s.site_dir.name + ".new")
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    base = s.site_url.rstrip("/")
    live = [r for r in rounds if r["state"] in ("running", "awaiting_resolution")]
    past = [r for r in rounds if r["state"] in ("resolved", "void")]
    past.sort(key=lambda r: -(r["resolved_at"] or 0))
    names = [AGENT_NAMES.get(a, a) for a in s.agents]
    vs = " vs ".join(names)
    counted = [r for r in rounds if not r.get("exhibition")]  # neutral tables never use excluded rounds
    best, worst = best_worst(counted)
    h2h = head_to_head(counted, s.agents)
    splits = [r for r in rounds if r["split"]]
    prompt = (Path(__file__).parent / "prompt.txt").read_text()
    upset, wrong = biggest_upset(counted), shame(counted)
    stats = {a: agent_stats(counted, a) for a in s.agents}
    # an infra-voided round's posts were taken down, so its cards are not shown either
    cards = {r["round_id"]: (None if r.get("exclusion") == "infra" else card_for(s, r)) for r in rounds}
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    weekly = {"race": _newest(s.cards_dir / "race", "race-*.*", {".mp4", ".gif", ".png"}),
              "awards": _newest(s.cards_dir / "awards", "*.png")}
    seasons = sorted(((p.stem, f"cards/seasons/{p.name}") for p in (s.cards_dir / "seasons").glob("*.png")
                      if not p.stem.endswith("-og")),
                     reverse=True) if (s.cards_dir / "seasons").exists() else []
    press_stats = {"rounds": sum(1 for r in rounds if r["state"] == "resolved"),
                   "bets": sum(1 for r in rounds for e in r["entries"] if e["outcome"])}
    latest_cards = [c for c in (card_for(s, r) for r in past) if c][:6]
    previews = _previews(s, lb, rounds, cards, reports)
    extra = _db_extras(s)
    from .motd import films as _films  # Match of the Day: cards/motd/<date>/film.json
    films = _films(s)
    film_of = {}
    for f in films:  # newest first: a round filmed twice shows its latest film
        film_of.setdefault(f["round_id"], f)

    for lang in CODES:
        _ = Translator(lang)
        roster = (_("{a} and {b}", a=", ".join(names[:-1]), b=names[-1]) if len(names) > 1
                  else (names[0] if names else ""))
        common = {"vs": vs, "roster": roster, "s": s, "board": board, "lb": lb, "colors": AGENT_COLORS,
                  "names": AGENT_NAMES, "base": base, "api_url": s.api_url, "built": built,
                  "tweet_url": tweet_url, "og_locale": OG_LOCALES.get(lang, lang),
                  "og_locale_alts": [OG_LOCALES.get(c, c) for c in CODES if c != lang],
                  "headline": lambda r, _=_: results_headline(r, _),
                  "split_headline": lambda r, _=_: split_headline(r, _),
                  "is_upset": is_upset, "ranked": ranked, "category": category,
                  "_": _, "_h": _.html, "lang": lang, "rtl": _.rtl, "langs": LANGS, "lang_codes": CODES,
                  "lang_name": NAMES[lang], "mascot_sprite": sprite(s.agents), "mi": mascot_use,
                  "commentary": commentary, "model_line": lambda a, _=_: model_line(a, _),
                  "models": AGENT_MODELS, "reasoning": REASONING, "exclusions": EXCLUSION_REASONS,
                  "fc": lb.get("forecast") or {}, "sig": lb.get("significance") or {}}
        prefix = _.prefix()

        def page(path: str, tpl: str, **ctx) -> None:
            depth = (prefix + path).count("/")
            root = "../" * depth                     # site root: shared assets, data, feeds
            rel = path.removesuffix("index.html")    # this page, relative to a language root
            ctx.setdefault("canonical", f"{base}/{prefix}{rel}")
            kind = page_type(path)
            text = ctx.pop("share_text", None) or ctx["title"]
            if "description" not in ctx:  # the share text describes the page; plain titles keep the site blurb
                ctx["description"] = None if text is ctx["title"] else (text.full if isinstance(text, ShareText) else text)
            ctx["share"] = share_bar(text, ctx["title"], ctx["canonical"], kind, _)
            og_rel = ctx.pop("og", None)
            if og_rel is None and kind in BRANDED:
                og_rel = previews["pages"].get((kind, lang))
            ctx["og_image"] = previews["pv"].url(og_rel)
            lastmod = ctx.pop("lastmod", None)
            if lang == "en":
                sitemap[rel] = lastmod or as_of
            html = env.get_template(tpl).render(**common, root=root, home=root + prefix, rel=rel, **ctx)
            f = out / prefix / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(html)

        site_name = " · AgentpitBench"
        page("index.html", "index.html", live=live, past=past[:30], upset=upset, description=None,
             share_text=_("Four frontier AIs bet on real prediction markets. Live scoreboard:"),
             jsonld=discover.dataset_ld(s, lb, f"{base}/"),
             title=_("AgentpitBench: {vs} bet on prediction markets", vs=vs))
        page("leaderboard/index.html", "leaderboard.html", ci_chart=ci_bar((lb.get("forecast") or {}).get("agents", []),
                                                                          (lb.get("forecast") or {}).get("crowd")),
             chart=pnl_chart(lb.get("series", {}), crowd=(lb.get("crowd") or {}).get("series"), _=_),
             best=best, worst=worst, h2h=h2h, weekly=weekly, seasons=seasons, title=_("Leaderboard") + site_name,
             share_text=leaderboard_share(lb, _), og=previews["standings"],
             jsonld=discover.dataset_ld(s, lb, f"{base}/leaderboard/"))
        page("facts/index.html", "facts.html", sections=discover.fact_sections(s, lb, metrics, _),
             bibtex=discover.bibtex(s, as_of), as_of=discover.stamp(as_of),
             jsonld=discover.dataset_ld(s, lb, f"{base}/facts/"), title=_("Facts") + site_name)
        for r in rounds:
            card = cards[r["round_id"]]
            page(f"round/{r['round_id']}/index.html", "round.html", r=r, card=card, film=film_of.get(r["round_id"]),
                 og=previews["rounds"].get(r["round_id"]), share_text=round_share(r, _, vs),
                 lastmod=r["resolved_at"] or r["started_at"],
                 jsonld=discover.round_ld(s, r, f"{base}/round/{r['round_id']}/"),
                 title=_("Round {n}", n=r["round_id"]) + f": {r['question']}")
        for a in s.agents:
            row = next((b for b in board if b["agent"] == a), None)
            page(f"agent/{a}/index.html", "agent.html", agent=a, name=AGENT_NAMES.get(a, a),
                 row=row, st=stats[a], og=previews["agents"].get(a),
                 share_text=agent_share(lb, a, AGENT_NAMES.get(a, a), row, _),
                 cats=(lb.get("by_category") or {}).get(a, []),
                 title=AGENT_NAMES.get(a, a) + site_name)
        page("films/index.html", "films.html", films=films, title="Match of the Day" + site_name)
        page("splits/index.html", "splits.html", splits=splits, h2h=h2h, title=_("Split decisions") + site_name,
             share_text=_("When {roster} disagree, who's right?", roster=roster))
        page("hall-of-shame/index.html", "shame.html", wrong=wrong, title=_("Hall of shame") + site_name,
             share_text=_("The AI hall of shame: most confident wrong calls on AgentpitBench."))
        page("data/index.html", "data.html", prompt=prompt, title=_("Data & method") + site_name)
        page("press/index.html", "press.html", stats=press_stats, latest_cards=latest_cards,
             title=_("Press kit") + site_name)
        page("methodology/index.html", "methodology.html", prompt=prompt, sweep_prompt=sweep_prompt_example(s),
             changelog=CHANGELOG, sealed=lb.get("sealed") or [], title=_("Methodology v{v}", v=s.methodology_version) + site_name,
             share_text=_("How AgentpitBench tests frontier AIs on real markets (methodology v{v}).", v=s.methodology_version),
             jsonld=discover.techarticle_ld(s, lb, f"{base}/methodology/", CHANGELOG))
        page("independence/index.html", "independence.html", title=_("Independence & conflicts") + site_name,
             share_text=_("How AgentpitBench tests frontier AIs on real markets (methodology v{v}).", v=s.methodology_version))
        page("incidents/index.html", "incidents.html", incidents=extra["incidents"] + INCIDENTS, title=_("Incidents") + site_name,
             share_text=_("AgentpitBench publishes its own incidents. Here's the log."))
        page("contrarian/index.html", "contrarian.html", calls=lb.get("contrarian") or [],
             title=_("Contrarian calls") + site_name, share_text=contrarian_share(lb.get("contrarian") or [], _),
             og=previews["contrarian"])
        page("reports/index.html", "reports.html", reports=reports, title=_("Weekly reports") + site_name,
             share_text=report_headline(reports[0], _) if reports else None)
        for rep in reports:
            page(f"reports/{rep['week']}/index.html", "report.html", rep=rep, share_text=report_headline(rep, _),
                 og=previews["weeks"].get(rep["week"]),
                 chart=ci_bar([dict(a, ci=[a["brier"], a["brier"]]) for a in rep["forecast"]],
                              {"brier": rep["crowd_brier"]} if rep.get("crowd_brier") is not None else None),
                 title=_("State of the AIs") + f" · {rep['week']}" + site_name)
        if lang == "en":  # embedded elsewhere; one shared copy
            page("widget/index.html", "widget.html", title="AgentpitBench standings")
            page("embed/index.html", "embed.html", title="AgentpitBench leaderboard")

    (out / "badge.svg").write_text(badge_svg(board, lb.get("crowd"), (lb.get("significance") or {}).get("label")))
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
    for f in ("favicon.svg", "favicon-32.png", "apple-touch-icon.png", og.DEFAULT):  # the emblem; the fallback preview
        shutil.copyfile(Path(__file__).resolve().parent / "static" / f, out / f)
    host = urlparse(s.site_url).hostname or ""
    if host and not host.endswith(".github.io"):  # custom domain: GitHub Pages drops it without this file
        (out / "CNAME").write_text(host + "\n")
    d = out / "data"
    for name in ("rounds.json", "leaderboard.json", "bets.csv", "metrics.json", "reports.json", "sealed.json"):
        if (exp / name).exists():
            shutil.copy2(exp / name, d / name)
    # sealed daily forecasts: the hash is public at once (/methodology/), the content and transcripts only once
    # that day's markets have resolved, so no agent can read a rival's forecast for a market still open
    from . import forecast as _fc
    from .db import DB as _DB
    open_ok = _fc.revealable_dates(_DB(s.db_path)) if s.db_path.exists() else set()
    if (exp / "forecasts").exists():
        (d / "forecasts").mkdir(parents=True, exist_ok=True)
        for f in (exp / "forecasts").glob("*.json"):
            if f.stem in open_ok:
                shutil.copy2(f, d / "forecasts" / f.name)
    if (s.transcripts_dir / "sweep").exists():
        for day in (s.transcripts_dir / "sweep").iterdir():
            if day.is_dir() and day.name in open_ok:
                shutil.copytree(day, out / "transcripts" / "sweep" / day.name, dirs_exist_ok=True)
    if s.cards_dir.exists():  # never a running round's cards: picks stay secret until every agent is done
        running = {str(r["round_id"]) for r in all_rounds if discover.seal(r)}
        shutil.copytree(s.cards_dir, out / "cards",
                        ignore=lambda d, names: [n for n in names if Path(d) == s.cards_dir and n in running])
    for r in rounds:
        for e in r["entries"]:
            if e["transcript"]:
                src = s.transcripts_dir / str(r["round_id"]) / f"{e['agent']}.txt"
                if src.exists():
                    dst = out / e["transcript"]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
    # for AI assistants and agents (docs/ai-referral-spec.md): crawl access, sitemap, llms.txt, facts, API
    (out / "robots.txt").write_text(discover.robots(s))
    for name, xml in discover.sitemaps(s, sitemap).items():
        (out / name).write_text(xml)
    discover.write_api(s, out, all_rounds, lb, metrics, lb.get("sealed") or [], exp, open_ok, set(sitemap),
                       CHANGELOG, EXCLUSION_REASONS)
    (out / "llms.txt").write_text(discover.llms_txt(s, lb, metrics))
    (out / "llms-full.txt").write_text(discover.llms_full_txt(s, lb, metrics, (out / "methodology/index.html").read_text()))
    check_pages(out)  # a page without a preview or the share bar fails the build; the live site stays as it was
    old = s.site_dir.with_name(s.site_dir.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if s.site_dir.exists():
        s.site_dir.rename(old)
    out.rename(s.site_dir)
    shutil.rmtree(old, ignore_errors=True)
    return s.site_dir


def _previews(s: Settings, lb: dict, rounds: list[dict], cards: dict, reports: list[dict]) -> dict:
    """Queue every link preview the build needs, render the changed ones, return site-relative paths."""
    pv = og.Previews(s)
    res: dict = {"pv": pv, "rounds": {}, "agents": {}, "pages": {}, "weeks": {}, "standings": None, "contrarian": None}

    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception:
            log.exception("link preview %s skipped", a[:1])
            return None
    for rid, card in cards.items():
        if card:
            res["rounds"][rid] = safe(pv.letterbox, card)
    html = safe(og.standings_html, s, lb)
    if html:
        res["standings"] = pv.card("standings", html)[1]
    for a in s.agents:
        html = safe(og.agent_html, s, lb, a)
        if html:
            res["agents"][a] = pv.card(f"agents/{a}", html)[1]
    for lang in CODES:
        tr = Translator(lang)
        for key, msg in BRANDED.items():
            res["pages"][(key, lang)] = safe(pv.page, key, tr(msg), lang, tr.rtl)
    contra = _newest(s.cards_dir / "contrarian", "*.png")
    if contra:
        res["contrarian"] = safe(pv.letterbox, contra)
    for rep in reports:
        if (s.cards_dir / "awards" / f"{rep['week']}.png").exists():
            res["weeks"][rep["week"]] = safe(pv.letterbox, f"cards/awards/{rep['week']}.png")
    pv.run()
    return res


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
    # the branch may have moved since our last push (another deploy, a manual push): build on its tip
    if _git(repo, "fetch", "-q", "--depth", "1", "origin", PAGES_BRANCH, env=env, check=False).returncode == 0:
        _git(repo, "reset", "-q", "--soft", "FETCH_HEAD", check=False)
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
            if await asyncio.to_thread(deploy, self.s):
                from .db import DB as _DB  # the deploy-stale detector reads this
                _DB(self.s.db_path).put("site_deployed_at", time.time())
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
