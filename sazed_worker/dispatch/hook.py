"""PreToolUse hook for dispatch agents: refuse what rules.py refuses, with the reason."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sazed_worker.dispatch.rules import bash_reason, edit_reason  # noqa: E402

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
AGY_EDIT_TOOLS = (
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "sed_file",
    "notebook_edit",
)


def reason_for(
    event: dict, worktree: str, owns: list[str], read_only: bool = False
) -> str | None:
    if "toolCall" in event:
        call = event.get("toolCall") or {}
        tool = call.get("name", "")
        args = call.get("args") or {}
        if read_only and (tool == "run_command" or tool in AGY_EDIT_TOOLS):
            return "this is a read-only task: you change no files"
        if tool == "run_command":
            return bash_reason(args.get("CommandLine", ""), worktree)
        if tool in AGY_EDIT_TOOLS:
            path = (
                args.get("Path")
                or args.get("path")
                or args.get("notebook_path")
                or args.get("file_path")
                or ""
            )
            return edit_reason(path, owns, worktree)
        return None
    tool = event.get("tool_name", "")
    args = event.get("tool_input") or {}
    if read_only and (tool == "Bash" or tool in EDIT_TOOLS):
        return "this is a read-only task: you change no files"
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
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    owns = json.loads(Path(args.owns_file).read_text())
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    reason = reason_for(event, args.worktree, owns, read_only=args.read_only)
    is_agy = "toolCall" in event
    if reason:
        if is_agy:
            json.dump({"decision": "deny", "reason": reason}, sys.stdout)
        else:
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
    elif is_agy:
        json.dump({"decision": "allow"}, sys.stdout)


if __name__ == "__main__":
    main()
