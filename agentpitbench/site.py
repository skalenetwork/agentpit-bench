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
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import urlparse, quote, urlencode
from xml.sax.saxutils import escape

from jinja2 import Environment, PackageLoader, pass_context, select_autoescape
from markupsafe import Markup

from . import og
from .cards import cents, is_dark, fmt_time, is_upset, payout_x, ranked, results_headline, signed, split_headline, model_eras
from . import challenge, discover, methodology
from .emblem import emblem_svg
from .config import AGENT_COLORS, AGENT_NAMES, AGENT_SLUGS, Settings, AGENT_MODELS
from .i18n import CODES, LANGS, NAMES, Translator
from .mascots import sprite, svg_file, use as mascot_use
from .twitter import TAG, clip, fit, market_name, results_hook
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
              "te": "te_IN", "tr": "tr_TR", "ta": "ta_IN", "vi": "vi_VN", "ko": "ko_KR", "ha": "ha_NG",
              "sw": "sw_KE", "pa": "pa_IN", "tl": "tl_PH", "fa": "fa_IR", "am": "am_ET", "th": "th_TH", "jv": "jv_ID",
              "it": "it_IT", "gu": "gu_IN", "kn": "kn_IN", "yo": "yo_NG", "pl": "pl_PL", "my": "my_MM", "ln": "ln_CD",
              "or": "or_IN", "uk": "uk_UA", "ml": "ml_IN", "sd": "sd_PK", "su": "su_ID", "ig": "ig_NG"}


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
            "text": t.fit(200), "title": title, "top": TOP_NETS}


# ---------- share UX v2 (docs/share-ux-v2-spec.md) ----------

TOP_NETS = ("x", "linkedin", "bluesky")  # the share row on desktop (§1); the rest sit under More
GRID_NETS = ("x", "bluesky")             # the result box (§3): the networks that take the grid's text as it is


def x_len(text: str) -> int:
    """X's weighted length: a URL is 23; code points outside X's light ranges (emoji, arrows, CJK) count 2."""
    def w(c: str) -> int:
        o = ord(c)
        return 1 if o <= 4351 or 8192 <= o <= 8205 or 8208 <= o <= 8223 or 8242 <= o <= 8247 else 2
    return sum(w(c) for c in re.sub(r"https?://\S+", "x" * 23, text))


def _shrink(fmt, part: str, n: int, weigh) -> str:
    """fmt(part), with part clipped until the whole fits n (never below 12 characters)."""
    t = fmt(part)
    if weigh(t) <= n:
        return t
    return fmt(clip(part, max(12, len(part) - (weigh(t) - n) - 1)))


def result_grid(r: dict, _) -> dict | None:
    """The Wordle-style result (§3): resolved rounds only, every value from the record, ranked like the page."""
    if r.get("state") != "resolved" or not r.get("winner"):
        return None
    rows = []
    for e in ranked(r["entries"]):
        if not e.get("outcome"):
            rows.append(f"⚪ {e['name']}")
        else:
            pnl = f" {signed(e['pnl'])}" if e.get("pnl") is not None else ""
            rows.append(f"{'🟢' if e.get('won') else '🔴'} {e['name']}{pnl}")
    return {"head": "AgentpitBench · " + _("Round {n}", n=r["round_id"]), "question": " ".join(r["question"].split()),
            "winner": r["winner"], "rows": rows}


def grid_text(g: dict, n: int | None = None, weigh=len) -> str:
    """The grid as text; within n (by weigh), the question is clipped first, never an agent line."""
    def fmt(q: str) -> str:
        return f"{g['head']}\n{q} → {g['winner']}\n" + "\n".join(g["rows"])
    return fmt(g["question"]) if n is None else _shrink(fmt, g["question"], n, weigh)


def grid_share(r: dict, url: str, _) -> dict | None:
    """The result box's links (§2, §3). X takes the URL as its own field (23 weighted characters + a space)."""
    g = result_grid(r, _)
    if not g:
        return None

    def q(params: dict) -> str:
        return urlencode(params, quote_via=quote)

    def u(net: str) -> str:
        return utm(url, net, "result")
    full, bsky = grid_text(g), u("bluesky")
    links = [
        ("x", "X", "https://x.com/intent/tweet?" + q({"text": grid_text(g, 280 - 24, x_len), "url": u("x")})),
        ("linkedin", "LinkedIn", "https://www.linkedin.com/sharing/share-offsite/?" + q({"url": u("linkedin")})),
        ("facebook", "Facebook", "https://www.facebook.com/sharer/sharer.php?" + q({"u": u("facebook")})),
        ("reddit", "Reddit", "https://www.reddit.com/submit?" + q({"url": u("reddit"), "title": _shrink(
            lambda x: f"{g['head']}: {x} → {g['winner']}", g["question"], 300, len)})),
        ("whatsapp", "WhatsApp", "https://wa.me/?" + q({"text": f"{full}\n{u('whatsapp')}"})),
        ("telegram", "Telegram", "https://t.me/share/url?" + q({"url": u("telegram"), "text": full})),
        ("bluesky", "Bluesky", "https://bsky.app/intent/compose?" + q({"text": f"{grid_text(g, 300 - len(bsky) - 1)}\n{bsky}"})),
        ("email", _("Email"), "mailto:?" + q({"subject": g["head"], "body": f"{full}\n\n{u('email')}"})),
    ]
    return {"links": links, "top": GRID_NETS, "text": full, "title": g["head"], "preview": full,
            "native_url": u("native"), "copy_url": u("copy"), "copy_text": f"{full}\n{u('copy')}"}


def _first_sentence(text: str | None) -> str:
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return clip(m.group(1) if m else text, 200)


def quote_text(name: str, question: str, words: str, n: int | None = None, weigh=len) -> str:
    """'{name} on "{question}": “{words}”', within n: the question is clipped first, then the quote."""
    def fmt(qq: str, ww: str) -> str:
        return f"{name} on \"{qq}\": “{ww}”"
    if n is None or weigh(fmt(question, words)) <= n:
        return fmt(question, words)
    t = _shrink(lambda qq: fmt(qq, words), question, n, weigh)
    if weigh(t) <= n:
        return t
    qq = clip(question, 12)
    return _shrink(lambda ww: fmt(qq, ww), words, n, weigh)


def quote_shares(s: Settings, r: dict, url: str) -> dict[str, dict]:
    """Quote cards (§4): per agent, its own words with its decision card. Never while the picks are sealed, never
    on an infra-voided round, and only when that agent's card exists. The question is the English one: the quote
    is English and the post goes to X or Bluesky."""
    if discover.seal(r) or r.get("exclusion") == "infra":
        return {}
    out = {}
    for e in r["entries"]:
        words = " ".join((e.get("quote") or "").split()) or _first_sentence(e.get("rationale"))
        card = f"cards/{r['round_id']}/decision-{e['agent']}.png"
        if e.get("sealed") or not e.get("outcome") or not words or not (s.cards_dir / card.removeprefix("cards/")).exists():
            continue
        bsky = utm(url, "bluesky", "quote")
        out[e["agent"]] = {
            "card": card, "name": f"agentpitbench-round-{r['round_id']}-{e['agent']}.png",
            "text": quote_text(e["name"], r["question"], words, 200), "url": utm(url, "native", "quote", "share-image"),
            "x": "https://x.com/intent/tweet?" + urlencode({"text": quote_text(e["name"], r["question"], words, 280 - 24, x_len),
                                                            "url": utm(url, "x", "quote")}, quote_via=quote),
            "bluesky": "https://bsky.app/intent/compose?" + urlencode(
                {"text": quote_text(e["name"], r["question"], words, 300 - len(bsky) - 1) + " " + bsky}, quote_via=quote)}
    return out


