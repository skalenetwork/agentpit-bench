"""Outputs that let AI assistants and agents read, quote and cite the benchmark (docs/ai-referral-spec.md).

Everything here is generated from the exports (leaderboard.json, metrics.json, rounds.json) at site build, never
hand-written: robots.txt, the sitemap, llms.txt / llms-full.txt, the /facts/ sentences and facts.json, JSON-LD,
and the static read-only API under /api/v1/.

No instructions to models anywhere: plain, dated facts with n and confidence intervals, and links.

Pick embargo: a running round's entries never carry a pick. exports.round_record already seals them; the API
seals them again here (seal()), so a change upstream cannot leak a pick through this surface.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .cards import signed
from .config import AGENT_MODELS, AGENT_NAMES, Settings
from .i18n import CODES, Translator

API = "api/v1"
API_VERSION = "v1"
OPENAPI_VERSION = "2.0"   # info.version: 2.0 dropped the Brier blocks (docs/simple-scoring-spec.md §6), a breaking change
DATA_LICENSE = "CC-BY-4.0"
DATA_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
GITHUB = "https://github.com/skalenetwork/agentpit-bench"
X_URL = "https://x.com/AgentpitBench"
OPERATOR = "SKALE Labs"
# pages that exist only to be embedded elsewhere: not listed in the sitemap
NOT_IN_SITEMAP = ("widget/", "embed/", "404.html")
PICK_FIELDS = ("outcome", "avg_price", "shares_filled", "stake_filled", "confidence", "rationale", "quote",
               "statement", "decided_s", "payout", "pnl", "won", "transcript_url")


def _(msg: str) -> str:
    """Marks a module-level message id for translation (see site._)."""
    return msg


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


def stamp(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def as_of_ts(lb: dict) -> float:
    """The export time: every surface (facts, llms.txt, API, JSON-LD) carries this one timestamp."""
    return float(lb.get("updated_at") or 0) or datetime.now(timezone.utc).timestamp()


def citation(s: Settings, ts: float, url: str) -> str:
    return (f"AgentpitBench ({OPERATOR}), data as of {day(ts)}, methodology v{s.methodology_version}. {url}")


def bibtex(s: Settings, ts: float) -> str:
    y = day(ts)[:4]
    return (f"@misc{{agentpitbench{y},\n  title  = {{AgentpitBench: frontier AI agents forecasting and betting on live "
            f"prediction markets}},\n  author = {{{{{OPERATOR}}}}},\n  year   = {{{y}}},\n"
            f"  url    = {{{s.site_url.rstrip('/')}/facts/}},\n"
            f"  note   = {{Data as of {day(ts)}, methodology v{s.methodology_version}}}\n}}")


# ---------- facts ----------

def _played_on(rounds: list[dict], agent: str) -> dict:
    from .exports import _played_on as p
    return p(rounds, agent)


def _model(agent: str) -> tuple[str, str, str]:
    """(model name, model id, reasoning effort) or empty strings."""
    m = AGENT_MODELS.get(agent)
    return (m[1], m[0], m[2]) if m else ("", "", "")


REASONING = {"max": _("max reasoning"), "xhigh": _("extra-high reasoning"), "high": _("high reasoning")}


def facts_data(s: Settings, lb: dict, metrics: dict) -> dict:
    """The numbers behind /facts/, language-neutral. Copied from the exports, never recomputed."""
    def bet_row(b: dict) -> dict:
        return {k: b.get(k) for k in ("agent", "name", "rank", "played", "wins", "losses", "no_bets", "win_rate",
                                      "net_pnl", "beat_crowd")}

    def crowd_row(c: dict | None) -> dict | None:
        return {k: c.get(k) for k in ("name", "played", "wins", "losses", "win_rate", "net_pnl")} if c else None

    season = [bet_row(b) for b in lb.get("agents", [])]
    all_time = [bet_row(b) for b in (lb.get("all_time") or {}).get("agents", [])]
    excluded = (metrics.get("betting_record") or {}).get("rounds_excluded") or {}
    return {
        "betting": {"season": lb.get("season"), "agents": season,
                    "leader": next((b for b in season if b["played"]), None),
                    "rounds_counted": max((b["played"] or 0 for b in season), default=0),
                    "crowd": crowd_row(lb.get("crowd"))},
        "betting_all_time": {"agents": all_time, "crowd": crowd_row((lb.get("all_time") or {}).get("crowd"))},
        "rounds_excluded": excluded,
        "rounds_excluded_total": sum(excluded.values()),
    }


def fact_sections(s: Settings, lb: dict, metrics: dict, _=Translator("en")) -> list[dict]:
    """/facts/ in one language: answer-first sections of self-contained, dated sentences with fixed patterns.
    Numbers come from facts_data; only the words are translated."""
    d = facts_data(s, lb, metrics)
    ts = as_of_ts(lb)
    date = day(ts)
    bet = d["betting"]
    lead, out = [], []
    if bet["leader"]:
        b = bet["leader"]
        lead.append(_("As of {date}, {name} leads the AgentpitBench betting table for season {season}: {wins} wins, {losses} losses and {pnl} tokens of profit/loss over {played} rounds (paper money).",
                      date=date, name=b["name"], season=bet["season"], wins=b["wins"], losses=b["losses"],
                      pnl=signed(b["net_pnl"]), played=b["played"]))
        lead.append(_("The betting table is not tested for statistical significance; season {season} has {n} counted rounds so far.",
                      season=bet["season"], n=bet["rounds_counted"]))
    else:
        lead.append(_("As of {date}, no betting round on AgentpitBench has resolved yet.", date=date))
    out.append({"id": "leaders", "title": _("Current leaders"), "facts": lead})

    crowd_facts = []
    c = bet["crowd"]
    if c and c.get("played"):
        crowd_facts.append(_("As of {date}, the Crowd (it backs the favourite at the start of every round) has a betting record of {wins} wins and {losses} losses, {pnl} tokens of profit/loss over {played} rounds.",
                             date=date, wins=c["wins"], losses=c["losses"], pnl=signed(c["net_pnl"]), played=c["played"]))
        for b in bet["agents"]:
            if b["played"]:
                crowd_facts.append(_("{name} won {n} rounds that the Crowd lost.", name=b["name"], n=b.get("beat_crowd") or 0))
    else:
        crowd_facts.append(_("As of {date}, no betting round on AgentpitBench has resolved yet.", date=date))
    out.append({"id": "crowd", "title": _("AIs vs the Crowd"), "facts": crowd_facts})

    records = []
    for b in bet["agents"]:
        if b["played"]:
            records.append(_("{name}, betting, season {season}: {wins} wins, {losses} losses, {pnl} tokens of profit/loss over {played} rounds.",
                             name=b["name"], season=bet["season"], wins=b["wins"], losses=b["losses"],
                             pnl=signed(b["net_pnl"]), played=b["played"]))
    out.append({"id": "agents", "title": _("Each agent's record"), "facts": records})

    models = []
    for a in s.agents:
        model, model_id, effort = _model(a)
        if model:
            models.append(_("{name} runs {model} (model ID {model_id}) at {reasoning}.", name=AGENT_NAMES.get(a, a),
                            model=model, model_id=model_id, reasoning=_(REASONING.get(effort, effort))))
    out.append({"id": "models", "title": _("Models and settings"), "facts": models})
    out.append({"id": "scoring", "title": _("How scoring works"), "facts": method_lines(s, _, d)})
    out.append({"id": "updated", "title": _("Last updated"), "facts": [
        _("Data as of {time}, methodology v{v}.", time=stamp(ts), v=s.methodology_version),
        _("These facts are regenerated from the published data on every site update, as betting rounds resolve.")]})
    return out


def method_lines(s: Settings, _=Translator("en"), d: dict | None = None) -> list[str]:
    """The method in five sentences (the /facts/ scoring section and llms.txt)."""
    names = [AGENT_NAMES.get(a, a) for a in s.agents]
    lines = [
        _("{n} AI agents ({names}) each run their lab's top model at its CLI's highest reasoning setting, in a sandbox, with the same prompt and web research allowed.",
          n=len(names), names=", ".join(names)),
        _("Headline metric, the betting record: on each eligible new market every agent has {minutes} minutes for one bet of {stake} tokens of paper money; profit/loss = payout − tokens spent. Ranked by profit, then win rate; a no-bet counts as a loss.",
          minutes=round(s.decision_limit_s / 60), stake=int(s.stake)),
        _("The Crowd is a reference, not a contestant: each round it backs the market's favourite at the start."),
        _("A daily forecast sweep also collects each agent's probabilities for the same {n} open markets as research data; its scores are not ranked.",
          n=s.sweep_markets),
        _("Results are append-only; each day's forecasts are sealed with a published SHA-256 hash before their markets resolve; excluded rounds (pre-season, High-Stakes Friday, summoned, voided) are published and flagged but never counted."),
    ]
    if d and d["rounds_excluded_total"]:
        lines.append(_("{n} rounds so far are published but excluded from the statistics.", n=d["rounds_excluded_total"]))
    return lines


def facts_json(s: Settings, lb: dict, metrics: dict) -> dict:
    ts = as_of_ts(lb)
    base = s.site_url.rstrip("/")
    return {**envelope(s, ts, f"{base}/facts/"), "data": {
        **facts_data(s, lb, metrics), "statements": fact_sections(s, lb, metrics),
        "bibtex": bibtex(s, ts), "downloads": {n: f"{base}/data/{n}" for n in ("metrics.json", "leaderboard.json",
                                                                             "rounds.json", "bets.csv")}}}


# ---------- envelope + API ----------

def envelope(s: Settings, ts: float, source_url: str) -> dict:
    return {"api_version": API_VERSION, "as_of": iso(ts), "methodology_version": s.methodology_version,
            "source_url": source_url, "citation": citation(s, ts, source_url), "license": DATA_LICENSE}


def seal(r: dict) -> bool:
    """True while the round's picks must stay secret (agents may still be deciding)."""
    return r.get("state") == "running" or bool(r.get("sealed"))


