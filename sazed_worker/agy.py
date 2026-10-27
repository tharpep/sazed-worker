"""Antigravity (`agy`): its command line and how its stream-json events are read."""

from typing import TYPE_CHECKING, Any

from .agent_process import safe_arg
from .config import AGY

if TYPE_CHECKING:
    from .run import Run


def command(job: dict[str, Any], prompt: str, resume: str | None) -> list[str]:
    params = job.get("params") or {}
    cmd = [
        AGY,
        f"--prompt={prompt}",
        "--output-format=stream-json",
        f"--model={safe_arg(job['model'], 'model')}",
        f"--print-timeout={int(params.get('timeout_s', 300))}s",
    ]
    if params.get("effort"):
        cmd.append(f"--effort={safe_arg(str(params['effort']), 'effort')}")
    if resume:
        cmd.append(f"--conversation={safe_arg(resume, 'session id')}")
    return cmd


def handle_event(run: "Run", event: dict[str, Any]) -> None:
    if isinstance(event.get("conversation_id"), str):
        run.session_id = event["conversation_id"]
    step = event.get("step_update")
    done_tool = isinstance(step, dict) and step.get("step_type") == "tool"
    if done_tool and step.get("state") == "DONE":
        run.count_tool(str(step.get("tool_name") or "?"))
    if event.get("event") == "result" and isinstance(event.get("result"), dict):
        r = event["result"]
        run.session_id = r.get("conversation_id") or run.session_id
        run.result = {
            "response": str(r.get("response") or "").strip(),
            "ok": r.get("status") == "SUCCESS",
            "error": r.get("error"),
            "usage": r.get("usage"),
            "duration_seconds": r.get("duration_seconds"),
            "denied": r.get("denied_actions"),
        }