def film_share(f: dict, url: str, _) -> dict:
    """Film end screen (§2): the film's hook (built from the round record) and the round page."""
    return share_bar(ShareText(f.get("hook") or "Match of the Day"), "Match of the Day", url, "film", _)


EMBED_W = 560                                   # oEmbed width; the iframe itself is 100% wide (max 640 px)
EMBED_H = {"round": 400, "leaderboard": 300}    # the snippet's fixed height; /embed.js then fits the content


def embed_back(url: str, kind: str) -> str:
    """An embed's link back: utm_source=embed; utm_medium is the embedding host, set in the browser (iframe if unknown)."""
    return url + "?" + urlencode({"utm_source": "embed", "utm_medium": "iframe", "utm_campaign": "embed", "utm_content": kind})


def embed_snippet(base: str, src: str, title: str, kind: str) -> str:
    """The copy-paste code (docs/embed-spec.md): the iframe plus the optional resize loader."""
    from html import escape as h
    return (f'<iframe src="{h(src)}" title="{h(title)}" width="100%" height="{EMBED_H[kind]}" loading="lazy" '
            f'style="border:0;width:100%;max-width:640px" data-agentpitbench></iframe>'
            f'<script async src="{h(base)}/embed.js"></script>')


def oembed(base: str, src: str, title: str, kind: str) -> dict:
    """Static oEmbed 1.0 'rich' response for one embed (served as a file, found through the discovery link)."""
    return {"version": "1.0", "type": "rich", "provider_name": "AgentpitBench", "provider_url": f"{base}/",
            "title": title, "html": embed_snippet(base, src, title, kind), "width": EMBED_W, "height": EMBED_H[kind],
            "cache_age": 600}


def page_type(path: str) -> str:
    """utm_content of a page: its first folder (round, agent, ...), 'home' for the front page."""
    first = path.split("/", 1)[0]
    return "home" if first == "index.html" else first


def round_share(r: dict, _, vs: str) -> ShareText:
    """Resolved: the results tweet's own hook, so the two never drift. Open: the question and every pick
    (or, while the picks are sealed, that the AIs are still betting)."""
    if r["state"] in ("resolved", "void"):
        hook = r.get("share_hook") or results_hook(r)   # the posted A/B hook, if one posted
        # the market name leads (long-tail search); on a short network it is the part clipped first, the hook survives
        return ShareText(hook) if r["state"] == "void" else ShareText("", market_name(r), f": {hook}")
    if r.get("sealed") or not any(e.get("outcome") for e in r["entries"]):
        return flex_text(_, _("{question}: {vs} are placing their bets. Who's right?"), "question", market_name(r), vs=vs)
    picks = ", ".join(f"{e['name']} {e['outcome'] or _('no bet')}" for e in r["entries"])
    return flex_text(_, _("{question}: {picks}. Who's right?"), "question", market_name(r), picks=picks)


PICK_NETS = ("x", "bluesky", "whatsapp", "telegram", "reddit")  # the networks that take a prefilled text