def api_entry(r: dict, e: dict, base: str) -> dict:
    out = {"agent": e["agent"], "name": e["name"], "model_reported": e.get("model_reported"),
           "outcome": e.get("outcome"), "avg_price": e.get("avg_price"), "shares_filled": e.get("shares_filled"),
           "stake_filled": e.get("stake_filled"), "confidence": e.get("confidence"), "rationale": e.get("rationale"),
           "quote": e.get("quote"), "statement": e.get("statement"), "decided_s": e.get("decided_s"),
           "exit_reason": e.get("exit_reason"), "payout": e.get("payout"), "pnl": e.get("pnl"), "won": e.get("won"),
           "transcript_url": f"{base}/{e['transcript']}" if e.get("transcript") else None, "sealed": False}
    if seal(r):
        out.update({k: None for k in PICK_FIELDS}, exit_reason=None, sealed=True)
    return out


def counts(r: dict) -> bool:
    return r["state"] == "resolved" and not r.get("exhibition")


def api_round(r: dict, base: str, page_url: str | None) -> dict:
    sealed = seal(r)
    return {"round_id": r["round_id"], "market_id": r.get("market_id"), "question": r["question"],
            "outcomes": r.get("outcomes") or [], "prices_at_start": r.get("prices_at_start") or [],
            "state": r["state"], "exhibition": bool(r.get("exhibition")), "exclusion": r.get("exclusion"),
            "counts": counts(r), "sealed": sealed, "high_stakes": bool(r.get("high_stakes")), "stake": r.get("stake"),
            "started_at": iso(r.get("started_at")), "end_date": r.get("end_date"), "resolved_at": iso(r.get("resolved_at")),
            "winner": r.get("winner"), "split": False if sealed else bool(r.get("split")),
            "market_link": r.get("market_link"), "url": page_url, "api_url": f"{base}/{API}/rounds/{r['round_id']}.json",
            "crowd": r.get("crowd"), "entries": [api_entry(r, e, base) for e in r.get("entries", [])]}


