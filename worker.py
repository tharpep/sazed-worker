"""sazed-worker: runs agent jobs (Antigravity, Claude Code) for Sazed. See plans/07.

Loop: claim a job from Sazed, run the agent CLI in a fresh workspace, stream its events back
(which doubles as the heartbeat and the cancel check), then post the result. One job at a time.

Deliberately dumb: Sazed decides the prompt, model, limits and who may start what. The worker
only enforces the hard limits it's handed (timeout, tool-call cap, which tools exist) and keeps
its own secrets (the worker key, the Claude token) out of agent processes that don't need them.

Standard library only, so the container needs nothing beyond python3 and git.
"""

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SAZED_URL = os.environ.get("SAZED_URL", "http://192.168.50.155:8000").rstrip("/")
WORKER_KEY = os.environ.get("SAZED_WORKER_KEY", "")
HOME = Path(os.environ.get("HOME", "/home/worker"))
JOBS_DIR = HOME / "jobs"
AGY = str(HOME / ".local/bin/agy")
CLAUDE = str(HOME / ".local/bin/claude")
POLL_SECONDS = 5
FLUSH_SECONDS = 2
WORKSPACE_KEEP_DAYS = 14
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sazed-worker")
_stop = threading.Event()


# ---------------------------------------------------------------- Sazed API


def _post(path: str, body: dict[str, Any], timeout: float = 30) -> tuple[int, Any]:
    req = urllib.request.Request(
        f"{SAZED_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Worker-Key": WORKER_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]


def _agents() -> list[str]:
    agents = []
    if Path(AGY).exists():
        agents.append("agy")
    if Path(CLAUDE).exists() and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        agents.append("claude")
    return agents


# ---------------------------------------------------------------- commands


def _agent_env(agent: str) -> dict[str, str]:
    """A minimal environment: no worker key, and the Claude token only for Claude Code."""
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
    env["HOME"] = str(HOME)
    if agent == "claude":
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        env["DISABLE_AUTOUPDATER"] = "1"
    return env


def _agy_command(job: dict[str, Any], prompt: str, resume: str | None) -> list[str]:
    params = job.get("params") or {}
    cmd = [
        AGY,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--model",
        job["model"],
        "--print-timeout",
        f"{int(params.get('timeout_s', 300))}s",
    ]
    if params.get("effort"):
        cmd += ["--effort", str(params["effort"])]
    if resume:
        cmd += ["--conversation", resume]
    return cmd


# Which built-in tools exist per profile (--tools), and which are pre-approved
# (--allowedTools). With --permission-mode dontAsk anything not pre-approved is refused.
_CLAUDE_PROFILES: dict[str, tuple[str, list[str]]] = {
    "review": (
        "Read,Grep,Glob,Bash",
        ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
         "Bash(git blame:*)"],
    ),
}


def _claude_command(job: dict[str, Any], prompt: str, resume: str | None) -> list[str]:
    tools, allowed = _CLAUDE_PROFILES[job["profile"]]
    cmd = [
        CLAUDE,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",  # required for stream-json in print mode
        "--model",
        job["model"],
        "--permission-mode",
        "dontAsk",
        # Only the worker's own user settings: a repo's .claude/settings.json (hooks!) is ignored.
        "--setting-sources",
        "user",
        "--strict-mcp-config",
        "--tools",
        tools,
        "--allowedTools",
        *allowed,
    ]
    if resume:
        cmd += ["--resume", resume]
    return cmd


# ---------------------------------------------------------------- one agent process


