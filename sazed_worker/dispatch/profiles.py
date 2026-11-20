"""Agent profiles: Sazed's registry, then the cache of its last answer; unattended, Sazed only."""

import json
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sazed_worker import sazed_api

TIMEOUT_S = 3
SHAPE_V2 = {"X-Sazed-Shape": "2"}
KEYS = (
    "model",
    "effort",
    "budget_usd",
    "timeout_s",
    "max_tool_calls",
    "capability",
    "evidence_required",
    "mcp",
    "engine",
)

_active: tuple[dict[str, dict[str, Any]], str] = ({}, "none")


class ProfilesError(RuntimeError):
    pass


def cache_path() -> Path:
    return Path(os.environ.get("DISPATCH_HOME", Path.home() / ".dispatch")) / "profiles.json"


def _fetch(base: str, key: str | None) -> list[dict[str, Any]]:
    headers = SHAPE_V2 | ({"X-API-Key": key} if key else {})
    req = urllib.request.Request(f"{base.rstrip('/')}/agents/profiles", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return _rows(json.load(resp))


def _rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("profiles are not a non-empty list")
    return rows


def _settings(row: dict[str, Any]) -> dict[str, Any]:
    """One row's run settings; rows from an older Sazed or cache carry read_only and no engine."""
    out = {k: row[k] for k in KEYS if k in row}
    out.setdefault("engine", "claude")
    capability = row.get("capability")
    out["read_only"] = capability != "change" if capability else bool(row["read_only"])
    return out


def _table(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["name"]: _settings(r) for r in rows}


def fetch_unattended(
    path: str,
    what: str,
    table: Callable[[list[Any]], dict[str, dict[str, Any]]],
    headers: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """A registry from Sazed's worker routes, or ProfilesError in one sentence; never a cache."""
    status, body = sazed_api.get(path, headers, TIMEOUT_S)
    lead = f"an unattended run needs {what} fresh from Sazed, and "
    if status == 0:
        raise ProfilesError(f"{lead}Sazed at {sazed_api.SAZED_URL} could not be reached ({body})")
    if status != 200:
        raise ProfilesError(f"{lead}{path} answered {status}")
    if not isinstance(body, list) or not body:
        raise ProfilesError(f"{lead}{path} did not answer a non-empty list")
    try:
        return table(body)
    except (KeyError, TypeError, AttributeError) as e:
        raise ProfilesError(f"{lead}{path} answered a malformed entry ({e!r})") from e


def load(unattended: bool = False) -> tuple[dict[str, dict[str, Any]], str]:
    """Fetch once, remember the result for resolve, and return it with its source."""
    global _active
    if unattended:
        # Never read or write the cache here: a root-owned cache would block the next hand run.
        _active = (
            fetch_unattended("/worker/agents/profiles", "agent profiles", _table, SHAPE_V2),
            "sazed",
        )
        return _active
    base = os.environ.get("DISPATCH_SAZED_URL")
    path = cache_path()
    if base:
        try:
            rows = _fetch(base, os.environ.get("DISPATCH_SAZED_KEY"))
            _active = (_table(rows), "sazed")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows))
            return _active
        except Exception as e:
            print(f"dispatch: profiles from {base} failed, using the cache: {e}", file=sys.stderr)
    if not path.exists():
        where = f"Sazed at {base} did not answer" if base else "DISPATCH_SAZED_URL is not set"
        raise ProfilesError(
            f"no agent profiles: {where} and there is no cache at {path}. "
            "Run once with Sazed reachable to write the cache."
        )
    try:
        _active = (_table(_rows(json.loads(path.read_text()))), "cache")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ProfilesError(f"no agent profiles: the cache at {path} is unreadable: {e}") from e
    return _active


def current() -> tuple[dict[str, dict[str, Any]], str]:
    return _active


def cache_note(source: str) -> str:
    """Where cached profiles came from and how old they are; empty for any other source."""
    if source != "cache":
        return ""
    path = cache_path()
    try:
        hours = (time.time() - path.stat().st_mtime) / 3600
    except OSError:
        return f" (profiles from the cache at {path})"
    return f" (profiles from the cache at {path}, written {hours:.1f} h ago)"