def round_summary(r: dict, base: str, page_url: str | None) -> dict:
    full = api_round(r, base, page_url)
    return {**{k: full[k] for k in ("round_id", "question", "state", "exhibition", "exclusion", "counts", "sealed",
                                     "started_at", "end_date", "resolved_at", "winner", "url", "api_url")},
            "picks": [{k: e[k] for k in ("agent", "name", "outcome", "won", "pnl")} for e in full["entries"]]}


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def write_api(s: Settings, out: Path, rounds: list[dict], lb: dict, metrics: dict, sealed: list[dict],
              exp: Path, open_ok: set[str], pages: set[str], changelog: list[tuple[str, str, dict, str]],
              exclusions: dict[str, str]) -> dict:
    """/api/v1/: static JSON, one file per resource. `rounds` is the unfiltered list (void rounds included and
    flagged); `pages` the site-relative English pages that exist, for source links. Returns facts.json."""
    base = s.site_url.rstrip("/")
    ts = as_of_ts(lb)
    api = out / API

    def page_url(rel: str) -> str | None:
        return f"{base}/{rel}" if rel in pages else None

    fj = facts_json(s, lb, metrics)
    _dump(api / "facts.json", fj)
    _dump(out / "facts.json", fj)

    keep_bet = ("agent", "name", "rank", "played", "wins", "losses", "no_bets", "win_rate", "net_pnl", "staked",
                "roi", "streak", "beat_crowd")
    keep_crowd = ("name", "played", "wins", "losses", "win_rate", "net_pnl")
    _dump(api / "leaderboard.json", {**envelope(s, ts, f"{base}/leaderboard/"), "data": {
        "betting": {"season": lb.get("season"), "rounds_counted": lb.get("rounds_counted"), "agents": [{k: b.get(k) for k in keep_bet} for b in lb.get("agents", [])],
                    "crowd": {k: (lb.get("crowd") or {}).get(k) for k in keep_crowd}},
        "betting_all_time": {"agents": [{k: b.get(k) for k in keep_bet} for b in (lb.get("all_time") or {}).get("agents", [])],
                             "crowd": {k: ((lb.get("all_time") or {}).get("crowd") or {}).get(k) for k in keep_crowd}},
    }})

    ordered = sorted(rounds, key=lambda r: -r["round_id"])
    _dump(api / "rounds.json", {**envelope(s, ts, f"{base}/data/"), "data": {
        "rounds": [round_summary(r, base, page_url(f"round/{r['round_id']}/")) for r in ordered]}})
    for r in ordered:
        url = page_url(f"round/{r['round_id']}/")
        _dump(api / "rounds" / f"{r['round_id']}.json",
              {**envelope(s, ts, url or f"{base}/{API}/rounds/{r['round_id']}.json"), "data": api_round(r, base, url)})

    season = {b["agent"]: b for b in lb.get("agents", [])}
    alltime = {b["agent"]: b for b in (lb.get("all_time") or {}).get("agents", [])}
    for a in s.agents:
        model, model_id, effort = _model(a)
        history = []
        for r in ordered:
            for e in r.get("entries", []):
                if e["agent"] == a:
                    x = api_entry(r, e, base)
                    history.append({"round_id": r["round_id"], "question": r["question"], "state": r["state"],
                                    "counts": counts(r), "exclusion": r.get("exclusion"), "sealed": x["sealed"],
                                    "outcome": x["outcome"], "won": x["won"], "pnl": x["pnl"]})
        _dump(api / "agents" / f"{a}.json", {**envelope(s, ts, page_url(f"agent/{a}/") or f"{base}/leaderboard/"), "data": {
            "agent": a, "name": AGENT_NAMES.get(a, a), "model": model, "model_id": model_id, "reasoning": effort,
            "played_on": _played_on(ordered, a),
            "betting_season": {"season": lb.get("season"), **{k: season[a].get(k) for k in keep_bet}} if a in season else None,
            "betting_all_time": {k: alltime[a].get(k) for k in keep_bet} if a in alltime else None,
            "by_category": (lb.get("by_category") or {}).get(a, []),
            "by_model": [m for m in lb.get("by_model", []) if m.get("agent") == a],
            "rounds": history[:100]}})

    index = []
    for x in sealed:
        pub = x["date"] in open_ok and (exp / "forecasts" / f"{x['date']}.json").exists()
        index.append({"date": x["date"], "sha256": x.get("sha256"), "forecasts": x.get("forecasts"),
                      "sealed_at": iso(x.get("sealed_at")), "published": pub,
                      "raw_url": f"{base}/data/forecasts/{x['date']}.json" if pub else None,
                      "api_url": f"{base}/{API}/forecasts/{x['date']}.json" if pub else None})
        if pub:  # only once that day's markets have resolved (forecast.revealable_dates), like /data/forecasts/
            raw = (exp / "forecasts" / f"{x['date']}.json").read_bytes()
            _dump(api / "forecasts" / f"{x['date']}.json", {**envelope(s, ts, f"{base}/methodology/"), "data": {
                "date": x["date"], "sha256": x.get("sha256"), "sha256_matches": hashlib.sha256(raw).hexdigest() == x.get("sha256"),
                "raw_url": f"{base}/data/forecasts/{x['date']}.json", **{k: v for k, v in json.loads(raw).items()
                                                                          if k not in ("date",)}}})
    _dump(api / "forecasts.json", {**envelope(s, ts, f"{base}/methodology/"), "data": {"sweeps": index}})

    en = Translator("en")
    _dump(api / "methodology.json", {**envelope(s, ts, f"{base}/methodology/"), "data": {
        "version": s.methodology_version, "season_start": s.season_start,
        "metric": "Betting record: wins, losses and profit/loss; ranked by profit, then win rate (a no-bet counts as a loss)",
        "summary": method_lines(s), "exclusions": {k: en(v) for k, v in exclusions.items()},
        "contestants": [{"agent": a, "name": AGENT_NAMES.get(a, a), "model": _model(a)[0], "model_id": _model(a)[1],
                         "reasoning": _model(a)[2]} for a in s.agents],
        "changelog": [{"date": d, "version": v, "change": en(m, **kw)} for d, m, kw, v in changelog],
        "pages": {"methodology": f"{base}/methodology/", "independence": f"{base}/independence/",
                  "incidents": f"{base}/incidents/", "facts": f"{base}/facts/"}}})
    _dump(api / "openapi.json", openapi(s))
    _dump(api / "index.json", {**envelope(s, ts, f"{base}/data/"), "data": {
        "endpoints": [f"{base}/{API}/{p}" for p in ("leaderboard.json", "facts.json", "rounds.json",
                                                     "rounds/{n}.json", "agents/{id}.json", "forecasts.json",
                                                     "forecasts/{date}.json", "methodology.json", "openapi.json")],
        "agents": list(s.agents)}})
    return fj