class Run:
    """One agent process: streams events to Sazed, enforces the timeout and tool-call cap."""

    def __init__(self, job: dict[str, Any]):
        self.job = job
        params = job.get("params") or {}
        self.deadline = time.monotonic() + int(params.get("timeout_s", 300)) + 30
        self.max_tools = int(params.get("max_tool_calls", 40))
        self.tool_calls = 0
        self.tools_used: list[str] = []
        self.result: dict[str, Any] | None = None  # normalized: response, ok, error, usage, ...
        self.session_id: str | None = None
        self.stop_reason: str | None = None  # cancelled | timed_out | tool_cap
        self.stderr = ""
        self._pending: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()

    def _flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_flush < FLUSH_SECONDS:
            return
        events, self._pending = self._pending, []
        self._last_flush = time.monotonic()
        status, body = _post(f"/worker/jobs/{self.job['id']}/events", {"events": events})
        if status == 200 and isinstance(body, dict) and body.get("cancel"):
            self.stop_reason = self.stop_reason or "cancelled"
        elif status != 200:
            log.warning("events post failed (%s): %s", status, body)

    def _count_tool(self, name: str) -> None:
        self.tool_calls += 1
        self.tools_used.append(name)
        if self.tool_calls > self.max_tools:
            self.stop_reason = "tool_cap"

    def _handle_agy(self, event: dict[str, Any]) -> None:
        if "conversation_id" in event:
            self.session_id = event["conversation_id"]
        step = event.get("step_update") or {}
        if step.get("step_type") == "tool" and step.get("state") == "DONE":
            self._count_tool(step.get("tool_name") or "?")
        if event.get("event") == "result":
            r = event.get("result") or {}
            self.session_id = r.get("conversation_id") or self.session_id
            self.result = {
                "response": (r.get("response") or "").strip(),
                "ok": r.get("status") == "SUCCESS",
                "error": r.get("error"),
                "usage": r.get("usage"),
                "duration_seconds": r.get("duration_seconds"),
                "denied": r.get("denied_actions"),
            }

    def _handle_claude(self, event: dict[str, Any]) -> None:
        if event.get("session_id"):
            self.session_id = event["session_id"]
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    self._count_tool(block.get("name") or "?")
        if event.get("type") == "result":
            self.result = {
                "response": (event.get("result") or "").strip(),
                "ok": not event.get("is_error") and event.get("subtype") == "success",
                "error": event.get("result") if event.get("is_error") else None,
                "usage": event.get("usage"),
                "duration_seconds": (event.get("duration_ms") or 0) / 1000,
                "denied": event.get("permission_denials") or None,
                "turns": event.get("num_turns"),
            }

    def run(self, cmd: list[str], cwd: Path) -> int:
        handle = self._handle_claude if self.job["agent"] == "claude" else self._handle_agy
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=_agent_env(self.job["agent"]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # so a kill takes the agent's children with it
        )
        stderr_tail: list[str] = []
        threading.Thread(
            target=lambda: [stderr_tail.append(line) for line in proc.stderr], daemon=True
        ).start()
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                handle(event)
                self._pending.append(event)
            except json.JSONDecodeError:
                self._pending.append({"event": "raw", "line": line[:2000]})
            self._flush()
        code = proc.wait()
        self._flush(force=True)
        self.stderr = "".join(stderr_tail[-20:])[-4000:]
        return code

    def _watch(self, proc: subprocess.Popen) -> None:
        """Kill the agent on cancel, timeout, or tool cap; flush events while it's quiet."""
        while proc.poll() is None:
            if time.monotonic() > self.deadline:
                self.stop_reason = self.stop_reason or "timed_out"
            if self.stop_reason:
                log.info("stopping job %s: %s", self.job["id"], self.stop_reason)
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(5)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return
            time.sleep(1)
            if time.monotonic() - self._last_flush > 20:
                self._flush(force=True)  # heartbeat during long silent steps


# ---------------------------------------------------------------- jobs


class JobFailed(Exception):
    """A job that can't run (bad repo, clone failure, unsupported profile)."""


