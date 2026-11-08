"""The plan-limits probe: a one-turn `claude -p` whose rate_limit_event Sazed records."""

import json
import subprocess
from collections.abc import Iterable
from typing import Any

from .adapters import claude
from .agent_process import agent_env, as_agent
from .config import AGENT_HOME, CLAUDE, log
from .sazed_api import post

CHECK_EVERY_S = 120
PROBE_TIMEOUT_S = 90
PROBE_ARGS = [
    "--print",
    "--output-format=stream-json",
    "--verbose",
    "--model=haiku",
    "--tools",
    "",
    "--strict-mcp-config",
    "--setting-sources=user",
    "--system-prompt=Reply with one word.",
    "--max-turns=1",
    "--",
    "ok",
]


def last_reading(lines: Iterable[str]) -> dict[str, Any] | None:
    info = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "rate_limit_event":
            if isinstance(event.get("rate_limit_info"), dict):
                info = event["rate_limit_info"]
    return info


def probe() -> None:
    """Read the plan windows when Sazed says a reading is due; never raises."""
    if not claude.available():
        return
    status, due = post("/worker/limits/due", {})
    if status != 200 or not isinstance(due, dict) or not due.get("due"):
        return
    try:
        out = subprocess.run(
            [CLAUDE, *PROBE_ARGS],
            cwd=AGENT_HOME,
            env=agent_env(claude.extra_env()),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            **as_agent(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("limits probe failed: %s", e)
        return
    info = last_reading(out.stdout.splitlines())
    if info is None:
        log.warning("limits probe got no reading (exit %s): %s", out.returncode, out.stderr[-200:])
        return
    status, body = post("/worker/limits/reading", {"rate_limit_info": info})
    log.info("limits probe (%s): %s %s", due.get("reason"), status, body)
