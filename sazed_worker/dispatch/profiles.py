"""Agent profiles: Sazed first, then the local cache, then the built-in table."""

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT_S = 3
KEYS = ("model", "effort", "budget_usd", "timeout_s", "read_only", "evidence_required", "mcp")

_CHEAP = {"model": "haiku", "budget_usd": 0.5, "timeout_s": 600, "read_only": True, "mcp": {}}

BUILTIN: dict[str, dict[str, Any]] = {
    "smoke": {**_CHEAP, "effort": "medium"},
    "sweep": {**_CHEAP, "effort": "max"},
    "verify": {**_CHEAP, "effort": "max", "evidence_required": True},
    "implement": {
        "model": "sonnet",
        "effort": "high",
        "budget_usd": 2.0,
        "timeout_s": 1200,
        "mcp": {},
    },
    "design": {"model": "opus", "effort": "xhigh", "budget_usd": 4.0, "timeout_s": 1200, "mcp": {}},
}

_active: tuple[dict[str, dict[str, Any]], str] = (BUILTIN, "builtin")


def cache_path() -> Path:
    return Path(os.environ.get("DISPATCH_HOME", Path.home() / ".dispatch")) / "profiles.json"


def _fetch(base: str, key: str | None) -> list[dict[str, Any]]:
    headers = {"X-API-Key": key} if key else {}
    req = urllib.request.Request(f"{base.rstrip('/')}/agents/profiles", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        rows = json.load(resp)
    if not isinstance(rows, list):
        raise ValueError("profiles are not a list")
    return rows


def _table(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["name"]: {k: r[k] for k in KEYS if k in r} for r in rows}


def load() -> tuple[dict[str, dict[str, Any]], str]:
    """Fetch once, remember the result for resolve, and return it with its source."""
    global _active
    base = os.environ.get("DISPATCH_SAZED_URL")
    path = cache_path()
    found: tuple[dict[str, dict[str, Any]], str] = (BUILTIN, "builtin")
    if base:
        try:
            rows = _fetch(base, os.environ.get("DISPATCH_SAZED_KEY"))
            found = (BUILTIN | _table(rows), "sazed")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows))
        except Exception as e:
            print(f"dispatch: profiles from {base} failed, using the cache: {e}", file=sys.stderr)
    if found[1] == "builtin" and path.exists():
        try:
            found = (BUILTIN | _table(json.loads(path.read_text())), "cache")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    _active = found
    return found


def current() -> tuple[dict[str, dict[str, Any]], str]:
    return _active
