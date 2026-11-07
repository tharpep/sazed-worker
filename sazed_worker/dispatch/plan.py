"""A dispatch plan: its shape, each task's resolved profile, and the checks that refuse it."""

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sazed_worker.dispatch.rules import glob_match

DEFAULTS: dict[str, Any] = {
    "model": "sonnet",
    "effort": "medium",
    "budget_usd": 2.0,
    "timeout_s": 1200,
    "read_only": False,
}

PROFILES: dict[str, dict[str, Any]] = {
    "implement": {},
    "design": {"model": "opus", "effort": "high", "budget_usd": 4.0},
    "verify": {"model": "haiku", "read_only": True, "budget_usd": 0.5, "timeout_s": 600},
    "sweep": {"model": "haiku", "read_only": True, "budget_usd": 0.5, "timeout_s": 600},
}

REQUIRED = ("model", "effort", "budget_usd", "timeout_s")
_WILD = set("*?[")


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    goal: str
    done: str
    owns: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    check: str | None = None
    profile: str = "implement"
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    id: str
    tasks: list[Task]
    check: str | None = None
    interfaces: list[str] = field(default_factory=list)
    regen: str | None = None
    interface_files: dict[str, str] = field(default_factory=dict)


_TASK_KEYS = {"id", "goal", "done", "owns", "depends_on", "check", "profile"}


def load(path: Path) -> Plan:
    data = json.loads(path.read_text())
    tasks = []
    for raw in data.get("tasks", []):
        known = {k: raw[k] for k in _TASK_KEYS if k in raw}
        if "id" not in known or "goal" not in known:
            raise PlanError(f"every task needs an id and a goal: {raw}")
        known.setdefault("done", "")
        overrides = {k: v for k, v in raw.items() if k not in _TASK_KEYS}
        tasks.append(Task(**known, overrides=overrides))
    files = data.get("interface_files", {})
    return Plan(
        id=data["id"],
        tasks=tasks,
        check=data.get("check"),
        interfaces=list(dict.fromkeys([*data.get("interfaces", []), *files])),
        regen=data.get("regen"),
        interface_files=files,
    )


def load_config(path: Path | None) -> dict[str, Any]:
    return tomllib.loads(path.read_text()) if path and path.exists() else {}


def resolve(task: Task, config: dict[str, Any]) -> dict[str, Any]:
    """The task's run settings: defaults, then its profile, then the config's, then its own."""
    profiles = PROFILES | config.get("profiles", {})
    if task.profile not in profiles:
        raise PlanError(f"task {task.id}: unknown profile {task.profile!r}")
    merged = DEFAULTS | config.get("defaults", {}) | PROFILES.get(task.profile, {})
    merged |= config.get("profiles", {}).get(task.profile, {}) | task.overrides
    missing = [k for k in REQUIRED if merged.get(k) in (None, "")]
    if missing:
        raise PlanError(f"task {task.id}: no {', '.join(missing)}")
    return merged | {"profile": task.profile}


def could_overlap(a: str, b: str) -> bool:
    """Whether some path could match both globs; errs toward yes."""
    sa, sb = a.strip("/").split("/"), b.strip("/").split("/")
    while sa and sb:
        x, y = sa.pop(0), sb.pop(0)
        if x == "**" or y == "**":
            return True
        wild_x, wild_y = bool(_WILD & set(x)), bool(_WILD & set(y))
        if wild_x and wild_y:
            continue
        if wild_x and not glob_match(y, x) or wild_y and not glob_match(x, y):
            return False
        if not wild_x and not wild_y and x != y:
            return False
    return not sa and not sb or (sa or sb) == ["**"]


def _ancestors(tasks: dict[str, Task]) -> dict[str, set[str]]:
    seen: dict[str, set[str]] = {}

    def walk(tid: str, path: tuple[str, ...]) -> set[str]:
        if tid in path:
            raise PlanError(f"dependency cycle: {' -> '.join((*path, tid))}")
        if tid not in seen:
            found: set[str] = set()
            for dep in tasks[tid].depends_on:
                found |= {dep} | walk(dep, (*path, tid))
            seen[tid] = found
        return seen[tid]

    for tid in tasks:
        walk(tid, ())
    return seen


def validate(plan: Plan, config: dict[str, Any] | None = None) -> list[str]:
    """Every reason this plan can't run; empty when it can."""
    config = config or {}
    problems: list[str] = []
    ids = [t.id for t in plan.tasks]
    if not plan.tasks:
        return ["the plan has no tasks"]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        return [f"duplicate task ids: {', '.join(dupes)}"]
    tasks = {t.id: t for t in plan.tasks}
    for t in plan.tasks:
        unknown = [d for d in t.depends_on if d not in tasks]
        if unknown:
            problems.append(f"task {t.id} depends on unknown tasks: {', '.join(unknown)}")
    if problems:
        return problems
    try:
        ancestors = _ancestors(tasks)
    except PlanError as e:
        return [str(e)]

    workstream_check = plan.check or config.get("check")
    settings: dict[str, dict[str, Any]] = {}
    for t in plan.tasks:
        try:
            settings[t.id] = resolve(t, config)
        except PlanError as e:
            problems.append(str(e))
            continue
        if not t.done.strip():
            problems.append(f"task {t.id} has no done: say what finished looks like")
        if settings[t.id]["read_only"]:
            if t.owns:
                problems.append(f"task {t.id} is read-only ({t.profile}) but owns files")
            continue
        if not t.owns:
            problems.append(f"task {t.id} changes code but owns nothing")
        if not (t.check or workstream_check):
            problems.append(f"task {t.id} has no check of its own or the plan's")
        for glob in t.owns:
            shared = [i for i in plan.interfaces if could_overlap(glob, i)]
            if shared:
                problems.append(
                    f"task {t.id} owns shared interface files {', '.join(shared)}; "
                    "interfaces are committed at init, never owned"
                )

    writers = [t for t in plan.tasks if t.id in settings and not settings[t.id]["read_only"]]
    for i, a in enumerate(writers):
        for b in writers[i + 1 :]:
            if a.id in ancestors[b.id] or b.id in ancestors[a.id]:
                continue
            clash = [f"{x} / {y}" for x in a.owns for y in b.owns if could_overlap(x, y)]
            if clash:
                problems.append(
                    f"tasks {a.id} and {b.id} can run together but both own {'; '.join(clash)}"
                )
    return problems
