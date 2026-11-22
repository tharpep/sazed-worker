"""A worker job's stop for everything its plan is running: the agents and the jailed checks."""

import contextlib
import threading
from collections.abc import Callable, Iterator
from typing import Any

from sazed_worker.adapters.contract import StopReason

Stopper = Callable[[StopReason], None]
ROWS: dict[str, tuple[str, str, str]] = {
    "timed_out": ("timed_out", "timeout", "stopped at the plan's wall-clock cap"),
    "cancelled": ("failed", "cancelled", "cancelled"),
    "shutdown": ("failed", "cancelled", "stopped because the worker was shutting down"),
    "rate_limit": ("failed", "plan_limit", "stopped because the plan hit Pryce's usage limit"),
}


class Halt:
    """The first reason wins; whatever starts after it is stopped at once."""

    def __init__(self) -> None:
        self.reason: StopReason | None = None
        self.rate_limited: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._running: dict[object, Stopper] = {}

    def stop(self, reason: StopReason, rate_limited: dict[str, Any] | None = None) -> None:
        if reason not in ROWS:
            raise ValueError(f"unknown halt reason {reason!r}")
        with self._lock:
            if self.reason is not None:
                return
            self.reason, self.rate_limited = reason, rate_limited
            running = list(self._running.values())
        for stopper in running:
            stopper(reason)

    @contextlib.contextmanager
    def over(self, stopper: Stopper) -> Iterator[None]:
        """While the block runs, a stop calls stopper, from another thread."""
        key = object()
        with self._lock:
            self._running[key] = stopper
            already = self.reason
        if already:
            stopper(already)
        try:
            yield
        finally:
            with self._lock:
                del self._running[key]

    def row(self) -> tuple[str, str, str] | None:
        """The state, reason code and line of a task this stop ended; None when not stopped."""
        return ROWS[self.reason] if self.reason else None
