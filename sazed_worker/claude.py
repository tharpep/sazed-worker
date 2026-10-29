"""Claude Code (`claude -p`): profiles, its command line, and how its stream-json is read."""

from typing import TYPE_CHECKING, Any

from .agent_process import safe_arg
from .config import CLAUDE, TOOL_RESULT_PREVIEW
from .shell import ALLOWED as SHELL_ALLOWED
from .shell import Shell

if TYPE_CHECKING:
    from .run import Run

# Which built-in tools exist per profile (--tools), which are pre-approved (--allowedTools),
# and which reads are refused outright. With --permission-mode dontAsk anything not pre-approved
# is refused. No Bash: even "read-only" git commands can write files (git diff --output=).
# The one exception is a change job's sandboxed shell (shell.py), for test and check commands.
SECRET_READS = ["Read(~/.claude/**)", "Read(~/.claude.json)", "Read(~/.gemini/**)",
                "Read(//proc/**)", "Read(//etc/sazed-eco/**)"]
PROFILES: dict[str, tuple[str, list[str]]] = {
    "review": ("Read,Grep,Glob", ["Read", "Grep", "Glob"]),
    # Edits only inside the job's working copy (cwd); no shell, so nothing runs and nothing
    # is pushed. The worker turns the edits into a file list that the gateway commits as a PR.
    "change": ("Read,Grep,Glob,Edit,Write",
               ["Read", "Grep", "Glob", "Edit(./**)", "Write(./**)"]),
}


def command(
    job: dict[str, Any], prompt: str, resume: str | None, shell: Shell | None = None
) -> list[str]:
    """The claude -p command line. With a shell (shell.py), Bash is added for the allowlisted
    commands, and its sandbox settings go in through --settings."""
    tools, allowed = PROFILES[job["profile"]]
    if shell:
        tools, allowed = f"{tools},Bash", [*allowed, *SHELL_ALLOWED]
    cmd = [
        CLAUDE,
        "--print",
        "--output-format=stream-json",
        "--verbose",  # required for stream-json in print mode
        f"--model={safe_arg(job['model'], 'model')}",
        "--permission-mode=dontAsk",
        # Only the agent user's own settings: a repo's .claude/settings.json (hooks!) is ignored.
        "--setting-sources=user",
        "--strict-mcp-config",
        f"--tools={tools}",
        "--allowedTools",
        *allowed,
        "--disallowedTools",
        *SECRET_READS,
    ]
    if shell:
        cmd += ["--settings", shell.settings]
    if resume:
        cmd.append(f"--resume={safe_arg(resume, 'session id')}")
    # The prompt goes last, after "--", so it can never be read as an option.
    return [*cmd, "--", prompt]


def _rate_limit_reading(event: dict[str, Any]) -> dict[str, Any] | None:
    """Pryce's plan usage from a rate_limit_event: status, overage, and each window's
    utilization (0-1) and reset time (epoch seconds)."""
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    reading: dict[str, Any] = {
        "status": info.get("status"),
        "overage": info.get("isUsingOverage"),
        "overage_status": info.get("overageStatus"),
    }
    windows = info.get("unifiedWindows")
    for name, window in (windows if isinstance(windows, dict) else {}).items():
        if isinstance(window, dict):
            reading[name] = {"utilization": window.get("utilization"),
                             "resets_at": window.get("resetsAt")}
    return {k: v for k, v in reading.items() if v is not None}


def handle_event(run: "Run", event: dict[str, Any]) -> None:
    if isinstance(event.get("session_id"), str):
        run.session_id = event["session_id"]
    if event.get("type") == "rate_limit_event" and (reading := _rate_limit_reading(event)):
        # First and last reading per job; the governor (plans/10 section 5) uses the change.
        run.rate_limits.setdefault("first", reading)
        run.rate_limits["last"] = reading
    if event.get("type") == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                run.count_tool(str(block.get("name") or "?"))
    if event.get("type") == "result":
        run.result = {
            "response": str(event.get("result") or "").strip(),
            "ok": not event.get("is_error") and event.get("subtype") == "success",
            "error": event.get("result") if event.get("is_error") else None,
            "usage": event.get("usage"),
            "duration_seconds": (event.get("duration_ms") or 0) / 1000,
            "denied": event.get("permission_denials") or None,
            "turns": event.get("num_turns"),
        }


def trim_event(event: dict[str, Any]) -> dict[str, Any]:
    """What's saved of an event: tool results are cut to a preview and the duplicate
    `tool_use_result` (the same file contents again) is dropped. Called after handle_event,
    which always sees the full event."""
    if event.get("type") != "user":
        return event
    event = {k: v for k, v in event.items() if k != "tool_use_result"}
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return event
    blocks = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            text = block.get("content")
            if isinstance(text, list):
                text = "\n".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
            text = str(text or "")
            if len(text) > TOOL_RESULT_PREVIEW:
                block = block | {"content": text[:TOOL_RESULT_PREVIEW], "truncated": len(text)}
        blocks.append(block)
    return event | {"message": message | {"content": blocks}}
