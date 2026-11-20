"""One agent process from a RunSpec: its events go to a caller's sink, under the timeout and cap."""

import collections
import contextlib
import json
import os
import subprocess
import threading
import time
from typing import Any, Protocol

from . import adapters
from .adapters.contract import (
    STOP_REASONS,
    Event,
    HarnessStop,
    RunResult,
    RunSpec,
    StopReason,
    ToolCall,
)
from .agent_process import agent_env, as_agent, kill_group
from .config import FLUSH_SECONDS, MAX_PENDING_EVENTS, log, stop
from .sazed_api import post


class Sink(Protocol):
    """Where a run's output goes; each method returns True when the caller asks for a cancel."""

    def event(self, line: str, event: dict[str, Any] | None, saved: dict[str, Any] | None) -> bool:
        ...

    def beat(self, force: bool) -> bool:
        ...

    def close(self) -> bool:
        ...


def launch_args(spec: RunSpec) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    """The argv, environment and user-switch kwargs a spec starts with."""
    extra = dict(spec.env_extra)
    env = agent_env(extra) if spec.env == "minimal" else os.environ | extra
    user = as_agent() if spec.as_agent else {}
    return adapters.ADAPTERS[spec.engine].command(spec), env, user


class Run:
    """Agent processes run one after another (a worker job's rounds), with totals across them."""

    def __init__(self, sink: Sink, name: str = "run", drain_s: float | None = None):
        self.sink = sink
        self.name = name
        self.drain_s = drain_s
        self._past_drain = False
        self.spec: RunSpec | None = None
        self.deadline = 0.0
        self.tool_calls = 0
        self.tools_used: list[str] = []
        self.result: RunResult | None = None
        # Summed over every agent process of the job (first run, fix rounds, repair round), since
        # run_job swaps self.result between rounds and the last one alone undercounts the job.
        self.rounds = 0
        self.usage: dict[str, Any] = {}
        self.duration_seconds = 0.0
        self.turns = 0
        self.session_id: str | None = None
        self.rate_limits: dict[str, Any] = {}  # first and last plan-usage reading (Claude only)
        # Set when Pryce's plan refused the run: {"window", "resets_at"} (plans/10 P1.2).
        self.rate_limited: dict[str, Any] | None = None
        self.stop_reason: StopReason | None = None
        self.stderr = ""
        self._ending_after_exit: HarnessStop | None = None

    def stop(self, reason: StopReason) -> None:
        """The first reason wins (pinned by test_stop_reasons_are_one_closed_set)."""
        if reason not in STOP_REASONS:
            raise ValueError(f"unknown stop reason {reason!r}")
        self.stop_reason = self.stop_reason or reason

    def _cancel_if(self, asked: bool) -> None:
        if asked:
            self.stop("cancelled")

    def emit(self, event: Event) -> None:
        """A normalized event from the adapter; the tool cap counts its ToolCalls."""
        if not isinstance(event, ToolCall):
            return
        self.tool_calls += 1
        self.tools_used.append(event.name)
        cap = self.spec.max_tool_calls if self.spec else None
        if cap is not None and self.tool_calls >= cap:
            self.stop("tool_cap")

    def record_result(self, result: RunResult, ending: HarnessStop | None = None) -> None:
        """An adapter's normalized result for one agent process: kept as self.result, and its
        usage, duration and turns added to the job's totals."""
        self.result = result
        self._ending_after_exit = ending
        self.rounds += 1
        _add_usage(self.usage, result.get("usage"))
        self.duration_seconds += result.get("duration_seconds") or 0
        self.turns += result.get("turns") or 0

    @contextlib.contextmanager
    def heartbeat(self):
        """Report in every 20 s while the worker itself is busy (checks, tests, dependency
        installs): they can outlast the 120 s Sazed gives a silent job before timing it out."""
        done = threading.Event()

        def beat() -> None:
            while not done.wait(20):
                self._cancel_if(self.sink.beat(force=True))

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join(timeout=5)

    def run(self, spec: RunSpec) -> int:
        agent = adapters.ADAPTERS[spec.engine]
        argv, env, user = launch_args(spec)
        self.spec = spec
        self._ending_after_exit = None
        self._past_drain = False
        self.deadline = time.monotonic() + spec.timeout_s + spec.grace_s
        with contextlib.ExitStack() as files:
            stderr = (
                files.enter_context(spec.stderr_path.open("a"))
                if spec.stderr_path
                else subprocess.PIPE
            )
            proc = subprocess.Popen(
                argv,
                cwd=spec.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                start_new_session=True,  # its own process group, killed as a whole
                **user,
            )
            return self._follow(agent, proc, spec)

    def _follow(self, agent: Any, proc: subprocess.Popen, spec: RunSpec) -> int:
        stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        reader = None
        if proc.stderr:
            reader = threading.Thread(target=lambda: stderr_tail.extend(proc.stderr), daemon=True)
            reader.start()
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        stdout = None
        try:
            if self.drain_s is None:
                for line in proc.stdout:
                    self._line(agent, line)
                code = proc.wait()
            else:
                stdout = threading.Thread(target=self._read, args=(agent, proc), daemon=True)
                stdout.start()
                code = proc.wait()
                kill_group(proc)
                stdout.join(timeout=self.drain_s)
                self._past_drain = True
            if self._ending_after_exit and spec.harness_stops:
                self.stop(self._ending_after_exit)
            after_exit = getattr(agent, "after_exit", None)
            if after_exit:
                after_exit(self, code)
            return code
        finally:
            kill_group(proc)  # also reaps background children the agent left behind
            if stdout is None or not stdout.is_alive():
                proc.stdout.close()
            if reader:
                reader.join(timeout=5)
                self.stderr = "".join(stderr_tail)[-4000:]
            else:
                self.stderr = spec.stderr_path.read_text()[-4000:]
            self._cancel_if(self.sink.close())

    def _read(self, agent: Any, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                if self._past_drain:
                    return
                self._line(agent, line)
        except Exception:
            log.exception("reading %s's output failed", self.name)

    def _line(self, agent: Any, line: str) -> None:
        text = line.strip()
        event = saved = None
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"event": "raw", "line": text[:2000]}
            if isinstance(parsed, dict):
                event = parsed
                try:
                    agent.handle_event(self, event)
                except Exception:
                    log.warning("unexpected event shape: %.200s", text, exc_info=True)
                try:
                    saved = agent.trim_event(event)
                except Exception:
                    log.warning("couldn't trim event: %.200s", text, exc_info=True)
                    saved = event
        self._cancel_if(self.sink.event(line, event, saved))

    def _watch(self, proc: subprocess.Popen) -> None:
        """Kill the agent on cancel, timeout, tool cap or shutdown, and its group once it exits."""
        try:
            while proc.poll() is None:
                if time.monotonic() > self.deadline:
                    self.stop("timed_out")
                if stop.is_set():
                    self.stop("shutdown")
                if self.stop_reason:
                    log.info("stopping %s: %s", self.name, self.stop_reason)
                    kill_group(proc)
                    return
                time.sleep(max(0.0, min(1.0, self.deadline - time.monotonic())))
                self._cancel_if(self.sink.beat(force=False))
            kill_group(proc)
        except Exception:
            log.exception("watchdog failed for %s; killing the agent", self.name)
            kill_group(proc)