def _end_ts(r: dict) -> float | None:
    try:
        return datetime.fromisoformat((r.get("end_date") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pick_share(r: dict, url: str, _, now: float | None = None) -> dict | None:
    """Share your pick (spec section 3), for a round still taking picks: one group of links per outcome.

    Only r["outcomes"] is used, never the entries: the visitor shares their own pick and nothing here can reveal
    the agents' picks while they are sealed. With an X opener (the reveal post, posted once every agent has
    picked), the X link replies to it: collect_humans reads picks from those replies, and parse_pick takes the
    outcome named first, so the reply starts with the outcome label verbatim (never passed through the
    translator, so no direction isolates either) and carries no question or link that could name another
    outcome first. Without an opener, X gets a normal post with the URL, like the other networks."""
    if r.get("state") not in ("running", "awaiting_resolution") or not r.get("outcomes"):
        return None
    end = _end_ts(r)
    if end is not None and (now or time.time()) >= end:  # replies after the close are not counted
        return None
    thread = r.get("thread_tweet_id") if tweet_url(r.get("thread_tweet_id")) else None
    mine, who = _("my pick."), _("Who's right, me or the AIs?")
    groups = []
    for o in r["outcomes"]:
        t = ShareText(f"{o}: {mine} ", r["question"], f" {who}")
        bar = share_bar(t, r["question"], url, "pick", _)
        links = [(net, label, href) for net, label, href in bar["links"] if net in PICK_NETS]
        if thread:
            reply = fit(f"{o}: ", f" {TAG}", f"{mine} {who}", flex_max=200)  # the label is never clipped
            links[0] = ("x", _("Reply on X"), "https://x.com/intent/tweet?" + urlencode(
                {"in_reply_to": thread, "text": reply}, quote_via=quote))
        groups.append({"outcome": o, "links": links, "text": bar["text"], "native_url": bar["native_url"]})
    return {"groups": groups, "thread": thread, "end": r.get("end_date") or ""}


def win_share(r: dict, url: str, _) -> list[dict]:
    """Share my win: for each human who beat at least one AI and has a card, the post and the card image."""
    beaten = [e["name"] for e in r.get("entries") or [] if e.get("outcome") and not e.get("won")]
    if r.get("state") != "resolved" or not beaten:
        return []
    names = beaten[0] if len(beaten) == 1 else _("{a} and {b}", a=", ".join(beaten[:-1]), b=beaten[-1])
    text = _("I beat {names} on AgentpitBench.", names=names)
    out = []
    for h in (r.get("humans") or {}).get("winners") or []:
        if not h.get("card"):
            continue
        out.append({"username": h["username"], "card": h["card"], "text": text, "url": utm(url, "native", "win", "share-image"),
                    "x": "https://x.com/intent/tweet?" + urlencode(
                        {"text": f"{text} {TAG}", "url": utm(url, "x", "win")}, quote_via=quote)})
    return out


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


# ---------- search (docs/seo-spec.md) ----------

BRAND = " · AgentpitBench"
TITLE_MAX = 60   # what Google shows; the brand goes last and is dropped first
ROUND_TITLE_MAX = 100  # round titles: the question is clipped first, the search words after it survive


def branded(title: str) -> str:
    return title + BRAND if len(title + BRAND) <= TITLE_MAX else title


def event_prefix(r: dict) -> str:
    """'Lille OSC vs. Stade Brestois 29 · ' when posts name the match in front of the question (longtail spec §6).
    On translated pages loc() sets it from the English record (translated like any market text)."""
    if "event_prefix" in r:
        return r["event_prefix"]
    name = r.get("market_name") or market_name(r)
    return name[: len(name) - len(" ".join((r.get("question") or "").split()))] if name != r.get("question") else ""


def round_title(r: dict, _, names: str, suffix: str = "") -> str:
    """Resolved: '{question}: which AI got it right?'; open: '{question}: AI predictions from …'. Never a pick."""
    q = event_prefix(r) + r["question"]
    if r["state"] == "resolved":
        t = flex_text(_, _("{question}: which AI got it right?"), "question", q)
    elif r["state"] == "void":
        t = flex_text(_, _("{question}: market voided"), "question", q)
    else:
        t = flex_text(_, _("{question}: AI predictions from {names}"), "question", q, names=names)
    return branded(ShareText(t.head, t.flex, t.tail + suffix).fit(ROUND_TITLE_MAX))


def _favourite(r: dict) -> tuple[str, float] | None:
    c = r.get("crowd") or {}
    if c.get("outcome") and c.get("price"):
        return c["outcome"], c["price"]
    ps = r.get("prices_at_start") or []
    if ps and len(ps) == len(r.get("outcomes") or []):
        i = max(range(len(ps)), key=ps.__getitem__)
        return r["outcomes"][i], ps[i]
    return None


def round_description(r: dict, _, suffix: str = "") -> str:
    """Search snippet of a round page. A running round's picks are sealed, so an open round never names one."""
    fav = _favourite(r)
    price = _("{outcome} at {price}", outcome=fav[0], price=cents(fav[1])) if fav else "—"
    if r["state"] == "resolved":
        right = [e["name"] for e in r["entries"] if e.get("won")]
        wrong = [e["name"] for e in r["entries"] if not e.get("won")]  # a no-bet counts as a loss
        if right and not wrong:
            d = _("Resolved {winner}. All {n} AIs got it right. The market's favourite at the start: {price}.",
                  winner=r["winner"], n=len(right), price=price)
        elif right:
            d = _("Resolved {winner}. Right: {right}. Wrong: {wrong}. The market's favourite at the start: {price}.",
                  winner=r["winner"], right=", ".join(right), wrong=", ".join(wrong) or "—", price=price)
        else:
            d = _("Resolved {winner}. No AI got it right. The market's favourite at the start: {price}.",
                  winner=r["winner"], price=price)
    elif r["state"] == "void":
        d = _("The market closed without a winner. No result this round.")
    elif discover.seal(r):
        d = _("Market favourite at the start: {price}. {n} AIs are placing their bets; the picks are revealed when all are in. Closes {date}.",
              price=price, n=len(r["entries"]), date=(r.get("end_date") or "")[:10] or "—")
    else:
        d = _("Market favourite at the start: {price}. See how {n} AIs bet before the market closes on {date}.",
              price=price, n=len(r["entries"]), date=(r.get("end_date") or "")[:10] or "—")
    pre = event_prefix(r)
    return (pre.removesuffix(" · ") + ". " if pre else "") + d + suffix


def card_alt(r: dict, _, names: str) -> str:
    if r["state"] == "resolved":
        return f"{results_headline(r, _)}: {r['question']}"
    return _("{question}: picks by {names}", question=r["question"], names=names)


def leaderboard_description(lb: dict, _, names: str, as_of: float) -> str:
    fc = lb.get("forecast") or {}
    scored = [a for a in fc.get("agents") or [] if a.get("brier") is not None and a.get("ci")]
    if scored and fc.get("markets_resolved"):
        a = min(scored, key=lambda x: x["brier"])
        return _("{leader} leads on forecast accuracy after {n} markets (Brier {score}, 95% CI {lo}–{hi}). Updated {date}.",
                 leader=a["name"], n=fc["markets_resolved"], score=f"{a['brier']:.3f}", lo=f"{a['ci'][0]:.3f}",
                 hi=f"{a['ci'][1]:.3f}", date=discover.day(as_of))
    return _("Forecast accuracy and betting record of {names} on real prediction markets, with confidence intervals.",
             names=names)


VS_MIN_N = 20  # no thin pages: a head-to-head page exists only with at least this many shared resolved markets


def vs_slug(a: str, b: str) -> str:
    return f"{AGENT_SLUGS.get(a, a)}-vs-{AGENT_SLUGS.get(b, b)}"


def vs_pairs(lb: dict, agents: list[str]) -> list[dict]:
    """The head-to-head pairs with a page: n >= VS_MIN_N, both sides still in the contest."""
    return [p for p in lb.get("pairs") or [] if p.get("n", 0) >= VS_MIN_N and p["a"] in agents
            and (p["b"] in agents or p["b"] == CROWD)]


def side_name(x: str, _) -> str:
    return _("the Crowd") if x == CROWD else AGENT_NAMES.get(x, x)


def vs_title(p: dict, _) -> str:
    if p["b"] == CROWD:
        return _("{a} vs the Crowd: who forecasts better?", a=AGENT_NAMES.get(p["a"], p["a"]))
    return _("{a} vs {b}: who forecasts better?", a=AGENT_NAMES.get(p["a"], p["a"]), b=AGENT_NAMES.get(p["b"], p["b"]))


def vs_description(p: dict, _, as_of: float) -> str:
    return _("{a} vs {b} on the same {n} markets: Brier {x} vs {y} (lower is better); {a} closer on {w}, {b} on {l}. Updated {date}.",
             a=AGENT_NAMES.get(p["a"], p["a"]), b=side_name(p["b"], _), n=p["n"], x=f"{p['a_brier']:.3f}",
             y=f"{p['b_brier']:.3f}", w=p["a_closer"], l=p["b_closer"], date=discover.day(as_of))


def home_poster(s: Settings, film: dict) -> str | None:
    """A small JPEG of the film's poster for the front page (the film's own poster is a full-size PNG; the home
    page stays light): cards/motd/<date>/poster-home.jpg, made once. None if the film has no poster."""
    return _jpeg_poster(s, film, "poster-home.jpg", 960, 72)


def web_poster(s: Settings, film: dict) -> str | None:
    """The poster the video players on /films/ and round pages show: the PNG (1-2 MB, fetched even with
    preload="none") as a full-size JPEG of about a sixth of the weight, poster-web.jpg beside it, made once."""
    return _jpeg_poster(s, film, "poster-web.jpg", 1920, 85)


def _jpeg_poster(s: Settings, film: dict, name: str, box: int, quality: int) -> str | None:
    if not film.get("poster"):
        return None
    src = s.cards_dir / film["dir"].removeprefix("cards/") / film["poster"]
    dst = src.with_name(name)
    try:
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            from PIL import Image
            im = Image.open(src).convert("RGB")
            im.thumbnail((box, box))
            im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)
        return film["dir"] + dst.name
    except Exception:
        log.exception("%s for %s skipped", name, film.get("date") or film.get("week"))
        return film["dir"] + film["poster"]


def home_pairs(vsp: list[dict], n: int = 4) -> list[dict]:
    """The front page's head-to-head strip: AI-vs-AI pairs first (most shared markets first), then vs the Crowd."""
    return sorted(vsp, key=lambda p: (p["b"] == CROWD, -p["n"]))[:n]


def vs_rounds(rounds: list[dict], a: str, b: str) -> dict:
    """Betting rounds where a and b (an agent or the Crowd's favourite) took opposite sides: W-L and the latest 10."""
    out, aw, bw = [], 0, 0
    for r in sorted(rounds, key=lambda r: -(r.get("started_at") or 0)):
        if discover.seal(r) or r.get("exhibition"):
            continue
        ea = next((e for e in r["entries"] if e["agent"] == a), None)
        pb = ((r.get("crowd") or {}).get("outcome") if b == CROWD
              else next((e["outcome"] for e in r["entries"] if e["agent"] == b), None))
        if not (ea and ea.get("outcome") and pb) or ea["outcome"] == pb:
            continue
        if r["state"] == "resolved":
            aw += ea["outcome"] == r["winner"]
            bw += pb == r["winner"]
        out.append({"r": r, "a": ea["outcome"], "b": pb})
    return {"a_wins": aw, "b_wins": bw, "latest": out[:10]}


def check_seo(seen: list[tuple[str, str, str, str]]) -> None:
    """The fixed page types (not rounds or weekly reports, which are data) must each have their own non-empty title
    and description within a language. A clash here is a template bug, so it fails the build."""
    bad = []
    fixed = [x for x in seen if not x[1].startswith(("round/", "reports/2", "widget/", "embed/"))]
    for lang in {x[0] for x in fixed}:
        rows = [x for x in fixed if x[0] == lang]
        for i, field in ((2, "title"), (3, "description")):
            vals = [x[i] for x in rows]
            bad += [f"{lang}/{x[1]}: empty {field}" for x in rows if not (x[i] or "").strip()]
            bad += [f"{lang}/{x[1]}: duplicate {field} {x[i]!r}" for x in rows if x[i] and vals.count(x[i]) > 1]
    if bad:
        raise RuntimeError(f"{len(bad)} SEO problem(s): " + "; ".join(sorted(bad)[:10]))


def round_suffixes(rounds: list[dict], _) -> dict[int, str]:
    """' · round {n}' for every round whose question another round shares (daily markets, summon repeats)."""
    seen: dict[str, int] = {}
    for r in rounds:
        seen[r["question"]] = seen.get(r["question"], 0) + 1
    return {r["round_id"]: (" · " + _("round {n}", n=r["round_id"]) if seen[r["question"]] > 1 else "") for r in rounds}


def indexnow_key(s: Settings) -> str | None:
    """IndexNow's key (public by design, served at /<key>.txt): generated once when IndexNow is turned on."""
    if not s.indexnow_enabled:
        return None
    f = s.data_dir / "indexnow-key.txt"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(16))
    return f.read_text().strip()


