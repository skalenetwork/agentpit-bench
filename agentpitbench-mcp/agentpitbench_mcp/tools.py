"""The six read-only tools. Each returns the API document's source_url, as_of, methodology_version and citation
next to the data, so an answer built on it can link back and say how fresh it is."""
from __future__ import annotations

import re

from .client import Client

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
OPEN_STATES = ("running", "awaiting_resolution")

TOOLS = [
    {"name": "get_leaderboard",
     "description": "AgentpitBench standings: forecast accuracy per AI (mean Brier score, lower is better, with 95% "
                    "confidence interval and n) compared with the market price (the Crowd), plus the betting tables.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_facts",
     "description": "Dated, quotable facts about AgentpitBench (which AI forecasts best, vs the Crowd, each agent's "
                    "record, models and settings, how to cite), with the numbers behind each sentence.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_round",
     "description": "One AgentpitBench round: the prediction market, each AI's pick, price, confidence, rationale and "
                    "result. Void and exhibition rounds are flagged with state and exclusion. A running round's "
                    "picks are sealed (null) until every agent has decided.",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "description": "Round number"}},
                     "required": ["n"], "additionalProperties": False}},
    {"name": "get_agent",
     "description": "One AI contestant: model and reasoning setting, forecast accuracy, betting record and recent rounds.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description":
                                                             "Agent id: claude, codex (ChatGPT), agy (Gemini) or grok"}},
                     "required": ["id"], "additionalProperties": False}},
    {"name": "list_open_rounds",
     "description": "Rounds whose markets have not resolved yet (running or awaiting resolution).",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_methodology",
     "description": "How AgentpitBench works: methodology version, scoring and statistics summary, what is excluded, "
                    "contestants, and the changelog.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]
for _t in TOOLS:
    _t["annotations"] = {"title": _t["name"].replace("_", " ").capitalize(), **READ_ONLY}

NAMES = {t["name"] for t in TOOLS}


class ToolError(Exception):
    """Bad arguments or an unreachable API: reported to the model as a tool error, never a crash."""


def _answer(doc: dict, data) -> dict:
    return {"source_url": doc.get("source_url"), "as_of": doc.get("as_of"),
            "methodology_version": doc.get("methodology_version"), "citation": doc.get("citation"), "data": data}


def _get(client: Client, path: str) -> dict:
    try:
        doc = client.get(path)
    except Exception as e:  # network, 404, bad JSON
        raise ToolError(f"Could not read {client.base}/{path}: {e}") from None
    if not isinstance(doc, dict) or "data" not in doc:
        raise ToolError(f"Unexpected response from {client.base}/{path}")
    return doc


def call(client: Client, name: str, args: dict | None = None) -> dict:
    args = args or {}
    if name == "get_leaderboard":
        doc = _get(client, "leaderboard.json")
        return _answer(doc, doc["data"])
    if name == "get_facts":
        doc = _get(client, "facts.json")
        return _answer(doc, doc["data"])
    if name == "get_round":
        n = args.get("n")
        if isinstance(n, str) and n.isdigit():
            n = int(n)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ToolError("n must be a positive round number")
        doc = _get(client, f"rounds/{n}.json")
        return _answer(doc, doc["data"])
    if name == "get_agent":
        aid = str(args.get("id", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9-]{1,32}", aid):
            raise ToolError("id must be an agent id such as claude, codex, agy or grok")
        doc = _get(client, f"agents/{aid}.json")
        return _answer(doc, doc["data"])
    if name == "list_open_rounds":
        doc = _get(client, "rounds.json")
        rounds = [r for r in doc["data"].get("rounds", []) if r.get("state") in OPEN_STATES]
        return _answer(doc, {"rounds": rounds,
                             "note": "Picks of a running round are sealed (null) until every agent has decided."})
    if name == "get_methodology":
        doc = _get(client, "methodology.json")
        return _answer(doc, doc["data"])
    raise ToolError(f"Unknown tool: {name}")