def openapi(s: Settings) -> dict:
    base = s.site_url.rstrip("/")
    env_props = {"api_version": {"type": "string", "const": API_VERSION},
                 "as_of": {"type": "string", "format": "date-time", "description": "UTC time of the data export"},
                 "methodology_version": {"type": "string"},
                 "source_url": {"type": "string", "format": "uri", "description": "Human-readable page to link and cite"},
                 "citation": {"type": "string"}, "license": {"type": "string"}}

    def env_of(data: dict) -> dict:
        return {"type": "object", "required": [*env_props, "data"], "properties": {**env_props, "data": data}}

    ref = lambda n: {"$ref": f"#/components/schemas/{n}"}  # noqa: E731
    num, nnum, nstr = {"type": "number"}, {"type": ["number", "null"]}, {"type": ["string", "null"]}
    entry = {"type": "object", "description": "One agent's decision. While a round is running every pick field is null "
                                              "and sealed is true: picks are published together when all agents are done.",
             "properties": {"agent": {"type": "string"}, "name": {"type": "string"}, "model_reported": nstr,
                            "outcome": nstr, "avg_price": nnum, "shares_filled": nnum, "stake_filled": nnum,
                            "confidence": nnum, "rationale": nstr, "quote": nstr, "statement": nstr, "decided_s": nnum,
                            "exit_reason": nstr, "payout": nnum, "pnl": nnum, "won": {"type": ["boolean", "null"]},
                            "transcript_url": nstr, "sealed": {"type": "boolean"}}}
    rnd = {"type": "object", "properties": {
        "round_id": {"type": "integer"}, "question": {"type": "string"},
        "state": {"type": "string", "enum": ["running", "awaiting_resolution", "resolved", "void"]},
        "exhibition": {"type": "boolean"}, "exclusion": {"type": ["string", "null"],
                                                         "enum": [None, "pre-season", "high-stakes", "summon", "infra"]},
        "counts": {"type": "boolean", "description": "Counted in the statistics (resolved and not excluded)"},
        "sealed": {"type": "boolean"}, "outcomes": {"type": "array", "items": {"type": "string"}},
        "prices_at_start": {"type": "array", "items": num}, "winner": nstr, "started_at": nstr, "end_date": nstr,
        "resolved_at": nstr, "url": nstr, "api_url": {"type": "string"}, "entries": {"type": "array", "items": ref("Entry")}}}

    def get(summary: str, schema: dict, params: list | None = None) -> dict:
        op = {"get": {"summary": summary, "responses": {"200": {"description": "OK", "content": {
            "application/json": {"schema": env_of(schema)}}}}}}
        if params:
            op["get"]["parameters"] = params
        return op

    p = lambda name, desc, t="string": [{"name": name, "in": "path", "required": True, "description": desc,  # noqa: E731
                                         "schema": {"type": t}}]
    return {
        "openapi": "3.1.0",
        "info": {"title": "AgentpitBench API", "version": OPENAPI_VERSION, "license": {"name": DATA_LICENSE, "url": DATA_LICENSE_URL},
                 "description": "Static, read-only JSON regenerated on every site update from the published exports. "
                                "Every response carries as_of, methodology_version, source_url and citation. Void and "
                                "exhibition rounds are included and flagged with state and exclusion. 2.0 (methodology "
                                "2.0): the forecast and vs_crowd blocks are gone from leaderboard.json and agents/{id}.json; "
                                "the headline is the betting record."},
        "servers": [{"url": f"{base}/{API}"}],
        "paths": {
            "/leaderboard.json": get("The betting record (the headline): season and all-time tables, with the Crowd", {"type": "object"}),
            "/facts.json": get("The dated facts behind /facts/, with the numbers and the English sentences", {"type": "object"}),
            "/rounds.json": get("Every round, newest first, with state, exclusion and picks (null while running)",
                                {"type": "object", "properties": {"rounds": {"type": "array", "items": {"type": "object"}}}}),
            "/rounds/{n}.json": get("One round in full", ref("Round"), p("n", "Round number", "integer")),
            "/agents/{id}.json": get("One agent: model, betting record, recent rounds", {"type": "object"},
                                     p("id", "Agent id: " + ", ".join(s.agents))),
            "/forecasts.json": get("Sealed daily forecast files (research data): date, SHA-256, and whether the content is published yet",
                                   {"type": "object"}),
            "/forecasts/{date}.json": get("One day's sealed forecasts, published once that day's markets have resolved",
                                          {"type": "object"}, p("date", "YYYY-MM-DD")),
            "/methodology.json": get("Methodology version, rules summary, exclusions and changelog", {"type": "object"}),
        },
        "components": {"schemas": {"Round": rnd, "Entry": entry}},
    }


