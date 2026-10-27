"""Job workspaces and repo checkouts: git as the agent user, and the agent's working copy."""

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .agent_process import JobFailed, agent_env, as_agent, kill_group
from .config import AGENT_GID, AGENT_UID, AGENT_USER, JOBS_DIR
from .scrub import extra_secrets

_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")

_CREDENTIAL_HELPER = (
    '!f() { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f'
)


def git(*args: str, cwd: Path | None = None, timeout: int = 180,
        token: str | None = None) -> str:
    """git as the agent user, with hooks disabled and no credential prompts.

    A token (read-only, one repo, from the gateway's GitHub App) goes in via environment config,
    not argv, so it never shows in a process listing.
    """
    env = agent_env("git")
    if token:
        # A credential helper reading the token from the environment. (An http.extraheader with
        # Basic auth was ignored for GitHub's newer long installation tokens.) Nothing is written
        # to .git/config.
        env |= {
            "GIT_TOKEN": token,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_0": _CREDENTIAL_HELPER,
        }
    proc = subprocess.Popen(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **as_agent(),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        raise JobFailed(f"git {args[0]} timed out") from None
    if proc.returncode != 0:
        raise JobFailed(f"git {args[0]} failed: {err.strip()[-500:]}")
    return out


def workspace(job_id: str) -> Path:
    path = JOBS_DIR / str(uuid.UUID(str(job_id)))  # validates: no path tricks in ids
    path.mkdir(parents=True, exist_ok=True)
    shutil.chown(path, AGENT_USER, AGENT_USER)
    return path


def _chown_tree(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            os.chown(Path(dirpath) / name, AGENT_UID, AGENT_GID, follow_symlinks=False)
    os.chown(root, AGENT_UID, AGENT_GID)


def _working_copy(repo_dir: Path, workspace: Path) -> Path:
    """A plain copy of the checkout without .git: the agent can't reach git internals or hooks."""
    work = workspace / "work"
    if not work.exists():
        shutil.copytree(repo_dir, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        _chown_tree(work)
    return work


def prepare_repo(job: dict[str, Any], workspace: Path) -> tuple[Path, Path, str | None]:
    """(checkout, agent cwd, session to resume). Change jobs get a working copy as cwd."""
    change = job["profile"] == "change"
    resume = job.get("resume_session_id")
    if resume and job.get("parent_id"):
        parent_ws = JOBS_DIR / str(uuid.UUID(str(job["parent_id"])))
        parent_repo = parent_ws / "repo"
        if parent_repo.is_dir():
            os.utime(parent_ws)  # keep an active session's workspace from aging out
            cwd = _working_copy(parent_repo, parent_ws) if change else parent_repo
            return parent_repo, cwd, resume
    repo = (job.get("params") or {}).get("repo") or ""
    if not _REPO_RE.match(repo):
        raise JobFailed(f"invalid repo '{repo}'")
    dest, tmp = workspace / "repo", workspace / "repo.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    # Public repos clone anonymously; private ones with a read-only token Sazed got from the
    # gateway's GitHub App for this job.
    token = job.get("clone_token") or None
    if token:
        extra_secrets.add(token)
    try:
        git("clone", "--depth", "50", "--single-branch", f"https://github.com/{repo}.git",
            str(tmp), token=token)
    except JobFailed:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    tmp.rename(dest)
    cwd = _working_copy(dest, workspace) if change else dest
    return dest, cwd, None  # a fresh checkout has no session to resume
