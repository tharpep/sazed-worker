"""One agent process: streams events to Sazed, enforces the timeout and tool-call cap."""

import collections
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import agy, claude
from .agent_process import agent_env, as_agent, kill_group
from .config import DEADLINE_GRACE_S, FLUSH_SECONDS, MAX_PENDING_EVENTS, log, stop
from .sazed_api import post


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
        self.rate_limits: dict[str, Any] = {}  # first and last plan-usage reading (Claude only)
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
        status, body = post(f"/worker/jobs/{self.job['id']}/events", {"events": events})
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

    def count_tool(self, name: str) -> None:
        self.tool_calls += 1
        self.tools_used.append(name)
        if self.tool_calls >= self.max_tools:
            self.stop_reason = self.stop_reason or "tool_cap"

    def run(self, cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
        agent = claude if self.job["agent"] == "claude" else agy
        self.deadline = (
            time.monotonic() + self.timeout_s + DEADLINE_GRACE_S
        )  # fresh budget per process
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=agent_env(self.job["agent"]) | (env or {}),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # its own process group, killed as a whole
            **as_agent(),
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
                        agent.handle_event(self, event)
                    except Exception:
                        log.warning("unexpected event shape: %.200s", line, exc_info=True)
                    try:
                        saved = agent.trim_event(event)
                    except Exception:
                        log.warning("couldn't trim event: %.200s", line, exc_info=True)
                        saved = event
                    if saved is not None:
                        self._queue(saved)
                self._flush()
            return proc.wait()
        finally:
            kill_group(proc)  # also reaps background children the agent left behind
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
                if stop.is_set():
                    self.stop_reason = self.stop_reason or "shutdown"
                if self.stop_reason:
                    log.info("stopping job %s: %s", self.job["id"], self.stop_reason)
                    kill_group(proc)
                    return
                time.sleep(1)
                if time.monotonic() - self._last_flush > 20:
                    self._flush(force=True)
        except Exception:
            log.exception("watchdog failed for job %s; killing the agent", self.job["id"])
            kill_group(proc)
