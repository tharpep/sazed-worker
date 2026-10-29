"""A repo's dependencies, installed in the job's workspace for the worker's test run (tests.py).

Everything runs in the jail (jail.py) with network, as the agent user, before any test: poetry
into a workspace virtualenv (the project itself is not installed; tests import it from their
folder), and go mod download into the workspace's module cache. All of it is removed with the
test run's cleanup.
"""

import re
from pathlib import Path

from .config import GO, POETRY, PYTHONS_DIR, log
from .jail import go_env, run_jailed


def _pythons() -> list[str]:
    """uv-managed Pythons, oldest first, then the system one (3.14 may lack wheels)."""
    found = []
    for exe in PYTHONS_DIR.glob("cpython-3.*/bin/python3.*"):
        match = re.fullmatch(r"python3\.(\d+)", exe.name)
        if match:
            found.append((int(match[1]), str(exe)))
    return [exe for _, exe in sorted(found)] + ["/usr/bin/python3"]


def poetry_env(root: Path) -> dict[str, str]:
    return {
        "POETRY_VIRTUALENVS_PATH": str(root / "venvs"),
        "POETRY_VIRTUALENVS_IN_PROJECT": "false",
        "POETRY_CACHE_DIR": str(root / "cache" / "poetry"),
        "POETRY_NO_INTERACTION": "1",
    }


def install_python(root: Path, project: Path) -> tuple[str | None, str]:
    """(the virtualenv's path, or None; what happened) for a poetry project."""
    if not Path(POETRY).exists():
        return None, "poetry isn't installed"
    env = poetry_env(root)
    for python in _pythons():
        code, _, _ = run_jailed([POETRY, "env", "use", python], project, root, 120, env)
        if code == 0:
            break
    else:
        return None, "no Python matches the project"
    code, _, err = run_jailed([POETRY, "install", "--no-root"], project, root, 600, env)
    if code != 0:
        log.info("poetry install failed: %s", err.strip()[-300:])
        return None, "poetry install failed"
    code, out, _ = run_jailed([POETRY, "env", "info", "-p"], project, root, 60, env)
    venv = out.strip()
    if code != 0 or not venv:
        return None, "no virtualenv found"
    return venv, "installed with poetry"


def download_go(root: Path, module: Path) -> tuple[bool, str]:
    """go mod download into the workspace's module cache."""
    if not Path(GO).exists():
        return False, "go isn't installed"
    code, _, err = run_jailed([GO, "mod", "download"], module, root, 300, go_env(root))
    if code != 0:
        log.info("go mod download failed: %s", err.strip()[-300:])
        return False, "go mod download failed"
    return True, "modules downloaded"
