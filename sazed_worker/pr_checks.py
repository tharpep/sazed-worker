"""Checks and tests on a pull request, for its review (My-AI plans/14, the shared check service).

The same before/after comparison a change job gets, with the PR's merge base as "before" and a
copy of its head as "after". The reviewer is told what the PR introduced (check findings on its
changed lines, tests that pass at the base and fail at the head), and the job records one
check_result like a change job does. Both trees are throwaway copies, so gofmt never touches the
checkout the reviewer reads.
"""

import shutil
from pathlib import Path
from typing import Any

from .agent_process import JobFailed
from .changes import changed_files
from .checks import Checks
from .config import log
from .repo import _chown_tree, git
from .tests import TestRun

_DEEPEN = 500


def _merge_base(repo_dir: Path, pr_number: int, token: str | None) -> str:
    ref = f"pr-{pr_number}"
    try:
        return git("merge-base", "origin/HEAD", ref, cwd=repo_dir).strip()
    except JobFailed:
        # The clone is shallow: the PR may branch off further back than its depth.
        git("fetch", f"--deepen={_DEEPEN}", "origin", cwd=repo_dir, token=token)
        git("fetch", f"--deepen={_DEEPEN}", "origin", f"pull/{pr_number}/head", cwd=repo_dir,
            token=token)
        return git("merge-base", "origin/HEAD", ref, cwd=repo_dir).strip()


def _trees(job: dict[str, Any], repo_dir: Path, ws: Path) -> tuple[Path, Path]:
    """(the merge base, the head) as plain trees without .git, inside the workspace."""
    pr_number = (job.get("params") or {})["pr_number"]
    base_sha = _merge_base(repo_dir, pr_number, job.get("clone_token") or None)
    base, head = ws / "pr-base", ws / "pr-head"
    for path in (base, head):
        shutil.rmtree(path, ignore_errors=True)
    git("worktree", "add", "--detach", str(base), base_sha, cwd=repo_dir)
    (base / ".git").unlink()
    git("worktree", "prune", cwd=repo_dir)
    shutil.copytree(repo_dir, head, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    _chown_tree(head)
    return base, head


_DIFF_CAP = 40_000


def pr_diff(job: dict[str, Any], repo_dir: Path) -> str:
    """The PR's diff against its merge base, as a prompt section: reviewers can't run git."""
    pr_number = (job.get("params") or {})["pr_number"]
    try:
        base = _merge_base(repo_dir, pr_number, job.get("clone_token") or None)
        stat = git("diff", "--stat", base, f"pr-{pr_number}", cwd=repo_dir).strip()
        diff = git("diff", base, f"pr-{pr_number}", cwd=repo_dir)
    except JobFailed as e:
        log.info("job %s: no PR diff: %s", job["id"], e)
        return ""
    cut = len(diff) > _DIFF_CAP
    if cut:
        diff = diff[:_DIFF_CAP].rsplit("\n", 1)[0]
    note = (
        f"\n\nThe diff is cut at {_DIFF_CAP // 1000}k characters; read the files in the stat "
        "above for the rest."
        if cut
        else ""
    )
    return (
        f"The PR's diff against its merge base ({base[:7]}), given here because you can't run "
        f"git yourself:\n\n```diff\n{stat}\n\n{diff}\n```{note}\n\n---\n\n"
    )


def pr_checks(job: dict[str, Any], repo_dir: Path, ws: Path) -> tuple[str, dict[str, Any]]:
    """(a prompt section for the reviewer, the lint meta for check_result). The section is ""
    when nothing could be checked; the reason is logged and kept in the meta."""
    try:
        base, head = _trees(job, repo_dir, ws)
    except JobFailed as e:
        log.info("job %s: no PR checks: %s", job["id"], e)
        return "", {"pr_checks_skipped": str(e)[:300]}
    checks, tests = Checks(base, head), TestRun(base, head)
    try:
        changes = changed_files(base, head)
        issues = checks.run(changes)
        failures = tests.run() or []
        lint_meta = (
            {"lint_found": len(issues), "tests_failing": len(failures)}
            | checks.meta()
            | {"tests": tests.meta()}
        )
    except JobFailed as e:
        log.info("job %s: PR checks stopped: %s", job["id"], e)
        return "", {"pr_checks_skipped": str(e)[:300]}
    finally:
        checks.cleanup()
        tests.cleanup()
        for path in (base, head):
            shutil.rmtree(path, ignore_errors=True)
    return _section(issues + failures, lint_meta), lint_meta


def _section(findings: list[str], lint_meta: dict[str, Any]) -> str:
    tests = lint_meta.get("tests") or {}
    skipped = [*lint_meta.get("checks_skipped", [])]
    if tests.get("skipped"):
        skipped.append(f"tests ({tests['skipped']})")
    ran = sorted(lint_meta.get("checks") or {})
    if tests.get("runner") and not tests.get("skipped"):
        ran.append(f"tests ({tests['runner']})")
    if not ran and not skipped:
        return ""
    lines = [
        "Before you started, the worker ran checks on this PR, comparing its head with its merge "
        "base so only what the PR introduced is listed. You can't run anything yourself; treat "
        "these as facts and include each one in your Findings section.",
        f"Ran: {', '.join(ran) or 'nothing applicable'}.",
    ]
    if skipped:
        lines.append(f"Skipped: {', '.join(skipped)}.")
    if findings:
        lines.append("Found:\n" + "\n".join(f"- {f}" for f in findings))
    else:
        lines.append("Found nothing new.")
    return "\n".join(lines) + "\n\n---\n\n"
