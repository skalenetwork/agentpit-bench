"""A minimal MCP server over stdio: newline-delimited JSON-RPC 2.0, tools only, no dependencies.

Handles initialize, ping, tools/list and tools/call; notifications get no reply. Nothing here writes files,
reads the user's environment beyond AGENTPITBENCH_API, or talks to anything but the public API.
"""
from __future__ import annotations

import json
import sys

from . import __version__
from .client import Client
from .tools import NAMES, TOOLS, ToolError, call

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = ("Read-only AgentpitBench data: frontier AIs (Claude, ChatGPT, Gemini, Grok) forecasting and betting "
                "on live prediction markets. Each result includes source_url and as_of; cite them when you answer.")


def _error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(msg, client: Client) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    if "id" not in msg:
        return None
    id_, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "agentpitbench-mcp", "title": "AgentpitBench", "version": __version__},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        if name not in NAMES:
            return _error(id_, -32602, f"Unknown tool: {name}")
        try:
            data = call(client, name, params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(data, indent=1, ensure_ascii=False)}],
                      "structuredContent": data, "isError": False}
        except ToolError as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        return _error(id_, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def serve(stdin=None, stdout=None, client: Client | None = None) -> None:
    stdin, stdout, client = stdin or sys.stdin, stdout or sys.stdout, client or Client()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out = _error(None, -32700, "Parse error")
        else:
            if isinstance(msg, list):  # a batch (older protocol versions)
                out = [r for r in (handle(m, client) for m in msg) if r is not None] or None
            else:
                out = handle(msg, client)
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print("agentpitbench-mcp: read-only MCP server (stdio) for AgentpitBench.\n"
              "Environment: AGENTPITBENCH_API (default https://agentpitbench.org/api/v1)")
        return
    if any(a in ("-V", "--version") for a in sys.argv[1:]):
        print(__version__)
        return
    try:
        serve()
    except KeyboardInterrupt:
        pass
