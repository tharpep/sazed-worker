"""The board: append-only rows per task, one writer per file (dispatch.md, board)."""

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sazed_worker.dispatch.mirror import Mirror

STATES = ("running", "done", "waiting", "failed", "landed", "conflict", "blocked", "cleaned")
SETTLED = ("done", "waiting", "failed", "landed", "conflict", "blocked", "cleaned")


class Board:
    def __init__(self, home: Path, mirror: "Mirror | None" = None):
        self.mirror = mirror
        self.dir = home / "board"
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, task: str, state: str, line: str, **fields: Any) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"unknown state {state}")
        row = {
            "ts": round(time.time(), 3),
            "task": task,
            "state": state,
            "line": line,
            "ref": fields.pop("ref", None),
            "cost_usd": fields.pop("cost_usd", None),
            "session": fields.pop("session", None),
            "meta": fields,
        }
        with (self.dir / f"{task}.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        if self.mirror:
            self.mirror.row(row)
        return row

    def rows(self, task: str) -> list[dict[str, Any]]:
        path = self.dir / f"{task}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def latest(self) -> dict[str, dict[str, Any]]:
        out = {}
        for path in sorted(self.dir.glob("*.jsonl")):
            rows = self.rows(path.stem)
            if rows:
                out[path.stem] = rows[-1]
        return out

    def state(self, task: str) -> str | None:
        rows = self.rows(task)
        if not rows or rows[-1]["state"] == "cleaned":
            return None
        return rows[-1]["state"]
