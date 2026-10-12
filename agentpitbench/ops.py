"""Nightly ops agent (docs/ai-automation-spec.md §11): PRs only.

`agentpitbench ops nightly`, run by the agentpitbench-ops.timer (never by the bench service):

1. snapshot (trusted code, no AI): the scrubbed last-24 h log, ai_calls statuses, open anomalies, round counts,
   missing translations. No DB file, no forecasts, no transcripts, no sealed pick.
2. checkout: a fresh `git clone` of main into ~/.cache/agentpitbench/ops/wt-<day>: never the live worktree.
3. agent: `claude -p` (opai's own login, opai_ops_model) inside its own bubblewrap profile, ops_sandbox(): the
   checkout (its .git read-only), the snapshot (read-only), the opai config dir and the venv (read-only) are all
   it sees. No bench, X, agentpit or GitHub credential, no var/, no home directory.
4. gate (trusted code): forbidden paths (the methodology-locked files among them), sandbox/secret code, a hash
   check of forecast.PROMPT and AGENT_MODELS against the lock, size, then the tests, run in the same sandbox.
5. publish: with GH_OPS_PR_TOKEN (secrets.env; a fine-grained PAT: one repo, Pull requests RW, Contents RW
   limited to branches) the runner pushes ops/<day> and opens a PR. Without it: a local patch plus a summary in
   var/ops/<day>/ and an info alert. main is branch-protected, so a PR is the only route in.
6. the checkout is deleted; one line goes into the digest (kv ops_last).

The runner itself writes to the bench's data_dir (var/ops/, var/proposals/, alerts, ai_calls): that's the
trusted side. The "never the live worktree" rule is about the agent's checkout.
"""
from __future__ import annotations

import ast
import base64
import dataclasses
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

PKG = Path(__file__).resolve().parent
CACHE = Path.home() / ".cache" / "agentpitbench" / "ops"
LIVE = "/d/PythonProject/agentpitbench-live"
TOKEN_VAR = "GH_OPS_PR_TOKEN"
TASK = "ops"
TEST_TIMEOUT_S = 1800
BOT = ("agentpitbench ops agent", "ops-agent@agentpitbench.org")

# paths the agent's diff may never touch (fnmatch, repo-relative)
FORBIDDEN = (
    "agentpitbench/methodology.json", "agentpitbench/methodology.py", "agentpitbench/prompt.txt",
    "agentpitbench/prompts/*", "agentpitbench/incidents.json", "agentpitbench/ops.py",
    "agentpitbench/benchcli.py", "bench.toml", "pyproject.toml", ".github/*", "integrations/systemd/*",
    "tests/test_methodology.py", "tests/test_ops.py", "tests/conftest.py", ".gitignore", ".gitattributes",
    "*/.gitattributes", ".ops/*",
)
# names whose appearance on a changed line rejects the diff: the sandbox and the secret filters
FORBIDDEN_RE = re.compile(r"\b(ENV_ALLOW|SECRET_PREFIXES|HOME_PREFIXES|DROP_VARS|sandboxed|CleanHome|agent_env|"
                          r"ops_sandbox|child_env|AUTH_FILES|load_secrets|SECRETS_FILE)\b")


# ---------------------------------------------------------------- git helpers (trusted side)

