"""sazed-worker: runs agent jobs (Antigravity, Claude Code) for Sazed. See plans/07.

Loop: claim a job from Sazed, run the agent CLI in a fresh workspace, stream its events back
(which doubles as the heartbeat and the cancel check), then post the result. One job at a time.

Deliberately dumb: Sazed decides the prompt, model, limits and who may start what. The worker
only enforces the hard limits it's handed (timeout, tool-call cap, which tools exist).

Isolation: this process runs as root only so it can start agents as the unprivileged `agent`
user. Agents therefore can't read this process's environment (/proc/<pid>/environ holds the
worker key and the Claude token) or /etc/sazed-eco. Each agent gets a minimal environment; Claude
Code gets only its own token. Known secret values are scrubbed from everything sent to Sazed.

Standard library only, so the container needs nothing beyond python3 and git.
"""

import base64
import collections
import difflib
import json
import logging
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SAZED_URL = os.environ.get("SAZED_URL", "http://192.168.50.155:8000").rstrip("/")
WORKER_KEY = os.environ.get("SAZED_WORKER_KEY", "")
CLAUDE_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
AGENT_USER = os.environ.get("SAZED_AGENT_USER", "agent")
_AGENT_PW = pwd.getpwnam(AGENT_USER)
AGENT_HOME = Path(_AGENT_PW.pw_dir)
AGENT_UID, AGENT_GID = _AGENT_PW.pw_uid, _AGENT_PW.pw_gid
JOBS_DIR = AGENT_HOME / "jobs"
AGY = str(AGENT_HOME / ".local/bin/agy")
CLAUDE = str(AGENT_HOME / ".local/bin/claude")
RUFF = "/usr/local/bin/ruff"  # lint pre-check for change jobs (run by the worker, not the agent)
POLL_SECONDS = 5
FLUSH_SECONDS = 2
MAX_PENDING_EVENTS = 2000
WORKSPACE_KEEP_DAYS = 14
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sazed-worker")
_stop = threading.Event()


_extra_secrets: set[str] = set()  # per-job values to scrub too (e.g. a clone token)


def _scrub(text: str) -> str:
    """Remove known secret values from anything leaving this process."""
    for secret in (WORKER_KEY, CLAUDE_TOKEN, *_extra_secrets):
        if secret and len(secret) >= 12:
            text = text.replace(secret, "[redacted]")
    return text


# ---------------------------------------------------------------- Sazed API