def _git(*args: str, cwd: Path | None = None, timeout: int = 180) -> str:
    """git with hooks disabled and no credential prompts."""
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env={**_agent_env("git"), "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise JobFailed(f"git {args[0]} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def _prepare_repo(job: dict[str, Any], workspace: Path) -> Path:
    """A checkout of the job's repo in its workspace (a follow-up reuses its parent's)."""
    if job.get("resume_session_id") and job.get("parent_id"):
        parent_repo = JOBS_DIR / str(job["parent_id"]) / "repo"
        if parent_repo.is_dir():
            return parent_repo
    repo = (job.get("params") or {}).get("repo") or ""
    if not _REPO_RE.match(repo):
        raise JobFailed(f"invalid repo '{repo}'")
    dest = workspace / "repo"
    if not dest.exists():
        # Public repos clone anonymously; private ones need the GitHub App (plans/07 phase 3b).
        _git("clone", "--depth", "50", "--single-branch", f"https://github.com/{repo}.git",
             str(dest))
    return dest


_EMPTY_RETRY_PROMPT = (
    "Some tools you tried are not available here. Using only what you have already found, "
    "answer the original question now in the requested format, with sources."
)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run one job to completion and return the finish payload for Sazed."""
    workspace = JOBS_DIR / str(job["id"])
    workspace.mkdir(parents=True, exist_ok=True)
    resume = job.get("resume_session_id")
    run = Run(job)

    if job["agent"] == "agy":
        code = run.run(_agy_command(job, job["prompt"], resume), workspace)
        result = run.result or {}
        # agy ends with an empty answer when its last step was a tool headless mode refused.
        # Resume the same conversation once and ask it to answer with what it has.
        if not result.get("response") and not run.stop_reason and run.session_id and result.get(
            "denied"
        ):
            log.info("job %s: empty answer after denied %s, resuming once", job["id"],
                     result.get("denied"))
            run.result = None
            code = run.run(_agy_command(job, _EMPTY_RETRY_PROMPT, run.session_id), workspace)
    elif job["agent"] == "claude":
        if job["profile"] not in _CLAUDE_PROFILES:
            return {"status": "failed", "error": f"profile '{job['profile']}' is not supported yet"}
        try:
            cwd = _prepare_repo(job, workspace)
        except (JobFailed, subprocess.TimeoutExpired) as e:
            return {"status": "failed", "error": str(e)}
        code = run.run(_claude_command(job, job["prompt"], resume), cwd)
    else:
        return {"status": "failed", "error": f"agent '{job['agent']}' is not supported"}

    result = run.result or {}
    response = result.get("response") or ""
    meta = {
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
    if run.stop_reason == "cancelled":
        return base | {"status": "failed", "error": "cancelled"}
    if run.stop_reason == "timed_out":
        return base | partial | {"status": "timed_out", "error": "hit the job timeout"}
    if run.stop_reason == "tool_cap":
        return base | partial | {"status": "failed",
                                 "error": f"stopped after {run.max_tools} tool calls"}
    if result.get("ok") and response:
        return base | {"status": "succeeded", "result": response}
    error = (
        result.get("error") or run.stderr.strip()[-1000:] or f"agent exited {code} with no answer"
    )
    return base | partial | {"status": "failed", "error": error}


# ---------------------------------------------------------------- main loop


def _cleanup_workspaces() -> None:
    cutoff = time.time() - WORKSPACE_KEEP_DAYS * 86400
    for path in JOBS_DIR.glob("*"):
        if path.is_dir() and path.stat().st_mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def main() -> None:
    if not WORKER_KEY:
        raise SystemExit("SAZED_WORKER_KEY is not set")
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    hostname = socket.gethostname()
    log.info("sazed-worker started: %s, agents %s", SAZED_URL, _agents())
    last_cleanup = 0.0

    while not _stop.is_set():
        if time.time() - last_cleanup > 3600:
            _cleanup_workspaces()
            last_cleanup = time.time()
        try:
            status, job = _post("/worker/claim", {"agents": _agents(), "worker": hostname})
        except (urllib.error.URLError, OSError) as e:
            log.warning("claim failed: %s", e)
            _stop.wait(POLL_SECONDS * 3)
            continue
        if status == 204 or not job:
            if status not in (200, 204):
                log.warning("claim returned %s: %s", status, job)
            _stop.wait(POLL_SECONDS)
            continue

        log.info("job %s: %s on %s/%s (%s)", job["id"], job["kind"], job["agent"], job["model"],
                 job["profile"])
        try:
            payload = run_job(job)
        except Exception as e:  # never leave a claimed job hanging
            log.exception("job %s crashed", job["id"])
            payload = {"status": "failed", "error": f"worker error: {type(e).__name__}: {e}"}
        status, body = _post(f"/worker/jobs/{job['id']}/finish", payload)
        log.info("job %s finished %s (posted %s %s)", job["id"], payload["status"], status, body)


if __name__ == "__main__":
    main()
