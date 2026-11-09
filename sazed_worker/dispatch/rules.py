"""What a dispatch agent may run and edit (plans/17 D1, dispatch.md Guardrails): pure functions."""

import os
import re
import shlex
from pathlib import PurePosixPath

_SPLIT = re.compile(r"&&|\|\||;|\n|\|")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TOKEN_ASSIGN = re.compile(r"\b[A-Za-z_]*(TOKEN|SECRET|PASSWORD|API_KEY)[A-Za-z_]*=", re.IGNORECASE)
_PIPE_TO_SHELL = re.compile(r"\|\s*(sudo\s+)?(ba|z|da)?sh\b")
_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
_PROTECTED = (".claude", ".git", ".agents", ".gemini")
_RC_FILES = {".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile", ".inputrc"}


def _words(segment: str) -> list[str]:
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    while words and _ENV_ASSIGN.match(words[0]):
        words.pop(0)
    return words


def _git_subcommand(words: list[str]) -> tuple[str, list[str]]:
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in _GIT_VALUE_FLAGS else 1
    return (words[i], words[i + 1 :]) if i < len(words) else ("", [])


def _git_reason(words: list[str]) -> str | None:
    sub, rest = _git_subcommand(words)
    if sub == "push":
        return "agents do not push; the integrator lands your commits"
    if sub == "add" and any(a in ("-A", "--all", ".", ":/") for a in rest):
        return "stage the files you changed by name, not with a sweeping add"
    if sub == "commit" and any(
        a == "--all" or re.fullmatch(r"-[a-zA-Z]*a[a-zA-Z]*", a) for a in rest
    ):
        return "stage the files you changed by name; commit -a sweeps in strays"
    if sub == "remote" and rest[:1] == ["set-url"]:
        return "agents do not change remotes"
    if sub == "config" and any("credential" in a for a in rest):
        return "agents do not touch git credentials"
    return None


def _cd_reason(words: list[str], worktree: str) -> str | None:
    if len(words) < 2:
        return None
    target = os.path.expanduser(words[1])
    if not target.startswith("/"):
        return None
    root = os.path.normpath(worktree)
    path = os.path.normpath(target)
    if path != root and not path.startswith(root + os.sep):
        return f"stay inside your worktree ({worktree})"
    return None


def bash_reason(command: str, worktree: str) -> str | None:
    """Why this Bash command is refused, or None when it may run."""
    if _PIPE_TO_SHELL.search(command):
        return "do not pipe downloads into a shell"
    if _TOKEN_ASSIGN.search(command):
        return "agents do not set credentials"
    for segment in _SPLIT.split(command):
        words = _words(segment)
        if not words:
            continue
        head = os.path.basename(words[0])
        if head == "git":
            reason = _git_reason(words)
        elif head == "gh" and words[1:2] in (["pr"], ["api"]):
            reason = "agents do not open PRs or call the GitHub API; the integrator does"
        elif head in ("pkill", "killall"):
            reason = "do not kill processes by name"
        elif head in ("claude", "agy") and words[1:2] in (["plugin"], ["mcp"], ["config"]):
            reason = f"agents do not change {head}'s setup"
        elif head == "cd":
            reason = _cd_reason(words, worktree)
        else:
            reason = None
        if reason:
            return reason
    return None


def glob_match(path: str, pattern: str) -> bool:
    """Path glob where `*` stays inside one directory and `**` spans any number."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None


def edit_reason(path: str, owns: list[str], worktree: str) -> str | None:
    """Why an edit to this path is refused, or None when the task owns it."""
    root = PurePosixPath(os.path.normpath(worktree))
    target = PurePosixPath(os.path.normpath(os.path.join(worktree, os.path.expanduser(path))))
    if target != root and root not in target.parents:
        return f"stay inside your worktree ({worktree})"
    rel = target.relative_to(root)
    if rel.parts and rel.parts[0] in _PROTECTED:
        return f"agents do not edit {rel.parts[0]}/"
    if rel.name in _RC_FILES:
        return "agents do not edit shell rc files"
    if not any(glob_match(rel.as_posix(), p) for p in owns):
        return (
            f"{rel.as_posix()} is outside this task's owns ({', '.join(owns) or 'none'}); "
            "finish with state waiting and name the change you need"
        )
    return None
