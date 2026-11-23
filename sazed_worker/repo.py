"""Job workspaces and repo checkouts: git as the agent user, and the agent's working copy."""

import contextlib
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .agent_process import JobFailed, agent_env, as_agent, kill_group
from .config import AGENT_GID, AGENT_UID, AGENT_USER, JOBS_DIR
from .scrub import extra_secrets

_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_BASE_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

_CREDENTIAL_HELPER = (
    '!f() { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f'
)


def git(*args: str, cwd: Path | None = None, timeout: int = 180,
        token: str | None = None) -> str:
    """git as the agent user, with hooks disabled and no credential prompts.

    A token (read-only, one repo, from the gateway's GitHub App) goes in via environment config,
    not argv, so it never shows in a process listing.
    """
    return _run_git(args, cwd, agent_env() | _token_env(token), timeout, as_agent())


def _token_env(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    # A credential helper reading the token from the environment. (An http.extraheader with
    # Basic auth was ignored for GitHub's newer long installation tokens.) Nothing is written
    # to .git/config.
    return {
        "GIT_TOKEN": token,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": _CREDENTIAL_HELPER,
    }


def _run_git(
    args: tuple[str, ...],
    cwd: Path | None,
    env: Mapping[str, str],
    timeout: int,
    user: dict[str, Any],
    watch: Callable[[subprocess.Popen], contextlib.AbstractContextManager] | None = None,
) -> str:
    proc = subprocess.Popen(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **user,
    )
    try:
        with watch(proc) if watch else contextlib.nullcontext():
            out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        raise JobFailed(f"git {args[0]} timed out") from None
    if proc.returncode != 0:
        raise JobFailed(f"git {args[0]} failed: {err.strip()[-500:]}")
    return out


def clone_url(repo: str) -> str:
    if not _REPO_RE.fullmatch(repo):
        raise JobFailed(f"invalid repo '{repo}'")
    return f"https://github.com/{repo}.git"


def clone_as_caller(
    url: str,
    base: str,
    dest: Path,
    token: str | None,
    git_env: Mapping[str, str],
    watch: Callable[[subprocess.Popen], contextlib.AbstractContextManager] | None = None,
) -> None:
    """Clone base for this process's own user (root), so agents can read it and never write
    it. The environment is built here, so neither the worker key nor the Claude token is in
    it."""
    if not _BASE_RE.fullmatch(base):
        raise JobFailed(f"invalid base '{base}'")
    if token:
        extra_secrets.add(token)
    env = {
        "HOME": str(dest.parent),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
        # The folders above belong to the agent, so root must never find a repository there.
        "GIT_CEILING_DIRECTORIES": str(dest.parent.parent),
    }
    args = ("clone", "--depth", "50", "--single-branch", "--no-tags", "--branch", base, "--",
            url, str(dest))
    _run_git(args, dest.parent, env | git_env | _token_env(token), 180, {}, watch)


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
    params = job.get("params") or {}
    pr_number = params.get("pr_number")
    branch = params.get("branch")
    try:
        if pr_number:
            git("clone", "--depth", "50", f"https://github.com/{repo}.git", str(tmp), token=token)
            git("fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}", cwd=tmp, token=token)
            git("checkout", f"pr-{pr_number}", cwd=tmp)
        elif branch and branch not in ("main", "master"):
            git("clone", "--depth", "50", "--branch", branch, f"https://github.com/{repo}.git",
                str(tmp), token=token)
        else:
            git("clone", "--depth", "50", "--single-branch", f"https://github.com/{repo}.git",
                str(tmp), token=token)
    except JobFailed:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    tmp.rename(dest)
    cwd = _working_copy(dest, workspace) if change else dest
    return dest, cwd, None  # a fresh checkout has no session to resume


# Neither agent loads CLAUDE.md here: Claude Code runs with --setting-sources=user, and agy reads
# only AGENTS.md and GEMINI.md from the checkout (checked headless on agy 1.4.12). So CLAUDE.md and
# REVIEW.md go in the prompt, and AGENTS.md or GEMINI.md would be duplicates for agy.
# kelsier's CLAUDE.md is about 31 KB.
_INSTRUCTION_FILES = (("CLAUDE.md", 40_000), ("REVIEW.md", 12_000))


def repo_instructions(repo_dir: Path, reads_itself: tuple[str, ...] = ()) -> str:
    """The repo's CLAUDE.md and REVIEW.md as a preamble for a fresh session's prompt, or "".
    Files the agent loads from the checkout on its own (its READS_CONTEXT) are left out."""
    parts = []
    for name, cap in _INSTRUCTION_FILES:
        if name in reads_itself:
            continue
        path = repo_dir / name
        if path.is_symlink() or not path.is_file():  # a symlink could point outside the repo
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) > cap:
            text = f"{text[:cap]}\n\n[{name} cut at {cap} characters]"
        if text:
            parts.append(f"<{name}>\n{text}\n</{name}>")
    if not parts:
        return ""
    return (
        "The repository's own instructions, from its default branch. Follow them where they "
        "don't conflict with the task below.\n\n" + "\n\n".join(parts) + "\n\n---\n\n"
    )