def git_env() -> dict:
    """No user/system git config (no insteadOf, credential helpers, fsmonitor), no hooks."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


def git(cwd: Path | str, *args: str, env: dict | None = None, check: bool = True,
        timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
                          cwd=cwd, env=env or git_env(), capture_output=True, text=True, check=check, timeout=timeout)


def source_repo(s: Settings) -> Path:
    """Where the checkout is cloned from: ops_repo, else the top of this package's git repository (the main
    repo for a worktree, via the common dir)."""
    if s.ops_repo:
        return Path(os.path.expanduser(s.ops_repo))
    common = git(PKG, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(common).parent


def clone(repo: Path, dest: Path, branch: str) -> str:
    """A fresh, self-contained clone of main (local objects are hardlinked) on a new branch. Returns the base sha."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if Path(LIVE).exists() and dest.resolve().is_relative_to(Path(LIVE).resolve()):
        raise RuntimeError("the ops checkout may never live inside the live worktree")
    git(dest.parent, "clone", "--quiet", "--branch", "main", "--single-branch", str(repo), str(dest))
    git(dest, "checkout", "--quiet", "-b", branch)
    return git(dest, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------- snapshot

def snapshot(s: Settings, db: DB, out_dir: Path, now: float | None = None,
             journal: Callable[[float, float], list[str]] | None = None) -> Path:
    """The agent's only view of production: scrubbed and aggregated. Never the DB, forecasts or transcripts."""
    from . import anomaly, i18n_fill, logs
    now = time.time() if now is None else now
    since = now - 86400
    lines = (journal or logs.journal)(since, now)
    calls = [dict(r) for r in db.q("SELECT task, status, COUNT(*) n FROM ai_calls WHERE started_at>=? "
                                   "GROUP BY task, status ORDER BY task, status", since)]
    rounds = {r["state"]: r["n"] for r in db.q("SELECT state, COUNT(*) n FROM rounds WHERE started_at>=? "
                                               "GROUP BY state", since)}
    runs = {r["exit_reason"] or "unfinished": r["n"] for r in db.q(
        "SELECT r.exit_reason, COUNT(*) n FROM runs r JOIN rounds ro USING(round_id) WHERE r.started_at>=? "
        "AND ro.state!='running' GROUP BY r.exit_reason", since)}
    no_bet = db.one("SELECT COUNT(*) n FROM bets b JOIN runs r USING(run_id) JOIN rounds ro USING(round_id) "
                    "WHERE r.started_at>=? AND ro.state!='running' AND b.outcome IS NULL", since)["n"]
    try:
        missing = {lang: len(ids) for lang, ids in i18n_fill.missing().items() if ids}
    except Exception:
        log.exception("i18n missing check failed")
        missing = {}
    anomalies = [{k: a[k] for k in ("anomaly_id", "detector", "severity", "detected_at", "detail_json")}
                 for a in anomaly.listing(db, include_closed=False)]
    snap = {"generated_at": now, "window_s": 86400, "log": logs.clean(lines, db)[-400:], "ai_calls": calls,
            "anomalies": anomalies, "rounds_by_state": rounds, "runs_by_exit": runs, "no_bet_runs": no_bet,
            "missing_translations": missing}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "snapshot.json"
    path.write_text(json.dumps(snap, indent=1, default=str))
    return path


# ---------------------------------------------------------------- the sandbox

def _program_dirs(exe: str) -> tuple[str, list[Path]]:
    from .orchestrator import _program_paths
    return _program_paths(exe)


def ops_sandbox(cmd: list[str], checkout: Path, snapshot_dir: Path, config_dir: Path, venv: Path,
                home: Path, extra_ro: list[Path] = ()) -> list[str]:
    """The ops agent's bubblewrap profile, separate from the contestants' sandboxed() and sharing no homes.
    System read-only; every home directory, /tmp, the repo tree (and so the live worktree and every var/) hidden;
    then only these come back: the checkout (rw, its .git read-only), the snapshot (ro), opai's config dir (rw),
    the venv (ro), a throwaway HOME (rw) and the agent program's own directories (ro)."""
    exe, binds = _program_dirs(cmd[0])
    if Path(cmd[0]).is_relative_to(venv):   # the venv's python by its venv path, or the venv is lost
        exe, binds = cmd[0], []
    repo_top = "/" + PKG.parts[1]
    hide = ["/home", "/root", "/tmp", "/var/tmp", "/mnt", "/media", "/srv", "/run/user", repo_top, LIVE]
    a = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "--die-with-parent",
         "--new-session"]
    for h in dict.fromkeys(hide):
        if os.path.isdir(h) and h != "/":
            a += ["--tmpfs", h]
    py_home = Path(os.path.realpath(venv / "bin" / "python")).parent.parent
    for b in [*binds, venv, py_home, *extra_ro]:
        if Path(b).exists():
            a += ["--ro-bind", str(b), str(b)]
    for d in (home, config_dir, checkout):
        a += ["--bind", str(d), str(d)]
    a += ["--ro-bind", str(checkout / ".git"), str(checkout / ".git")]
    a += ["--ro-bind", str(snapshot_dir), str(snapshot_dir)]
    return a + ["--chdir", str(checkout), "--", exe, *cmd[1:]]


