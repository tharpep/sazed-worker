"""One dispatch task start to exit: a `claude -p` run in its own worktree, under its guardrails."""

import json
import os
import queue
import shlex
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sazed_worker.adapters.contract import RunSpec
from sazed_worker.dispatch import mcp
from sazed_worker.dispatch import settings as agent_settings
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import Mirror
from sazed_worker.dispatch.plan import Plan, Task, could_overlap, resolve
from sazed_worker.dispatch.steps import StepSender
from sazed_worker.dispatch.workspace import Workspace
from sazed_worker.run import Run

STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"enum": ["done", "waiting"]},
        "summary": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["state", "summary"],
}

EVIDENCE_TOOLS = (
    "Read",
    "Grep",
    "Glob",
    "view_file",
    "read_file",
    "grep_search",
    "find_by_name",
    "list_dir",
)
READ_ONLY_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
STOPPED_LINES = {
    "refusal": "the model refused to continue",
    "max_tokens": "stopped at the model's output token limit",
    "shutdown": "stopped because dispatch was shutting down",
    "cancelled": "cancelled",
}
DRAIN_S = 5
CLOSE_S = 10
# Sazed's reaper fails a running task after DISPATCH_HEARTBEAT_GRACE_S (150 s) without one.
HEARTBEAT_S = 30

PROMPT = """You are one task in the dispatch plan {plan}. Task {task}: {goal}

Done means: {done}
{scope}
Shared interfaces, which you read and never edit: {interfaces}
The check that must pass once your work lands: {check}

Rules:
- Work only in this directory.{commit_rule}
- If you need a change outside your paths, stop and finish with state waiting, naming the change.
- Finish with the structured result: state done with a one-line summary, or state waiting with
  the question you need answered."""


def prompt_for(plan: Plan, task: Task, read_only: bool, check: str | None) -> str:
    if read_only:
        scope = "This is a read-only task: you change no files, and your result is the summary."
        commit_rule = ""
    else:
        scope = f"You own these paths, and only these: {', '.join(task.owns)}"
        commit_rule = (
            " Commit with named paths (git add <paths> && git commit -m ...);"
            " never push. Run the check before you finish."
        )
    return PROMPT.format(
        plan=plan.id,
        task=task.id,
        goal=task.goal,
        done=task.done,
        scope=scope,
        interfaces=", ".join(plan.interfaces) or "none",
        check=check or "none",
        commit_rule=commit_rule,
    )


def unready_deps(plan: Plan, task: Task, board: Board, config: dict[str, Any]) -> list[str]:
    """Dependencies not settled yet: a code task must have landed, a read-only one finished."""
    tasks = {t.id: t for t in plan.tasks}
    out = []
    for dep in task.depends_on:
        settled = ("landed", "done") if resolve(tasks[dep], config)["read_only"] else ("landed",)
        if board.state(dep) not in settled:
            out.append(dep)
    return out


def refusal(plan: Plan, task: Task, board: Board, config: dict[str, Any]) -> str | None:
    """Why this task can't start yet, or None."""
    waiting_on = unready_deps(plan, task, board, config)
    if waiting_on:
        return f"waiting on {', '.join(waiting_on)} to land"
    tasks = {t.id: t for t in plan.tasks}
    for other, row in board.latest().items():
        if other == task.id or row["state"] != "running" or other not in tasks:
            continue
        if any(could_overlap(a, b) for a in task.owns for b in tasks[other].owns):
            return f"its paths overlap {other}, which is running"
    return None


def _claude() -> tuple[str, ...]:
    return tuple(shlex.split(os.environ.get("DISPATCH_CLAUDE", "claude")))


def _agy() -> tuple[str, ...]:
    return tuple(shlex.split(os.environ.get("DISPATCH_AGY", "agy")))


