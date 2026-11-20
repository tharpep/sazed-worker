"""Stands in for `claude -p` in tests: a commit or no-shell writes, a schema-shaped final event."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


def _target(owns: list[str], task: str) -> Path:
    first = owns[0] if owns else "."
    literal = [p for p in first.split("/") if not set(p) & set("*?[")]
    return Path(*literal) / f"{task}.txt" if literal else Path(f"{task}.txt")


def _commit(path: Path, text: str, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    subprocess.run(["git", "add", "--", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], check=True, capture_output=True)


def _emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def _write(path: Path, text: str, n: int) -> None:
    """The Write tool, as claude reports it; the no-shell modes never run git."""
    block = {"type": "tool_use", "id": f"toolu_w{n}", "name": "Write",
             "input": {"file_path": str(path), "content": text}}
    _emit({"type": "assistant", "message": {"content": [block]}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class CheckServer:
    """The run_check server named in --mcp-config, spoken to over stdio as claude does."""

    def __init__(self) -> None:
        config = json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text())
        server = config["mcpServers"]["check"]
        self.proc = subprocess.Popen(
            [server["command"], *server.get("args", [])],
            env=os.environ | server.get("env", {}),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "fake-agent", "version": "1"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                          "method": "notifications/initialized"}) + "\n")
        tools = self.request("tools/list", {})["tools"]
        if [t["name"] for t in tools] != ["run_check"]:
            raise SystemExit(f"unexpected tools {tools}")

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())["result"]

    def run_check(self) -> bool:
        """One run_check call and its result, as claude's stream-json shows them."""
        call_id = f"toolu_c{self.next_id}"
        block = {"type": "tool_use", "id": call_id, "name": "mcp__check__run_check", "input": {}}
        _emit({"type": "assistant", "message": {"content": [block]}})
        result = self.request("tools/call", {"name": "run_check", "arguments": {}})
        reply = {"type": "tool_result", "tool_use_id": call_id, "content": result["content"],
                 "is_error": result["isError"]}
        _emit({"type": "user", "message": {"role": "user", "content": [reply]}})
        return not result["isError"]

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def _no_shell(mode: str, owns: list[str], task: str) -> None:
    """The unattended agent: writes files, tests them with run_check, never commits."""
    server = CheckServer()
    target = _target(owns, task)
    if mode == "noshell":
        _write(target, f"{task}\n", 1)
        _write(Path("README.md"), f"{task} strayed\n", 2)
        server.run_check()
    elif mode == "noshell-fix":
        _write(target, "FAIL\n", 1)
        if not server.run_check():
            _write(target, f"{task} fixed\n", 2)
        server.run_check()
    elif mode == "noshell-nochange":
        _write(Path("stray.txt"), "outside owns\n", 1)
        server.run_check()
    server.close()


def main() -> None:
    task = os.environ["DISPATCH_TASK"]
    owns = json.loads(os.environ.get("DISPATCH_OWNS", "[]"))
    mode = json.loads(os.environ.get("DISPATCH_FAKE_MODES", "{}")).get(task, "inside")
    is_agy = (
        os.environ.get("DISPATCH_ENGINE") == "agy" or "--dangerously-skip-permissions" in sys.argv
    )
    session = str(uuid.uuid4())
    if is_agy:
        print(
            json.dumps({"event": "init", "init": {"model": "gemini-3.8-flash-high"}}), flush=True
        )
    else:
        print(
            json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True
        )
    state, extra, ending = "done", {}, {}
    if mode == "inside":
        _commit(_target(owns, task), f"{task}\n", f"{task}: work")
    elif mode == "outside":
        _commit(Path("README.md"), f"{task} strayed\n", f"{task}: stray edit")
    elif mode == "failcheck":
        _commit(_target(owns, task), "FAIL\n", f"{task}: breaks the check")
    elif mode.startswith("noshell"):
        _no_shell(mode, owns, task)
    elif mode == "nocommit":
        pass
    elif mode == "waiting":
        state, extra = "waiting", {"question": "Which file holds the shared type?"}
    elif mode == "reads":
        if is_agy:
            print(
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 1,
                            "state": "ACTIVE",
                            "step_type": "tool",
                            "tool_name": "view_file",
                        },
                    }
                ),
                flush=True,
            )
        else:
            block = {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}
            print(json.dumps({"type": "assistant", "message": {"content": [block]}}), flush=True)
    elif mode == "tools":
        for i in range(5):
            block = {"type": "tool_use", "id": f"toolu_{i}", "name": "Grep", "input": {"p": "x"}}
            print(json.dumps({"type": "assistant", "message": {"content": [block]}}), flush=True)
    elif mode == "multi":
        blocks = [
            {"type": "tool_use", "id": f"toolu_{i}", "name": "Read", "input": {"p": "x"}}
            for i in range(3)
        ]
        print(json.dumps({"type": "assistant", "message": {"content": blocks}}), flush=True)
    elif mode == "subagent":
        task_use = {"type": "tool_use", "id": "toolu_t", "name": "Task", "input": {"p": "x"}}
        print(json.dumps({"type": "assistant", "message": {"content": [task_use]}}), flush=True)
        inner = {"type": "tool_use", "id": "toolu_i", "name": "mcp__c7__query", "input": {}}
        event = {"type": "assistant", "parent_tool_use_id": "toolu_t",
                 "message": {"content": [inner]}}
        print(json.dumps(event), flush=True)
    elif mode == "ratelimit":
        info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1794103800}
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": info}), flush=True)
        time.sleep(5)
    elif mode in ("refusal", "max_tokens"):
        ending = {"subtype": "error_during_execution", "is_error": True, "stop_reason": mode}
    elif mode == "hang":
        time.sleep(600)
    if is_agy:
        print(
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "conversation_id": session,
                        "num_turns": 1,
                        "structured_output": {"state": state, "summary": f"{task} {mode}"}
                        | extra,
                    },
                }
            ),
            flush=True,
        )
    else:
        answer = {
            "type": "tool_use",
            "id": "toolu_answer",
            "name": "StructuredOutput",
            "input": {"state": state, "summary": f"{task} {mode}"} | extra,
        }
        print(json.dumps({"type": "assistant", "message": {"content": [answer]}}), flush=True)
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": session,
                    "result": f"{task} {mode}",
                    "total_cost_usd": 0.0,
                    "num_turns": 1,
                    "structured_output": {"state": state, "summary": f"{task} {mode}"} | extra,
                }
                | ending
            ),
            flush=True,
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
