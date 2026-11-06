"""One summary of a change job's checks and tests (My-AI plans/14, the shared check result).

Assembled from what the checks and the test run already record, so the board, bake-offs and
learning read one `passed` instead of five scattered fields.
"""

from typing import Any


def check_result(lint_meta: dict[str, Any]) -> dict[str, Any] | None:
    """None when the checks didn't run (no change, or the agent's run didn't succeed)."""
    if "lint_found" not in lint_meta:
        return None
    tests = lint_meta.get("tests") or {}
    # lint_left exists only after a fix round; without one, everything found is still there.
    left = lint_meta.get("lint_left")
    lint_left = len(left) if left is not None else lint_meta["lint_found"]
    new_failures = tests.get("left") or []
    return {
        "passed": lint_left == 0 and not new_failures,
        "lint": {"found": lint_meta["lint_found"], "left": lint_left},
        "checks": lint_meta.get("checks") or {},
        "skipped_checks": lint_meta.get("checks_skipped") or [],
        "tests": {
            "runner": tests.get("runner"),
            "runs": tests.get("runs"),
            "failing_before": tests.get("failing_before"),
            "new_failures": new_failures,
            "skipped": tests.get("skipped"),
        },
    }
