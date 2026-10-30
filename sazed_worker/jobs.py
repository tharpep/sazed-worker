"""One job from start to the finish payload Sazed receives."""

import uuid
from datetime import UTC, datetime
from typing import Any

from . import agy, claude
from .agent_process import JobFailed
from .changes import changed_files
from .checks import Checks, checks_prompt
from .config import JOBS_DIR, log
from .repo import prepare_repo, repo_instructions, workspace
from .report import repair_prompt, report_problem
from .run import Run
from .tests import TestRun

# Stops that don't mean "give up": Sazed pauses a change job stopped this way (plans/10 P1.7),
# and a rate_limit one resumes on its own after the reset (P1.2).
PAUSING_STOPS = ("timed_out", "tool_cap", "shutdown", "rate_limit")

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
        if not resume:
            prompt = repo_instructions(repo_dir) + prompt
        code = run.run(claude.command(job, prompt, resume), cwd)
        if job["profile"] == "change" and (run.result or {}).get("ok"):
            # The worker's checks and the repo's own tests (both run by the worker; the agent has
            # no shell) feed one fix round.
            checks, tests = Checks(repo_dir, cwd), TestRun(repo_dir, cwd)
            try:
                changes = changed_files(repo_dir, cwd)
                issues = checks.run(changes)
                if checks.formatted:
                    changes = changed_files(repo_dir, cwd)  # gofmt rewrote some files
                failures = tests.run() or []
                lint_meta = {"lint_found": len(issues), "tests_failing": len(failures)}
                if (issues or failures) and not run.stop_reason and run.session_id:
                    log.info("job %s: %d check finding(s), %d new test failure(s), one fix round",
                             job["id"], len(issues), len(failures))
                    first = run.result
                    run.result = None
                    fix = claude.command(job, checks_prompt(issues + failures), run.session_id)
                    code = run.run(fix, cwd)
                    if (run.result or {}).get("ok"):
                        changes = changed_files(repo_dir, cwd)
                    else:
                        run.result = first  # keep the original answer if the fix round failed
                    lint_meta["lint_left"] = checks.run(changes)
                    if failures:
                        tests.run()
                    changes = changed_files(repo_dir, cwd)
                lint_meta |= checks.meta() | {"tests": tests.meta()}
            finally:
                checks.cleanup()
                tests.cleanup()
            # Sazed builds the PR description from the reply's JSON block: one repair round if
            # its shape is off. A failed repair keeps the reply (Sazed falls back to its prose).
            problem = report_problem((run.result or {}).get("response") or "")
            if problem and not run.stop_reason and run.session_id:
                log.info("job %s: result block unusable (%s), one repair round", job["id"],
                         problem)
                first = run.result
                run.result = None
                code = run.run(claude.command(job, repair_prompt(problem), run.session_id), cwd)
                repaired = run.result or {}
                if repaired.get("ok") and not report_problem(repaired.get("response") or ""):
                    changes = changed_files(repo_dir, cwd)  # in case it edited anyway
                else:
                    run.result = first
                lint_meta["result_repaired"] = run.result is not first
        if job["profile"] == "change" and run.stop_reason in PAUSING_STOPS:
            # Sazed pauses the job and saves this as a draft PR, so a resume picks it up.
            try:
                changes = changed_files(repo_dir, cwd)
            except JobFailed as e:
                log.warning("job %s: couldn't collect the work so far: %s", job["id"], e)
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
        "rate_limited": run.rate_limited,
        "stop_reason": run.stop_reason,
    }
    base = {"agent_session_id": run.session_id, "meta": meta}
    partial = {"result": response or None}
    if job.get("profile") == "change" and run.stop_reason in PAUSING_STOPS:
        base["changes"] = changes
        meta["changed_files"] = sorted(changes)
    if run.stop_reason in ("cancelled", "shutdown"):
        return base | {"status": "failed", "error": run.stop_reason}
    if run.stop_reason == "timed_out":
        return base | partial | {"status": "timed_out", "error": "hit the job timeout"}
    if run.stop_reason == "rate_limit":
        limited = run.rate_limited or {}
        window = (limited.get("window") or "usage").replace("_", " ")
        error = f"hit Pryce's {window} plan limit"
        if isinstance(limited.get("resets_at"), (int, float)):
            reset = datetime.fromtimestamp(limited["resets_at"], UTC).strftime("%Y-%m-%d %H:%M")
            error += f"; it resets at {reset} UTC"
        return base | partial | {"status": "failed", "error": error}
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