def task_spec(
    cfg: dict[str, Any],
    worktree: Path,
    prompt: str,
    settings_path: Path,
    read_only: bool,
    grant: mcp.Grant | None = None,
    env_extra: dict[str, str] | None = None,
    stderr_path: Path | None = None,
) -> RunSpec:
    """A hand run's spec: the caller's environment and user, the exact timeout, no grace."""
    effort = cfg.get("effort")
    common: dict[str, Any] = {
        "prompt": prompt,
        "model": str(cfg["model"]),
        "cwd": worktree,
        "effort": str(effort) if effort else None,
        "output_schema": STATE_SCHEMA,
        "timeout_s": int(cfg["timeout_s"]),
        "max_tool_calls": cfg.get("max_tool_calls"),
        "env": "inherited",
        "env_extra": env_extra or {},
        "as_agent": False,
        "argv_layout": "dispatch",
        "stderr_path": stderr_path,
        "harness_stops": True,
    }
    if cfg.get("engine") == "agy":
        return RunSpec(
            engine="agy",
            binary=_agy(),
            permission_mode="bypassPermissions",
            slash_commands=False,
            **common,
        )
    grant = grant or mcp.Grant()
    return RunSpec(
        engine="claude",
        binary=_claude(),
        permission_mode="acceptEdits",
        settings=settings_path,
        setting_sources="project",
        budget_usd=cfg.get("budget_usd"),
        denied_tools=READ_ONLY_TOOLS if read_only else (),
        mcp_config=grant.config,
        allowed_tools=tuple(grant.allowed),
        **common,
    )


