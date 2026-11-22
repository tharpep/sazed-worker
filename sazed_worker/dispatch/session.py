"""What a run works in: the one way the command line and a worker job open a plan."""

import os
from pathlib import Path
from typing import Any

from sazed_worker.dispatch import unattended
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import Mirror
from sazed_worker.dispatch.plan import Plan, PlanError, validate
from sazed_worker.dispatch.workspace import Workspace

SHARED_UMASK = 0o022


def prepare(
    plan: Plan,
    config: dict[str, Any],
    repo: Path,
    home: Path,
    mirror: Mirror | None,
    *,
    resolves: bool = True,
    launches: bool = True,
) -> tuple[Workspace, Board]:
    """Validate the plan and return its workspace and board. An unattended launch passes
    preflight before anything is written, and its dispatch home is sealed."""
    problems = validate(plan, config) if resolves else []
    if problems:
        raise PlanError("the plan doesn't validate:\n" + "\n".join(problems))
    if not config["unattended"]:
        return Workspace(repo, plan.id, home), Board(home, mirror)
    ws = unattended.UnattendedWorkspace(repo, plan.id, home, plan.regen_outputs)
    if not launches:
        return ws, Board(home, mirror)
    unattended.preflight(plan, config, ws)
    os.umask(SHARED_UMASK)
    os.environ.update(unattended.ROOT_GIT_ENV)
    board = Board(home, mirror)
    ws.seal()
    return ws, board
