"""The integrator: seed interfaces, land done tasks in dependency order, export one draft PR."""

import os
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.plan import Plan, Task, resolve
from sazed_worker.dispatch.rules import glob_match
from sazed_worker.dispatch.run_agent import refusal, run_task, unready_deps
from sazed_worker.dispatch.workspace import Workspace, run_code

NO_TESTS_RAN = 5


class IntegrateError(RuntimeError):
    pass


def order(plan: Plan) -> list[Task]:
    tasks = {t.id: t for t in plan.tasks}
    done: list[str] = []

    def visit(tid: str) -> None:
        if tid in done:
            return
        for dep in tasks[tid].depends_on:
            visit(dep)
        done.append(tid)

    for t in plan.tasks:
        visit(t.id)
    return [tasks[t] for t in done]


def _check(plan: Plan, task: Task | None, config: dict[str, Any]) -> str:
    found = (task.check if task else None) or plan.check or config.get("check")
    if not found:
        raise IntegrateError("no check to land against")
    return found


def init(
    plan: Plan, ws: Workspace, board: Board, base: str, config: dict[str, Any] | None = None
) -> str:
    """Open integ/<plan> and commit the shared interface files once the check passes on them."""
    config = config or {}
    fresh = not ws.integ_exists()
    ws.open(base)
    if plan.interface_files:
        for rel, content in plan.interface_files.items():
            path = ws.integ_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        code, out = run_code(_check(plan, None, config), ws.integ_dir)
        if code not in (0, NO_TESTS_RAN):
            if fresh:
                ws.discard([])
            raise IntegrateError(f"the check fails on the interface files: {out[-300:]}")
        ws.commit_all(f"Interfaces for {plan.id}", list(plan.interface_files))
    return ws.tip()


def land(
    plan: Plan, ws: Workspace, board: Board, config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Land every done code task whose dependencies landed, in order; stop at the first red."""
    config = config or {}
    rows = []
    for task in order(plan):
        cfg = resolve(task, config)
        if cfg["read_only"] or board.state(task.id) != "done":
            continue
        if unready_deps(plan, task, board, config):
            continue
        strays = [
            p for p in ws.changed_paths(task.id) if not any(glob_match(p, g) for g in task.owns)
        ]
        if strays:
            rows.append(board.write(task.id, "failed", f"left its owns: {', '.join(strays)}"))
            break
        landing = ws.land(task.id, _check(plan, task, config), plan.regen, int(cfg["timeout_s"]))
        rows.append(board.write(task.id, landing.state, landing.line, ref=landing.ref))
        if landing.state != "landed":
            break
    return rows


def go(
    plan: Plan,
    ws: Workspace,
    board: Board,
    base: str,
    config: dict[str, Any] | None = None,
    parallel: int = 3,
) -> dict[str, str]:
    """Run every ready task, land what finished, repeat until nothing moves."""
    config = config or {}
    init(plan, ws, board, base, config)
    while True:
        states = {t.id: board.state(t.id) for t in plan.tasks}
        ready = [
            t
            for t in plan.tasks
            if states[t.id] in (None, "blocked") and not refusal(plan, t, board, config)
        ]
        if ready:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                list(pool.map(lambda t: run_task(plan, t, ws, board, config), ready))
        landed = land(plan, ws, board, config)
        moved = bool(ready) or any(r["state"] == "landed" for r in landed)
        red = any(r["state"] in ("failed", "conflict") for r in landed)
        if red or not moved:
            return {t.id: board.state(t.id) or "not started" for t in plan.tasks}


def export(
    plan: Plan,
    ws: Workspace,
    board: Board,
    base: str,
    title: str,
    body: str,
    config: dict[str, Any] | None = None,
    open_pr: bool = True,
) -> str:
    """Push integ/<plan> and open one draft PR into base; never merges, never targets main."""
    config = config or {}
    code = [t for t in plan.tasks if not resolve(t, config)["read_only"]]
    unlanded = [t.id for t in code if board.state(t.id) != "landed"]
    if unlanded:
        raise IntegrateError(f"not every code task landed: {', '.join(unlanded)}")
    branch = ws.export()
    if not open_pr:
        return branch
    gh = shlex.split(os.environ.get("DISPATCH_GH", "gh"))
    proc = subprocess.run(
        [
            *gh,
            "pr",
            "create",
            "--draft",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=ws.integ_dir,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise IntegrateError(f"gh pr create failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def clean(plan: Plan, ws: Workspace, board: Board) -> None:
    running = [t.id for t in plan.tasks if board.state(t.id) == "running"]
    if running:
        raise IntegrateError(f"still running: {', '.join(running)}")
    ws.discard([t.id for t in plan.tasks])
    for t in plan.tasks:
        board.write(t.id, "cleaned", "worktree and branch removed")
