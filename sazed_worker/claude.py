"""Claude Code (`claude -p`): profiles, its command line, and how its stream-json is read."""

from typing import TYPE_CHECKING, Any

from .agent_process import safe_arg
from .config import CLAUDE

if TYPE_CHECKING:
    from .run import Run

# Which built-in tools exist per profile (--tools), which are pre-approved (--allowedTools),
# and which reads are refused outright. With --permission-mode dontAsk anything not pre-approved
# is refused. No Bash: even "read-only" git commands can write files (git diff --output=).
SECRET_READS = ["Read(~/.claude/**)", "Read(~/.claude.json)", "Read(~/.gemini/**)",
                "Read(//proc/**)", "Read(//etc/sazed-eco/**)"]
PROFILES: dict[str, tuple[str, list[str]]] = {
    "review": ("Read,Grep,Glob", ["Read", "Grep", "Glob"]),
    # Edits only inside the job's working copy (cwd); no shell, so nothing runs and nothing
    # is pushed. The worker turns the edits into a file list that the gateway commits as a PR.
    "change": ("Read,Grep,Glob,Edit,Write",
               ["Read", "Grep", "Glob", "Edit(./**)", "Write(./**)"]),
}


def command(job: dict[str, Any], prompt: str, resume: str | None) -> list[str]:
    tools, allowed = PROFILES[job["profile"]]
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
    if resume:
        cmd.append(f"--resume={safe_arg(resume, 'session id')}")
    # The prompt goes last, after "--", so it can never be read as an option.
    return [*cmd, "--", prompt]


def handle_event(run: "Run", event: dict[str, Any]) -> None:
    if isinstance(event.get("session_id"), str):
        run.session_id = event["session_id"]
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
