"""The closed list of codes a failed, blocked, conflict or timed_out row carries in meta.reason."""

REASONS = frozenset(
    {
        "timeout",
        "budget",
        "plan_limit",
        "tool_cap",
        "refused",
        "max_tokens",
        "cancelled",
        "no_result",
        "no_done_state",
        "no_evidence",
        "no_change",
        "wrapper_failed",
        "left_owns",
        "check_failed",
        "regen_failed",
        "conflict",
        "dependency_failed",
        "dependency_pending",
        "overlap",
    }
)
NEEDS_REASON = frozenset({"failed", "blocked", "conflict", "timed_out"})