def _post(path: str, body: dict[str, Any], timeout: float = 30) -> tuple[int, Any]:
    """POST to Sazed. Never raises: network or decode failures come back as status 0."""
    req = urllib.request.Request(
        f"{SAZED_URL}{path}",
        data=_scrub(json.dumps(body)).encode(),
        headers={"Content-Type": "application/json", "X-Worker-Key": WORKER_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]
    except (OSError, ValueError) as e:  # URLError, timeouts, resets, bad JSON
        return 0, f"{type(e).__name__}: {e}"


def _agents() -> list[str]:
    agents = []
    if Path(AGY).exists():
        agents.append("agy")
    if Path(CLAUDE).exists() and CLAUDE_TOKEN:
        agents.append("claude")
    return agents


# ---------------------------------------------------------------- commands


def _agent_env(agent: str) -> dict[str, str]:
    """A minimal environment for an agent process (no worker key; Claude gets its own token)."""
    env = {
        "HOME": str(AGENT_HOME),
        "USER": AGENT_USER,
        "PATH": f"{AGENT_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
    }
    if agent == "claude":
        env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_TOKEN
        env["DISABLE_AUTOUPDATER"] = "1"
    return env


def _as_agent() -> dict[str, Any]:
    """Popen kwargs that run the child as the unprivileged agent user."""
    return {"user": AGENT_USER, "group": AGENT_USER, "extra_groups": []}


def _safe_arg(value: str, what: str) -> str:
    """Values that go into argv as their own arguments (model, effort, session ids)."""
    if not _SAFE_ARG_RE.match(value):
        raise JobFailed(f"invalid {what} '{value}'")
    return value


def _agy_command(job: dict[str, Any], prompt: str, resume: str | None) -> list[str]:
    params = job.get("params") or {}
    cmd = [
        AGY,
        f"--prompt={prompt}",
        "--output-format=stream-json",
        f"--model={_safe_arg(job['model'], 'model')}",
        f"--print-timeout={int(params.get('timeout_s', 300))}s",
    ]
    if params.get("effort"):
        cmd.append(f"--effort={_safe_arg(str(params['effort']), 'effort')}")
    if resume:
        cmd.append(f"--conversation={_safe_arg(resume, 'session id')}")
    return cmd


# Which built-in tools exist per profile (--tools), which are pre-approved (--allowedTools),
# and which reads are refused outright. With --permission-mode dontAsk anything not pre-approved
# is refused. No Bash: even "read-only" git commands can write files (git diff --output=).
_AGENT_SECRET_READS = ["Read(~/.claude/**)", "Read(~/.claude.json)", "Read(~/.gemini/**)",
                       "Read(//proc/**)", "Read(//etc/sazed-eco/**)"]
_CLAUDE_PROFILES: dict[str, tuple[str, list[str]]] = {
    "review": ("Read,Grep,Glob", ["Read", "Grep", "Glob"]),
    # Edits only inside the job's working copy (cwd); no shell, so nothing runs and nothing
    # is pushed. The worker turns the edits into a file list that the gateway commits as a PR.
    "change": ("Read,Grep,Glob,Edit,Write",
               ["Read", "Grep", "Glob", "Edit(./**)", "Write(./**)"]),
}


def _claude_command(job: dict[str, Any], prompt: str, resume: str | None) -> list[str]:
    tools, allowed = _CLAUDE_PROFILES[job["profile"]]
    cmd = [
        CLAUDE,
        "--print",
        "--output-format=stream-json",
        "--verbose",  # required for stream-json in print mode
        f"--model={_safe_arg(job['model'], 'model')}",
        "--permission-mode=dontAsk",
        # Only the agent user's own settings: a repo's .claude/settings.json (hooks!) is ignored.
        "--setting-sources=user",
        "--strict-mcp-config",
        f"--tools={tools}",
        "--allowedTools",
        *allowed,
        "--disallowedTools",
        *_AGENT_SECRET_READS,
    ]
    if resume:
        cmd.append(f"--resume={_safe_arg(resume, 'session id')}")
    # The prompt goes last, after "--", so it can never be read as an option.
    return [*cmd, "--", prompt]


# ---------------------------------------------------------------- one agent process


class Run:
    """One agent process: streams events to Sazed, enforces the timeout and tool-call cap."""

    def __init__(self, job: dict[str, Any]):
        self.job = job
        params = job.get("params") or {}
        self.timeout_s = int(params.get("timeout_s", 300))
        self.max_tools = int(params.get("max_tool_calls", 40))
        self.deadline = 0.0
        self.tool_calls = 0
        self.tools_used: list[str] = []
        self.result: dict[str, Any] | None = None  # normalized: response, ok, error, usage, ...
        self.session_id: str | None = None
        self.stop_reason: str | None = None  # cancelled | timed_out | tool_cap | shutdown
        self.stderr = ""
        self._pending: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

    def _flush(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.monotonic() - self._last_flush < FLUSH_SECONDS:
                return
            events, self._pending = self._pending, []
            self._last_flush = time.monotonic()
        status, body = _post(f"/worker/jobs/{self.job['id']}/events", {"events": events})
        if status == 200:
            if isinstance(body, dict) and body.get("cancel"):
                self.stop_reason = self.stop_reason or "cancelled"
            return
        log.warning("events post failed (%s): %s", status, body)
        with self._lock:  # keep the batch for the next try, within reason
            self._pending = (events + self._pending)[-MAX_PENDING_EVENTS:]

    def _queue(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._pending.append(event)
            if len(self._pending) > MAX_PENDING_EVENTS:
                self._pending = self._pending[-MAX_PENDING_EVENTS:]

    def _count_tool(self, name: str) -> None:
        self.tool_calls += 1
        self.tools_used.append(name)
        if self.tool_calls >= self.max_tools:
            self.stop_reason = self.stop_reason or "tool_cap"

    def _handle_agy(self, event: dict[str, Any]) -> None:
        if isinstance(event.get("conversation_id"), str):
            self.session_id = event["conversation_id"]
        step = event.get("step_update")
        done_tool = isinstance(step, dict) and step.get("step_type") == "tool"
        if done_tool and step.get("state") == "DONE":
            self._count_tool(str(step.get("tool_name") or "?"))
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            r = event["result"]
            self.session_id = r.get("conversation_id") or self.session_id
            self.result = {
                "response": str(r.get("response") or "").strip(),
                "ok": r.get("status") == "SUCCESS",
                "error": r.get("error"),
                "usage": r.get("usage"),
                "duration_seconds": r.get("duration_seconds"),
                "denied": r.get("denied_actions"),
            }

    def _handle_claude(self, event: dict[str, Any]) -> None:
        if isinstance(event.get("session_id"), str):
            self.session_id = event["session_id"]
        if event.get("type") == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._count_tool(str(block.get("name") or "?"))
        if event.get("type") == "result":
            self.result = {
                "response": str(event.get("result") or "").strip(),
                "ok": not event.get("is_error") and event.get("subtype") == "success",
                "error": event.get("result") if event.get("is_error") else None,
                "usage": event.get("usage"),
                "duration_seconds": (event.get("duration_ms") or 0) / 1000,
                "denied": event.get("permission_denials") or None,
                "turns": event.get("num_turns"),
            }

    def run(self, cmd: list[str], cwd: Path) -> int:
        handle = self._handle_claude if self.job["agent"] == "claude" else self._handle_agy
        self.deadline = time.monotonic() + self.timeout_s + 30  # fresh budget per process
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=_agent_env(self.job["agent"]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # its own process group, killed as a whole
            **_as_agent(),
        )
        stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        reader = threading.Thread(target=lambda: stderr_tail.extend(proc.stderr), daemon=True)
        reader.start()
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {"event": "raw", "line": line[:2000]}
                if isinstance(event, dict):
                    try:
                        handle(event)
                    except Exception:
                        log.warning("unexpected event shape: %.200s", line, exc_info=True)
                    self._queue(event)
                self._flush()
            return proc.wait()
        finally:
            _kill_group(proc)  # also reaps background children the agent left behind
            proc.stdout.close()
            reader.join(timeout=5)
            self.stderr = "".join(stderr_tail)[-4000:]
            self._flush(force=True)

    def _watch(self, proc: subprocess.Popen) -> None:
        """Kill the agent on cancel, timeout, tool cap or shutdown; heartbeat while it's quiet."""
        try:
            while proc.poll() is None:
                if time.monotonic() > self.deadline:
                    self.stop_reason = self.stop_reason or "timed_out"
                if _stop.is_set():
                    self.stop_reason = self.stop_reason or "shutdown"
                if self.stop_reason:
                    log.info("stopping job %s: %s", self.job["id"], self.stop_reason)
                    _kill_group(proc)
                    return
                time.sleep(1)
                if time.monotonic() - self._last_flush > 20:
                    self._flush(force=True)
        except Exception:
            log.exception("watchdog failed for job %s; killing the agent", self.job["id"])
            _kill_group(proc)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------- jobs


class JobFailed(Exception):
    """A job that can't run (bad repo, clone failure, unsupported profile, bad input)."""


_CREDENTIAL_HELPER = (
    '!f() { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f'
)


def _git(*args: str, cwd: Path | None = None, timeout: int = 180,
         token: str | None = None) -> str:
    """git as the agent user, with hooks disabled and no credential prompts.

    A token (read-only, one repo, from the gateway's GitHub App) goes in via environment config,
    not argv, so it never shows in a process listing.
    """
    env = _agent_env("git")
    if token:
        # A credential helper reading the token from the environment. (An http.extraheader with
        # Basic auth was ignored for GitHub's newer long installation tokens.) Nothing is written
        # to .git/config.
        env |= {
            "GIT_TOKEN": token,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_0": _CREDENTIAL_HELPER,
        }
    proc = subprocess.Popen(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **_as_agent(),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise JobFailed(f"git {args[0]} timed out") from None
    if proc.returncode != 0:
        raise JobFailed(f"git {args[0]} failed: {err.strip()[-500:]}")
    return out


def _workspace(job_id: str) -> Path:
    path = JOBS_DIR / str(uuid.UUID(str(job_id)))  # validates: no path tricks in ids
    path.mkdir(parents=True, exist_ok=True)
    shutil.chown(path, AGENT_USER, AGENT_USER)
    return path


def _chown_tree(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            os.chown(Path(dirpath) / name, AGENT_UID, AGENT_GID, follow_symlinks=False)
    os.chown(root, AGENT_UID, AGENT_GID)


def _working_copy(repo_dir: Path, workspace: Path) -> Path:
    """A plain copy of the checkout without .git: the agent can't reach git internals or hooks."""
    work = workspace / "work"
    if not work.exists():
        shutil.copytree(repo_dir, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        _chown_tree(work)
    return work


def _prepare_repo(job: dict[str, Any], workspace: Path) -> tuple[Path, Path, str | None]:
    """(checkout, agent cwd, session to resume). Change jobs get a working copy as cwd."""
    change = job["profile"] == "change"
    resume = job.get("resume_session_id")
    if resume and job.get("parent_id"):
        parent_ws = JOBS_DIR / str(uuid.UUID(str(job["parent_id"])))
        parent_repo = parent_ws / "repo"
        if parent_repo.is_dir():
            os.utime(parent_ws)  # keep an active session's workspace from aging out
            cwd = _working_copy(parent_repo, parent_ws) if change else parent_repo
            return parent_repo, cwd, resume
    repo = (job.get("params") or {}).get("repo") or ""
    if not _REPO_RE.match(repo):
        raise JobFailed(f"invalid repo '{repo}'")
    dest, tmp = workspace / "repo", workspace / "repo.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    # Public repos clone anonymously; private ones with a read-only token Sazed got from the
    # gateway's GitHub App for this job.
    token = job.get("clone_token") or None
    if token:
        _extra_secrets.add(token)
    try:
        _git("clone", "--depth", "50", "--single-branch", f"https://github.com/{repo}.git",
             str(tmp), token=token)
    except JobFailed:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    tmp.rename(dest)
    cwd = _working_copy(dest, workspace) if change else dest
    return dest, cwd, None  # a fresh checkout has no session to resume


_MAX_CHANGED_FILES = 200
_MAX_CHANGED_BYTES = 5 * 1024 * 1024


def _changed_files(repo_dir: Path, work: Path) -> dict[str, str | None]:
    """Files the agent changed: path -> new content (base64), or None if deleted. Symlinks skip."""

    def files(root: Path) -> dict[str, Path]:
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for name in filenames:
                path = Path(dirpath) / name
                if not path.is_symlink():
                    out[path.relative_to(root).as_posix()] = path
        return out

    before, after = files(repo_dir), files(work)
    changes: dict[str, str | None] = {}
    total = 0
    for rel in sorted(before.keys() | after.keys()):
        if rel not in after:
            changes[rel] = None
            continue
        data = after[rel].read_bytes()
        if rel in before and before[rel].read_bytes() == data:
            continue
        total += len(data)
        changes[rel] = base64.b64encode(data).decode()
    if len(changes) > _MAX_CHANGED_FILES or total > _MAX_CHANGED_BYTES:
        raise JobFailed(f"too many changes for one PR ({len(changes)} files, {total} bytes)")
    return changes


def _changed_lines(before: bytes | None, after: bytes) -> set[int]:
    """1-based line numbers in `after` that are new or edited relative to `before`."""
    old = (before or b"").decode(errors="replace").splitlines()
    new = after.decode(errors="replace").splitlines()
    lines: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag in ("replace", "insert"):
            lines.update(range(j1 + 1, j2 + 1))
    return lines


def _lint_changed(repo_dir: Path, work: Path, changes: dict[str, str | None]) -> list[str]:
    """Ruff findings on lines the agent changed, using the repo's own ruff config.

    Run by the worker with fixed arguments (as the agent user), so the agent never needs a shell.
    Old code isn't held to the rules, only the agent's own lines.
    """
    py = [path for path, content in changes.items() if content is not None and path.endswith(".py")]
    if not py or not Path(RUFF).exists():
        return []
    proc = subprocess.Popen(
        [RUFF, "check", "--output-format=json", "--no-cache", "--quiet", "--exit-zero", *py],
        cwd=work,
        env=_agent_env("ruff"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **_as_agent(),
    )
    try:
        out, _ = proc.communicate(timeout=60)
        findings = json.loads(out or "[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        _kill_group(proc)
        return []
    issues = []
    touched: dict[str, set[int]] = {}
    for f in findings if isinstance(findings, list) else []:
        try:
            rel = Path(f["filename"]).resolve().relative_to(work.resolve()).as_posix()
            row = int(f["location"]["row"])
        except (KeyError, ValueError, TypeError):
            continue
        if rel not in touched:
            before = repo_dir / rel
            touched[rel] = _changed_lines(
                before.read_bytes() if before.is_file() else None, (work / rel).read_bytes()
            )
        if row in touched[rel]:
            issues.append(f"{rel}:{row}: {f.get('code')} {f.get('message')}")
    return issues[:40]


def _lint_prompt(issues: list[str]) -> str:
    return (
        "The repo's linter (ruff, with its own config) flagged these problems on lines you "
        "changed:\n" + "\n".join(f"- {i}" for i in issues) + "\n\nFix them, touching only what "
        "is needed. Then reply with the complete PR description again (updated if anything "
        "changed), in the same format as before."
    )


_EMPTY_RETRY_PROMPT = (
    "Some tools you tried are not available here. Using only what you have already found, "
    "answer the original question now in the requested format, with sources."
)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run one job to completion and return the finish payload for Sazed."""
    workspace = _workspace(job["id"])
    run = Run(job)
    changes: dict[str, str | None] = {}
    lint_meta: dict[str, Any] = {}

    if job["agent"] == "agy":
        resume = job.get("resume_session_id")
        if resume and job.get("parent_id"):
            parent = JOBS_DIR / str(uuid.UUID(str(job["parent_id"])))
            workspace = parent if parent.is_dir() else workspace
        code = run.run(_agy_command(job, job["prompt"], resume), workspace)
        result = run.result or {}
        # agy ends with an empty answer when its last step was a tool headless mode refused.
        # Resume the same conversation once and ask it to answer with what it has.
        if (not result.get("response") and not run.stop_reason and run.session_id
                and result.get("denied")):
            log.info("job %s: empty answer after denied %s, resuming once", job["id"],
                     result.get("denied"))
            run.result = None
            code = run.run(_agy_command(job, _EMPTY_RETRY_PROMPT, run.session_id), workspace)
    elif job["agent"] == "claude":
        if job["profile"] not in _CLAUDE_PROFILES:
            return {"status": "failed", "error": f"profile '{job['profile']}' is not supported yet"}
        repo_dir, cwd, resume = _prepare_repo(job, workspace)
        prompt = job["prompt"]
        if job.get("resume_session_id") and not resume:
            prompt = f"(The earlier session could not be resumed; start fresh.)\n\n{prompt}"
        code = run.run(_claude_command(job, prompt, resume), cwd)
        if job["profile"] == "change" and (run.result or {}).get("ok"):
            changes = _changed_files(repo_dir, cwd)
            issues = _lint_changed(repo_dir, cwd, changes)
            lint_meta = {"lint_found": len(issues)}
            if issues and not run.stop_reason and run.session_id:
                log.info("job %s: %d lint issue(s) on changed lines, one fix round", job["id"],
                         len(issues))
                first = run.result
                run.result = None
                code = run.run(_claude_command(job, _lint_prompt(issues), run.session_id), cwd)
                if (run.result or {}).get("ok"):
                    changes = _changed_files(repo_dir, cwd)
                else:
                    run.result = first  # keep the original answer if the fix round failed
                lint_meta["lint_left"] = _lint_changed(repo_dir, cwd, changes)
    else:
        return {"status": "failed", "error": f"agent '{job['agent']}' is not supported"}

    result = run.result or {}
    response = result.get("response") or ""
    meta = lint_meta | {
        "tool_calls": run.tool_calls,
        "tools_used": run.tools_used,
        "usage": result.get("usage"),
        "duration_seconds": result.get("duration_seconds"),
        "denied": result.get("denied"),
        "turns": result.get("turns"),
        "exit_code": code,
    }
    base = {"agent_session_id": run.session_id, "meta": meta}
    partial = {"result": response or None}
    if run.stop_reason in ("cancelled", "shutdown"):
        return base | {"status": "failed", "error": run.stop_reason}
    if run.stop_reason == "timed_out":
        return base | partial | {"status": "timed_out", "error": "hit the job timeout"}
    if run.stop_reason == "tool_cap":
        return base | partial | {"status": "failed",
                                 "error": f"stopped at the {run.max_tools}-tool-call cap"}
    if result.get("ok") and response:
        out = base | {"status": "succeeded", "result": response}
        if job.get("profile") == "change":
            out["changes"] = changes
            meta["changed_files"] = sorted(changes)
        return out
    error = (
        result.get("error") or run.stderr.strip()[-1000:] or f"agent exited {code} with no answer"
    )
    return base | partial | {"status": "failed", "error": error}


# ---------------------------------------------------------------- main loop


def _cleanup_workspaces() -> None:
    cutoff = time.time() - WORKSPACE_KEEP_DAYS * 86400
    for path in JOBS_DIR.glob("*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            log.warning("cleanup skipped %s", path, exc_info=True)


def _finish(job_id: str, payload: dict[str, Any]) -> None:
    """Post the result, retrying through short Sazed outages so a result isn't lost."""
    for attempt in range(6):
        status, body = _post(f"/worker/jobs/{job_id}/finish", payload)
        if status == 200:
            log.info("job %s finished %s (%s)", job_id, payload["status"], body)
            return
        log.warning("finish post for %s failed (%s): %s", job_id, status, body)
        time.sleep(min(60, 5 * 2**attempt))
    log.error("giving up on posting job %s; Sazed will time it out", job_id)


def main() -> None:
    if not WORKER_KEY:
        raise SystemExit("SAZED_WORKER_KEY is not set")
    if os.geteuid() != 0:
        raise SystemExit("run as root (it starts agents as the unprivileged agent user)")
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(JOBS_DIR, AGENT_USER, AGENT_USER)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    hostname = socket.gethostname()
    log.info("sazed-worker started: %s, agents %s as %s", SAZED_URL, _agents(), AGENT_USER)
    last_cleanup = 0.0

    while not _stop.is_set():
        if time.time() - last_cleanup > 3600:
            _cleanup_workspaces()
            last_cleanup = time.time()
        status, job = _post("/worker/claim", {"agents": _agents(), "worker": hostname})
        if status != 200 or not isinstance(job, dict):
            if status not in (200, 204):
                log.warning("claim returned %s: %s", status, job)
            _stop.wait(POLL_SECONDS if status in (200, 204) else POLL_SECONDS * 3)
            continue

        log.info("job %s: %s on %s/%s (%s)", job.get("id"), job.get("kind"), job.get("agent"),
                 job.get("model"), job.get("profile"))
        try:
            payload = run_job(job)
        except JobFailed as e:
            payload = {"status": "failed", "error": str(e)}
        except Exception as e:  # never leave a claimed job hanging
            log.exception("job %s crashed", job.get("id"))
            payload = {"status": "failed", "error": f"worker error: {type(e).__name__}: {e}"}
        _finish(str(job.get("id")), payload)
        _extra_secrets.clear()


if __name__ == "__main__":
    main()
