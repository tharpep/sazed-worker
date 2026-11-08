"""One dispatch task start to exit: a `claude -p` run in its own worktree, under its guardrails."""

import json
import os
import shlex
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

from sazed_worker.dispatch import mcp
from sazed_worker.dispatch import settings as agent_settings
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import Mirror
from sazed_worker.dispatch.plan import Plan, Task, could_overlap, resolve
from sazed_worker.dispatch.steps import StepSender
from sazed_worker.dispatch.workspace import Workspace

STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"enum": ["done", "waiting"]},
        "summary": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["state", "summary"],
}

EVIDENCE_TOOLS = ("Read", "Grep", "Glob")
READ_ONLY_TOOLS = "Edit,Write,MultiEdit,NotebookEdit,Bash"

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


def _claude() -> list[str]:
    return shlex.split(os.environ.get("DISPATCH_CLAUDE", "claude"))


def agent_command(
    cfg: dict[str, Any], settings_path: Path, read_only: bool, mcp_args: list[str] | None = None
) -> list[str]:
    cmd = [
        *_claude(),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        str(cfg["model"]),
        "--effort",
        str(cfg["effort"]),
        "--settings",
        str(settings_path),
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--permission-mode",
        "acceptEdits",
        "--max-budget-usd",
        str(cfg["budget_usd"]),
        "--json-schema",
        json.dumps(STATE_SCHEMA),
    ]
    if read_only:
        cmd += ["--disallowedTools", READ_ONLY_TOOLS]
    return cmd + (mcp_args or [])


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
    settings_path = agent_settings.build(worktree, task.owns, run_dir, timeout_s)
    granted = mcp.build(cfg.get("mcp") or {}, run_dir)
    cmd = agent_command(cfg, settings_path, read_only, granted.args)
    cmd += ["--", prompt_for(plan, task, read_only, check)]
    env = os.environ | {
        "DISPATCH_TASK": task.id,
        "DISPATCH_OWNS": json.dumps(task.owns),
        "DISPATCH_WORKTREE": str(worktree),
    }
    board.write(
        task.id,
        "running",
        f"{cfg['model']} / {cfg['effort']}{granted.note}",
        worktree=str(worktree),
        profile=cfg["profile"],
    )
    outcome = _run(cmd, worktree, env, timeout_s, run_dir / "agent.jsonl", board.mirror, task.id)
    return _finish(
        task, ws, board, outcome, read_only, timeout_s, bool(cfg.get("evidence_required"))
    )


def _reads(event: dict[str, Any]) -> int:
    if event.get("type") != "assistant":
        return 0
    content = (event.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else []
    return sum(
        1
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in EVIDENCE_TOOLS
    )


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


def _run(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_s: int,
    log: Path,
    mirror: Mirror | None = None,
    task: str = "",
) -> dict[str, Any]:
    err_path = log.with_name("agent.stderr")
    events: list[dict[str, Any]] = []
    timed_out = False
    sender = StepSender(mirror, task, str(cwd))
    readings = ReadingSender(mirror)
    with (
        err_path.open("a") as err,
        subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=err,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        ) as proc,
    ):

        def pump() -> None:
            with log.open("a") as f:
                for line in proc.stdout:
                    f.write(line)
                    if line.startswith("{"):
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        events.append(event)
                        sender.add(event)
                        readings.add(event)
            sender.close()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            reader.join(timeout=5)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    return {
        "code": proc.returncode,
        "timed_out": timed_out,
        "result": result,
        "evidence": sum(_reads(e) for e in events),
        "stderr": err_path.read_text()[-500:],
    }


def _finish(
    task: Task,
    ws: Workspace,
    board: Board,
    outcome: dict[str, Any],
    read_only: bool,
    timeout_s: int,
    evidence_required: bool = False,
) -> dict[str, Any]:
    result = outcome["result"] or {}
    common = {
        "cost_usd": result.get("total_cost_usd"),
        "session": result.get("session_id"),
        "exit": outcome["code"],
        "turns": result.get("num_turns"),
    }
    if outcome["timed_out"]:
        return board.write(task.id, "failed", f"timed out after {timeout_s} s", **common)
    if result.get("subtype") == "error_max_budget_usd":
        return board.write(task.id, "failed", "went over its budget", **common)
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
