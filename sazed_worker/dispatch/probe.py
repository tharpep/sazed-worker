"""Run each headless claim dispatch relies on and write CAPABILITIES.md (dispatch.md, Probe)."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

CLAUDE = os.environ.get("DISPATCH_CLAUDE", "claude")
CHEAP = "haiku"
ALLOW_BASH = json.dumps(
    {
        "permissions": {"allow": ["Bash"], "deny": ["Bash(git push:*)"]},
        "env": {"BASH_DEFAULT_TIMEOUT_MS": "300000", "BASH_MAX_TIMEOUT_MS": "300000"},
    }
)

HOOK = """#!/usr/bin/env python3
import json, sys, pathlib
event = json.load(sys.stdin)
log = pathlib.Path(sys.argv[1])
with log.open("a") as f:
    f.write(json.dumps(event.get("tool_input", {})) + "\\n")
cmd = (event.get("tool_input") or {}).get("command", "")
if "git push" in cmd:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "deny", "permissionDecisionReason": "agents do not push"}}))
"""


def sh(*args: str, cwd: Path, check: bool = True) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


def repo(tmp: Path, name: str = "work") -> Path:
    path = tmp / name
    path.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=path)
    sh("git", "config", "user.email", "probe@local", cwd=path)
    sh("git", "config", "user.name", "probe", cwd=path)
    (path / "README.md").write_text("# probe\n")
    sh("git", "add", "README.md", cwd=path)
    sh("git", "commit", "-q", "-m", "seed", cwd=path)
    return path


def run(cwd: Path, prompt: str, *flags: str, model: str = CHEAP, timeout: int = 300) -> dict:
    cmd = [
        CLAUDE,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "project",
        "--model",
        model,
        *flags,
        "--",
        prompt,
    ]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
    )
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    init = next((e for e in events if e.get("subtype") == "init"), {})
    tools = [
        b
        for e in events
        if e.get("type") == "assistant"
        for b in e["message"]["content"]
        if b.get("type") == "tool_use"
    ]
    outputs = [
        str(b.get("content"))[:200]
        for e in events
        if e.get("type") == "user"
        for b in (e.get("message") or {}).get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    usage = result.get("usage") or {}
    return {
        "code": proc.returncode,
        "result": result,
        "init": init,
        "tools": tools,
        "outputs": outputs,
        "output_tokens": usage.get("output_tokens", 0),
        "seconds": round(time.monotonic() - t0, 1),
        "stderr": proc.stderr[-300:],
    }


def write_hook_settings(tmp: Path, log: Path, extra: dict | None = None) -> Path:
    hook = tmp / "hook.py"
    hook.write_text(HOOK)
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"{sys.executable} {hook} {log}"}],
                }
            ]
        }
    }
    settings |= extra or {}
    path = tmp / "agent-settings.json"
    path.write_text(json.dumps(settings))
    return path


def claim_auth(tmp: Path) -> tuple[bool, str]:
    r = run(repo(tmp), "Reply with exactly: ok")
    return r["code"] == 0 and "ok" in str(r["result"].get("result", "")).lower(), (
        f"exit {r['code']}, result {r['result'].get('result')!r}, {r['seconds']} s"
    )


def claim_cost(tmp: Path) -> tuple[bool, str]:
    r = run(repo(tmp), "Reply with exactly: ok")
    cost = r["result"].get("total_cost_usd")
    return isinstance(cost, (int, float)), f"total_cost_usd = {cost!r}"


def claim_models(tmp: Path) -> tuple[bool, str]:
    seen = {}
    for alias in ("haiku", "sonnet", "opus"):
        r = run(repo(tmp, alias), "Reply with exactly: ok", model=alias)
        seen[alias] = (r["code"], (r["init"].get("model") or "?"))
    return all(code == 0 for code, _ in seen.values()), "; ".join(
        f"{a} → exit {c}, {m}" for a, (c, m) in seen.items()
    )


def claim_schema(tmp: Path) -> tuple[bool, str]:
    schema = json.dumps(
        {
            "type": "object",
            "properties": {"state": {"enum": ["done", "waiting"]}},
            "required": ["state"],
        }
    )
    r = run(repo(tmp), "You are done. Report state done.", "--json-schema", schema)
    out = r["result"].get("structured_output")
    return isinstance(out, dict) and out.get("state") == "done", f"structured_output = {out!r}"


def claim_resume(tmp: Path) -> tuple[bool, str]:
    work = repo(tmp)
    first = run(work, "Remember the word heliotrope. Reply with exactly: noted")
    sid = first["result"].get("session_id")
    second = run(work, "What word did I ask you to remember? One word.", "--resume", str(sid))
    said = str(second["result"].get("result", ""))
    return "heliotrope" in said.lower(), f"session {sid}, second answer {said!r}"


def claim_hooks_and_push(tmp: Path) -> tuple[bool, str]:
    work = repo(tmp)
    remote = tmp / "remote.git"
    sh("git", "init", "-q", "--bare", str(remote), cwd=tmp)
    sh("git", "remote", "add", "origin", str(remote), cwd=work)
    log = tmp / "hook.log"
    settings = write_hook_settings(tmp, log)
    run(
        work,
        "Run this exact shell command with the Bash tool: git push origin main",
        "--settings",
        str(settings),
        "--permission-mode",
        "acceptEdits",
    )
    fired = log.exists() and "git push" in log.read_text()
    pushed = sh("git", "rev-parse", "--verify", "-q", "refs/heads/main", cwd=remote, check=False)
    return (
        fired and not pushed,
        f"hook log {'has' if fired else 'lacks'} the push; remote main = {pushed or 'absent'}",
    )


def claim_commit(tmp: Path) -> tuple[bool, str]:
    work = repo(tmp)
    (work / "note.txt").write_text("hi\n")
    r = run(
        work,
        "Run this exact shell command with the Bash tool: "
        "git add note.txt && git commit -m 'probe commit'",
        "--settings",
        ALLOW_BASH,
        "--permission-mode",
        "acceptEdits",
    )
    subject = sh("git", "log", "-1", "--format=%s", cwd=work)
    calls = [t.get("name") for t in r["tools"]]
    return (
        subject == "probe commit",
        f"HEAD subject {subject!r}; tools {calls}; last output {r['outputs'][-1:]}",
    )


def claim_bash_modes(tmp: Path) -> tuple[bool, str]:
    out = []
    for mode in ("acceptEdits", "default"):
        work = repo(tmp, mode)
        run(
            work,
            "Run this exact shell command with the Bash tool: touch ran.txt",
            "--permission-mode",
            mode,
        )
        out.append(f"{mode}: touch {'ran' if (work / 'ran.txt').exists() else 'denied'}")
    ok = out[0].endswith("ran") and out[1].endswith("denied")
    return ok, "; ".join(out)


def claim_settings_deny(tmp: Path) -> tuple[bool, str]:
    work = repo(tmp)
    settings = tmp / "deny.json"
    settings.write_text(json.dumps({"permissions": {"deny": ["Write", "Edit", "Bash"]}}))
    r = run(
        work,
        "Create a file blocked.txt containing hi.",
        "--settings",
        str(settings),
        "--permission-mode",
        "acceptEdits",
    )
    landed = (work / "blocked.txt").exists()
    calls = [t.get("name") for t in r["tools"]]
    return not landed, (
        f"deny Write, Edit, Bash in --settings: file {'landed' if landed else 'refused'}; "
        f"tools {calls}; outputs {r['outputs'][-1:]}"
    )


def claim_read_only(tmp: Path) -> tuple[bool, str]:
    work = repo(tmp)
    r = run(
        work,
        "Create a file nope.txt containing hi.",
        "--disallowedTools",
        "Edit,Write,MultiEdit,NotebookEdit,Bash",
        "--permission-mode",
        "acceptEdits",
    )
    tools = r["init"].get("tools") or []
    gone = not {"Edit", "Write", "Bash"} & set(tools)
    landed = (work / "nope.txt").exists()
    return gone and not landed, f"init tools lack Edit/Write/Bash: {gone}; write landed: {landed}"


def claim_budget(tmp: Path) -> tuple[bool, str]:
    r = run(repo(tmp), "Write a 300 word story.", "--max-budget-usd", "0.0001")
    sub = r["result"].get("subtype")
    return sub == "error_max_budget_usd", f"exit {r['code']}, subtype {sub!r}"


def claim_effort(tmp: Path) -> tuple[bool, str]:
    prompt = "How many primes are there below 200? Think it through, then give the number."
    low = run(repo(tmp, "low"), prompt, "--effort", "low", model="sonnet")
    high = run(repo(tmp, "max"), prompt, "--effort", "max", model="sonnet")
    return high["output_tokens"] > low["output_tokens"] * 1.5, (
        f"output tokens low {low['output_tokens']}, max {high['output_tokens']}"
    )


def claim_bash_timeout(tmp: Path) -> tuple[bool, str]:
    work = repo(tmp)
    env_flags = ("--settings", ALLOW_BASH)
    r = run(
        work,
        "Run this exact shell command with the Bash tool, in the foreground, and "
        "report when it finishes: python3 -c \"import time; time.sleep(185); print('finished')\"",
        *env_flags,
        "--permission-mode",
        "acceptEdits",
        timeout=600,
    )
    backgrounded = any((t.get("input") or {}).get("run_in_background") for t in r["tools"])
    bash_calls = sum(t.get("name") == "Bash" for t in r["tools"])
    finished = any("finished" in o for o in r["outputs"])
    return bash_calls == 1 and not backgrounded and finished and r["seconds"] >= 185, (
        f"{bash_calls} Bash call(s), backgrounded {backgrounded}, {r['seconds']} s, "
        f"outputs {r['outputs'][-1:]}"
    )


def claim_deny_rule_push(tmp: Path) -> tuple[bool, str]:
    work = repo(tmp)
    remote = tmp / "remote.git"
    sh("git", "init", "-q", "--bare", str(remote), cwd=tmp)
    sh("git", "remote", "add", "origin", str(remote), cwd=work)
    r = run(
        work,
        "Run this exact shell command with the Bash tool: git push origin main",
        "--settings",
        ALLOW_BASH,
        "--permission-mode",
        "acceptEdits",
    )
    pushed = sh("git", "rev-parse", "--verify", "-q", "refs/heads/main", cwd=remote, check=False)
    return (
        not pushed,
        f"Bash allowed, Bash(git push:*) denied: remote main = {pushed or 'absent'}; "
        f"outputs {r['outputs'][-1:]}",
    )


CLAIMS: list[tuple[str, Callable[[Path], tuple[bool, str]]]] = [
    ("auth: headless auth with --setting-sources project", claim_auth),
    ("cost: total_cost_usd present in the result event", claim_cost),
    ("models: haiku, sonnet, opus aliases resolve", claim_models),
    ("schema: --json-schema returns structured_output", claim_schema),
    ("resume: claude -p --resume recalls the first answer", claim_resume),
    ("hooks + push: a --settings PreToolUse hook fires and refuses git push", claim_hooks_and_push),
    ("commit: with Bash allowed in --settings, an agent commits headless", claim_commit),
    (
        "deny-rule-push: Bash allowed, a Bash(git push:*) deny rule still refuses the push",
        claim_deny_rule_push,
    ),
    (
        "acceptEdits-bash: acceptEdits admits a file-edit command (touch), default mode denies it",
        claim_bash_modes,
    ),
    ("settings-deny: permissions.deny in --settings is enforced headless", claim_settings_deny),
    ("read-only: --disallowedTools removes Edit/Write/Bash", claim_read_only),
    ("budget: --max-budget-usd stops a run over its cap", claim_budget),
    ("effort: --effort max thinks more than low", claim_effort),
    (
        "bash-timeout: the timeout variables keep a 185 s command in the foreground",
        claim_bash_timeout,
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="CAPABILITIES.md")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    version = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True).stdout.strip()
    rows = []
    for name, fn in CLAIMS:
        if args.only and not any(o in name for o in args.only):
            continue
        tmp = Path(tempfile.mkdtemp(prefix="dispatch-probe-"))
        try:
            ok, evidence = fn(tmp)
        except Exception as e:
            ok, evidence = False, f"probe error: {type(e).__name__}: {e}"[:300]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        rows.append((name, ok, evidence))
        print(f"{'yes' if ok else 'NO ':3} {name} | {evidence}", flush=True)
    lines = [
        "# CAPABILITIES",
        "",
        f"Written by `sazed_worker/dispatch/probe.py` on {date.today()}, Claude Code {version}, "
        f"{os.uname().sysname if hasattr(os, 'uname') else sys.platform}, no managed policy.",
        "Re-run after a Claude Code upgrade. Read the evidence column, not the result.",
        "",
        "| Claim | Holds | Evidence |",
        "| --- | --- | --- |",
        *[f"| {n} | {'yes' if ok else 'no'} | {e.replace('|', '/')} |" for n, ok, e in rows],
        "",
    ]
    Path(args.out).write_text("\n".join(lines))


if __name__ == "__main__":
    main()