def agent_env_for(s: Settings, checkout: Path, home: Path) -> dict:
    """opai's child env (the ENV_ALLOW allowlist, no SECRET_PREFIXES vars: no GH_, X_, AGENTPIT_), its own
    CLAUDE_CONFIG_DIR, a throwaway HOME and the checkout first on the import path."""
    from . import opai
    env = opai.child_env(s)
    env.update(HOME=str(home), PYTHONPATH=str(checkout), GIT_OPTIONAL_LOCKS="0", PYTHONDONTWRITEBYTECODE="1",
               DISABLE_AUTOUPDATER="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    for p in _extra_ro():   # Chromium for the card tests, bound read-only at its real path
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(p)
    return env


def _extra_ro() -> list[Path]:
    """Read-only extras the test suite needs inside the sandbox (Chromium for the card renders)."""
    return [p for p in (Path.home() / ".cache" / "ms-playwright",) if p.exists()]


@dataclass
class Ctx:
    s: Settings
    checkout: Path
    snapshot_dir: Path
    venv: Path
    home: Path
    config_dir: Path

    def wrap(self, cmd: list[str]) -> list[str]:
        return ops_sandbox(cmd, self.checkout, self.snapshot_dir, self.config_dir, self.venv, self.home, _extra_ro())

    @property
    def env(self) -> dict:
        return agent_env_for(self.s, self.checkout, self.home)


def _run(argv: list[str], env: dict, cwd: Path, timeout: float, stdin: str | None = None) -> tuple[int | None, str]:
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(stdin, timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = p.communicate()
        return None, out or ""


def agent_argv(s: Settings, venv: Path) -> list[str]:
    from . import opai
    exe = shutil.which("claude") or "claude"
    return [exe, "-p", "--model", s.opai_ops_model, "--output-format", "json", "--safe-mode",
            "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands", "--system-prompt", opai.prompt_path(opai.SYSTEM_PROMPT).read_text().strip(),
            "--tools", "Read,Grep,Glob,Edit,Write,Bash",
            "--allowedTools", "Read", "Grep", "Glob", "Edit", "Write",
            f"Bash({venv}/bin/python -m pytest:*)", "Bash(git diff:*)", "Bash(git status:*)"]


def run_agent(ctx: Ctx, snapshot_path: Path) -> tuple[str, str]:
    """(status, detail): ok / timeout / crash / quota / auth."""
    from . import opai
    from .orchestrator import infra_failure
    prompt = opai.prompt_text(TASK)
    data = {"snapshot": str(snapshot_path), "checkout": str(ctx.checkout), "python": str(ctx.venv / "bin" / "python"),
            "day": snapshot_path.parent.name}
    code, out = _run(ctx.wrap(agent_argv(ctx.s, ctx.venv)), ctx.env, ctx.checkout, ctx.s.opai_ops_timeout_s,
                     f"{prompt.strip()}\n\n{opai.data_block(data)}\n")
    if code is None:
        return "timeout", f"over {ctx.s.opai_ops_timeout_s}s"
    if code != 0:
        kind = infra_failure(out)
        return (kind if kind in ("quota", "auth") else "crash"), out[-300:]
    return "ok", ""


def run_tests(ctx: Ctx) -> tuple[bool, str]:
    """The checkout's test suite, in the same sandbox as the agent (agent-written code never runs with the
    runner's privileges). First proves the checkout, not an installed copy, is what gets imported."""
    py = str(ctx.venv / "bin" / "python")
    code, out = _run(ctx.wrap([py, "-c", "import agentpitbench; print(agentpitbench.__file__)"]), ctx.env,
                     ctx.checkout, 120)
    where = out.strip().splitlines()[-1] if out.strip() else ""
    if code != 0 or not where.startswith(str(ctx.checkout)):
        return False, f"agentpitbench imports from {where or '?'}, not the checkout"
    code, out = _run(ctx.wrap([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"]), ctx.env, ctx.checkout,
                     TEST_TIMEOUT_S)
    summary = next((ln for ln in reversed(out.strip().splitlines()) if re.search(r"\d+ (passed|failed|error)", ln)),
                   out.strip().splitlines()[-1] if out.strip() else "no output")
    return code == 0, summary.strip("= ")


# ---------------------------------------------------------------- the gate (trusted, no AI)

def numstat(checkout: Path, base: str) -> list[tuple[str, int | None, int | None]]:
    out = git(checkout, "diff", "--cached", "--numstat", "--no-renames", base).stdout
    rows = []
    for ln in out.splitlines():
        add, rem, path = ln.split("\t", 2)
        rows.append((path, None if add == "-" else int(add), None if rem == "-" else int(rem)))
    return rows


def check_diff(files: list[tuple[str, int | None, int | None]], patch: str, max_lines: int = 400) -> list[str]:
    """Reasons to refuse a diff; empty = it may go out. Pure, so tests feed it synthetic diffs."""
    reasons = []
    if not files:
        return ["no diff"]
    for path, add, rem in files:
        if any(fnmatch.fnmatch(path, pat) for pat in FORBIDDEN):
            reasons.append(f"forbidden path: {path}")
        if add is None:
            reasons.append(f"binary file: {path}")
    for ln in patch.splitlines():
        if ln.startswith(("new file mode 120000", "new mode 120000")):
            reasons.append("symlink in the diff")
        elif ln[:1] in "+-" and not ln.startswith(("+++", "---")) and (m := FORBIDDEN_RE.search(ln)):
            reasons.append(f"sandbox/secret code touched: {m.group(1)}")
    total = sum((a or 0) + (r or 0) for _, a, r in files)
    if total > max_lines:
        reasons.append(f"diff too large: {total} lines > {max_lines}")
    return list(dict.fromkeys(reasons))


def _literal(src: str, name: str):
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"{name} not found as a literal")


def locked_hashes(checkout: Path) -> list[str]:
    """The methodology-hashed inputs in the checkout still match the lock: read by parsing, never by importing
    the agent's code."""
    from . import methodology
    lock = methodology.load(checkout / "agentpitbench" / "methodology.json")
    sha = lambda b: hashlib.sha256(b.encode() if isinstance(b, str) else b).hexdigest()  # noqa: E731
    pkg = checkout / "agentpitbench"
    try:
        cur = {"prompt.txt": sha((pkg / "prompt.txt").read_bytes()),
               "sweep_prompt": sha(_literal((pkg / "forecast.py").read_text(), "PROMPT")),
               "rater_prompt": sha((pkg / "prompts" / f"rate.v{lock['selection']['rater']['prompt_version']}.md")
                                   .read_bytes()),
               "agent_models": sha(json.dumps(_literal((pkg / "config.py").read_text(), "AGENT_MODELS"),
                                              sort_keys=True))}
    except (OSError, ValueError, SyntaxError) as e:
        return [f"methodology inputs unreadable: {e}"]
    return [f"methodology hash drift: {k}" for k, v in lock["hashes"].items() if cur.get(k) != v]


def gate(ctx: Ctx | None, checkout: Path, base: str, s: Settings,
         tests: Callable[[], tuple[bool, str]] | None = None) -> tuple[list[str], str, str]:
    """(reasons, patch, test summary). Stages everything first (.ops/ is already gone)."""
    git(checkout, "add", "-A")
    files = numstat(checkout, base)
    patch = git(checkout, "diff", "--cached", "--no-renames", base).stdout
    reasons = check_diff(files, patch, s.ops_max_diff_lines)
    if not reasons:
        reasons += locked_hashes(checkout)
    summary = ""
    if not reasons:
        ok, summary = (tests or (lambda: run_tests(ctx)))()
        if not ok:
            reasons.append(f"tests fail: {summary}")
    return reasons, patch, summary


# ---------------------------------------------------------------- publishing (runner only)

def push(checkout: Path, slug: str, branch: str, token: str) -> None:
    """To an explicit HTTPS URL; the token travels in the environment (an http.extraheader), never in argv."""
    env = git_env()
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
               GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    git(checkout, "push", "--quiet", f"https://github.com/{slug}.git", f"HEAD:refs/heads/{branch}", env=env,
        timeout=180)


def github(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agentpitbench-ops"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def open_pr(slug: str, branch: str, title: str, body: str, token: str,
            http: Callable[..., tuple[int, dict]] = github) -> str:
    api = f"https://api.github.com/repos/{slug}"
    code, pr = http("POST", f"{api}/pulls", token, {"title": title, "body": body, "head": branch, "base": "main"})
    if code not in (200, 201) or "html_url" not in pr:
        raise RuntimeError(f"PR creation failed: HTTP {code} {str(pr.get('message', ''))[:120]}")
    try:   # a PR-only token may lack issues:write; the title carries [ops-agent] anyway
        http("POST", f"{api}/issues/{pr['number']}/labels", token, {"labels": ["ops-agent"]})
    except Exception:
        log.info("ops PR label not set")
    return pr["html_url"]


# ---------------------------------------------------------------- availability and the run

def available(s: Settings, db: DB, now: float | None = None) -> tuple[bool, str]:
    """opai's availability rules (enabled, backoff, contestant breaker, sweep window, global budget) with the
    ops run counted as one call a day: it needs no entry in opai_tasks or opai_task_budget."""
    from . import opai
    if not s.ops_agent_enabled:
        return False, "disabled"
    if not s.opai_enabled:
        return False, "opai disabled"
    if not shutil.which("bwrap"):
        return False, "bwrap missing"
    s2 = dataclasses.replace(s, opai_tasks=[*s.opai_tasks, TASK],
                             opai_task_budget={**s.opai_task_budget, TASK: 1})
    return opai.available(s2, db, TASK, now)


def _pr_text(checkout: Path) -> tuple[str, str] | None:
    f = checkout / ".ops" / "pr.md"
    if not f.exists():
        return None
    lines = f.read_text(errors="replace").strip().splitlines()
    if not lines:
        return None
    title = re.sub(r"^#+\s*", "", lines[0]).strip()[:90]
    body = "\n".join(lines[1:]).strip()[:20000]
    return f"[ops-agent] {title}", body


def _proposal(s: Settings, checkout: Path) -> str | None:
    """.ops/proposal.json -> var/proposals/ through methodology.propose (validated against PROPOSAL_BOUNDS,
    author ops-agent, never --rehash). Never part of the PR."""
    from . import methodology
    f = checkout / ".ops" / "proposal.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
        p = methodology.propose(s, dict(d.get("changes") or {}), str(d.get("text") or ""), "ops-agent",
                                rationale=str(d.get("rationale") or "")[:2000])
        return p["id"]
    except (ValueError, TypeError, AttributeError, methodology.MethodologyError) as e:
        log.warning("ops-agent proposal refused: %s", e)
        return None


def nightly(s: Settings, db: DB, *, now: float | None = None, force: bool = False, cache: Path | None = None,
            journal=None, agent: Callable[[Ctx, Path], tuple[str, str]] | None = None,
            tests: Callable[[Ctx], tuple[bool, str]] | None = None, http=github, pusher=push) -> dict:
    """One nightly run. Returns the record written to kv['ops_last'] and var/ops/<day>/summary.json."""
    from . import alerts, opai
    now = time.time() if now is None else now
    day = opai.utc_day(now)
    out: dict = {"day": day, "at": now}
    if not force and db.get(f"ops_{day}"):
        return {**out, "status": "skipped", "why": "already ran today"}
    ok, why = available(s, db, now)
    if not ok:
        out.update(status="skipped", why=why)
        if why != "disabled":
            db.put("ops_last", out)
        return out
    db.put(f"ops_{day}", True)
    cache = cache or CACHE
    snap_dir, wt = cache / day, cache / f"wt-{day}"
    branch = f"ops/{day}"
    var_dir = s.data_dir / "ops" / day
    home = Path(tempfile.mkdtemp(prefix="ops-home-"))
    venv = Path(sys.prefix)   # the venv running the runner (the main checkout's .venv); read-only inside
    try:
        snap = snapshot(s, db, snap_dir, now, journal)
        base = clone(source_repo(s), wt, branch)
        cfg = Path(os.path.expanduser(s.opai_config_dir))
        cfg.mkdir(parents=True, exist_ok=True)
        ctx = Ctx(s, wt, snap_dir, venv, home, cfg)
        run_t = (lambda: (tests or run_tests)(ctx))
        base_ok, base_summary = run_t()
        d = json.loads(snap.read_text())
        d["tests_on_main"] = {"ok": base_ok, "summary": base_summary}
        snap.write_text(json.dumps(d, indent=1, default=str))
        start = time.time()
        call_id = db.x("INSERT INTO ai_calls(task,subject,day,started_at,model_alias,prompt_name,prompt_version,"
                       "prompt_sha,input_sha,status) VALUES(?,?,?,?,?,?,?,?,?,'running')", TASK, f"ops:{day}", day,
                       start, s.opai_ops_model, TASK, opai.PROMPTS[TASK], opai.sha(opai.prompt_text(TASK)),
                       opai.sha(snap.read_text()))
        status, detail = (agent or run_agent)(ctx, snap)
        db.x("UPDATE ai_calls SET status=?, detail=?, duration_s=? WHERE call_id=?", status, detail[:300] or None,
             time.time() - start, call_id)
        if status in ("quota", "auth"):
            db.put("opai_backoff_until", time.time() + s.opai_backoff_s)
        out["agent"] = status
        pr = _pr_text(wt)
        out["proposal"] = _proposal(s, wt)
        shutil.rmtree(wt / ".ops", ignore_errors=True)
        if status != "ok":
            return _finish(s, db, out, var_dir, "aborted", f"agent {status}")
        if pr is None:
            git(wt, "add", "-A")
            files = numstat(wt, base)
            return _finish(s, db, out, var_dir, "nothing" if not files else "aborted",
                           "no change" if not files else "no .ops/pr.md")
        reasons, patch, summary = gate(ctx, wt, base, s, run_t)
        out.update(tests=summary, title=pr[0])
        if reasons:
            var_dir.mkdir(parents=True, exist_ok=True)
            if patch:
                (var_dir / "rejected.patch").write_text(patch)
            out["reasons"] = reasons
            return _finish(s, db, out, var_dir, "nothing" if reasons == ["no diff"] else "rejected",
                           "; ".join(reasons))
        git(wt, "-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}", "commit", "--quiet", "-m",
            f"{pr[0]}\n\n{pr[1]}\n\nOpened by the nightly ops agent ({s.opai_ops_model}); gate: tests {summary}.")
        token = os.environ.get(TOKEN_VAR, "").strip()
        body = f"{pr[1]}\n\n---\nNightly ops agent, {day}. Gate passed: forbidden paths, sandbox code, " \
               f"methodology hashes, size, tests ({summary})."
        if token:
            pusher(wt, s.ops_github_repo, branch, token)
            out["pr"] = open_pr(s.ops_github_repo, branch, pr[0], body, token, http)
            return _finish(s, db, out, var_dir, "pr", out["pr"])
        var_dir.mkdir(parents=True, exist_ok=True)
        patch_file = var_dir / f"ops-{day}.patch"
        patch_file.write_text(git(wt, "format-patch", "-1", "--stdout").stdout)
        (var_dir / "summary.md").write_text(f"# {pr[0]}\n\n{body}\n\nApply: git am {patch_file}\n")
        out["patch"] = str(patch_file)
        alerts.send(s, db, "info", "ops-patch", f"ops agent: {pr[0]}",
                    f"No {TOKEN_VAR} in secrets.env, so the nightly ops agent's change is a local patch, not a PR.",
                    action=f"patch at {patch_file}", resume=f"review, then: git am {patch_file}")
        return _finish(s, db, out, var_dir, "patch", str(patch_file))
    except Exception as e:
        log.exception("ops nightly failed")
        return _finish(s, db, out, var_dir, "error", str(e)[:300])
    finally:
        shutil.rmtree(wt, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def _finish(s: Settings, db: DB, out: dict, var_dir: Path, status: str, line: str) -> dict:
    out.update(status=status, summary=f"ops agent {out['day']}: {status}: {line}"[:400])
    db.put("ops_last", out)
    try:
        var_dir.mkdir(parents=True, exist_ok=True)
        (var_dir / "summary.json").write_text(json.dumps(out, indent=1, default=str))
    except OSError:
        log.exception("could not write the ops summary")
    log.info("%s", out["summary"])
    return out


# ---------------------------------------------------------------- the timer (written, never enabled here)

UNIT_DIR = PKG.parent / "integrations" / "systemd"
UNITS = ("agentpitbench-ops.service", "agentpitbench-ops.timer")


def install_timer(dest: Path | None = None) -> list[Path]:
    """Copy the unit files into ~/.config/systemd/user/ (or dest). Never runs systemctl: the operator enables it."""
    dest = dest or Path.home() / ".config" / "systemd" / "user"
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in UNITS:
        shutil.copyfile(UNIT_DIR / name, dest / name)
        out.append(dest / name)
    return out
