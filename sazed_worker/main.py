"""The claim loop: claim a job, run it, post the result; clean old workspaces hourly."""

import os
import shutil
import signal
import socket
import time
from pathlib import Path
from typing import Any

from .agent_process import JobFailed
from .config import (
    AGENT_USER,
    AGY,
    CLAUDE,
    CLAUDE_TOKEN,
    JOBS_DIR,
    POLL_SECONDS,
    SAZED_URL,
    WORKER_KEY,
    WORKSPACE_KEEP_DAYS,
    log,
    stop,
)
from .jobs import run_job
from .sazed_api import post
from .scrub import extra_secrets


def _agents() -> list[str]:
    agents = []
    if Path(AGY).exists():
        agents.append("agy")
    if Path(CLAUDE).exists() and CLAUDE_TOKEN:
        agents.append("claude")
    return agents


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
        status, body = post(f"/worker/jobs/{job_id}/finish", payload)
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
        signal.signal(sig, lambda *_: stop.set())
    hostname = socket.gethostname()
    log.info("sazed-worker started: %s, agents %s as %s", SAZED_URL, _agents(), AGENT_USER)
    last_cleanup = 0.0

    while not stop.is_set():
        if time.time() - last_cleanup > 3600:
            _cleanup_workspaces()
            last_cleanup = time.time()
        status, job = post("/worker/claim", {"agents": _agents(), "worker": hostname})
        if status != 200 or not isinstance(job, dict):
            if status not in (200, 204):
                log.warning("claim returned %s: %s", status, job)
            stop.wait(POLL_SECONDS if status in (200, 204) else POLL_SECONDS * 3)
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
        extra_secrets.clear()
