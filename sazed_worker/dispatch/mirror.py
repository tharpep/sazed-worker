"""Best-effort HTTP mirror of a plan and its board rows to Sazed."""

import json
import os
import socket
import sys
import urllib.request
from pathlib import Path
from typing import Any

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

    def _post(self, path: str, body: dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-API-Key"] = self.key
        req = urllib.request.Request(
            f"{self.base}{path}", json.dumps(body).encode(), headers, method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=TIMEOUT_S).close()
        except Exception as e:
            if not self.failed:
                self.failed = True
                print(f"dispatch: mirror to {self.base} failed: {e}", file=sys.stderr)

    def row(self, row: dict[str, Any]) -> None:
        self._post("/dispatch/rows", {"plan": self.plan_id, **row})

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
        self._post("/dispatch/plans", body)
