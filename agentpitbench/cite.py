"""Citable results for the labs (docs/hf-borrow-spec.md, Part B): the per-agent result line, badge text, badge
snippets and bibtex, built only from a leaderboard export row. One function feeds the agent page's "Cite this result"
box and `agentpitbench lab-kit`, so the note a lab gets is the line on its page.

Honest wording (enforced by tests/test_hf_borrow.py and checked again by `lab-kit`): no superlatives, always the
as-of date, the methodology version and the number of counted rounds; fewer than EARLY rounds adds "(early season)".
The strings stay English on every language's page: they are citations, like the prompt on /data/."""
from __future__ import annotations

import re

from .config import AGENT_MODELS, AGENT_NAMES

EARLY = 20  # counted rounds below this: the line says "(early season)"
NEUTRALITY = ("No lab pays for inclusion; every model gets the same prompt at the same minute; "
              "transcripts are published redacted, never edited.")
SUPERLATIVES = re.compile(r"\b(best|smartest|greatest|strongest|leading|unbeat\w*|dominat\w*|undisputed|"
                          r"world'?s|most (accurate|intelligent|capable)|top (ai|model))\b", re.I)


def signed(v: float | None) -> str:
    v = float(v or 0)
    return f"+{v:,.0f}" if v >= 0 else f"-{abs(v):,.0f}"


def model_name(agent: str) -> str:
    """The display name of the model the agent's current runs use (AGENT_MODELS)."""
    return AGENT_MODELS[agent][1] if agent in AGENT_MODELS else AGENT_NAMES.get(agent, agent)


def bibtex(year: str, site: str, note: str) -> str:
    """The dataset's bibtex entry (also in the Hugging Face card); `note` names the season or the agent's result.
    LaTeX specials in the note (the rank's #) are escaped so the entry typesets."""
    note = re.sub(r"([#%&_$])", r"\\\1", note)
    return (f"@misc{{agentpitbench{year},\n"
            f"  title  = {{AgentpitBench: coding agents betting on live prediction markets}},\n"
            f"  author = {{SKALE Labs}},\n"
            f"  year   = {{{year}}},\n"
            f"  url    = {{{site}}},\n"
            f"  note   = {{{note}}}\n}}")


def result(agent: str, row: dict | None, version: str, as_of_day: str, season: str, site: str) -> dict:
    """{line, badge, badge_url, page_url, markdown, html, bibtex, rounds} for one agent. `row` is the agent's row of
    leaderboard.json's season table (rank, wins, losses, net_pnl, played), None before its first counted round."""
    site = site.rstrip("/")
    model = model_name(agent)
    n = int((row or {}).get("played") or 0)
    page_url, badge_url = f"{site}/agent/{agent}/", f"{site}/badge/{agent}.svg"
    if n:
        line = (f"{model} is #{row['rank']} on AgentpitBench ({row['wins']}-{row['losses']}, "
                f"{signed(row.get('net_pnl'))} tokens, {n} rounds, methodology v{version}, as of {as_of_day})")
        badge = f"{model} · #{row['rank']} · {signed(row.get('net_pnl'))} · season {season}"
    else:
        line = f"{model} has 0 rounds counted on AgentpitBench yet (methodology v{version}, as of {as_of_day})"
        badge = f"{model} · no counted rounds yet · season {season}"
    if n < EARLY:
        line += " (early season)"
    alt = f"AgentpitBench: {badge}"
    return {
        "agent": agent, "model": model, "rounds": n, "line": line, "badge": badge,
        "badge_url": badge_url, "page_url": page_url,
        "markdown": f"[![{alt}]({badge_url})]({page_url})",
        "html": f'<a href="{page_url}"><img src="{badge_url}" alt="{alt}"></a>',
        "bibtex": bibtex(as_of_day[:4], site, f"{line}. {page_url}"),
    }


def problems(c: dict, version: str, as_of_day: str) -> list[str]:
    """Why a result's strings break the honest-wording rules (empty = fine)."""
    out = []
    for k in ("line", "badge", "markdown", "html", "bibtex"):
        if SUPERLATIVES.search(c[k]):
            out.append(f"{k}: superlative {SUPERLATIVES.search(c[k]).group(0)!r}")
    line = c["line"]
    if as_of_day not in line:
        out.append("line: no as-of date")
    if f"methodology v{version}" not in line:
        out.append("line: no methodology version")
    if f"{c['rounds']} rounds" not in line:
        out.append("line: no rounds count")
    if c["rounds"] < EARLY and "(early season)" not in line:
        out.append("line: fewer than 20 rounds without (early season)")
    return out


TRIGGERS = [
    "the agent takes #1, or keeps it for 7 days in a row;",
    "the lab ships a model the bench switches to (a new methodology version);",
    "a season ends (the monthly champion is posted).",
]


def lab_kit(c: dict, dataset_url: str) -> str:
    """The operator's note to a lab's developer-relations team. Printed only; the operator sends it by hand."""
    name = AGENT_NAMES.get(c["agent"], c["agent"])
    return "\n".join([
        f"Lab kit: {name} ({c['model']}). Printed only; nothing has been sent. Send it by hand.",
        "",
        "--- note ---",
        "Hi,",
        "",
        f"{c['model']} competes on AgentpitBench, an open benchmark where frontier AI agents bet on live prediction "
        "markets. Its current result, filled from our public leaderboard export:",
        "",
        f"  {c['line']}",
        "",
        "You're welcome to cite it. The badge updates with every site build:",
        f"  {c['markdown']}",
        f"Agent page: {c['page_url']}",
        f"Open dataset (CC BY 4.0): {dataset_url}",
        "",
        NEUTRALITY.replace("No lab pays for inclusion;", "No lab pays for inclusion or placement;"),
        "",
        "--- when it's worth sending ---",
        *(f"- {t}" for t in TRIGGERS),
        "The bot never mentions or tags labs on X.",
    ])
