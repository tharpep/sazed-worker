"""The sandboxed shell for change jobs (plans/10 P1.11).

On repos in SHELL_REPOS, a change job's agent gets the Bash tool for a fixed set of test and check
commands, inside Claude Code's own sandbox (bubblewrap on Linux), set through --settings:
- only the allowlisted commands run; anything else is refused (dontAsk), chained commands too,
  and sandboxed commands aren't auto-approved past the allowlist
- if the sandbox can't start, Claude Code exits: never a shell without it
- no network; writes only in the working copy, the sandbox's temp folder and the workspace cache
- no reads of the agent user's logins (~/.claude, ~/.gemini), ~/.local, ~/.config, or other jobs
- the OAuth token unset in every command's environment, and subprocess credential scrubbing on

The sandbox has no network, so the worker installs dependencies first, in its own jail (jail.py):
poetry into a workspace virtualenv, go mod download, and the pnpm/npm install the checks use.
"""

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_process import agent_env
from .checks import Checks
from .config import AGENT_USER, GO, POETRY, PYTHONS_DIR, SHELL_REPOS, log
from .jail import go_env, run_jailed

ALLOWED = [
    "Bash(pytest *)",
    "Bash(python -m pytest *)",
    "Bash(ruff *)",
    "Bash(bash -n *)",
    "Bash(go test *)",
    "Bash(go vet *)",
    "Bash(gofmt -l *)",
    "Bash(tsc *)",
]
_COMMANDS = "pytest, python -m pytest, ruff, bash -n, go test, go vet, gofmt -l, tsc"


def _settings(root: Path) -> str:
    return json.dumps({
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": False,
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "filesystem": {
                "allowWrite": [str(root / "cache")],
                "denyRead": [
                    "~/.claude", "~/.claude.json", "~/.gemini", "~/.local", "~/.config", "~/jobs",
                ],
                "allowRead": [str(root)],  # this job's workspace, inside the denied ~/jobs
            },
            "credentials": {"envVars": [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"}]},
        }
    })  # fmt: skip


def _pythons() -> list[str]:
    """uv-managed Pythons, oldest first, then the system one (3.14 may lack wheels)."""
    found = []
    for exe in PYTHONS_DIR.glob("cpython-3.*/bin/python3.*"):
        match = re.fullmatch(r"python3\.(\d+)", exe.name)
        if match:
            found.append((int(match[1]), str(exe)))
    return [exe for _, exe in sorted(found)] + ["/usr/bin/python3"]


@dataclass
class Shell:
    root: Path
    env: dict[str, str]
    settings: str
    ready: list[str]
    skipped: list[str]
    checks: Checks

    def note(self) -> str:
        """What the agent can run, prepended to its prompt."""
        lines = [
            f"In this job you can run tests and checks yourself with the Bash tool: {_COMMANDS}.",
            "They run in a sandbox with no network; any other command is refused.",
        ]
        if self.ready:
            lines.append(f"Dependencies are already installed ({', '.join(self.ready)}).")
        if self.skipped:
            lines.append(f"Not set up: {'; '.join(self.skipped)}.")
        lines.append(
            "Run the relevant tests before you finish, and list anything you couldn't run in "
            "not_verified."
        )
        return " ".join(lines) + "\n\n---\n\n"

    def meta(self) -> dict[str, Any]:
        return {"ready": self.ready, "skipped": self.skipped}

    def cleanup(self) -> None:
        """Remove the installed dependencies and caches; the workspace may be kept for weeks."""
        self.checks.cleanup()
        for name in ("venvs", "cache", "home", "node_modules"):
            shutil.rmtree(self.root / name, ignore_errors=True)


def prepare_shell(job: dict[str, Any], repo_dir: Path, work: Path) -> Shell | None:
    """The shell for this job, with dependencies installed, or None if it doesn't get one."""
    if job.get("profile") != "change" or (job.get("params") or {}).get("repo") not in SHELL_REPOS:
        return None
    root = repo_dir.parent  # the workspace: holds repo/ and work/
    cache = root / "cache"
    cache.mkdir(exist_ok=True)
    shutil.chown(cache, AGENT_USER, AGENT_USER)
    ready: list[str] = []
    skipped: list[str] = []
    path: list[str] = []
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "RUFF_NO_CACHE": "true",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
    }

    if (work / "pyproject.toml").is_file() and (work / "poetry.lock").is_file():
        venv = _poetry(root, work, skipped)
        if venv:
            ready.append("Python, with poetry")
            path.append(f"{venv}/bin")
    if (work / "go.mod").is_file():
        code, _, err = run_jailed([GO, "mod", "download"], work, root, 300, go_env(root))
        if code == 0:
            ready.append("Go modules")
        else:
            skipped.append("Go modules (download failed)")
            log.info("go mod download failed: %s", err.strip()[-300:])
    checks = Checks(repo_dir, work)
    if (work / "tsconfig.json").is_file():
        if checks.install_packages(""):
            ready.append("node packages")
            path.append(str(root / "node_modules" / ".bin"))
        else:
            skipped.append("node packages (install failed)")

    env |= go_env(root) | {"GOPROXY": "off"}
    if path:
        env["PATH"] = ":".join([*path, agent_env("claude")["PATH"]])
    return Shell(root, env, _settings(root), ready, skipped + sorted(checks.skipped), checks)


def _poetry(root: Path, work: Path, skipped: list[str]) -> str | None:
    """Install the project with poetry into a virtualenv in the workspace; its path, or None."""
    env = {
        "POETRY_VIRTUALENVS_PATH": str(root / "venvs"),
        "POETRY_VIRTUALENVS_IN_PROJECT": "false",
        "POETRY_CACHE_DIR": str(root / "cache" / "poetry"),
        "POETRY_NO_INTERACTION": "1",
    }
    if not Path(POETRY).exists():
        skipped.append("Python dependencies (poetry isn't installed)")
        return None
    for python in _pythons():
        code, _, _ = run_jailed([POETRY, "env", "use", python], work, root, 120, env)
        if code == 0:
            break
    else:
        skipped.append("Python dependencies (no Python matches the project)")
        return None
    code, _, err = run_jailed([POETRY, "install"], work, root, 600, env)
    if code != 0:
        log.info("poetry install failed: %s", err.strip()[-300:])
        skipped.append("Python dependencies (poetry install failed)")
        return None
    code, out, _ = run_jailed([POETRY, "env", "info", "-p"], work, root, 60, env)
    venv = out.strip()
    if code != 0 or not venv:
        skipped.append("Python dependencies (no virtualenv found)")
        return None
    return venv
