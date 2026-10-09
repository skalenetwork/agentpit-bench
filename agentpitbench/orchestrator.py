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
from .config import Settings, AGENT_MODELS
from .db import DB
from .virality import clean_quote

log = logging.getLogger(__name__)

SECRET_PREFIXES = ("AGENTPIT_", "X_", "XAI_", "GH_", "BENCH_SECRETS", "SMTP_", "HF_", "KAGGLE_", "HUGGING")
DROP_VARS = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "VIRTUAL_ENV", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}
HOME_PREFIXES = ("GROK_", "XDG_CONFIG_", "XDG_DATA_", "XDG_CACHE_", "XDG_STATE_")

# The only files an agent sees from the operator's home: its login. Everything else (config, skills,
# MCP servers, hooks, memory) stays out. agy keeps its login in the desktop keyring (session bus).
AUTH_FILES = {"claude": [".claude/.credentials.json"], "codex": [".codex/auth.json"],
              "grok": [".grok/auth.json"], "agy": []}
CLAUDE_JSON_KEYS = ("hasCompletedOnboarding", "oauthAccount", "userID", "lastOnboardingVersion")
_auth_locks: dict[str, asyncio.Lock] = {}


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
        ["claude", "-p", "{prompt}", "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
         "--model", AGENT_MODELS["claude"][0], "--effort", AGENT_MODELS["claude"][2]],
        ["claude", "--version"],
        [r'"model"\s*:\s*"([^"]+)"'],
    ),
    "codex": AgentCLI(
        "codex",
        ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
         "-m", AGENT_MODELS["codex"][0], "-c", f'model_reasoning_effort="{AGENT_MODELS["codex"][2]}"', "{prompt}"],
        ["codex", "--version"],
        [r"^model:\s*(\S+)", r'"model"\s*:\s*"([^"]+)"'],
    ),
    "agy": AgentCLI(
        "agy",
        ["agy", "-p", "{prompt}", "--dangerously-skip-permissions", "--model", AGENT_MODELS["agy"][0]],
        ["agy", "--version"],
        [r"[Mm]odel:\s*(\S+)", r'"model"\s*:\s*"([^"]+)"'],
    ),
    "grok": AgentCLI(
        "grok",
        ["grok", "-p", "{prompt}", "--always-approve", "--output-format", "streaming-json",
         "-m", AGENT_MODELS["grok"][0], "--reasoning-effort", AGENT_MODELS["grok"][2]],
        ["grok", "--version"],
        [r'"modelUsage"\s*:\s*\{\s*"([^"]+)"', r'"model"\s*:\s*"([^"]+)"'],
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


def agent_env(sock: str, shim_dir: str, home: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(SECRET_PREFIXES) and k not in DROP_VARS}
    env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
    env["BENCH_SOCKET"] = sock
    if home is not None:
        env = {k: v for k, v in env.items() if not k.startswith(HOME_PREFIXES)}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
                   XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".local/state"),
                   DISABLE_AUTOUPDATER="1")
    return env


def home_model(agent: str, home: Path) -> str | None:
    """Model names a CLI writes only to its own files, not its output. agy logs the label it runs on."""
    if agent != "agy":
        return None
    for log_file in sorted(home.glob(".gemini/antigravity-cli/log/cli-*.log"), reverse=True):
        m = re.search(r'selected model override to backend: label="([^"]+)"', log_file.read_text(errors="replace"))
        if m:
            return m.group(1)[:80]
    return None


