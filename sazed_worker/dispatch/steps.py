"""Turns an agent's stream-json events into steps and posts them to Sazed in batches."""

import queue
import threading
import time
from typing import Any

from sazed_worker.dispatch.mirror import Mirror

MAX_TEXT = 160
BATCH = 10
INTERVAL_S = 2.0


def _short(text: str, worktree: str) -> str:
    text = text.replace(f"{worktree}/", "").replace(worktree, ".")
    return text[:MAX_TEXT]


def _first_line(content: Any) -> str:
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    lines = str(content or "").strip().splitlines()
    return lines[0] if lines else ""


def event_steps(event: dict[str, Any], worktree: str) -> list[dict[str, Any]]:
    """The steps one assistant or user event holds, without ts or seq."""
    if event.get("type") not in ("assistant", "user"):
        return []
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if event["type"] == "assistant" and kind == "tool_use":
            args = block.get("input") or {}
            first = next(iter(args.values()), "") if isinstance(args, dict) else ""
            step = ("tool", block.get("name"), _short(str(first), worktree), False)
        elif event["type"] == "user" and kind == "tool_result":
            text = _short(_first_line(block.get("content")), worktree)
            step = ("result", None, text, bool(block.get("is_error")))
        elif event["type"] == "assistant" and kind == "text":
            step = ("text", None, _short(str(block.get("text") or ""), worktree), False)
        else:
            continue
        out.append(dict(zip(("kind", "tool", "text", "error"), step, strict=True)))
    return out


class StepSender:
    def __init__(self, mirror: Mirror | None, task: str, worktree: str):
        self.mirror = mirror
        self.task = task
        self.worktree = worktree
        self.seq = 0
        self.pending: list[dict[str, Any]] = []
        self.last_send = time.monotonic()
        self.queue: queue.Queue[list[dict[str, Any]] | None] = queue.Queue()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        if mirror:
            self.thread.start()

    def _drain(self) -> None:
        while (batch := self.queue.get()) is not None:
            assert self.mirror
            self.mirror.steps(self.task, batch)

    def add(self, event: dict[str, Any]) -> None:
        if not self.mirror:
            return
        ts = time.time()
        for step in event_steps(event, self.worktree):
            self.pending.append({"ts": ts, "seq": self.seq, **step})
            self.seq += 1
        while len(self.pending) >= BATCH:
            self._send(BATCH)
        if self.pending and time.monotonic() - self.last_send >= INTERVAL_S:
            self._send(len(self.pending))

    def _send(self, n: int) -> None:
        self.queue.put(self.pending[:n])
        self.pending = self.pending[n:]
        self.last_send = time.monotonic()

    def close(self) -> None:
        if not self.mirror:
            return
        if self.pending:
            self._send(len(self.pending))
        self.queue.put(None)
        self.thread.join(timeout=10)
