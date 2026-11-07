"""Stands in for `claude -p` in tests: one commit and a schema-shaped final event, no tokens."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


def _target(owns: list[str], task: str) -> Path:
    first = owns[0] if owns else "."
    literal = [p for p in first.split("/") if not set(p) & set("*?[")]
    return Path(*literal) / f"{task}.txt" if literal else Path(f"{task}.txt")


def _commit(path: Path, text: str, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    subprocess.run(["git", "add", "--", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], check=True, capture_output=True)


def main() -> None:
    task = os.environ["DISPATCH_TASK"]
    owns = json.loads(os.environ.get("DISPATCH_OWNS", "[]"))
    mode = json.loads(os.environ.get("DISPATCH_FAKE_MODES", "{}")).get(task, "inside")
    session = str(uuid.uuid4())
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
    state, extra = "done", {}
    if mode == "inside":
        _commit(_target(owns, task), f"{task}\n", f"{task}: work")
    elif mode == "outside":
        _commit(Path("README.md"), f"{task} strayed\n", f"{task}: stray edit")
    elif mode == "failcheck":
        _commit(_target(owns, task), "FAIL\n", f"{task}: breaks the check")
    elif mode == "nocommit":
        pass
    elif mode == "waiting":
        state, extra = "waiting", {"question": "Which file holds the shared type?"}
    elif mode == "reads":
        block = {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}
        print(json.dumps({"type": "assistant", "message": {"content": [block]}}), flush=True)
    elif mode == "hang":
        time.sleep(600)
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session,
                "total_cost_usd": 0.0,
                "num_turns": 1,
                "structured_output": {"state": state, "summary": f"{task} {mode}"} | extra,
            }
        ),
        flush=True,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
