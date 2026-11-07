"""PreToolUse hook for dispatch agents: refuse what rules.py refuses, with the reason."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sazed_worker.dispatch.rules import bash_reason, edit_reason  # noqa: E402

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def reason_for(event: dict, worktree: str, owns: list[str]) -> str | None:
    tool = event.get("tool_name", "")
    args = event.get("tool_input") or {}
    if tool == "Bash":
        return bash_reason(args.get("command", ""), worktree)
    if tool in EDIT_TOOLS:
        path = args.get("file_path") or args.get("notebook_path") or ""
        return edit_reason(path, owns, worktree)
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--owns-file", required=True)
    args = parser.parse_args()
    owns = json.loads(Path(args.owns_file).read_text())
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    reason = reason_for(event, args.worktree, owns)
    if reason:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )


if __name__ == "__main__":
    main()
