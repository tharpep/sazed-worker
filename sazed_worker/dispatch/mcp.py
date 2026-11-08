"""MCP servers for dispatch agents: the Sazed registry and the grants a profile earns."""

import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TIMEOUT_S = 3
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_active: tuple[dict[str, dict[str, Any]], str] = ({}, "none")


@dataclass
class Grant:
    args: list[str] = field(default_factory=list)
    note: str = ""


def cache_path() -> Path:
    return Path(os.environ.get("DISPATCH_HOME", Path.home() / ".dispatch")) / "mcp-servers.json"


def _fetch(base: str, key: str | None) -> list[dict[str, Any]]:
    headers = {"X-API-Key": key} if key else {}
    req = urllib.request.Request(f"{base.rstrip('/')}/agents/mcp-servers", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        rows = json.load(resp)
    if not isinstance(rows, list):
        raise ValueError("mcp servers are not a list")
    return rows


def _table(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in rows}


def load() -> tuple[dict[str, dict[str, Any]], str]:
    """Fetch the registry once, remember it for build, and return it with its source."""
    global _active
    base = os.environ.get("DISPATCH_SAZED_URL")
    path = cache_path()
    found: tuple[dict[str, dict[str, Any]], str] = ({}, "none")
    if base:
        try:
            rows = _fetch(base, os.environ.get("DISPATCH_SAZED_KEY"))
            found = (_table(rows), "sazed")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows))
        except Exception as e:
            print(
                f"dispatch: mcp servers from {base} failed, using the cache: {e}", file=sys.stderr
            )
    if found[1] == "none" and path.exists():
        try:
            found = (_table(json.loads(path.read_text())), "cache")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    _active = found
    return found


def current() -> tuple[dict[str, dict[str, Any]], str]:
    return _active


def _server(spec: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """The Claude-format entry for one server; KeyError names an unset variable."""
    if spec.get("transport") == "stdio":
        return {"command": spec["command"], "args": list(spec.get("args") or [])}
    headers = {
        k: _VAR.sub(lambda m: env[m.group(1)], str(v))
        for k, v in (spec.get("headers") or {}).items()
    }
    out: dict[str, Any] = {"type": "http", "url": spec["url"]}
    if headers:
        out["headers"] = headers
    return out


def build(grants: dict[str, list[str]], run_dir: Path, env: dict[str, str] | None = None) -> Grant:
    """Write run_dir/mcp.json for the granted servers and return the flags to pass."""
    env = os.environ if env is None else env
    registry, _ = current()
    servers: dict[str, Any] = {}
    allowed: list[str] = []
    skipped: list[str] = []
    for name, tools in grants.items():
        spec = registry.get(name)
        if spec is None:
            skipped.append(f"{name} skipped: not in the registry")
            continue
        try:
            servers[name] = _server(spec, env)
        except KeyError as e:
            skipped.append(f"{name} skipped: {e.args[0]} unset")
            continue
        allowed += [f"mcp__{name}"] if "*" in tools else [f"mcp__{name}__{t}" for t in tools]
    note = ""
    if servers or skipped:
        note = " · mcp: " + (", ".join(servers) or "none")
        if skipped:
            note += " (" + "; ".join(skipped) + ")"
    if not servers:
        return Grant([], note)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"mcpServers": servers}, indent=2))
    path.chmod(0o600)
    return Grant(["--mcp-config", str(path), "--allowedTools", ",".join(allowed)], note)
