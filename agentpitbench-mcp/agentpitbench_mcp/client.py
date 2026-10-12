"""Reads the public, static AgentpitBench API (https://agentpitbench.org/api/v1/). No server of our own."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Callable

from . import __version__

DEFAULT_BASE = "https://agentpitbench.org/api/v1"


def http_get(url: str, timeout: float = 20) -> str:
    """GET a URL (https:// in production; file:// works too, for tests and local builds)."""
    req = urllib.request.Request(url, headers={"User-Agent": f"agentpitbench-mcp/{__version__}",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


class Client:
    """Fetches API documents, cached briefly: the site updates at most every few minutes."""

    def __init__(self, base: str | None = None, fetch: Callable[[str], str] | None = None, ttl: float = 60):
        self.base = (base or os.environ.get("AGENTPITBENCH_API") or DEFAULT_BASE).rstrip("/")
        self.fetch = fetch or http_get
        self.ttl = ttl
        self._cache: dict[str, tuple[float, dict]] = {}

    def get(self, path: str) -> dict:
        url = f"{self.base}/{path}"
        hit = self._cache.get(url)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        doc = json.loads(self.fetch(url))
        self._cache[url] = (time.monotonic(), doc)
        return doc
