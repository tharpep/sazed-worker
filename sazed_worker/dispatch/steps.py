"""Turns an agent's stream-json events into steps and posts them to Sazed in batches."""

import queue
import threading
import time
from typing import Any

from sazed_worker.dispatch.mirror import Mirror

MAX_TEXT = 160
BATCH = 10
INTERVAL_S = 2.0
TICK_S = 1.0


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
    if event.get("type") in ("assistant", "user"):
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
    if event.get("event") == "step_update":
        update = event.get("step_update") or {}
        step_type = update.get("step_type")
        state = update.get("state")
        if step_type in ("tool", "tool_call") and state == "ACTIVE":
            tool = update.get("tool_name") or (update.get("tool_info") or {}).get("name")
            params = (
                (update.get("tool_info") or {}).get("parameters")
                or (update.get("tool_call") or {}).get("arguments")
                or {}
            )
            first = next(iter(params.values()), "") if isinstance(params, dict) else ""
            step = ("tool", tool, _short(str(first), worktree), False)
            return [dict(zip(("kind", "tool", "text", "error"), step, strict=True))]
        if step_type == "tool" and state == "DONE":
            output = (update.get("tool_info") or {}).get("output", "")
            if output:
                text = _short(_first_line(output), worktree)
                step = ("result", None, text, False)
                return [dict(zip(("kind", "tool", "text", "error"), step, strict=True))]
        if step_type == "tool_result":
            res = update.get("tool_result") or {}
            text = _short(_first_line(res.get("result", "")), worktree)
            step = ("result", None, text, bool(res.get("error")))
            return [dict(zip(("kind", "tool", "text", "error"), step, strict=True))]
        if step_type == "agent_response" and state == "DONE":
            text = (update.get("text_delta") or "").strip()
            if text:
                step = ("text", None, _short(text, worktree), False)
                return [dict(zip(("kind", "tool", "text", "error"), step, strict=True))]
    return []


class StepSender:
    def __init__(self, mirror: Mirror | None, task: str, worktree: str):
        self.mirror = mirror
        self.task = task
        self.worktree = worktree
        self.seq = 0
        self.pending: list[dict[str, Any]] = []
        self.last_send = time.monotonic()
        self.lock = threading.Lock()
        self.queue: queue.Queue[list[dict[str, Any]] | None] = queue.Queue()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        if mirror:
            self.thread.start()

    def _drain(self) -> None:
        assert self.mirror
        while True:
            try:
                batch = self.queue.get(timeout=TICK_S)
            except queue.Empty:
                with self.lock:
                    if self.pending and time.monotonic() - self.last_send >= INTERVAL_S:
                        self._send(len(self.pending))
                continue
            if batch is None:
                return
            self.mirror.steps(self.task, batch)

    def add(self, event: dict[str, Any]) -> None:
        if not self.mirror:
            return
        ts = time.time()
        with self.lock:
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
        with self.lock:
            if self.pending:
                self._send(len(self.pending))
            self.queue.put(None)
        self.thread.join(timeout=10)
