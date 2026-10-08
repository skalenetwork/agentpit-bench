"""Runs one benchmark round: snapshot, launch three agents, serve `bench`, place bets."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Protocol

from .agentpit import Agentpit, OrderRejected, best_ask
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

SECRET_PREFIXES = ("AGENTPIT_", "X_API", "X_ACCESS", "X_CLIENT", "GH_", "BENCH_SECRETS", "SMTP_", "XAI_")
DROP_VARS = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "VIRTUAL_ENV"}


@dataclass
class AgentCLI:
    name: str
    argv: list[str]                    # "{prompt}" is replaced with the prompt
    version_argv: list[str]
    model_patterns: list[str] = field(default_factory=list)

    def command(self, prompt: str) -> list[str]:
        return [prompt if a == "{prompt}" else a for a in self.argv]


CLIS = {
    "claude": AgentCLI(
        "claude",
        ["claude", "-p", "{prompt}", "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"],
        ["claude", "--version"],
        [r'"model"\s*:\s*"([^"]+)"'],
    ),
    "codex": AgentCLI(
        "codex",
        ["codex", "exec", "--full-auto", "--skip-git-repo-check", "{prompt}"],
        ["codex", "--version"],
        [r"^model:\s*(\S+)", r'"model"\s*:\s*"([^"]+)"'],
    ),
    "agy": AgentCLI(
        "agy",
        ["agy", "-p", "{prompt}", "--dangerously-skip-permissions"],
        ["agy", "--version"],
        [r"[Mm]odel:\s*(\S+)", r'"model"\s*:\s*"([^"]+)"'],
    ),
    "grok": AgentCLI(
        "grok",
        ["grok", "-p", "{prompt}", "--always-approve", "--output-format", "streaming-json"],
        ["grok", "--version"],
        [r'"model"\s*:\s*"([^"]+)"'],
    ),
}


class Publisher(Protocol):
    async def on_bet(self, round_id: int, run_id: int) -> None: ...
    async def on_bets_done(self, round_id: int) -> None: ...
    async def on_resolved(self, round_id: int) -> None: ...


class NullPublisher:
    async def on_bet(self, round_id, run_id): pass
    async def on_bets_done(self, round_id): pass
    async def on_resolved(self, round_id): pass


def simulate_fill(book: dict, limit: float, stake: float) -> dict:
    """Dry-run fill: walk the asks up to `limit`, spending at most `stake`."""
    shares = cost = 0.0
    for lvl in sorted(book.get("asks", []), key=lambda l: float(l["price"])):
        p, sz = float(lvl["price"]), float(lvl["size"])
        if p > limit + 1e-9 or cost >= stake:
            break
        take = min(sz, (stake - cost) / p)
        shares += take
        cost += take * p
    return {"shares": shares, "cost": cost, "avg_price": (cost / shares) if shares else None}


def agent_env(sock: str, shim_dir: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(SECRET_PREFIXES) and k not in DROP_VARS}
    env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
    env["BENCH_SOCKET"] = sock
    return env


def write_shim(shim_dir: Path) -> None:
    pkg_root = Path(__file__).resolve().parent.parent
    shim = shim_dir / "bench"
    shim.write_text(f'#!/bin/sh\nPYTHONPATH="{pkg_root}" exec "{sys.executable}" -m agentpitbench.benchcli "$@"\n')
    shim.chmod(0o755)


async def cli_version(cli: AgentCLI) -> str | None:
    if not shutil.which(cli.version_argv[0]):
        return None
    try:
        p = await asyncio.create_subprocess_exec(*cli.version_argv, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), 20)
        return out.decode(errors="replace").strip().splitlines()[0][:80] if out else None
    except Exception:
        return None


def build_prompt(market: dict, memory: list[dict]) -> str:
    tpl = resources.files("agentpitbench").joinpath("prompt.txt").read_text()
    prices = ", ".join(f"{o} {p:.3f}" for o, p in zip(market["outcomes_list"], market["prices_list"]))
    if memory:
        lines = []
        for h in memory:
            pick = h["outcome"] or "no bet"
            res = "won" if h["outcome"] and h["outcome"] == h["winner"] else "lost"
            lines.append(f"- {h['question'][:90]} | you picked {pick} | winner {h['winner']} | {res}"
                         f" | P&L {h['pnl'] or 0:+.1f}")
        played = len(memory)
        wins = sum(1 for h in memory if h["outcome"] and h["outcome"] == h["winner"])
        net = sum(h["pnl"] or 0 for h in memory)
        mem = (f"\nYour record over your last {played} rounds: {wins}-{played - wins}, net P&L {net:+.1f}.\n"
               + "\n".join(lines))
    else:
        mem = "\nThis is your first round."
    return tpl.format(question=market["question"], outcomes=" / ".join(market["outcomes_list"]),
                      prices=prices, end_date=market.get("endDate"), memory=mem).strip() + "\n"


class Round:
    """One market, three agents, one 5-minute window."""

    def __init__(self, s: Settings, db: DB, public: Agentpit, market: dict, publisher: Publisher,
                 agents: list[str] | None = None):
        self.s, self.db, self.public, self.market, self.pub = s, db, public, market, publisher
        self.agents = agents or s.agents
        self.round_id: int | None = None
        self.run_ids: dict[str, int] = {}
        self.bet_done: dict[str, asyncio.Event] = {}
        self.launched_at: dict[str, float] = {}
        self._bet_queue: list[tuple[str, dict, asyncio.Future]] = []
        self._bet_flush: asyncio.Task | None = None
        self._hooks: set[asyncio.Task] = set()
        self.clients = {a: Agentpit(s.api_url, s.agentpit_key(a)) for a in self.agents}

    # bench socket
    async def _serve(self, agent: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            req = json.loads(await reader.readline())
            result = await self._handle(agent, req)
            resp = {"ok": True, "result": result}
        except BetError as e:
            resp = {"ok": False, "error": str(e)}
        except Exception as e:  # never leak internals beyond a message
            log.exception("bench call failed for %s", agent)
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()

    def _token(self, outcome: str) -> tuple[str, str]:
        labels = self.market["outcomes_list"]
        match = [i for i, l in enumerate(labels) if l.lower() == outcome.strip().lower()]
        if not match:
            raise BetError(f"unknown outcome {outcome!r}; choose one of {labels}")
        i = match[0]
        return labels[i], self.market["token_ids"][i]

    async def _handle(self, agent: str, req: dict):
        cmd = req.get("cmd")
        m = self.market
        if cmd == "market":
            fresh = await self.public.market(m["id"]) or m
            return {"question": m["question"], "description": m.get("description"),
                    "outcomes": m["outcomes_list"], "prices": fresh["prices_list"],
                    "best_bid": fresh.get("bestBid"), "best_ask": fresh.get("bestAsk"),
                    "end_date": m.get("endDate"), "stake": self.s.stake,
                    "seconds_left": round(self._remaining(agent))}
        if cmd == "book":
            label, tok = self._token(req["outcome"])
            b = await self.public.book(tok)
            return {"outcome": label, "bids": b.get("bids", [])[:15], "asks": b.get("asks", [])[:15],
                    "last_trade_price": b.get("last_trade_price")}
        if cmd == "history":
            self._token(req["outcome"])
            return await self.public.prices_history(m["conditionId"])
        if cmd == "bet":
            if self.bet_done[agent].is_set():
                raise BetError("you already placed your bet this round")
            fut = asyncio.get_running_loop().create_future()
            self._bet_queue.append((agent, req, fut))
            if not self._bet_flush or self._bet_flush.done():
                self._bet_flush = asyncio.create_task(self._flush_bets())
            return await fut
        raise BetError(f"unknown command {cmd!r}")

    async def _flush_bets(self):
        """Bets arriving within 1 s of each other are sent in random order, so no agent always fills first."""
        await asyncio.sleep(1.0)
        batch, self._bet_queue = self._bet_queue, []
        random.shuffle(batch)
        for agent, req, fut in batch:
            try:
                fut.set_result(await self._place(agent, req))
            except Exception as e:
                fut.set_exception(e if isinstance(e, BetError) else BetError(str(e)))

    async def _place(self, agent: str, req: dict) -> dict:
        if self.bet_done[agent].is_set():
            raise BetError("you already placed your bet this round")
        label, tok = self._token(req["outcome"])
        book = await self.public.book(tok)
        ask = best_ask(book)
        limit = req.get("max_price") or ask
        if limit is None:
            raise BetError(f"no sellers for {label} right now; try another outcome or set --max-price")
        if not 0 < limit < 1:
            raise BetError("--max-price must be between 0 and 1")
        run_id = self.run_ids[agent]
        coid = f"apb-{self.round_id}-{agent}"
        if self.s.dry_run:
            fill, order_id = simulate_fill(book, limit, self.s.stake), f"dry-{uuid.uuid4().hex[:12]}"
            if not fill["shares"]:
                raise BetError(f"order did not fill: no asks at or below {limit}")
        else:
            try:
                resp = await self.clients[agent].buy_fak(tok, limit, self.s.stake, coid)
            except OrderRejected as e:
                raise BetError(f"order rejected: {e}")
            fill, order_id = resp["fill"], resp.get("orderID")
        self.db.record_bet(
            run_id, outcome=label, token_id=tok, rationale=req["rationale"][:2000],
            confidence=req.get("confidence"), max_price=req.get("max_price"), order_id=order_id,
            avg_price=fill["avg_price"], shares_filled=fill["shares"], stake_filled=round(fill["cost"], 6),
            decided_s=round(time.time() - self.launched_at[agent], 1),
        )
        self.bet_done[agent].set()
        log.info("round %s: %s bet %s @ %s", self.round_id, agent, label, fill["avg_price"])
        self._hook(self.pub.on_bet(self.round_id, run_id))
        return {"placed": True, "outcome": label, "avg_price": round(fill["avg_price"], 4),
                "shares": round(fill["shares"], 4), "stake_filled": round(fill["cost"], 2),
                "unspent": round(self.s.stake - fill["cost"], 2)}

    def _remaining(self, agent: str) -> float:
        return max(0.0, self.s.decision_limit_s - (time.time() - self.launched_at.get(agent, time.time())))

    def _hook(self, coro) -> None:
        """Run a publisher hook in the background; run() waits for all of them before on_bets_done."""
        t = asyncio.create_task(self._safe(coro))
        self._hooks.add(t)
        t.add_done_callback(self._hooks.discard)

    @staticmethod
    async def _safe(coro):
        try:
            await coro
        except Exception:
            log.exception("publisher hook failed")

    # agent processes
    async def _run_agent(self, agent: str, prompt: str) -> None:
        cli = CLIS[agent]
        work = Path(tempfile.mkdtemp(prefix=f"apb-{self.round_id}-{agent}-"))
        ctl = Path(tempfile.mkdtemp(prefix="apbs-"))
        sock = str(ctl / "s")
        write_shim(ctl)
        server = await asyncio.start_unix_server(lambda r, w: self._serve(agent, r, w), path=sock)
        tdir = self.s.transcripts_dir / str(self.round_id)
        tdir.mkdir(parents=True, exist_ok=True)
        tpath = tdir / f"{agent}.txt"
        deadline = self.launched_at[agent] + self.s.decision_limit_s
        attempts, exit_reason, model = 0, "timeout", None
        try:
            with tpath.open("wb") as tf:
                tf.write(f"$ {' '.join(cli.argv[:2])} <prompt>\n--- prompt ---\n{prompt}\n--- output ---\n".encode())
                while True:
                    attempts += 1
                    if not shutil.which(cli.argv[0]):
                        tf.write(f"[bench] {cli.argv[0]} not installed\n".encode())
                        exit_reason = "crash"
                        break
                    proc = await asyncio.create_subprocess_exec(
                        *cli.command(prompt), cwd=work, env=agent_env(sock, str(ctl)),
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                    out_task = asyncio.create_task(self._pump(proc, tf))
                    bet_task = asyncio.create_task(self.bet_done[agent].wait())
                    done, _ = await asyncio.wait({out_task, bet_task}, timeout=max(0, deadline - time.time()),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if bet_task in done:
                        # give the agent a few seconds to wrap up, then stop it
                        try:
                            await asyncio.wait_for(asyncio.shield(out_task), 10)
                        except asyncio.TimeoutError:
                            pass
                    self._kill(proc)
                    await asyncio.gather(out_task, return_exceptions=True)
                    bet_task.cancel()
                    model = model or self._model(cli, tpath)
                    if self.bet_done[agent].is_set():
                        exit_reason = "bet"
                        break
                    if time.time() >= deadline:
                        exit_reason = "timeout"
                        break
                    # exited early without a bet: one restart if enough time remains
                    exit_reason = "crash"
                    if attempts >= 2 or deadline - time.time() < self.s.restart_min_remaining_s:
                        break
                    tf.write(f"\n[bench] process exited with {proc.returncode} before betting; restarting\n".encode())
        finally:
            server.close()
            shutil.rmtree(ctl, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
        if not self.bet_done[agent].is_set():
            self.bet_done[agent].set()
            self.db.record_bet(self.run_ids[agent], outcome=None, decided_s=None)
            self._hook(self.pub.on_bet(self.round_id, self.run_ids[agent]))
        self.db.finish_run(self.run_ids[agent], exit_reason, str(tpath), model)
        log.info("round %s: %s finished (%s)", self.round_id, agent, exit_reason)

    @staticmethod
    async def _pump(proc, tf):
        while chunk := await proc.stdout.read(65536):
            tf.write(chunk)
            tf.flush()
        await proc.wait()

    @staticmethod
    def _kill(proc):
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @staticmethod
    def _model(cli: AgentCLI, tpath: Path) -> str | None:
        text = tpath.read_text(errors="replace")
        text = text.split("--- output ---", 1)[-1]
        for pat in cli.model_patterns:
            m = re.search(pat, text, re.M)
            if m:
                return m.group(1)[:80]
        return None

    async def run(self) -> int:
        self.round_id = self.db.create_round(self.market)
        versions = await asyncio.gather(*(cli_version(CLIS[a]) for a in self.agents))
        for a, v in zip(self.agents, versions):
            self.run_ids[a] = self.db.create_run(self.round_id, a, cli_version=v)
            self.bet_done[a] = asyncio.Event()
        prompts = {a: build_prompt(self.market, self.db.agent_history(a)) for a in self.agents}
        now = time.time()
        for a in self.agents:
            self.launched_at[a] = now
        await asyncio.gather(*(self._run_agent(a, prompts[a]) for a in self.agents))
        self.db.set_round(self.round_id, state="awaiting_resolution")
        await asyncio.gather(*list(self._hooks))
        await self._safe(self.pub.on_bets_done(self.round_id))
        for c in self.clients.values():
            await c.close()
        return self.round_id


class BetError(Exception):
    pass
