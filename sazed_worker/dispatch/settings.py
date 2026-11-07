"""The per-run settings file a dispatch agent starts with (plans/17 D1, CAPABILITIES.md)."""

import json
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "hook.py"

ALLOW = ["Bash", "Read", "Glob", "Grep", "Edit", "Write", "MultiEdit", "NotebookEdit"]

DENY = [
    "Bash(git push:*)",
    "Bash(git add -A:*)",
    "Bash(git add --all:*)",
    "Bash(git commit -a:*)",
    "Bash(git commit --all:*)",
    "Bash(git remote set-url:*)",
    "Bash(gh pr:*)",
    "Bash(gh api:*)",
    "Bash(pkill:*)",
    "Bash(killall:*)",
    "Bash(claude plugin:*)",
    "Bash(claude mcp:*)",
    "Bash(claude config:*)",
    "Edit(.claude/**)",
    "Write(.claude/**)",
    "Edit(.git/**)",
    "Write(.git/**)",
]


def build(worktree: Path, owns: list[str], run_dir: Path, timeout_s: int) -> Path:
    """Write the settings and owns files into run_dir and return the settings path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    owns_file = run_dir / "owns.json"
    owns_file.write_text(json.dumps(owns))
    command = f'"{sys.executable}" "{HOOK}" --worktree "{worktree}" --owns-file "{owns_file}"'
    hook = [{"type": "command", "command": command}]
    timeout_ms = str(timeout_s * 1000)
    settings = {
        "permissions": {"allow": ALLOW, "deny": DENY},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": hook},
                {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": hook},
            ]
        },
        "env": {
            "BASH_DEFAULT_TIMEOUT_MS": timeout_ms,
            "BASH_MAX_TIMEOUT_MS": timeout_ms,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        },
    }
    path = run_dir / "agent-settings.json"
    path.write_text(json.dumps(settings, indent=2))
    return path
