"""Child processes run as the unprivileged agent user, and how they're killed."""

import os
import signal
import subprocess
import time
from typing import Any

from .config import AGENT_HOME, AGENT_USER, CLAUDE_TOKEN, SAFE_ARG_RE


class JobFailed(Exception):
    """A job that can't run (bad repo, clone failure, unsupported profile, bad input)."""


def agent_env(agent: str) -> dict[str, str]:
    """A minimal environment for an agent process (no worker key; Claude gets its own token)."""
    env = {
        "HOME": str(AGENT_HOME),
        "USER": AGENT_USER,
        "PATH": f"{AGENT_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
    }
    if agent == "claude":
        env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_TOKEN
        env["DISABLE_AUTOUPDATER"] = "1"
    return env


def as_agent() -> dict[str, Any]:
    """Popen kwargs that run the child as the unprivileged agent user."""
    return {"user": AGENT_USER, "group": AGENT_USER, "extra_groups": []}


def safe_arg(value: str, what: str) -> str:
    """Values that go into argv as their own arguments (model, effort, session ids)."""
    if not SAFE_ARG_RE.match(value):
        raise JobFailed(f"invalid {what} '{value}'")
    return value


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
