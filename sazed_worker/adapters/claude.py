"""Claude Code (`claude -p`): profiles, its command line, and how its stream-json is read."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import config
from ..config import CLAUDE_TOKEN, TOOL_RESULT_PREVIEW
from .contract import HarnessStop, PermissionDenied, RunSpec, ToolCall, tool_kind

if TYPE_CHECKING:
    from ..run import Run

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

NAME = "claude"
CAPABILITIES = frozenset(PROFILES)
RESUMES = True
TOOL_POLICY = "allowlist"  # --tools and --allowedTools per profile; dontAsk refuses the rest
# --setting-sources=user: Claude Code loads none of the checkout's CLAUDE.md or AGENTS.md here.
READS_CONTEXT: tuple[str, ...] = ()
PERMISSION_MODES = frozenset({"dontAsk", "acceptEdits"})
STRUCTURED_ANSWER = "StructuredOutput"  # pinned by test_the_structured_answer_is_not_capped


def available() -> bool:
    return Path(config.CLAUDE).exists() and bool(CLAUDE_TOKEN)


def extra_env() -> dict[str, str]:
    """Only Claude's process gets the OAuth token; git and the jailed checks never do."""
    return {"CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN, "DISABLE_AUTOUPDATER": "1"}


def command(spec: RunSpec) -> list[str]:
    if spec.permission_mode not in PERMISSION_MODES:
        raise ValueError(f"claude can't set permission mode {spec.permission_mode!r}")
    if not spec.setting_sources:
        raise ValueError("a claude run needs its setting sources named")
    job = spec.argv_layout == "job"

    def opt(flag: str, value: str) -> list[str]:
        return [f"{flag}={value}"] if job else [flag, value]

    def many(flag: str, values: tuple[str, ...]) -> list[str]:
        return [flag, *values] if job else [flag, ",".join(values)]

    mode = opt("--permission-mode", spec.permission_mode)
    cmd = [
        *spec.binary,
        "--print" if job else "-p",
        *opt("--output-format", "stream-json"),
        "--verbose",  # required for stream-json in print mode
        *opt("--model", spec.model),
    ]
    if spec.effort:
        cmd += opt("--effort", spec.effort)
    if spec.settings:
        cmd += opt("--settings", str(spec.settings))
    if job:
        cmd += mode
    # pinned by test_claude_always_isolates_settings_and_mcp
    cmd += [*opt("--setting-sources", spec.setting_sources), "--strict-mcp-config"]
    if not job:
        cmd += mode
    if spec.tools is not None:
        cmd += opt("--tools", spec.tools)
    if spec.budget_usd is not None:
        cmd += opt("--max-budget-usd", str(spec.budget_usd))
    if spec.output_schema is not None:
        cmd += opt("--json-schema", json.dumps(spec.output_schema))
    if job and spec.allowed_tools:
        cmd += many("--allowedTools", spec.allowed_tools)
    if spec.denied_tools:
        cmd += many("--disallowedTools", spec.denied_tools)
    if spec.mcp_config:
        cmd += opt("--mcp-config", str(spec.mcp_config))
    if not job and spec.allowed_tools:
        cmd += many("--allowedTools", spec.allowed_tools)
    if spec.resume:
        cmd += opt("--resume", spec.resume)
    # The prompt goes last, after "--", so it can never be read as an option.
    return [*cmd, "--", spec.prompt]


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
    run.stop("rate_limit")


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
                name = str(block.get("name") or "?")
                if name == STRUCTURED_ANSWER:
                    continue
                call_id = block.get("id") if isinstance(block.get("id"), str) else None
                run.emit(ToolCall(call_id, name, tool_kind(name), "pending"))
    if event.get("type") == "result" and event.get("api_error") == "usage_limit_reached":
        last = run.rate_limits.get("last") or {}
        window = max(
            ((w.get("utilization") or 0, name, w.get("resets_at"))
             for name, w in last.items() if isinstance(w, dict)),
            default=(0, None, None),
        )
        _limited(run, window[1], window[2])
    if event.get("type") == "result":
        ok = not event.get("is_error") and event.get("subtype") == "success"
        denied = event.get("permission_denials") or None
        for item in denied or []:
            item = item if isinstance(item, dict) else {"tool_name": str(item)}
            run.emit(PermissionDenied(str(item.get("tool_name") or "?"), item.get("tool_use_id")))
        run.record_result({
            "response": str(event.get("result") or "").strip(),
            "ok": ok,
            "error": event.get("result") if event.get("is_error") else None,
            "usage": event.get("usage"),
            "duration_seconds": (event.get("duration_ms") or 0) / 1000,
            "denied": denied,
            "turns": event.get("num_turns"),
            "session_id": event.get("session_id"),
            "cost_usd": event.get("total_cost_usd"),
            "structured_output": event.get("structured_output"),
        }, ending=_ending(event, ok))


def _ending(event: dict[str, Any], ok: bool) -> HarnessStop | None:
    """How the harness itself ended a run that didn't succeed, if it said."""
    if event.get("subtype") == "error_max_budget_usd":
        return "budget"
    if not ok and event.get("stop_reason") == "refusal":
        return "refusal"
    if not ok and event.get("stop_reason") == "max_tokens":
        return "max_tokens"
    return None


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
