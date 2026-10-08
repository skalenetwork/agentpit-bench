"""Post-match press conference: after a round resolves, each agent that bet on the losing side gets one short,
clean-home run to give a one-line statement. Statements go on the round page and in one link-free reply under
the results post. It never touches scoring, and a failure never blocks results."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil

from .config import AGENT_NAMES, Settings
from .db import DB
from .orchestrator import CLIS, CleanHome, Round, agent_env, sandboxed
from .virality import clean_quote

log = logging.getLogger(__name__)

STATEMENT_MAX = 200
PROMPT = ('You lost AgentpitBench round {n}: you bet {outcome} at {price} on "{question}"; it resolved {winner}. '
          "Give a one-line statement to the press (max 200 characters). No hashtags, links or @mentions.")


def prompt_for(rnd, e) -> str:
    price = f"{round(e['avg_price'] * 100)}¢" if e["avg_price"] is not None else "market price"
    return PROMPT.format(n=rnd["round_id"], outcome=e["outcome"], price=price, question=rnd["question"],
                         winner=rnd["winner"])


def extract(agent: str, output: str) -> str | None:
    """The agent's final plain-text line, from each CLI's output format."""
    text = None
    if agent == "claude":  # stream-json: the final {"type": "result", "result": "..."}
        for line in output.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("type") == "result" and isinstance(d.get("result"), str):
                text = d["result"]
    elif agent == "grok":  # streaming-json: {"type": "text", "data": "..."} chunks
        parts = []
        for line in output.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("type") == "text" and isinstance(d.get("data"), str):
                parts.append(d["data"])
        text = "".join(parts)
    else:  # codex and agy print plain text; codex ends with "tokens used / N / <final message>"
        text = output
    lines = [l.strip().strip('"“”').strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l and l.lower() != "tokens used" and not l.replace(",", "").isdigit()]
    return lines[-1] if lines else None


async def run_cli(s: Settings, agent: str, prompt: str) -> str:
    """One clean-home run of the agent's CLI with a hard time limit; returns its stdout (and stderr)."""
    cli = CLIS[agent]
    if not shutil.which(cli.argv[0]):
        return ""
    if s.sandbox and not shutil.which("bwrap"):
        log.warning("bwrap not installed; skipping %s's press statement rather than running unsandboxed", agent)
        return ""
    home_box = CleanHome(agent)
    home = home_box.setup()
    try:
        argv = cli.command(prompt)
        if s.sandbox:
            argv = sandboxed(argv, home, home, home)
        p = await asyncio.create_subprocess_exec(
            *argv, cwd=home, env=agent_env("", str(home), home),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), s.press_timeout_s)
        except asyncio.TimeoutError:
            Round._kill(p)
            await p.wait()
            return ""
        return out.decode(errors="replace")
    finally:
        await home_box.sync()
        home_box.cleanup()


async def collect(s: Settings, db: DB, rnd: dict, runner=None) -> list[dict]:
    """Ask every losing bettor without a statement yet; store each clean line. Returns all statements."""
    runner = runner or run_cli
    if rnd["state"] != "resolved":
        return []
    losers = [e for e in db.round_entries(rnd["round_id"])
              if e["outcome"] and e["outcome"] != rnd["winner"] and e["statement"] is None]

    async def one(e):
        try:
            raw = await runner(s, e["agent"], prompt_for(rnd, e))
            line, why = clean_quote(extract(e["agent"], raw), STATEMENT_MAX)
            if why:
                log.info("press statement from %s dropped: %s", e["agent"], why)
            # "" marks "asked, nothing usable" so a retry never re-runs the agent
            db.x("UPDATE bets SET statement=? WHERE run_id=? AND statement IS NULL", line or "", e["run_id"])
        except Exception:
            log.exception("press statement from %s failed", e["agent"])

    await asyncio.gather(*(one(e) for e in losers))
    return [{"agent": e["agent"], "name": AGENT_NAMES.get(e["agent"], e["agent"]), "statement": e["statement"]}
            for e in db.round_entries(rnd["round_id"]) if e["statement"]]


def texts(statements: list[dict], limit: int = 280) -> list[str]:
    """'Post-match statements:' plus one line per loser, in at most two link-free replies."""
    from .twitter import tweet_len
    head = "Post-match statements:"
    lines = [f"{st['name']}: “{st['statement']}”" for st in statements]
    out, cur = [], head
    for line in lines:
        if tweet_len(cur + "\n" + line) <= limit:
            cur += "\n" + line
            continue
        out.append(cur)
        cur = line if tweet_len(line) <= limit else line[:limit - 2] + "…”"
    out.append(cur)
    if len(out) > 2:  # keep it to two replies: the rest are on the round page
        out = out[:2]
    return out if lines else []
