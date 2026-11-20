"""Antigravity (`agy`): its command line and how its stream-json events are read."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import config
from .contract import PermissionDenied, RunSpec, ToolCall, tool_kind

if TYPE_CHECKING:
    from ..run import Run

NAME = "agy"
CAPABILITIES = frozenset({"research", "review"})
RESUMES = True
TOOL_POLICY = "print_mode_denial"  # pinned by test_worker_launch_pins
READS_CONTEXT = ("AGENTS.md", "GEMINI.md")
PERMISSION_MODES = frozenset({"default", "bypassPermissions"})


def available() -> bool:
    return Path(config.AGY).exists()


def extra_env() -> dict[str, str]:
    return {}


def command(spec: RunSpec) -> list[str]:
    if spec.permission_mode not in PERMISSION_MODES:
        raise ValueError(f"agy can't set permission mode {spec.permission_mode!r}")
    job = spec.argv_layout == "job"
    cmd = [*spec.binary]
    if job:
        cmd.append(f"--prompt={spec.prompt}")
    cmd += ["--output-format=stream-json", f"--model={spec.model}"]
    if spec.print_timeout_s is not None:
        cmd.append(f"--print-timeout={spec.print_timeout_s}s")
    if spec.effort:
        cmd.append(f"--effort={spec.effort}")
    if spec.permission_mode == "bypassPermissions":
        cmd.append("--dangerously-skip-permissions")
    if not spec.slash_commands:
        cmd.append("--disable-slash-commands")
    if spec.output_schema is not None:
        cmd += ["--json-schema", json.dumps(spec.output_schema)]
    if spec.resume:
        cmd.append(f"--conversation={spec.resume}")
    if not job:
        cmd.append(f"--prompt={spec.prompt}")
    return cmd


def trim_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """What's saved of an event: the answer's word-by-word chunks are dropped (the result event
    carries the whole answer); everything else is kept."""
    step = event.get("step_update")
    if (isinstance(step, dict) and step.get("step_type") == "agent_response"
            and step.get("state") == "ACTIVE" and "text_delta" in step):
        return None
    return event


def handle_event(run: "Run", event: dict[str, Any]) -> None:
    if isinstance(event.get("conversation_id"), str):
        run.session_id = event["conversation_id"]
    step = event.get("step_update")
    done_tool = isinstance(step, dict) and step.get("step_type") == "tool"
    if done_tool and step.get("state") == "DONE":
        tool_name = str(step.get("tool_name") or "?")
        if tool_name == "call_mcp_tool":
            info = step.get("tool_info")
            params = info.get("parameters") if isinstance(info, dict) else None
            mcp_tool = params.get("ToolName") if isinstance(params, dict) else None
            if mcp_tool:
                tool_name = str(mcp_tool)
        index = step.get("step_index")
        status = "failed" if step.get("error") else "completed"
        call_id = None if index is None else str(index)
        run.emit(ToolCall(call_id, tool_name, tool_kind(tool_name), status))
    if event.get("event") == "result" and isinstance(event.get("result"), dict):
        r = event["result"]
        run.session_id = r.get("conversation_id") or run.session_id
        denied = r.get("denied_actions")
        for item in denied if isinstance(denied, list) else []:
            run.emit(PermissionDenied(str(item), None))
        run.record_result({
            "response": str(r.get("response") or "").strip(),
            "ok": r.get("status") == "SUCCESS",
            "error": r.get("error"),
            "usage": r.get("usage"),
            "duration_seconds": r.get("duration_seconds"),
            "denied": denied,
            "turns": r.get("num_turns"),
            "session_id": r.get("conversation_id"),
            "cost_usd": None,
            "structured_output": r.get("structured_output"),
        })
