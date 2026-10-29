"""Claude Code (`claude -p`): profiles, its command line, and how its stream-json is read."""

from typing import TYPE_CHECKING, Any

from .agent_process import safe_arg
from .config import CLAUDE, TOOL_RESULT_PREVIEW

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


# A reading this full before a silent exit is taken as the limit (plans/10 P1.2): long sessions
# can exit 1 with no rejected event and no result (anthropics/claude-code#103820).
_NEAR_LIMIT = 0.95


def _limited(run: "Run", window: str | None, resets_at: Any) -> None:
    """Pryce's plan refused the run: stop it as rate_limit (Sazed pauses it until the reset)."""
    if run.rate_limited is None:
        run.rate_limited = {"window": window, "resets_at": resets_at}
    run.stop_reason = run.stop_reason or "rate_limit"


def after_exit(run: "Run", code: int) -> None:
    """The fallback for a session that died at the limit without saying so."""
    if run.stop_reason or run.result is not None or code == 0:
        return
    last = run.rate_limits.get("last") or {}
    windows = [(w.get("utilization") or 0, name, w.get("resets_at"))
               for name, w in last.items() if isinstance(w, dict)]
    full = max(windows, default=None)
    if full and full[0] >= _NEAR_LIMIT:
        _limited(run, full[1], full[2])
        run.rate_limited["inferred"] = True


def handle_event(run: "Run", event: dict[str, Any]) -> None:
    if isinstance(event.get("session_id"), str):
        run.session_id = event["session_id"]
    if event.get("type") == "rate_limit_event" and (reading := _rate_limit_reading(event)):
        # First and last reading per job; the governor (plans/10 section 5) uses the change.
        run.rate_limits.setdefault("first", reading)
        run.rate_limits["last"] = reading
        info = event.get("rate_limit_info") or {}
        # rejected while extra usage covers it isn't a stop (Pryce has extra usage off today).
        if info.get("status") == "rejected" and not info.get("isUsingOverage"):
            _limited(run, info.get("rateLimitType"), info.get("resetsAt"))
    if event.get("type") == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                run.count_tool(str(block.get("name") or "?"))
    if event.get("type") == "result" and event.get("api_error") == "usage_limit_reached":
        last = run.rate_limits.get("last") or {}
        window = max(
            ((w.get("utilization") or 0, name, w.get("resets_at"))
             for name, w in last.items() if isinstance(w, dict)),
            default=(0, None, None),
        )
        _limited(run, window[1], window[2])
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