def _digest(path: Path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


class CleanHome:
    """A throwaway HOME holding only the agent's login, copied from the operator's home.

    Logins share the operator's tokens, and some vendors rotate refresh tokens, so whenever the agent's
    copy changes it is written straight back (atomically, 0600) to the operator's file."""

    def __init__(self, agent: str, real_home: Path | None = None):
        self.agent = agent
        self.real = real_home or Path(os.environ.get("BENCH_REAL_HOME") or Path.home())
        self.home: Path | None = None
        self.seen: dict[str, str | None] = {}

    def setup(self) -> Path:
        self.home = Path(tempfile.mkdtemp(prefix=f"apbh-{self.agent}-"))
        for rel in AUTH_FILES.get(self.agent, []):
            src, dst = self.real / rel, self.home / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.copyfile(src, dst)
                dst.chmod(0o600)
            self.seen[rel] = _digest(dst)
        if self.agent == "claude":  # just enough of ~/.claude.json to skip onboarding; never synced back
            try:
                full = json.loads((self.real / ".claude.json").read_text())
            except (OSError, ValueError):
                full = {}
            (self.home / ".claude.json").write_text(json.dumps({k: full[k] for k in CLAUDE_JSON_KEYS if k in full}))
        return self.home

    async def sync(self) -> None:
        lock = _auth_locks.setdefault(self.agent, asyncio.Lock())
        async with lock:
            for rel, seen in self.seen.items():
                cur = _digest(self.home / rel)
                if cur and cur != seen:
                    dst = self.real / rel
                    tmp = dst.with_name(dst.name + ".apb-tmp")
                    shutil.copyfile(self.home / rel, tmp)
                    tmp.chmod(0o600)
                    os.replace(tmp, dst)
                    self.seen[rel] = cur
                    log.info("%s login refreshed during a run; synced back to %s", self.agent, dst)

    async def watch(self, every: float = 2.0) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.sync()
            except Exception:
                log.exception("login sync for %s failed", self.agent)

    def cleanup(self) -> None:
        if self.home:
            shutil.rmtree(self.home, ignore_errors=True)


async def warm_login(agent: str, timeout: float = 120) -> bool:
    """Run the agent once on a trivial prompt so any pending token refresh happens once, before parallel
    rounds each copy the same (soon rotated) refresh token."""
    cli = CLIS[agent]
    if not shutil.which(cli.argv[0]):
        return False
    ch = CleanHome(agent)
    home = ch.setup()
    try:
        env = agent_env("", str(home), home)
        argv = cli.command("Reply with exactly: ok")
        if shutil.which("bwrap"):
            argv = sandboxed(argv, home, home, home)
        p = await asyncio.create_subprocess_exec(*argv, cwd=home, env=env,
                                                 stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            await asyncio.wait_for(p.wait(), timeout)
        except asyncio.TimeoutError:
            Round._kill(p)
        await ch.sync()
        return p.returncode == 0
    finally:
        ch.cleanup()


def write_shim(shim_dir: Path) -> None:
    """`bench` = a standalone copy of benchcli.py (stdlib only) run by the system Python, so nothing in the
    agent's reach points back at the repo or its virtualenv."""
    src = Path(__file__).resolve().parent / "benchcli.py"
    shutil.copyfile(src, shim_dir / "benchcli.py")
    py = next((p for p in ("/usr/bin/python3", "/usr/local/bin/python3") if os.path.exists(p)), sys.executable)
    shim = shim_dir / "bench"
    shim.write_text(f'#!/bin/sh\nexec "{py}" -I "{shim_dir / "benchcli.py"}" "$@"\n')
    shim.chmod(0o755)


def _program_paths(argv0: str) -> tuple[str, list[Path]]:
    """The agent CLI's real executable and the directories it needs (read-only) inside the sandbox."""
    real = Path(os.path.realpath(shutil.which(argv0) or argv0))
    binds = [real.parent]
    if real.suffix in (".js", ".mjs", ".cjs"):  # a node package (codex): the whole package plus node itself
        binds = [next((d for d in real.parents if (d / "package.json").exists()), real.parent)]
        node = shutil.which("node")
        if node:
            binds.append(Path(os.path.realpath(node)).parent)
    return str(real), binds


def sandboxed(cmd: list[str], home: Path, work: Path, ctl: Path) -> list[str]:
    """Wrap an agent command in bubblewrap: the system is read-only, every home directory, /tmp and the
    bench's own tree are hidden, and only the agent's program, clean home, work dir and bench socket show.
    Network stays on (agents may research online)."""
    exe, binds = _program_paths(cmd[0])
    repo_top = "/" + Path(__file__).resolve().parts[1]
    hide = ["/home", "/root", "/tmp", "/var/tmp", "/mnt", "/media", "/srv", repo_top]
    a = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "--die-with-parent"]
    for h in dict.fromkeys(hide):
        if os.path.isdir(h) and h != "/":
            a += ["--tmpfs", h]
    for b in binds:
        a += ["--ro-bind", str(b), str(b)]
    for d in (home, work, ctl):
        a += ["--bind", str(d), str(d)]
    return a + ["--chdir", str(work), "--", exe, *cmd[1:]]


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
    stake = market.get("bench_stake") or 100
    high = " HIGH STAKES round: this bet counts at full size in the season standings." if market.get("high_stakes") else ""
    return tpl.format(question=market["question"], outcomes=" / ".join(market["outcomes_list"]),
                      prices=prices, end_date=market.get("endDate"), memory=mem,
                      stake=f"{stake:g}", high_stakes=high).strip() + "\n"


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
        try:
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except ConnectionError:  # the agent hung up before reading the reply; the bet is already recorded
            log.info("%s disconnected before reading the bench reply", agent)
        finally:
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
                    "end_date": m.get("endDate"), "stake": self._stake(),
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
        # no --max-price: accept up to default_slippage past the best ask, so a tiny top-of-book offer
        # can't leave a 100-token bet nearly empty
        limit = req.get("max_price") or (min(0.99, round(ask + self.s.default_slippage, 3)) if ask else None)
        if limit is None:
            raise BetError(f"no sellers for {label} right now; try another outcome or set --max-price")
        if not 0 < limit < 1:
            raise BetError("--max-price must be between 0 and 1")
        run_id = self.run_ids[agent]
        coid = f"apb-{self.round_id}-{agent}"
        if self.s.dry_run:
            fill, order_id = simulate_fill(book, limit, self._stake()), f"dry-{uuid.uuid4().hex[:12]}"
            if not fill["shares"]:
                raise BetError(f"order did not fill: no asks at or below {limit}")
        else:
            try:
                # size by walking the live book: sizing at the limit price under-spends when it fills lower
                plan = simulate_fill(book, limit, self._stake())
                if not plan["shares"]:
                    raise BetError(f"order would not fill: no asks at or below {limit}")
                resp = await self.clients[agent].buy_fak(tok, limit, self._stake(), coid, size=plan["shares"])
            except OrderRejected as e:
                raise BetError(f"order rejected: {e}")
            fill, order_id = resp["fill"], resp.get("orderID")
        quote, quote_dropped = clean_quote(req.get("quote"))
        self.db.record_bet(
            run_id, outcome=label, token_id=tok, rationale=req["rationale"][:2000], quote=quote,
            tx_hashes=json.dumps(fill.get("tx_hashes") or []),
            confidence=req.get("confidence"), max_price=req.get("max_price"), order_id=order_id,
            avg_price=fill["avg_price"], shares_filled=fill["shares"], stake_filled=round(fill["cost"], 6),
            decided_s=round(time.time() - self.launched_at[agent], 1),
        )
        self.bet_done[agent].set()
        log.info("round %s: %s bet %s @ %s", self.round_id, agent, label, fill["avg_price"])
        self._hook(self.pub.on_bet(self.round_id, run_id))
        resp = {"placed": True, "outcome": label, "avg_price": round(fill["avg_price"], 4),
                "shares": round(fill["shares"], 4), "stake_filled": round(fill["cost"], 2),
                "unspent": round(self._stake() - fill["cost"], 2)}
        if quote:
            resp["quote"] = quote
        elif quote_dropped:
            resp["quote_dropped"] = quote_dropped
        return resp

    def _stake(self) -> float:
        """High-Stakes Friday rounds carry their own stake in the market snapshot."""
        return float(self.market.get("bench_stake") or self.s.stake)

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
        home_box = CleanHome(agent)
        home = home_box.setup()
        watcher = asyncio.create_task(home_box.watch())
        try:
            with tpath.open("wb") as tf:
                tf.write(f"$ {' '.join(cli.argv[:2])} <prompt>\n--- prompt ---\n{prompt}\n--- output ---\n".encode())
                while True:
                    attempts += 1
                    if not shutil.which(cli.argv[0]):
                        tf.write(f"[bench] {cli.argv[0]} not installed\n".encode())
                        exit_reason = "crash"
                        break
                    argv = cli.command(prompt)
                    if self.s.sandbox:
                        if not shutil.which("bwrap"):
                            tf.write(b"[bench] bwrap not installed; refusing to run the agent unsandboxed\n")
                            exit_reason = "crash"
                            break
                        argv = sandboxed(argv, home, work, ctl)
                    proc = await asyncio.create_subprocess_exec(
                        *argv, cwd=work, env=agent_env(sock, str(ctl), home),
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
                    model = model or self._model(cli, tpath) or home_model(agent, home)
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
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            try:
                await home_box.sync()  # last chance to save a refreshed login before the home goes
            except Exception:
                log.exception("final login sync for %s failed", agent)
            home_box.cleanup()
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
