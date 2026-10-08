"""`bench`: the only tool an agent has. Talks to the orchestrator over the round's Unix socket."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys


def call(req: dict) -> dict:
    path = os.environ.get("BENCH_SOCKET")
    if not path:
        return {"ok": False, "error": "BENCH_SOCKET not set: bench only works inside an AgentpitBench round"}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(60)
        s.connect(path)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b'{"ok": false, "error": "no reply"}')


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench", description="Inspect the market and place your one bet.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("market", help="Market snapshot: question, description, outcomes, prices, end date")
    b = sub.add_parser("book", help="Order book for one outcome")
    b.add_argument("--outcome", required=True)
    h = sub.add_parser("history", help="Price history for the market")
    h.add_argument("--outcome", required=True)
    bet = sub.add_parser("bet", help="Place your one 100-token bet. Works once.")
    bet.add_argument("--outcome", required=True)
    bet.add_argument("--rationale", required=True)
    bet.add_argument("--confidence", type=float, required=True, help="0 to 1")
    bet.add_argument("--max-price", type=float, default=None, help="worst price you accept (default: best ask)")
    a = p.parse_args(argv)
    if a.cmd == "bet" and not 0 <= a.confidence <= 1:
        p.error("--confidence must be between 0 and 1")
    req = {k: v for k, v in vars(a).items() if v is not None}
    try:
        resp = call(req)
    except OSError as e:
        resp = {"ok": False, "error": f"cannot reach the bench: {e}"}
    print(json.dumps(resp.get("result", resp), indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
