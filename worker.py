"""sazed-worker: runs agent jobs (Antigravity, later Claude Code) for Sazed. See plans/07.

Loop: claim a job from Sazed, run the agent CLI in a fresh workspace, stream its events back
(which doubles as the heartbeat and the cancel check), then post the result. One job at a time.

Deliberately dumb: Sazed decides the prompt, model, limits and who may start what. The worker
only enforces the hard limits it's handed (timeout, tool-call cap) and keeps its own secrets
(the worker key, the Claude token) out of agent processes that don't need them.

Standard library only, so the container needs nothing beyond python3.
"""

import json
import logging
import os
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
POLL_SECONDS = 5
FLUSH_SECONDS = 2
WORKSPACE_KEEP_DAYS = 14

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
    """Agents this worker can run. Claude Code jobs come in phase 3 of plans/07."""
    return ["agy"] if Path(AGY).exists() else []


# ---------------------------------------------------------------- running an agent


def _agent_env(agent: str) -> dict[str, str]:
    """A minimal environment: no worker key, and the Claude token only for Claude Code."""
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
    env["HOME"] = str(HOME)
    if agent == "claude" and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
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


class Run:
    """One agent process: streams events to Sazed, enforces the timeout and tool-call cap."""

    def __init__(self, job: dict[str, Any], workspace: Path):
        self.job = job
        self.workspace = workspace
        params = job.get("params") or {}
        self.deadline = time.monotonic() + int(params.get("timeout_s", 300)) + 30
        self.max_tools = int(params.get("max_tool_calls", 40))
        self.tool_calls = 0
        self.tools_used: list[str] = []
        self.result: dict[str, Any] | None = None
        self.session_id: str | None = None
        self.stop_reason: str | None = None  # cancelled | timed_out | tool_cap
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

    def _handle(self, event: dict[str, Any]) -> None:
        if "conversation_id" in event:
            self.session_id = event["conversation_id"]
        step = event.get("step_update") or {}
        if step.get("step_type") == "tool" and step.get("state") == "DONE":
            self.tool_calls += 1
            self.tools_used.append(step.get("tool_name") or "?")
            if self.tool_calls > self.max_tools:
                self.stop_reason = "tool_cap"
        if event.get("event") == "result":
            self.result = event.get("result") or {}
            self.session_id = self.result.get("conversation_id") or self.session_id
        self._pending.append(event)

    def run(self, cmd: list[str]) -> int:
        proc = subprocess.Popen(
            cmd,
            cwd=self.workspace,
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

        watchdog = threading.Thread(target=self._watch, args=(proc,), daemon=True)
        watchdog.start()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._handle(json.loads(line))
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


_EMPTY_RETRY_PROMPT = (
    "Some tools you tried are not available here. Using only what you have already found, "
    "answer the original question now in the requested format, with sources."
)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run one job to completion and return the finish payload for Sazed."""
    workspace = JOBS_DIR / str(job["id"])
    workspace.mkdir(parents=True, exist_ok=True)
    if job["agent"] != "agy":
        return {"status": "failed", "error": f"agent '{job['agent']}' is not supported yet"}

    run = Run(job, workspace)
    code = run.run(_agy_command(job, job["prompt"], job.get("resume_session_id")))
    result = run.result or {}
    response = (result.get("response") or "").strip()

    # agy ends with an empty answer when its last step was a tool headless mode refused.
    # Resume the same conversation once and ask it to answer with what it has.
    if not response and not run.stop_reason and run.session_id and result.get("denied_actions"):
        log.info("job %s: empty answer after denied %s, resuming once", job["id"],
                 result.get("denied_actions"))
        run.result = None
        run.run(_agy_command(job, _EMPTY_RETRY_PROMPT, run.session_id))
        result = run.result or {}
        response = (result.get("response") or "").strip()

    meta = {
        "tool_calls": run.tool_calls,
        "tools_used": run.tools_used,
        "usage": result.get("usage"),
        "duration_seconds": result.get("duration_seconds"),
        "denied_actions": result.get("denied_actions"),
        "exit_code": code,
    }
    base = {"agent_session_id": run.session_id, "meta": meta}
    if run.stop_reason == "cancelled":
        return base | {"status": "failed", "error": "cancelled"}
    partial = {"result": response or None}
    if run.stop_reason == "timed_out":
        return base | partial | {"status": "timed_out", "error": "hit the job timeout"}
    if run.stop_reason == "tool_cap":
        return base | partial | {"status": "failed",
                                 "error": f"stopped after {run.max_tools} tool calls"}
    if result.get("status") == "SUCCESS" and response:
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

        log.info("job %s: %s on %s (%s)", job["id"], job["kind"], job["model"], job["profile"])
        try:
            payload = run_job(job)
        except Exception as e:  # never leave a claimed job hanging
            log.exception("job %s crashed", job["id"])
            payload = {"status": "failed", "error": f"worker error: {type(e).__name__}: {e}"}
        status, body = _post(f"/worker/jobs/{job['id']}/finish", payload)
        log.info("job %s finished %s (posted %s %s)", job["id"], payload["status"], status, body)


if __name__ == "__main__":
    main()