# ---------- robots, sitemap ----------

def robots(s: Settings) -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {s.site_url.rstrip('/')}/sitemap.xml\n"


def sitemaps(s: Settings, pages: dict[str, float]) -> dict[str, str]:
    """{filename: xml}: an index at sitemap.xml and one sitemap per language, each URL listing its 20 language
    alternates (per-language files keep each one far below the 50,000-URL / 50 MB limits as rounds pile up)."""
    base = s.site_url.rstrip("/")
    rels = sorted((rel for rel in pages if not rel.startswith(NOT_IN_SITEMAP)), key=lambda r: (r.count("/"), r))

    def href(code: str, rel: str) -> str:
        return f"{base}/{'' if code == 'en' else code + '/'}{rel}"

    files, index = {}, []
    for code in CODES:
        urls = []
        for rel in rels:
            alts = "".join(f'<xhtml:link rel="alternate" hreflang="{c}" href={quoteattr(href(c, rel))}/>' for c in CODES)
            alts += f'<xhtml:link rel="alternate" hreflang="x-default" href={quoteattr(href("en", rel))}/>'
            urls.append(f"<url><loc>{escape(href(code, rel))}</loc><lastmod>{day(pages[rel])}</lastmod>{alts}</url>")
        name = f"sitemap-{code}.xml"
        files[name] = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                       'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n' + "\n".join(urls) + "\n</urlset>\n")
        last = max((pages[r] for r in rels), default=0)
        index.append(f"<sitemap><loc>{base}/{name}</loc><lastmod>{day(last)}</lastmod></sitemap>")
    files["sitemap.xml"] = ('<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                            + "\n".join(index) + "\n</sitemapindex>\n")
    return files


# ---------- llms.txt ----------

class _Text(HTMLParser):
    """The <main> of a rendered English page as plain Markdown: headings, paragraphs and list items. Tables,
    code blocks, scripts and SVG are left out (llms-full.txt carries the prose; the page has the rest)."""
    SKIP = {"pre", "table", "script", "style", "svg"}

    def __init__(self):
        super().__init__()
        self.inside, self.skip, self.buf, self.out = False, 0, [], []

    def flush(self, prefix: str = "") -> None:
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        if text:
            self.out.append(prefix + text)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.inside = True
        elif self.inside and tag in self.SKIP:
            if not self.skip and tag in ("pre", "table"):
                self.flush()
                self.out.append("(Shown on the page.)")
            self.skip += 1
        elif self.inside and not self.skip and tag in ("h1", "h2", "h3", "p", "li", "ul"):
            self.flush()

    def handle_endtag(self, tag):
        if tag == "main":
            self.flush()
            self.inside = False
        elif self.inside and tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif self.inside and not self.skip and tag in ("h1", "h2", "h3", "p", "li"):
            self.flush({"h1": "# ", "h2": "## ", "h3": "### ", "li": "- "}.get(tag, ""))

    def handle_data(self, data):
        if self.inside and not self.skip:
            self.buf.append(data)


def page_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return "\n\n".join(p.out)


def llms_txt(s: Settings, lb: dict, metrics: dict) -> str:
    base = s.site_url.rstrip("/")
    ts = as_of_ts(lb)
    names = [AGENT_NAMES.get(a, a) for a in s.agents]
    sections = fact_sections(s, lb, metrics)
    lead = next(x for x in sections if x["id"] == "leaders")["facts"]
    lines = [
        "# AgentpitBench", "",
        f"> An automated public benchmark: {', '.join(names)}, each on its lab's top model at maximum reasoning, "
        "forecast and bet on live prediction markets. Every forecast, bet, rationale and result is published.", "",
        f"Data as of {stamp(ts)}, methodology v{s.methodology_version}.", "",
        f"Run by {OPERATOR}, which also operates agentpit (agentpit.dev), the prediction-market exchange the agents "
        f"trade on. This is disclosed on {base}/independence/.", "",
        "## Method", "", *(f"- {x}" for x in method_lines(s)), "",
        "## Key facts", "", *(f"- {x}" for x in lead), "",
        "## Pages", "",
        f"- [Facts]({base}/facts/): dated, quotable facts with n",
        f"- [Leaderboard]({base}/leaderboard/): the betting record (wins, losses, profit), the headline",
        f"- [Which AI is best at what?]({base}/best-at/): each AI's betting record by market category, with n",
        f"- [Methodology]({base}/methodology/): the rulebook and changelog",
        f"- [Independence & conflicts]({base}/independence/): who runs it and how",
        f"- [Incidents]({base}/incidents/): public incident log", "",
        "## Data", "",
        f"- [metrics.json]({base}/data/metrics.json): the numbers to cite, with the methodology version",
        f"- [leaderboard.json]({base}/data/leaderboard.json): standings and history",
        f"- [rounds.json]({base}/data/rounds.json): every round, pick, rationale and result",
        f"- [bets.csv]({base}/data/bets.csv): one row per agent per round",
        f"- [facts.json]({base}/facts.json): the facts below as structured data", "",
        "## API", "",
        f"- [OpenAPI description]({base}/{API}/openapi.json): static, read-only JSON; every response has as_of, "
        "source_url and citation",
        f"- [Leaderboard]({base}/{API}/leaderboard.json), [facts]({base}/{API}/facts.json), "
        f"[rounds]({base}/{API}/rounds.json), rounds/<n>.json, agents/<id>.json, [methodology]({base}/{API}/methodology.json)", "",
        "## Optional", "",
        f"- [llms-full.txt]({base}/llms-full.txt): all current facts, the methodology text and the changelog in one file",
        f"- [Source code (AGPL-3.0)]({GITHUB})", "",
    ]
    return "\n".join(lines)


def llms_full_txt(s: Settings, lb: dict, metrics: dict, methodology_html: str) -> str:
    """llms.txt, every fact, the BibTeX, and the English methodology page as text (its changelog included)."""
    parts = [llms_txt(s, lb, metrics).rstrip(), "", "## All facts", ""]
    for sec in fact_sections(s, lb, metrics):
        parts += [f"### {sec['title']}", "", *(f"- {x}" for x in sec["facts"]), ""]
    parts += ["### How to cite", "", "```bibtex", bibtex(s, as_of_ts(lb)), "```", "",
              re.sub(r"^(#+) ", lambda m: "#" + m.group(1) + " ", page_text(methodology_html), flags=re.M), ""]
    return "\n".join(parts)


# ---------- JSON-LD ----------

def organization(s: Settings) -> dict:
    return {"@type": "Organization", "@id": f"{s.site_url.rstrip('/')}/#operator", "name": OPERATOR,
            "sameAs": [X_URL, GITHUB]}


def dataset_ld(s: Settings, lb: dict, page_url: str) -> dict:
    base = s.site_url.rstrip("/")
    ts = as_of_ts(lb)
    org = organization(s)
    files = [("metrics.json", "application/json"), ("leaderboard.json", "application/json"),
             ("rounds.json", "application/json"), ("bets.csv", "text/csv")]
    return {"@context": "https://schema.org", "@graph": [org, {
        "@type": "Dataset", "@id": f"{base}/#dataset", "name": "AgentpitBench", "url": page_url,
        "description": (f"{' vs '.join(AGENT_NAMES.get(a, a) for a in s.agents)}: frontier AI agents forecast and bet on "
                        "live prediction markets. The headline is the betting record: every bet, rationale and result, "
                        "with a sealed daily forecast sweep as research data."),
        "creator": {"@id": org["@id"]}, "publisher": {"@id": org["@id"]}, "license": DATA_LICENSE_URL,
        "isAccessibleForFree": True, "keywords": ["LLM benchmark", "forecasting", "prediction markets", "AI agents"],
        "temporalCoverage": f"{s.season_start[:10]}/{day(ts)}", "dateModified": iso(ts),
        "version": s.methodology_version,
        "variableMeasured": [
            {"@type": "PropertyValue", "name": "Profit/loss", "description": "Paper-money tokens won or lost per bet", "unitText": "tokens"},
            {"@type": "PropertyValue", "name": "Win rate"}],
        "distribution": [{"@type": "DataDownload", "contentUrl": f"{base}/data/{n}", "encodingFormat": t} for n, t in files]
        + [{"@type": "DataDownload", "contentUrl": f"{base}/{API}/openapi.json", "encodingFormat": "application/json",
            "name": "API (OpenAPI 3.1)"}],
        "citation": citation(s, ts, f"{base}/facts/")}]}


def techarticle_ld(s: Settings, lb: dict, page_url: str, changelog: list[tuple[str, str, dict]]) -> dict:
    org = organization(s)
    modified = max((d for d, *_ in changelog), default=day(as_of_ts(lb)))
    return {"@context": "https://schema.org", "@graph": [org, {
        "@type": "TechArticle", "headline": f"AgentpitBench methodology v{s.methodology_version}", "url": page_url,
        "version": s.methodology_version, "dateModified": modified, "inLanguage": "en",
        "author": {"@id": org["@id"]}, "publisher": {"@id": org["@id"]},
        "isPartOf": {"@id": f"{s.site_url.rstrip('/')}/#dataset"}}]}


def breadcrumb_ld(trail: list[tuple[str, str]]) -> dict:
    """BreadcrumbList node from (name, url) pairs, home first; every url is a real page."""
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i, "name": name, "item": url} for i, (name, url) in enumerate(trail, 1)]}


def round_ld(s: Settings, r: dict, page_url: str) -> dict:
    """The market as an Event; the four picks as an ItemList only once they are public."""
    ev = {"@type": "Event", "name": r["question"], "url": page_url, "startDate": iso(r.get("started_at")),
          "eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode",
          "eventStatus": "https://schema.org/EventScheduled",
          "location": {"@type": "VirtualLocation", "url": r.get("market_link") or page_url},
          "organizer": {"@id": organization(s)["@id"]}}
    if r.get("end_date"):
        ev["endDate"] = r["end_date"]
    graph = [organization(s), ev]
    if not seal(r):
        picks = [e for e in r.get("entries", []) if e.get("outcome")]
        if picks:
            graph.append({"@type": "ItemList", "name": f"AgentpitBench round {r['round_id']}: the picks",
                          "itemListElement": [{"@type": "ListItem", "position": i,
                                               "name": f"{e['name']}: {e['outcome']}"} for i, e in enumerate(picks, 1)]})
    return {"@context": "https://schema.org", "@graph": graph}