class JobEvents:
    """A worker job's sink: events batched to Sazed, which doubles as heartbeat and cancel."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._pending: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

    def event(self, line: str, event: dict[str, Any] | None, saved: dict[str, Any] | None) -> bool:
        if not line.strip():
            return False
        if saved is not None:
            with self._lock:
                self._pending.append(saved)
                if len(self._pending) > MAX_PENDING_EVENTS:
                    self._pending = self._pending[-MAX_PENDING_EVENTS:]
        return self.flush()

    def beat(self, force: bool) -> bool:
        if force or time.monotonic() - self._last_flush > 20:
            return self.flush(force=True)
        return False

    def close(self) -> bool:
        return self.flush(force=True)

    def flush(self, force: bool = False) -> bool:
        with self._lock:
            if not force and time.monotonic() - self._last_flush < FLUSH_SECONDS:
                return False
            events, self._pending = self._pending, []
            self._last_flush = time.monotonic()
        status, body = post(f"/worker/jobs/{self.job_id}/events", {"events": events})
        if status == 200:
            return isinstance(body, dict) and bool(body.get("cancel"))
        log.warning("events post failed (%s): %s", status, body)
        with self._lock:  # keep the batch for the next try, within reason
            self._pending = (events + self._pending)[-MAX_PENDING_EVENTS:]
        return False


def _add_usage(total: dict[str, Any], usage: Any) -> None:
    """Add one round's token counts into the running total: numbers are summed (nested counts
    such as cache_creation too), and anything else keeps its last value."""
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, bool):
            total[key] = value
        elif isinstance(value, int | float):
            total[key] = (total.get(key) or 0) + value
        elif isinstance(value, dict):
            inner = total.get(key) if isinstance(total.get(key), dict) else {}
            _add_usage(inner, value)
            total[key] = inner
        else:
            total[key] = value
