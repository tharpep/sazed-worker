"""Best-effort HTTP mirror of a plan and its board rows to Sazed."""

import json
import os
import socket
import sys
import urllib.request
from pathlib import Path
from typing import Any

from sazed_worker import sazed_api
from sazed_worker.dispatch.plan import Plan, resolve
from sazed_worker.dispatch.workspace import GitError, git

TIMEOUT_S = 3


def repo_name(repo: Path) -> str:
    try:
        url = git("remote", "get-url", "origin", cwd=repo)
    except (GitError, OSError):
        url = ""
    parts = url.removesuffix(".git").replace(":", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 and parts[-1] else repo.name


class Mirror:
    def __init__(self, base: str, plan_id: str, key: str | None = None):
        self.base = base.rstrip("/")
        self.plan_id = plan_id
        self.key = key
        self.failed = False

    @classmethod
    def from_env(cls, plan_id: str) -> "Mirror | None":
        base = os.environ.get("DISPATCH_SAZED_URL")
        return cls(base, plan_id, os.environ.get("DISPATCH_SAZED_KEY")) if base else None

    def _send(self, path: str, body: dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-API-Key"] = self.key
        req = urllib.request.Request(
            f"{self.base}{path}", json.dumps(body).encode(), headers, method="POST"
        )
        urllib.request.urlopen(req, timeout=TIMEOUT_S).close()

    def _post(self, path: str, body: dict[str, Any], always: bool = False) -> None:
        if self.failed and not always:
            return
        try:
            self._send(path, body)
        except Exception as e:
            if not self.failed:
                print(
                    f"dispatch: mirror to {self.base} failed, run sync later: {e}",
                    file=sys.stderr,
                )
            self.failed = True

    def row(self, row: dict[str, Any]) -> None:
        # A settled row is tried even after a failure: the task's beats reach Sazed regardless,
        # so a missing final row would let the reaper fail a task that finished.
        settled = row["state"] != "running"
        self._post("/dispatch/rows", {"plan": self.plan_id, **row}, always=settled)

    def heartbeat(self, task: str) -> None:
        """Never raises, and ignores the failed flag: one blip must not stop the beats and get a
        healthy task reaped, and an unreachable Sazed must not fail the task (seam 4)."""
        try:
            self._send("/dispatch/heartbeat", {"plan": self.plan_id, "task": task})
        except Exception:
            pass

    def reading(self, info: dict[str, Any]) -> None:
        body = {"machine": socket.gethostname(), "rate_limit_info": info}
        self._post("/claude-usage/readings", body)

    def steps(self, task: str, steps: list[dict[str, Any]]) -> None:
        self._post("/dispatch/steps", {"plan": self.plan_id, "task": task, "steps": steps})

    def plan(self, plan: Plan, config: dict[str, Any], repo: Path, base: str) -> None:
        tasks = []
        for t in plan.tasks:
            cfg = resolve(t, config)
            tasks.append(
                {
                    "id": t.id,
                    "goal": t.goal,
                    "owns": t.owns,
                    "depends_on": t.depends_on,
                    "profile": cfg["profile"],
                    "model": cfg["model"],
                }
            )
        body = {
            "plan": plan.id,
            "repo": repo_name(repo),
            "base": base,
            "host": socket.gethostname(),
            "tasks": tasks,
        }
        for key in ("project", "board_plan"):
            if getattr(plan, key):
                body[key] = getattr(plan, key)
        self._post("/dispatch/plans", body)


class JobMirror(Mirror):
    """A worker job's mirror: the worker key on the worker routes, which Sazed takes only while
    the plan's dispatch job runs. Those routes take no usage readings."""

    def __init__(self, plan_id: str):
        super().__init__(sazed_api.SAZED_URL, plan_id)

    def _send(self, path: str, body: dict[str, Any]) -> None:
        status, answer = sazed_api.post(f"/worker{path}", body, timeout=TIMEOUT_S)
        if not 200 <= status < 300:
            raise OSError(f"{path} answered {status}: {answer}")

    def reading(self, info: dict[str, Any]) -> None:
        pass