INDEXNOW_MAX = 100


def indexnow_changes(s: Settings) -> list[str]:
    """Pages worth announcing since the last ping: rounds that are new or changed state, and with any of them the
    leaderboard and the home page (every page's footer timestamp changes on each build, so a file diff can't tell).
    The round states last announced live in data_dir/indexnow-state.json."""
    f = s.export_dir / "rounds.json"
    rounds = json.loads(f.read_text()) if f.exists() else []
    state_f = s.data_dir / "indexnow-state.json"
    try:
        old = json.loads(state_f.read_text())
    except (OSError, ValueError):
        old = {}
    now = {str(r["round_id"]): r["state"] for r in rounds if not (s.site_hide_void and r["state"] == "void")}
    fresh = [f"round/{rid}/" for rid, st in sorted(now.items(), key=lambda x: -int(x[0])) if old.get(rid) != st]
    state_f.parent.mkdir(parents=True, exist_ok=True)
    state_f.write_text(json.dumps(now))
    return ["", "leaderboard/", *fresh] if fresh else []


def indexnow_payload(s: Settings, key: str, rels: list[str]) -> dict | None:
    """IndexNow's JSON body for English page paths ('' = home, 'round/7/'), at most 100 URLs."""
    base = s.site_url.rstrip("/")
    urls = [f"{base}/{rel}" for rel in rels][:INDEXNOW_MAX]
    if not urls:
        return None
    return {"host": urlparse(base).hostname, "key": key, "keyLocation": f"{base}/{key}.txt", "urlList": urls}


def indexnow_ping(s: Settings, rels: list[str]) -> bool:
    """One POST per deploy; a failure is logged and never retried."""
    key = indexnow_key(s)
    body = indexnow_payload(s, key, rels) if key else None
    if not body:
        return False
    try:
        req = urllib.request.Request("https://api.indexnow.org/indexnow", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            log.info("IndexNow: %d URLs, HTTP %s", len(body["urlList"]), resp.status)
        return True
    except Exception as e:
        log.warning("IndexNow ping failed: %s", e)
        return False


SHARE_BAR = 'data-share-bar'  # every public page carries it (checked at build)
OG_REQUIRED = ('property="og:title"', 'property="og:description"', 'property="og:image"', SHARE_BAR,
               'data-share-top')  # share UX v2: a share control in the first screen
NO_SHARE = ("widget/", "embed/", "404.html", "go/")  # embedded elsewhere, the not-found page, or the A/B redirect hop: no bar, no preview


GO_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AgentpitBench → agentpit</title>
<noscript><meta http-equiv="refresh" content="0;url={dest_attr}"></noscript>{beacon}
<script>(function(){{var d={dest_js},done=false;function go(){{if(done)return;done=true;location.replace(d)}}
function soon(){{setTimeout(go,300)}}if(document.readyState==="complete")soon();else addEventListener("load",soon);
setTimeout(go,1500)}})();</script></head>
<body style="font:16px system-ui;background:#0b1020;color:#e8ecf8;padding:24px"><p><a style="color:#9ecbff" href="{dest_attr}">Continue to the market →</a></p></body></html>
"""


def go_page(s: Settings, dest: str) -> str:
    """The /go/ hop of the A/B site CTA: the Cloudflare beacon records the view (on load; it reports with
    navigator.sendBeacon, which survives the navigation), then the page redirects to the unchanged destination
    300 ms after load (1.5 s at the latest, if the beacon is blocked). Without JS: an instant meta refresh."""
    from html import escape as h
    beacon = (f'\n<script defer type="module" src="https://static.cloudflareinsights.com/beacon.min.js" '
              f'data-cf-beacon=\'{{"token": "{s.cf_analytics_token}"}}\'></script>' if s.cf_analytics_token else "")
    return GO_HTML.format(dest_attr=h(dest, quote=True), dest_js=json.dumps(dest).replace("</", "<\\/"), beacon=beacon)


def site_cta(s: Settings) -> dict | None:
    """The day's site.round_cta arm (English round pages): {arm, text, arms}; None without a database."""
    if not s.db_path.exists():
        return None
    try:
        from . import ab
        from .db import DB as _DB
        db = _DB(s.db_path)
        ab.seed(db)
        key = f"site-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        c = ab.choose(db, s, "site.round_cta", key, kind="site")
        text = c.text or ab.CONTROL_TEXT["site.round_cta"]
        ab.realize(db, key, "site.round_cta", c.arm_id, text)
        return {"arm": c.arm_id, "text": None if c.is_control else c.text,
                "arms": [a["arm_id"] for a in ab.arms(db, "site.round_cta", None)]}
    except Exception:
        log.exception("site CTA arm failed; English round pages use the control")
        return None


def experiments_data(s: Settings) -> dict | None:
    if not s.db_path.exists():
        return None
    try:
        from . import ab_report
        from .db import DB as _DB
        return ab_report.experiments(_DB(s.db_path), s)
    except Exception:
        log.exception("experiments data failed")
        return None


def check_pages(out: Path) -> None:
    """Acceptance check: every public page has a title, description and image preview, and the share bar."""
    bad = []
    for f in out.rglob("*.html"):
        rel = f.relative_to(out).as_posix()
        if (rel.startswith(("cards/", "data/", "transcripts/")) or any(f"{x}" in rel for x in NO_SHARE)
                or re.fullmatch(r"google[\w-]*\.html", rel)):  # Search Console's verification file
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


def streak(rounds: list[dict], agent: str) -> dict:
    """An AI's runs over resolved rounds in resolution order (docs/growth-overnight-spec.md §2): `cur` > 0 is the
    current run of wins, < 0 the current run of rounds not won (a no-bet counts as a loss, the rule of the betting
    table and of the scoreboard's streak in `db.leaderboard`, so the two agree). `longest` is the longest run of
    wins (the agent page's figure). Only resolved rounds count, so a sealed pick never reaches it."""
    longest = cur = 0
    for r in sorted((r for r in rounds if r["state"] == "resolved"), key=lambda r: r["resolved_at"] or 0):
        e = next((e for e in r["entries"] if e["agent"] == agent), None)
        if e is None:
            continue
        cur = (max(cur, 0) + 1) if e["won"] else (min(cur, 0) - 1)
        longest = max(longest, cur)
    return {"agent": agent, "cur": cur, "longest": longest}


def streaks(rounds: list[dict], agents: list[str]) -> list[dict] | None:
    """'Who's on fire': every AI's streak, hottest first; None while no AI has a current run of 2 or more."""
    rows = [streak(rounds, a) for a in agents]
    if not any(abs(x["cur"]) >= 2 for x in rows):
        return None
    return sorted(rows, key=lambda x: (-x["cur"], -x["longest"]))


CATEGORY_MIN = 10  # resolved rounds a category needs before its page names a leader (§1)
CATEGORY_SLUGS = {"Sports": "sports", "Crypto": "crypto", "Politics": "politics", "Economy": "economy",
                  "Esports": "esports"}  # the categories with a page; "Other" has none


def category_pages(rounds: list[dict], lb: dict, agents: list[str]) -> list[dict]:
    """Per market category (§1): the table pivoted from the export's all-time `by_category` (never recomputed),
    ranked like the betting table (profit, then win rate); the resolved rounds of that category, newest first;
    the leader, named only from CATEGORY_MIN rounds and a strict lead. `rounds` must be the counted rounds."""
    by = lb.get("by_category") or {}
    out = []
    for cat in [*CATEGORY_SLUGS, "Other"]:
        rows = [{"agent": a, "name": AGENT_NAMES.get(a, a), **c} for a in agents for c in by.get(a, [])
                if c["category"] == cat]
        rows.sort(key=lambda x: (-x["net_pnl"], -x["win_rate"]))
        rs = sorted((r for r in rounds if r["state"] == "resolved" and not r.get("exhibition")
                     and category(r["question"]) == cat), key=lambda r: -(r["resolved_at"] or 0))
        n = len(rs)
        leader = rows[0] if (n >= CATEGORY_MIN and len(rows) > 1 and rows[0]["net_pnl"] > rows[1]["net_pnl"]) else None
        out.append({"category": cat, "slug": CATEGORY_SLUGS.get(cat), "rows": rows, "rounds": rs[:30], "n": n,
                    "leader": leader, "streaks": streaks(rs, agents) if n >= CATEGORY_MIN else None})
    return out


def agent_stats(rounds: list[dict], agent: str) -> dict:
    picks = [(r, e) for r, e in _flat(rounds) if e["agent"] == agent]
    resolved = sorted([(r, e) for r, e in picks if r["state"] == "resolved"], key=lambda x: x[0]["resolved_at"] or 0)
    longest = streak(rounds, agent)["longest"]
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


# Methodology changelog: (date, message id, values), newest first, read from methodology.json (the lock) at
# every build: the weekly selection review can add a version while the service runs. Generated entries
# (automatic weekly review, operator revert) store one of these templates as their id, filled with the change.
CHANGELOG_TEMPLATES = (
    _("Market selection: {param} {old} → {new} (automatic weekly review; evidence: {link})"),
    _("Market selection: {param} {old} → {new} (operator revert of v{v}; evidence: {link})"),
)
assert CHANGELOG_TEMPLATES == (methodology.AUTO_TEXT, methodology.REVERT_TEXT)

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
           "hall-of-shame": _("Hall of shame"), "reports": _("Weekly reports"), "contrarian": _("Contrarian calls"),
           "experiments": _("Experiments"), "challenge": _("Beat the AIs"), "best-at": _("Which AI is best at what?")}

