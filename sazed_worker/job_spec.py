"""The RunSpec for one agent process of a worker job: the job dict read and validated once."""

from pathlib import Path
from typing import Any

from . import config
from .adapters import agy, claude
from .adapters.contract import RunSpec
from .agent_process import safe_arg
from .config import DEADLINE_GRACE_S


def job_limits(job: dict[str, Any]) -> tuple[int, int]:
    """The job's timeout and tool cap; a NULL either way raises, before any work starts."""
    params = job.get("params") or {}
    return int(params.get("timeout_s", 300)), int(params.get("max_tool_calls", 40))


def job_spec(job: dict[str, Any], prompt: str, resume: str | None, cwd: Path) -> RunSpec:
    """Minimal environment, the agent user, the job's timeout plus grace and its tool cap."""
    params = job.get("params") or {}
    model = safe_arg(job["model"], "model")
    timeout_s, max_tool_calls = job_limits(job)
    common: dict[str, Any] = {
        "prompt": prompt,
        "cwd": cwd,
        "timeout_s": timeout_s,
        "grace_s": DEADLINE_GRACE_S,
        "max_tool_calls": max_tool_calls,
        "env": "minimal",
        "as_agent": True,
        "argv_layout": "job",
    }
    if job["agent"] == "agy":
        effort = params.get("effort")
        effort = safe_arg(str(effort), "effort") if effort else None
        return RunSpec(
            engine="agy",
            binary=(config.AGY,),
            model=model,
            permission_mode="default",
            effort=effort,
            print_timeout_s=timeout_s,
            resume=safe_arg(resume, "session id") if resume else None,
            env_extra=agy.extra_env(),
            **common,
        )
    tools, allowed = claude.PROFILES[job["profile"]]
    return RunSpec(
        engine="claude",
        binary=(config.CLAUDE,),
        model=model,
        permission_mode="dontAsk",
        setting_sources="user",
        tools=tools,
        allowed_tools=tuple(allowed),
        denied_tools=tuple(claude.SECRET_READS),
        resume=safe_arg(resume, "session id") if resume else None,
        env_extra=claude.extra_env(),
        **common,
    )
