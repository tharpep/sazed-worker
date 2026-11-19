"""The dispatch profiles as Sazed serves them with X-Sazed-Shape 2, for tests that resolve tasks."""

import json
from pathlib import Path

from sazed_worker.dispatch import profiles


def _row(name, capability, model, effort, budget, timeout, evidence=False, engine="claude"):
    return {
        "name": name,
        "engine": engine,
        "capability": capability,
        "model": model,
        "effort": effort,
        "budget_usd": budget,
        "timeout_s": timeout,
        "max_tool_calls": None,
        "evidence_required": evidence,
        "mcp": {},
        "read_only": capability != "change",
        "description": "",
        "updated_by": "t",
        "updated_at": "now",
        "version": 1,
    }


ROWS = [
    _row("smoke", "review", "haiku", "medium", 0.5, 600),
    _row("sweep", "review", "haiku", "max", 0.5, 600),
    _row("verify", "review", "haiku", "max", 0.5, 600, evidence=True),
    _row("implement", "change", "sonnet", "high", 2.0, 1200),
    _row("design", "change", "opus", "xhigh", 4.0, 1200),
    _row("flash", "change", "gemini-3.8-flash-high", "high", 0.5, 1200, engine="agy"),
]


def install() -> None:
    profiles._active = (profiles._table(ROWS), "sazed")


def write_cache(home: Path, rows: list[dict] = ROWS) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "profiles.json").write_text(json.dumps(rows))