def run_task(
    plan: Plan, task: Task, ws: Workspace, board: Board, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = config or {}
    cfg = resolve(task, config)
    blocked = refusal(plan, task, board, config)
    if blocked:
        return board.write(task.id, "blocked", blocked)
    read_only = bool(cfg["read_only"])
    timeout_s = int(cfg["timeout_s"])
    check = task.check or plan.check or config.get("check")
    worktree = ws.create(task.id)
    run_dir = ws.home / "runs" / task.id
    engine = cfg.get("engine", "claude")
    settings_path = agent_settings.build(
        worktree, task.owns, run_dir, timeout_s, engine=engine, read_only=read_only
    )
    granted = mcp.build(cfg.get("mcp") or {}, run_dir) if engine != "agy" else mcp.Grant()
    prompt = prompt_for(plan, task, read_only, check)
    task_env = {
        "DISPATCH_TASK": task.id,
        "DISPATCH_OWNS": json.dumps(task.owns),
        "DISPATCH_WORKTREE": str(worktree),
        "DISPATCH_ENGINE": engine,
    }
    stderr_path = run_dir / "agent.stderr"
    spec = task_spec(
        cfg, worktree, prompt, settings_path, read_only, granted, task_env, stderr_path
    )
    board.write(
        task.id,
        "running",
        f"{cfg['model']} / {cfg.get('effort') or '-'}{granted.note}",
        worktree=str(worktree),
        profile=cfg["profile"],
    )
    outcome = _run(spec, run_dir / "agent.jsonl", board.mirror, task.id)
    return _finish(task, ws, board, outcome, spec, read_only, bool(cfg.get("evidence_required")))


def _reads(event: dict[str, Any]) -> int:
    if event.get("type") == "assistant":
        content = (event.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        return sum(
            1
            for b in blocks
            if isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("name") in EVIDENCE_TOOLS
        )
    if event.get("event") == "step_update":
        update = event.get("step_update") or {}
        if update.get("step_type") in ("tool", "tool_call") and update.get("state") == "ACTIVE":
            name = (
                update.get("tool_name")
                or (update.get("tool_info") or {}).get("name")
                or (update.get("tool_call") or {}).get("name", "")
            )
            if name in EVIDENCE_TOOLS:
                return 1
    return 0


def _beat(mirror: Mirror, task: str, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_S):
        mirror.heartbeat(task)


class ReadingSender:
    """Mirrors the plan windows from rate_limit_event, once per change in their utilization."""

    def __init__(self, mirror: Mirror | None):
        self.mirror = mirror
        self.last: dict[str, Any] | None = None

    def add(self, event: dict[str, Any]) -> None:
        info = event.get("rate_limit_info")
        if not self.mirror or event.get("type") != "rate_limit_event" or not isinstance(info, dict):
            return
        windows = info.get("unifiedWindows")
        if not isinstance(windows, dict):
            return
        used = {k: w.get("utilization") for k, w in windows.items() if isinstance(w, dict)}
        if used and used != self.last:
            self.last = used
            self.mirror.reading(info)


class TaskSink:
    """A task's agent.jsonl, steps, readings and heartbeat; it never waits on Sazed (S2-5)."""

    def __init__(self, log: Path, mirror: Mirror | None, task: str, worktree: str):
        self.file = log.open("a")
        self.closed = False
        self.evidence = 0
        self.steps = StepSender(mirror, task, worktree)
        self.readings = ReadingSender(mirror)
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.mirrored = threading.Thread(target=self._mirror, daemon=True)
        self.mirrored.start()
        self.stop_beats = threading.Event()
        if mirror:
            args = (mirror, task, self.stop_beats)
            threading.Thread(target=_beat, args=args, daemon=True).start()

    def _mirror(self) -> None:
        while (event := self.queue.get()) is not None:
            self.steps.add(event)
            self.readings.add(event)

    def event(self, line: str, event: dict[str, Any] | None, saved: dict[str, Any] | None) -> bool:
        if self.closed:
            return False
        self.file.write(line)
        if event is not None and line.startswith("{"):
            self.evidence += _reads(event)
            self.queue.put(event)
        return False

    def beat(self, force: bool) -> bool:
        return False

    def close(self) -> bool:
        """Beats stop now; the step sender closes only after the mirror thread, within CLOSE_S."""
        if self.closed:
            return False
        self.closed = True
        self.stop_beats.set()
        self.file.close()
        self.queue.put(None)

        def drain() -> None:
            self.mirrored.join()
            self.steps.close()

        closing = threading.Thread(target=drain, daemon=True)
        closing.start()
        closing.join(timeout=CLOSE_S)
        return False


def _run(spec: RunSpec, log: Path, mirror: Mirror | None = None, task: str = "") -> dict[str, Any]:
    sink = TaskSink(log, mirror, task, str(spec.cwd))
    run = Run(sink, f"task {task}", drain_s=DRAIN_S)
    try:
        code = run.run(spec)
    except BaseException:
        sink.close()
        raise
    return {
        "code": code,
        "stop_reason": run.stop_reason,
        "rate_limited": run.rate_limited,
        "result": run.result,
        "evidence": sink.evidence,
        "stderr": run.stderr[-500:],
    }


def _limit_line(limited: dict[str, Any] | None) -> str:
    limited = limited or {}
    window = (limited.get("window") or "usage").replace("_", " ")
    line = f"hit Pryce's {window} plan limit"
    if isinstance(limited.get("resets_at"), (int, float)):
        reset = datetime.fromtimestamp(limited["resets_at"], UTC).strftime("%Y-%m-%d %H:%M")
        line += f"; it resets at {reset} UTC"
    return line


def _finish(
    task: Task,
    ws: Workspace,
    board: Board,
    outcome: dict[str, Any],
    spec: RunSpec,
    read_only: bool,
    evidence_required: bool = False,
) -> dict[str, Any]:
    result = outcome["result"] or {}
    cost = result.get("cost_usd")
    common = {
        "cost_usd": 0.0 if cost is None else cost,
        "session": result.get("session_id"),
        "exit": outcome["code"],
        "turns": result.get("turns"),
    }
    stopped = outcome["stop_reason"]
    if stopped == "timed_out":
        return board.write(task.id, "failed", f"timed out after {spec.timeout_s} s", **common)
    if stopped == "tool_cap":
        line = f"hit the tool cap of {spec.max_tool_calls} calls"
        return board.write(task.id, "failed", line, **common)
    if stopped == "budget":
        return board.write(task.id, "failed", "went over its budget", **common)
    if stopped == "rate_limit":
        return board.write(task.id, "failed", _limit_line(outcome["rate_limited"]), **common)
    if stopped in STOPPED_LINES:
        return board.write(task.id, "failed", STOPPED_LINES[stopped], **common)
    if not outcome["result"]:
        detail = outcome["stderr"].strip()[-200:] or "no output"
        return board.write(
            task.id, "failed", f"no result (exit {outcome['code']}): {detail}", **common
        )
    structured = result.get("structured_output") or {}
    if structured.get("state") == "waiting":
        return board.write(
            task.id, "waiting", structured.get("question") or "needs an answer", **common
        )
    if structured.get("state") != "done":
        return board.write(task.id, "failed", "finished without a done or waiting result", **common)
    if evidence_required and not outcome["evidence"]:
        return board.write(task.id, "failed", "no evidence read", **common)
    summary = structured.get("summary") or "done"
    if not read_only and ws.commits(task.id) == 0:
        return board.write(task.id, "failed", f"said done but made no commits: {summary}", **common)
    return board.write(task.id, "done", summary, **common)
