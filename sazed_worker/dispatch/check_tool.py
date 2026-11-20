"""run_check: the stdio MCP server that runs the approved plan's check for an unattended agent."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sazed_worker.jail import run_jailed  # noqa: E402

TOOL = "run_check"
PROTOCOL = "2025-06-18"
TAIL = 4000
SHELL = "/bin/sh"
DESCRIPTION = (
    "Run this task's check (the command the approved plan names) on your worktree and return "
    "pass or fail with the end of its output. It takes no arguments."
)


def run(spec: dict[str, Any]) -> tuple[bool, str]:
    """(passed, text for the agent) of one run of the check in the untrusted jail."""
    timeout = int(spec["timeout_s"])
    code, out, err = run_jailed(
        [SHELL, "-c", spec["check"]],
        Path(spec["cwd"]),
        Path(spec["root"]),
        timeout,
        dict(spec["env"]),
        [Path(p) for p in spec["writable"]],
        [Path(p) for p in spec["read_only"]],
    )
    if code is None:
        why = f"it was stopped after {timeout} s" if err.endswith("timed out") else err.strip()
        return False, f"check failed: {why}"
    verdict = "passed" if code == 0 else "failed"
    return code == 0, f"check {verdict} (exit {code})\n{(out + err)[-TAIL:]}"


def _result(text: str, failed: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def handle(message: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    """The reply to one JSON-RPC message, or None for a notification."""
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None
    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
    if method == "initialize":
        reply["result"] = {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "dispatch-check", "version": "1"},
        }
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        reply["result"] = {
            "tools": [{"name": TOOL, "description": DESCRIPTION, "inputSchema": schema}]
        }
    elif method == "tools/call":
        if params.get("name") != TOOL:
            reply["result"] = _result(f"there is no tool {params.get('name')!r}", True)
        elif params.get("arguments"):
            reply["result"] = _result(f"{TOOL} takes no arguments", True)
        else:
            try:
                passed, text = run(spec)
            except OSError as e:
                passed, text = False, f"check failed: it could not start: {e}"
            reply["result"] = _result(text, not passed)
    else:
        reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
    return reply


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        sys.exit("run_check runs as the agent user, never as root")
    spec = json.loads(Path(args.spec).read_text())
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        else:
            reply = handle(message, spec) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
