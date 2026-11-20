"""What every adapter takes and gives: the run spec, normalized events, stop reasons, the result."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, get_args

RunnerStop = Literal["cancelled", "timed_out", "tool_cap", "shutdown", "rate_limit"]
HarnessStop = Literal["budget", "refusal", "max_tokens"]
StopReason = RunnerStop | HarnessStop
STOP_REASONS: frozenset[str] = frozenset(get_args(RunnerStop) + get_args(HarnessStop))


@dataclass(frozen=True)
class RunSpec:
    """One agent process and everything its launch depends on; each caller builds its own."""

    engine: str
    binary: tuple[str, ...]
    prompt: str
    model: str
    cwd: Path
    permission_mode: str  # pinned by test_permission_mode_has_no_default
    timeout_s: int
    env: Literal["minimal", "inherited"]
    env_extra: Mapping[str, str]
    as_agent: bool
    argv_layout: Literal["job", "dispatch"]  # pinned by test_*_launch_pins
    grace_s: int = 0
    effort: str | None = None
    tools: str | None = None
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    mcp_config: Path | None = None
    settings: Path | None = None
    setting_sources: str | None = None
    budget_usd: float | None = None
    max_tool_calls: int | None = None
    output_schema: dict[str, Any] | None = None
    resume: str | None = None
    stderr_path: Path | None = None
    print_timeout_s: int | None = None
    slash_commands: bool = True
    harness_stops: bool = False


@dataclass(frozen=True)
class ToolCall:
    id: str | None
    name: str
    kind: str
    status: Literal["pending", "completed", "failed"]


@dataclass(frozen=True)
class PermissionDenied:
    tool: str
    id: str | None


Event = ToolCall | PermissionDenied


class RunResult(TypedDict):
    """One agent process's normalized result; cost_usd is that process's own, not a session's."""

    response: str
    ok: bool
    error: Any
    usage: dict[str, Any] | None
    turns: int | None
    duration_seconds: float | None
    session_id: str | None
    cost_usd: float | None
    structured_output: dict[str, Any] | None
    denied: list[Any] | None


_KINDS = {
    "read": ("Read", "NotebookRead", "view_file", "read_file"),
    "edit": ("Edit", "Write", "MultiEdit", "NotebookEdit", "write_file", "edit_file",
             "write_to_file", "replace_file_content", "multi_replace_file_content"),
    "search": ("Grep", "Glob", "grep_search", "find_by_name", "list_dir"),
    "execute": ("Bash", "run_command"),
    "fetch": ("WebFetch", "WebSearch", "read_url"),
}
_KIND_OF = {name: kind for kind, names in _KINDS.items() for name in names}


def tool_kind(name: str) -> str:
    """ACP's tool kind for a tool name, so a later ACP adapter reports the same kinds."""
    return _KIND_OF.get(name, "other")
