"""What submit and the worker's job both hold a plan to before any of it runs (D7)."""

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from sazed_worker.dispatch.plan import Plan, PlanError, resolve
from sazed_worker.dispatch.pr_export import PLAN_ID
from sazed_worker.dispatch.rules import folded

# pinned by test_caps_that_meet_the_budget_are_accepted_and_over_it_refused
CAP_DECIMALS = 6


def plan_hash(plan: dict[str, Any], config: dict[str, Any]) -> str:
    """Sazed computes this with the same expression; both suites pin one literal."""
    text = json.dumps(
        {"plan": plan, "config": config},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _names(name: Any) -> bool:
    """A git ref component and a folder; a leading dash would read as a flag."""
    return isinstance(name, str) and bool(PLAN_ID.fullmatch(name)) and name[0] not in ".-"


def _inside(rel: str) -> bool:
    path = PurePosixPath(rel)
    parts = {folded(p) for p in path.parts}
    return bool(path.parts) and not path.is_absolute() and not parts & {"..", ".git"}


def name_refusal(plan: Plan) -> str | None:
    """Why a name in the plan cannot become a path or a branch that root creates."""
    for what, name in (("plan", plan.id), *(("task", t.id) for t in plan.tasks)):
        if not _names(name):
            return f"{what} id {name!r} cannot name a branch and a folder"
    for rel in plan.interface_files:
        if not _inside(rel):
            return f"interface file {rel!r} is not a path inside the repository"
    return None


def caps(plan: Plan, config: dict[str, Any]) -> dict[str, float]:
    """Each task's resolved budget; PlanError for a task without one."""
    out = {}
    for task in plan.tasks:
        cap = resolve(task, config).get("budget_usd")
        if isinstance(cap, bool) or not isinstance(cap, int | float) or not cap > 0:
            raise PlanError(f"task {task.id} has no budget_usd")
        out[task.id] = cap
    return out


def over_budget(task_caps: dict[str, float], budget_usd: float) -> str | None:
    total = round(sum(task_caps.values()), CAP_DECIMALS)
    if total > budget_usd:
        return f"the task caps add up to ${total:.2f}, over the ${budget_usd:.2f} budget"
    return None
