"""The repo's own tests, run by the worker after a change job's build (plans/10 P1.11).

pytest for poetry projects, `go test` for Go modules; nothing else has tests today. The agent has
no shell: the worker runs them itself, in the jail's untrusted mode (jail.py), on throwaway copies
of the untouched checkout and of the working copy. A test that passes before the edit and fails
after it is a new failure; pre-existing and network-dependent failures fail on both sides and
don't count. New failures join the check findings in the job's one fix round, and if any remain,
Sazed opens the PR as a draft.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .config import GO, log
from .deps import download_go, install_python
from .jail import go_env, run_jailed
from .repo import _chown_tree

TIMEOUT_S = 600
MAX_REPORTED = 20
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"
)
_PYTEST_LINE = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")


def _any(root: Path, pattern: str) -> bool:
    return any(".git" not in p.parts for p in root.rglob(pattern))


class TestRun:
    """One change job's test runs. The untouched checkout's results are computed once and reused
    by the run after the fix round."""

    __test__ = False  # not a pytest class, whatever the name suggests

    def __init__(self, repo_dir: Path, work: Path) -> None:
        self.repo_dir, self.work = repo_dir, work
        self.root = repo_dir.parent  # the workspace: holds repo/ and work/
        self.runner = self._detect()
        self.skipped: str | None = None
        self.ready = False
        self.venv: str | None = None
        self.baseline: dict[str, str] | None = None
        self.new: list[str] = []
        self.runs = 0

    def _detect(self) -> str | None:
        work = self.work
        if (work / "pyproject.toml").is_file() and (work / "poetry.lock").is_file():
            if _any(work, "test_*.py") or _any(work, "*_test.py"):
                return "pytest"
        if (work / "go.mod").is_file() and _any(work, "*_test.go"):
            return "go"
        return None

    def run(self) -> list[str] | None:
        """New failures, one line each (capped), or None when there's nothing to run or it
        couldn't run (the reason is in skipped)."""
        if not self.runner or self.skipped:
            return None
        if not self.ready:
            self._prepare()
            if self.skipped:
                return None
        if self.baseline is None:
            self.baseline = self._run_on(self.repo_dir, "before")
            if self.baseline is None:
                self.skipped = "couldn't run on the untouched checkout"
                return None
        after = self._run_on(self.work, "after")
        if after is None:
            self.skipped = "couldn't run after the edit"
            return None
        self.runs += 1
        self.new = [
            f"tests: {name} fails after your change: {detail}"[:300]
            for name, detail in sorted(after.items())
            if name not in self.baseline
        ][:MAX_REPORTED]
        return self.new

    def meta(self) -> dict[str, Any]:
        """Recorded on the job; Sazed opens the PR as a draft when `left` isn't empty."""
        return {
            "runner": self.runner,
            "skipped": self.skipped,
            "failing_before": None if self.baseline is None else len(self.baseline),
            "runs": self.runs,
            "left": self.new if self.runs else [],
        }

    def cleanup(self) -> None:
        """Remove the copies, installed dependencies and caches; the workspace may be kept for
        weeks."""
        for name in ("testrun-before", "testrun-after", "testrun-cache", "venvs", "cache", "home"):
            shutil.rmtree(self.root / name, ignore_errors=True)

    # ------------------------------------------------------------------ setup

    def _prepare(self) -> None:
        self.ready = True
        if self.runner == "pytest":
            self.venv, note = install_python(self.root, self.work)
            if not self.venv:
                self.skipped = f"Python dependencies: {note}"
        elif self.runner == "go":
            ok, note = download_go(self.root, self.work)
            if not ok:
                self.skipped = f"Go modules: {note}"

    def _copy(self, src: Path, label: str) -> tuple[Path, Path]:
        """A throwaway copy of src, and a fresh scratch folder (HOME, build cache): the only
        writable paths of the test run."""
        copy, scratch = self.root / f"testrun-{label}", self.root / "testrun-cache" / label
        for path in (copy, scratch):
            shutil.rmtree(path, ignore_errors=True)
        shutil.copytree(src, copy, symlinks=True, ignore=_COPY_IGNORE)
        (scratch / "home").mkdir(parents=True)
        _chown_tree(copy)
        _chown_tree(self.root / "testrun-cache")
        return copy, scratch

    # ------------------------------------------------------------------ runners

    def _run_on(self, src: Path, label: str) -> dict[str, str] | None:
        """Failing tests (name -> short detail) on a copy of src, or None if the run didn't
        finish."""
        copy, scratch = self._copy(src, label)
        env = {"HOME": str(scratch / "home")}
        if self.runner == "pytest":
            cmd = [f"{self.venv}/bin/python", "-m", "pytest", "-q", "-rfE", "--color=no",
                   "-p", "no:cacheprovider"]  # fmt: skip
            # COLUMNS: pytest cuts its summary lines to the terminal width (80 by default).
            env |= {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": f"{copy}:{copy}/src",
                    "COLUMNS": "240"}
        else:
            cmd = [GO, "test", "-json", "-count=1", "./..."]
            env |= go_env(self.root) | {"GOCACHE": str(scratch / "go-build"), "GOPROXY": "off"}
        code, out, err = run_jailed(cmd, copy, self.root, TIMEOUT_S, env, [copy, scratch])
        if code is None:
            log.info("%s run on %s didn't finish: %s", self.runner, label, err.strip()[-200:])
            return None
        failed = _pytest_failures(out) if self.runner == "pytest" else _go_failures(out)
        if code not in (0, 5) and not failed:  # 5: pytest collected nothing
            tail = (err or out).strip().splitlines()[-1:] or ["no output"]
            failed = {f"{self.runner} run": f"exited {code}: {tail[0][:200]}"}
        return failed


def _pytest_failures(out: str) -> dict[str, str]:
    failed = {}
    for line in out.splitlines():
        match = _PYTEST_LINE.match(line.strip())
        if match:
            failed[match[2]] = (match[3] or match[1].lower()).strip()
    return failed


def _go_failures(out: str) -> dict[str, str]:
    """Failed tests from `go test -json`; a package that failed without a failed test (a build
    error) counts as one failure."""
    failed: dict[str, str] = {}
    detail: dict[tuple[str, str], str] = {}
    packages_failed: set[str] = set()
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        pkg, test, action = event.get("Package", ""), event.get("Test", ""), event.get("Action")
        text = (event.get("Output") or "").strip()
        if action == "output" and text and (".go:" in text or "FAIL" in text):
            detail.setdefault((pkg, test), text)
        elif action == "fail" and test:
            failed[f"{pkg}.{test}"] = detail.get((pkg, test), "failed")
        elif action == "fail":
            packages_failed.add(pkg)
    for pkg in packages_failed:
        if not any(name.startswith(f"{pkg}.") for name in failed):
            failed[pkg] = detail.get((pkg, ""), "package failed (build error?)")
    return failed