# Extra top-nav links, after Films and Head to head: (path under the language root, English label passed
# through _()). The slot for sections that land later, e.g. ("challenge/", "Beat the AIs"); the label must be a
# message id with translations (i18n.message_ids reads this list) before it goes in.
NAV_EXTRA: list[tuple[str, str]] = [("challenge/", "Beat the AIs")]

REASONING = {"max": "max reasoning", "xhigh": "extra-high reasoning", "high": "high reasoning"}


def model_line(agent: str, _, eras: dict | None = None) -> str:
    """'Fable 5.1 · max reasoning' from config.AGENT_MODELS, so a model swap is one edit there. With `eras`
    (cards.model_eras) a record that spans several models says so: '… (since 12 Oct; earlier rounds on GPT-6 Luna)'."""
    if agent not in AGENT_MODELS:
        return ""
    _id, name, effort = AGENT_MODELS[agent]
    label = {"max": _("max reasoning"), "xhigh": _("extra-high reasoning"),
             "high": _("high reasoning")}.get(effort, effort)
    line = f"{name} · {label}"
    if eras and eras.get("earlier"):
        others = ", ".join(eras["earlier"])
        if eras.get("since"):
            when = datetime.fromtimestamp(eras["since"], timezone.utc)
            day = when.strftime("%-d %b") if _.lang == "en" else when.strftime("%Y-%m-%d")
            note = _("(since {date}; earlier rounds on {models})", date=day, models=others)
        else:
            note = _("(not played yet; rounds so far on {models})", models=others)
        line += f" {note}"
    return line


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


