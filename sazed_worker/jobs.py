"""One job from start to the finish payload Sazed receives."""

import uuid
from typing import Any

from . import agy, claude
from .changes import changed_files
from .config import JOBS_DIR, log
from .lint import lint_changed, lint_prompt
from .repo import prepare_repo, workspace
from .run import Run

_EMPTY_RETRY_PROMPT = (
    "Some tools you tried are not available here. Using only what you have already found, "
    "answer the original question now in the requested format, with sources."
)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run one job to completion and return the finish payload for Sazed."""
    ws = workspace(job["id"])
    run = Run(job)
    changes: dict[str, str | None] = {}
    lint_meta: dict[str, Any] = {}

    if job["agent"] == "agy":
        resume = job.get("resume_session_id")
        if resume and job.get("parent_id"):
            parent = JOBS_DIR / str(uuid.UUID(str(job["parent_id"])))
            ws = parent if parent.is_dir() else ws
        code = run.run(agy.command(job, job["prompt"], resume), ws)
        result = run.result or {}
        # agy ends with an empty answer when its last step was a tool headless mode refused.
        # Resume the same conversation once and ask it to answer with what it has.
        if (not result.get("response") and not run.stop_reason and run.session_id
                and result.get("denied")):
            log.info("job %s: empty answer after denied %s, resuming once", job["id"],
                     result.get("denied"))
            run.result = None
            code = run.run(agy.command(job, _EMPTY_RETRY_PROMPT, run.session_id), ws)
    elif job["agent"] == "claude":
        if job["profile"] not in claude.PROFILES:
            return {"status": "failed", "error": f"profile '{job['profile']}' is not supported yet"}
        repo_dir, cwd, resume = prepare_repo(job, ws)
        prompt = job["prompt"]
        if job.get("resume_session_id") and not resume:
            prompt = f"(The earlier session could not be resumed; start fresh.)\n\n{prompt}"
        code = run.run(claude.command(job, prompt, resume), cwd)
        if job["profile"] == "change" and (run.result or {}).get("ok"):
            changes = changed_files(repo_dir, cwd)
            issues = lint_changed(repo_dir, cwd, changes)
            lint_meta = {"lint_found": len(issues)}
            if issues and not run.stop_reason and run.session_id:
                log.info("job %s: %d lint issue(s) on changed lines, one fix round", job["id"],
                         len(issues))
                first = run.result
                run.result = None
                code = run.run(claude.command(job, lint_prompt(issues), run.session_id), cwd)
                if (run.result or {}).get("ok"):
                    changes = changed_files(repo_dir, cwd)
                else:
                    run.result = first  # keep the original answer if the fix round failed
                lint_meta["lint_left"] = lint_changed(repo_dir, cwd, changes)
    else:
        return {"status": "failed", "error": f"agent '{job['agent']}' is not supported"}

    result = run.result or {}
    response = result.get("response") or ""
    meta = lint_meta | {
        "tool_calls": run.tool_calls,
        "tools_used": run.tools_used,
        "usage": result.get("usage"),
        "duration_seconds": result.get("duration_seconds"),
        "denied": result.get("denied"),
        "turns": result.get("turns"),
        "exit_code": code,
        "rate_limits": run.rate_limits or None,
    }
    base = {"agent_session_id": run.session_id, "meta": meta}
    partial = {"result": response or None}
    if run.stop_reason in ("cancelled", "shutdown"):
        return base | {"status": "failed", "error": run.stop_reason}
    if run.stop_reason == "timed_out":
        return base | partial | {"status": "timed_out", "error": "hit the job timeout"}
    if run.stop_reason == "tool_cap":
        return base | partial | {"status": "failed",
                                 "error": f"stopped at the {run.max_tools}-tool-call cap"}
    if result.get("ok") and response:
        out = base | {"status": "succeeded", "result": response}
        if job.get("profile") == "change":
            out["changes"] = changes
            meta["changed_files"] = sorted(changes)
        return out
    error = (
        result.get("error") or run.stderr.strip()[-1000:] or f"agent exited {code} with no answer"
    )
    return base | partial | {"status": "failed", "error": error}