def market_text(mt: dict[str, str]):
    """Per-language market wording from the market_i18n table (empty for English: pages unchanged).
    tq: plain translated text (falls back to English). mq: the translation with the exact English source in a
    title attribute, so the market wording stays auditable. loc: a round dict with translated question and
    labels, for page titles, descriptions, alt text and share text only (never data attributes, JSON-LD, API)."""
    def tq(t):
        return mt.get(t, t) if isinstance(t, str) else t

    def mq(t):
        if isinstance(t, str) and mt.get(t) and mt[t] != t:
            return Markup('<span class="mt" title="{}">{}</span>').format(t, mt[t])
        return t

    def loc(r: dict) -> dict:
        if not mt:
            return r
        crowd = r.get("crowd") or {}
        pre = event_prefix(r)
        return dict(r, question=tq(r["question"]), winner=tq(r.get("winner")),
                    event_prefix=tq(pre.removesuffix(" · ")) + " · " if pre else "",
                    outcomes=[tq(o) for o in r.get("outcomes") or []],
                    crowd=dict(crowd, outcome=tq(crowd.get("outcome"))) if crowd else crowd)
    return tq, mq, loc


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
    changelog = methodology.changelog()
    # the evidence behind every generated methodology change (/methodology/changes/<version>.json)
    if methodology.changes_dir(s).exists():
        for f in methodology.changes_dir(s).glob("*.json"):
            dst = out / "methodology" / "changes" / f.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)

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
    fire = streaks(counted, s.agents)               # "Who's on fire" (docs/growth-overnight-spec.md §2)
    cat_pages = category_pages(counted, lb, s.agents)  # /best-at/ (§1)
    cat_live = [c for c in cat_pages if c["slug"] and c["n"]]  # a category gets its page once it has a round
    # an infra-voided round's posts were taken down, so its cards are not shown either
    # nor a running round's: its cards are not published (they would reveal the sealed picks)
    cards = {r["round_id"]: (None if r.get("exclusion") == "infra" or discover.seal(r) else card_for(s, r))
             for r in rounds}
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
    cta = site_cta(s)                # A/B: the day's round-page CTA arm (English pages only)
    exp_ab = experiments_data(s)     # A/B: the public /experiments/ numbers (posts older than 7 days)
    vsp = vs_pairs(lb, s.agents)
    vs_links = {(p["a"], p["b"]): f"vs/{vs_slug(p['a'], p['b'])}/" for p in vsp}
    seo: list[tuple[str, str, str, str]] = []  # (lang, page, title, description), checked after the build
    from .motd import films as _films  # Match of the Day: cards/motd/<date>/film.json
    films = _films(s)
    from .week import episodes as _episodes  # Week in the Pit: cards/week/<week>/episode.json
    episodes = _episodes(s)[:4]
    cap_hit = _reply_caps(s, rounds)  # "first N picks counted"
    # Beat the AIs (docs/daily-challenge-spec.md): chosen from every round, void ones too, so a featured round that
    # later voids stays featured; a sealed round's picks never reach the page or data/challenge.json
    ch_days = challenge.days(all_rounds)
    page_ids = {r["round_id"] for r in rounds}
    eras = {a: model_eras(rounds, a) for a in s.agents}  # the models each record was played on
    if films:
        films[0]["home_poster"] = home_poster(s, films[0])
    for f in films + episodes:
        f["poster_web"] = web_poster(s, f)
    film_of = {}
    for f in films:  # newest first: a round filmed twice shows its latest film
        film_of.setdefault(f["round_id"], f)

    n_resolved = sum(1 for r in all_rounds if discover.counts(r))  # the embeds' credibility line
    for lang in CODES:
        _ = Translator(lang)
        r_embeds: dict[int, dict] = {}
        tq, mq, loc = market_text(extra["mt"].get(lang, {}) if lang != "en" else {})
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
                  "commentary": commentary, "model_line": lambda a, _=_: model_line(a, _, eras.get(a)),
                  "models": AGENT_MODELS, "reasoning": REASONING, "exclusions": EXCLUSION_REASONS,
                  "fc": lb.get("forecast") or {}, "sig": lb.get("significance") or {},
                  "tq": tq, "mq": mq}
        common["vs_links"] = vs_links
        common["cat_links"] = {c["category"]: f"best-at/{c['slug']}/" for c in cat_live}
        common["cat_min"] = CATEGORY_MIN
        common.update(nav_extra=NAV_EXTRA, has_experiments=exp_ab is not None)
        prefix = _.prefix()
        common["film_share"] = lambda f, _=_, prefix=prefix: film_share(f, f"{base}/{prefix}round/{f['round_id']}/", _)
        suffixes = round_suffixes(rounds, _)
        blurb = _("{vs}, each on its lab's top model at maximum reasoning, bet 100 tokens on live prediction markets. Every bet, rationale and result is public.", vs=vs)

        def page(path: str, tpl: str, **ctx) -> None:
            depth = (prefix + path).count("/")
            root = "../" * depth                     # site root: shared assets, data, feeds
            rel = path.removesuffix("index.html")    # this page, relative to a language root
            ctx.setdefault("canonical", f"{base}/{prefix}{rel}")
            kind = page_type(path)
            text = ctx.pop("share_text", None) or ctx["title"]
            if "description" not in ctx:  # the share text describes the page; plain titles keep the site blurb
                ctx["description"] = None if text is ctx["title"] else (text.full if isinstance(text, ShareText) else text)
            ctx["description"] = ctx["description"] or blurb
            seo.append((lang, rel, ctx["title"], ctx["description"]))
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

        def crumbs(ld: dict | None, *items: tuple[str, str]) -> dict:
            """Adds a BreadcrumbList (Home › … › this page) to a page's JSON-LD graph."""
            trail = [(_("Home"), f"{base}/{prefix}"), *((nm, f"{base}/{prefix}{rel}") for nm, rel in items)]
            node = discover.breadcrumb_ld(trail)
            if ld is None:
                return {"@context": "https://schema.org", "@graph": [node]}
            return {**ld, "@graph": [*ld["@graph"], node]}

        def embed(kind: str, rel: str, oembed_rel: str, full_url: str, tpl: str, **ctx) -> dict:
            """An embed (docs/embed-spec.md): the standalone page embed/<rel>, its static oEmbed JSON, and the
            snippet for the share bar. No base.html, no beacon, no share bar."""
            title = _("What the AIs think about this market") if kind == "round" else _("Leaderboard") + " · AgentpitBench"
            path, src = f"embed/{rel}index.html", f"{base}/{prefix}embed/{rel}"
            o_url = f"{base}/{prefix}oembed/{oembed_rel}"
            root = "../" * (prefix + path).count("/")
            html = env.get_template(tpl).render(**common, root=root, home=root + prefix, rel=f"embed/{rel}", kind=kind,
                                                title=title, full_url=full_url, back_url=embed_back(full_url, kind),
                                                oembed=o_url, n_resolved=n_resolved, **ctx)
            for f, body in ((out / prefix / path, html),
                            (out / prefix / "oembed" / oembed_rel,
                             json.dumps(oembed(base, src, title, kind), indent=1, ensure_ascii=False))):
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(body)
            return {"code": embed_snippet(base, src, title, kind), "oembed": o_url}

        lb_embed = embed("leaderboard", "leaderboard/", "leaderboard.json", f"{base}/{prefix}leaderboard/", "embed.html",
                         poll=f"{'../' * (prefix.count('/') + 2)}{discover.API}/leaderboard.json",
                         state_sig=discover.iso(as_of), every=300000)

        page("index.html", "index.html", live=live, past=past[:30], upset=upset, description=None,
             film=films[0] if films else None, h2h_strip=home_pairs(vsp), fire=fire,
             today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
             share_text=_("Four frontier AIs bet on real prediction markets. Live scoreboard:"),
             jsonld=discover.dataset_ld(s, lb, f"{base}/"),
             title=_("AgentpitBench: {vs} on real prediction markets", vs=vs))
        page("leaderboard/index.html", "leaderboard.html", ci_chart=ci_bar((lb.get("forecast") or {}).get("agents", []),
                                                                          (lb.get("forecast") or {}).get("crowd")),
             chart=pnl_chart(lb.get("series", {}), crowd=(lb.get("crowd") or {}).get("series"), _=_),
             best=best, worst=worst, h2h=h2h, weekly=weekly, seasons=seasons, fire=fire,
             title=branded(_("AI forecasting leaderboard: {names}", names=", ".join(names))),
             description=leaderboard_description(lb, _, ", ".join(names), as_of),
             share_text=leaderboard_share(lb, _), og=previews["standings"],
             img=share_image(previews["standings_card"], "standings", leaderboard_share(lb, _),
                             f"{base}/{prefix}leaderboard/", "leaderboard"),
             img_url=utm(f"{base}/{prefix}leaderboard/", "native", "leaderboard", "share-image"),
             embed=lb_embed["code"], oembed=lb_embed["oembed"],
             jsonld=discover.dataset_ld(s, lb, f"{base}/leaderboard/"))
        page("facts/index.html", "facts.html", sections=discover.fact_sections(s, lb, metrics, _),
             bibtex=discover.bibtex(s, as_of), as_of=discover.stamp(as_of),
             description=_("Dated, quotable facts about AgentpitBench: forecast accuracy, betting record and methodology, regenerated on every update."),
             jsonld=discover.dataset_ld(s, lb, f"{base}/facts/"), title=branded(_("Facts")))
        by_id = sorted(rounds, key=lambda r: r["round_id"])
        for i, r in enumerate(by_id):
            card, rid, sfx = cards[r["round_id"]], r["round_id"], suffixes[r["round_id"]]
            cat = category(r["question"])
            same = [x for x in sorted(rounds, key=lambda x: -(x.get("started_at") or 0))
                    if x is not r and category(x["question"]) == cat][:3]
            rurl = f"{base}/{prefix}round/{rid}/"
            lr = loc(r)  # translated question/outcome labels for the page's own text; data stays English
            ar = discover.api_round(r, base, rurl)  # the public, sealed-safe shape: no pick before the reveal
            open_r = r["state"] in ("running", "awaiting_resolution")
            r_embeds[rid] = embed("round", f"round/{rid}/", f"round/{rid}.json", rurl, "embed_round.html", r=ar,
                                  poll=f"{'../' * (prefix.count('/') + 3)}{discover.API}/rounds/{rid}.json" if open_r else None,
                                  state_sig=f"{ar['state']}|{'true' if ar['sealed'] else 'false'}", every=60000)
            page(f"round/{rid}/index.html", "round.html", r=r, card=card, card_alt=card_alt(lr, _, ", ".join(names)),
                 film=film_of.get(rid), cta=cta if lang == "en" else None,
                 picks=pick_share(r, rurl, _), wins=win_share(r, rurl, _), cap_hit=cap_hit.get(rid),
                 grid=grid_share(r, rurl, _), qshare=quote_shares(s, r, rurl),
                 img=share_image(card, f"round-{rid}", round_share(lr, _, vs), rurl, "round"),
                 og=previews["rounds"].get(rid), share_text=round_share(lr, _, vs),
                 prev_r=by_id[i - 1] if i else None, next_r=by_id[i + 1] if i + 1 < len(by_id) else None,
                 cat=cat, same_cat=same,
                 lastmod=r["resolved_at"] or r["started_at"],
                 jsonld=crumbs(discover.round_ld(s, r, f"{base}/round/{rid}/"),
                               (_("Round {n}", n=rid), f"round/{rid}/")),
                 embed=r_embeds[rid]["code"], oembed=r_embeds[rid]["oembed"],
                 title=round_title(lr, _, ", ".join(names), sfx), description=round_description(lr, _, sfx))
        # the docs page's live example: the newest open round, else the newest round
        ex_r = max(by_id, key=lambda x: (x["state"] in ("running", "awaiting_resolution"), x["round_id"]), default=None)
        examples = ([{"heading": _("Round {n}", n=ex_r["round_id"]), **r_embeds[ex_r["round_id"]]}] if ex_r else []) + \
            [{"heading": _("Leaderboard"), **lb_embed}]
        page("embed/index.html", "embed_docs.html", examples=examples, title=_("Embed AgentpitBench"),
             description=_("Put the AIs' live picks on a market, or the leaderboard, on your site, blog or newsletter. Each embed updates itself from the public data, sets no cookies, tracks no one and keeps every pick sealed until all four AIs have bet."))
        for a in s.agents:
            row = next((b for b in board if b["agent"] == a), None)
            nm = AGENT_NAMES.get(a, a)
            page(f"agent/{a}/index.html", "agent.html", agent=a, name=nm,
                 row=row, st=stats[a], og=previews["agents"].get(a),
                 img=share_image(previews["agent_cards"].get(a), f"agent-{a}",
                                 agent_share(lb, a, nm, row, _), f"{base}/{prefix}agent/{a}/", "agent"),
                 share_text=agent_share(lb, a, nm, row, _),
                 cats=(lb.get("by_category") or {}).get(a, []),
                 vs_mine=[p for p in vsp if a in (p["a"], p["b"])],
                 jsonld=crumbs(None, (_("Leaderboard"), "leaderboard/"), (nm, f"agent/{a}/")),
                 title=branded(_("{name} ({model}) forecasting record", name=nm,
                                 model=AGENT_MODELS[a][1] if a in AGENT_MODELS else a)))
        for p in vsp:
            rel = f"vs/{vs_slug(p['a'], p['b'])}/"
            page(rel + "index.html", "vs.html", p=p, a_name=AGENT_NAMES.get(p["a"], p["a"]), b_name=side_name(p["b"], _),
                 bets=vs_rounds(counted, p["a"], p["b"]), as_of_day=discover.day(as_of),
                 heading=vs_title(p, _), title=branded(vs_title(p, _)), description=vs_description(p, _, as_of),
                 jsonld=crumbs(discover.dataset_ld(s, lb, f"{base}/{rel}"), (_("Leaderboard"), "leaderboard/"),
                               (vs_title(p, _), rel)))
        bt = _("Which AI is best at what?")
        page("best-at/index.html", "best_at_index.html", cats=cat_pages, as_of_day=discover.day(as_of),
             heading=bt, title=branded(bt),
             description=_("Each AI's all-time record by market category: rounds won and profit/loss, with how many markets each table rests on."),
             jsonld=crumbs(None, (_("Leaderboard"), "leaderboard/"), (bt, "best-at/")))
        for c in cat_live:
            rel, cn = f"best-at/{c['slug']}/", _(c["category"])
            ct = _("{category}: which AI bets best?", category=cn)
            page(rel + "index.html", "best_at.html", c=c, as_of_day=discover.day(as_of), heading=ct, title=branded(ct),
                 lastmod=max((r["resolved_at"] or 0 for r in c["rounds"]), default=0) or None,
                 description=_("{category} markets on AgentpitBench: each AI's wins and profit/loss, and every round, as of {date}.",
                               category=cn, date=discover.day(as_of)),
                 jsonld=crumbs(None, (_("Leaderboard"), "leaderboard/"), (bt, "best-at/"), (ct, rel)))
        ch = [challenge.entry(d, tq) for d in ch_days]
        curl = f"{base}/{prefix}challenge/"
        page("challenge/index.html", "challenge.html", ch=ch, feat=ch[0] if ch else None, round_pages=page_ids,
             picks=pick_share(ch_days[0]["round"], curl, _) if ch_days else None, cap_hit=None,
             title=branded(_("Beat the AIs")),
             description=_("One market a day. Lock in your pick before you see how the AIs bet, then compare once it resolves."),
             share_text=_("Can you beat the AIs? Today's AgentpitBench challenge:"))
        if vsp:
            page("vs/index.html", "vs_index.html", pairs=vsp, as_of_day=discover.day(as_of),
                 title=branded(_("Head to head")),
                 description=_("Who forecasts better, pair by pair: each side's Brier score on the same markets (lower is better), with a 95% confidence interval on the difference."),
                 jsonld=crumbs(None, (_("Leaderboard"), "leaderboard/"), (_("Head to head"), "vs/")))
        page("films/index.html", "films.html", films=films, episodes=episodes, title=branded("Match of the Day"),
             description=_("Match of the Day: a short film of the most dramatic AgentpitBench round each day."))
        page("splits/index.html", "splits.html", splits=splits, h2h=h2h, title=branded(_("Split decisions")),
             share_text=_("When {roster} disagree, who's right?", roster=roster))
        page("hall-of-shame/index.html", "shame.html", wrong=wrong, title=branded(_("Hall of shame")),
             share_text=_("The AI hall of shame: most confident wrong calls on AgentpitBench."))
        page("data/index.html", "data.html", prompt=prompt, title=branded(_("Data & method")),
             description=_("Download every AgentpitBench bet, forecast and transcript (CSV and JSON, CC BY 4.0), with the exact prompts the AIs receive."))
        page("press/index.html", "press.html", stats=press_stats, latest_cards=latest_cards,
             title=branded(_("Press kit")),
             description=_("AgentpitBench press kit: key numbers, share cards, mascots and contacts for writing about the AI forecasting benchmark."))
        page("methodology/index.html", "methodology.html", prompt=prompt, sweep_prompt=sweep_prompt_example(s),
             changelog=changelog, changelog_ids=methodology.changelog_anchors(),
             sealed=lb.get("sealed") or [], title=branded(_("Methodology v{v}", v=s.methodology_version)),
             share_text=_("How AgentpitBench tests frontier AIs on real markets (methodology v{v}).", v=s.methodology_version),
             jsonld=discover.techarticle_ld(s, lb, f"{base}/methodology/", changelog))
        page("independence/index.html", "independence.html", title=branded(_("Independence & conflicts")),
             share_text=_("How AgentpitBench tests frontier AIs on real markets (methodology v{v}).", v=s.methodology_version),
             description=_("Who runs AgentpitBench, how it stays independent of the markets it bets on, and the conflicts we disclose."))
        if exp_ab is not None:
            page("experiments/index.html", "experiments.html", ab=exp_ab, title=branded(_("Experiments")),
                 share_text=_("How AgentpitBench tests the wording of its posts. Facts are never varied."))
        page("incidents/index.html", "incidents.html", incidents=extra["incidents"] + INCIDENTS, title=branded(_("Incidents")),
             share_text=_("AgentpitBench publishes its own incidents. Here's the log."))
        page("contrarian/index.html", "contrarian.html", calls=lb.get("contrarian") or [],
             title=branded(_("Contrarian calls")), share_text=contrarian_share(lb.get("contrarian") or [], _),
             description=contrarian_share(lb.get("contrarian") or [], _)
             or _("Every time an AI forecast strayed far from the market price, and whether it was right."),
             og=previews["contrarian"])
        page("reports/index.html", "reports.html", reports=reports, title=branded(_("Weekly reports")),
             share_text=report_headline(reports[0], _) if reports else None,
             description=_("State of the AIs: a weekly report on how {names} forecast and bet.", names=", ".join(names)))
        for rep in reports:
            page(f"reports/{rep['week']}/index.html", "report.html", rep=rep, share_text=report_headline(rep, _),
                 description=report_headline(rep, _) or _("State of the AIs, {week}.", week=rep["week"]),
                 og=previews["weeks"].get(rep["week"]),
                 chart=ci_bar([dict(a, ci=[a["brier"], a["brier"]]) for a in rep["forecast"]],
                              {"brier": rep["crowd_brier"]} if rep.get("crowd_brier") is not None else None),
                 title=branded(_("State of the AIs") + f" · {rep['week']}"))
        if lang == "en":  # embedded elsewhere; one shared copy
            page("widget/index.html", "widget.html", title="AgentpitBench standings")

    if cta:  # the A/B CTA hop: one static redirect per (arm, round), so a click on any arm's link is counted
        for arm in cta["arms"]:
            for r in rounds:
                f = out / "go" / "round-cta" / str(arm) / str(r["round_id"]) / "index.html"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(go_page(s, r["market_link"]))
    check_seo(seo)  # an empty or duplicate title/description on a fixed page type is a template bug
    (out / "404.html").write_text(not_found_page(s, rounds, vsp))
    if s.google_site_verification.endswith(".html"):  # Search Console's file method
        name = Path(s.google_site_verification).name
        (out / name).write_text(f"google-site-verification: {name}")
    if s.bing_site_verification:
        (out / "BingSiteAuth.xml").write_text('<?xml version="1.0"?>\n<users>\n\t<user>'
                                              f"{escape(s.bing_site_verification)}</user>\n</users>\n")
    key = indexnow_key(s)
    if key:
        (out / f"{key}.txt").write_text(key)
    (out / "badge.svg").write_text(badge_svg(board, lb.get("crowd"), (lb.get("significance") or {}).get("label")))
    kit = out / "press" / "mascots"
    kit.mkdir(parents=True, exist_ok=True)
    for a in [*s.agents, "crowd"]:
        (kit / f"{a}.svg").write_text(svg_file(a))
    widget_src = Path(__file__).parent / "widget" / "agentpit-widget.js"
    if widget_src.exists():  # the agentpit.dev market-page widget, served next to the standings widget
        shutil.copy2(widget_src, out / "widget" / "agentpit-widget.js")
    (out / "data").mkdir(exist_ok=True)
    (out / "data" / "challenge.json").write_text(json.dumps([challenge.entry(d) for d in ch_days], indent=1))
    rss, jf = feeds(s, rounds)
    (out / "feed.xml").write_text(rss)
    (out / "feed.json").write_text(jf)
    (out / ".nojekyll").write_text("")  # also lets GitHub Pages serve .well-known/
    if s.bluesky_did:  # the @agentpitbench.org handle proof (docs/bluesky-spec.md §7); DNS TXT is the primary route
        (out / ".well-known").mkdir(exist_ok=True)
        (out / ".well-known" / "atproto-did").write_text(s.bluesky_did.strip())
    for f in ("favicon.svg", "favicon-32.png", "apple-touch-icon.png", og.DEFAULT, "embed.js"):  # the emblem; the fallback preview; the embed loader
        shutil.copyfile(Path(__file__).resolve().parent / "static" / f, out / f)
    host = urlparse(s.site_url).hostname or ""
    if host and not host.endswith(".github.io"):  # custom domain: GitHub Pages drops it without this file
        (out / "CNAME").write_text(host + "\n")
    d = out / "data"
    for name in ("rounds.json", "leaderboard.json", "bets.csv", "metrics.json", "reports.json", "sealed.json",
                 "experiments.json", "newsworthiness.json"):
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
                       changelog, EXCLUSION_REASONS)
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


def not_found_page(s: Settings, rounds: list[dict], vsp: list[dict]) -> str:
    """The one 404.html GitHub Pages serves for every missing path: absolute links, no share bar, noindex, and
    its own language check (the path's language folder, else the browser's) over strings for every language."""
    strings = {}
    for code in CODES:
        _ = Translator(code)
        strings[code] = {"title": _("Page not found"), "lead": _("This page doesn't exist, or it has moved."),
                         "home": _("Home"), "leaderboard": _("Leaderboard"), "latest": _("Latest round"),
                         "h2h": _("Head to head"), "dir": "rtl" if _.rtl else "ltr",
                         "vs": [vs_title(p, _) for p in vsp]}
    latest = max(rounds, key=lambda r: r["round_id"], default=None)
    return env.get_template("404.html").render(
        base=s.site_url.rstrip("/"), strings=strings, latest=latest, s=s, t=strings["en"],
        vs=[f"vs/{vs_slug(p['a'], p['b'])}/" for p in vsp])


def _previews(s: Settings, lb: dict, rounds: list[dict], cards: dict, reports: list[dict]) -> dict:
    """Queue every link preview the build needs, render the changed ones, return site-relative paths."""
    pv = og.Previews(s)
    res: dict = {"pv": pv, "rounds": {}, "agents": {}, "pages": {}, "weeks": {}, "standings": None, "contrarian": None,
                 "standings_card": None, "agent_cards": {}}

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
        res["standings_card"], res["standings"] = pv.card("standings", html)
    for a in s.agents:
        html = safe(og.agent_html, s, lb, a, model_eras(rounds, a))
        if html:
            res["agent_cards"][a], res["agents"][a] = pv.card(f"agents/{a}", html)
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
    # Share image needs the 1200x675 card itself; without a browser it may not exist
    res["standings_card"] = _existing(s, res["standings_card"])
    res["agent_cards"] = {a: c for a, c in res["agent_cards"].items() if _existing(s, c)}
    return res


def _existing(s: Settings, rel: str | None) -> str | None:
    return rel if rel and (s.cards_dir / rel.removeprefix("cards/")).exists() else None


def share_image(card: str | None, name: str, text: "ShareText | str | None", url: str, page_type: str) -> dict | None:
    """Share image (spec section 2): the card PNG as a file via Web Share Level 2 where supported, else a plain
    download link (also what JavaScript-off visitors get). The href is site-relative: same origin, so it can be
    fetched and downloaded."""
    if not card:
        return None
    text = text.fit(200) if isinstance(text, ShareText) else (text or "")
    return {"src": card, "name": f"agentpitbench-{name}.png", "text": text,
            "url": utm(url, "native", page_type, "share-image")}


def _reply_caps(s: Settings, rounds: list[dict]) -> dict[int, bool]:
    """Rounds whose opener has more replies than collect_humans reads (xstats.reply_cap_hit). Best effort."""
    if not s.db_path.exists():
        return {}
    try:
        from . import xstats
        from .db import DB as _DB
        db = _DB(s.db_path)
        return {r["round_id"]: True for r in rounds if r.get("thread_tweet_id")
                and xstats.reply_cap_hit(db, r["round_id"], s.max_reply_reads)}
    except Exception:
        log.exception("reply cap check skipped")
        return {}


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
    if s.indexnow_enabled:  # search engines that take IndexNow (Bing, Yandex, Seznam) learn of new pages at once
        try:  # the push succeeded: nothing here may turn it into a failed deploy
            indexnow_ping(s, indexnow_changes(s))
        except Exception as e:
            log.warning("IndexNow skipped: %s", e)
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
