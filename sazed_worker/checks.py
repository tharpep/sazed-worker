"""Checks the worker runs on a change job's edits before the PR (plans/10 P1.5).

Fixed commands from an allowlist, run by the worker as the agent user (the agent has no shell),
each reporting only what the agent's edits introduced:
- ruff: findings on changed lines, with the repo's own config
- bash -n: shell files that parse in the untouched checkout but not after the edit
- gofmt: applied to edited Go files that were formatted before (not reported)
- go vet, tsc: findings that the same command doesn't report on the untouched checkout

Findings feed the job's one fix round. A check whose tool isn't installed, or whose setup fails
(module download, package install), is skipped and named in the job's meta. Every command runs
in the worker's bubblewrap jail (jail.py): tsc and go vet load package code.
"""

import difflib
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import AGENT_GID, AGENT_UID, BASH, COREPACK, GO, GOFMT, NPM, RUFF, log
from .jail import go_env, run_jailed

MAX_FINDINGS = 40
_GO_LINE = re.compile(r"^(?:vet: )?(?:\./)?(\S+?\.go):(\d+)(?::\d+)?: (.+)$")
_TSC_LINE = re.compile(r"^(\S.*?\.[cm]?tsx?)\((\d+),\d+\): error (TS\d+: .+)$")
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
# Lockfile -> install command (no lifecycle scripts: package code never runs during install).
_INSTALLERS = {
    "pnpm-lock.yaml": [COREPACK, "pnpm", "install", "--frozen-lockfile", "--ignore-scripts"],
    "package-lock.json": [NPM, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
}


def _changed_lines(before: bytes | None, after: bytes) -> set[int]:
    """1-based line numbers in `after` that are new or edited relative to `before`."""
    old = (before or b"").decode(errors="replace").splitlines()
    new = after.decode(errors="replace").splitlines()
    lines: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag in ("replace", "insert"):
            lines.update(range(j1 + 1, j2 + 1))
    return lines


def _nearest(root: Path, rel: str, marker: str) -> str | None:
    """The closest folder at or above `rel`'s folder (inside root) holding `marker`, as a
    root-relative path ("" for the root itself)."""
    folder = (root / rel).parent
    while True:
        if (folder / marker).is_file():
            rel = folder.relative_to(root).as_posix()
            return "" if rel == "." else rel
        if folder == root:
            return None
        folder = folder.parent


class Checks:
    """One change job's checks. The untouched checkout's results are computed once and reused
    by the run after the fix round."""

    def __init__(self, repo_dir: Path, work: Path) -> None:
        self.repo_dir, self.work = repo_dir, work
        self.deps_root = repo_dir.parent  # the workspace: an ancestor of both trees
        self.counts: dict[str, int] = {}
        self.skipped: set[str] = set()
        self.formatted: list[str] = []
        self._baselines: dict[Any, dict[str, str] | None] = {}
        self._installed: list[Path] = []

    def run(self, changes: dict[str, str | None]) -> list[str]:
        """Findings in the agent's edits (capped). gofmt may rewrite files: re-read the changes
        when self.formatted grows."""
        edited = sorted(path for path, content in changes.items() if content is not None)
        self.counts = {}
        findings: list[str] = []
        for name, check in (
            ("ruff", self._ruff),
            ("bash", self._bash),
            ("go_vet", self._go),
            ("tsc", self._tsc),
        ):
            found = check(edited)
            if found is not None:
                self.counts[name] = len(found)
                findings += found
        return findings[:MAX_FINDINGS]

    def meta(self) -> dict[str, Any]:
        return {
            "checks": self.counts,
            "checks_skipped": sorted(self.skipped),
            "gofmt_applied": self.formatted,
        }

    def cleanup(self) -> None:
        """Remove installed packages; the workspace may be kept for weeks."""
        for path in self._installed:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        self._installed = []

    def _run(
        self, cmd: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None
    ) -> tuple[int | None, str, str]:
        return run_jailed(cmd, cwd, self.deps_root, timeout, env)

    def _baseline(self, key: Any, compute: Callable[[], dict[str, str] | None]) -> dict[str, str]:
        if key not in self._baselines:
            self._baselines[key] = compute()
        return self._baselines[key] or {}

    # ------------------------------------------------------------------ ruff

    def _ruff(self, edited: list[str]) -> list[str] | None:
        py = [path for path in edited if path.endswith(".py")]
        if not py:
            return None
        if not Path(RUFF).exists():
            self.skipped.add("ruff (not installed)")
            return None
        cmd = [RUFF, "check", "--output-format=json", "--no-cache", "--quiet", "--exit-zero"]
        code, out, _ = self._run([*cmd, *py], self.work, 60)
        try:
            findings = json.loads(out or "[]") if code is not None else None
        except json.JSONDecodeError:
            findings = None
        if not isinstance(findings, list):
            self.skipped.add("ruff (no usable output)")
            return None
        issues = []
        touched: dict[str, set[int]] = {}
        for f in findings:
            try:
                rel = Path(f["filename"]).resolve().relative_to(self.work.resolve()).as_posix()
                row = int(f["location"]["row"])
            except (KeyError, ValueError, TypeError):
                continue
            if rel not in touched:
                before = self.repo_dir / rel
                old = before.read_bytes() if before.is_file() else None
                touched[rel] = _changed_lines(old, (self.work / rel).read_bytes())
            if row in touched[rel]:
                issues.append(f"{rel}:{row}: ruff {f.get('code')} {f.get('message')}")
        return issues

    # ------------------------------------------------------------------ bash -n

    def _is_shell(self, rel: str) -> bool:
        if rel.endswith((".sh", ".bash")):
            return True
        if "." in Path(rel).name:
            return False
        try:
            with (self.work / rel).open("rb") as f:
                first = f.readline(200)
        except OSError:
            return False
        return first.startswith(b"#!") and re.search(rb"/(?:env )?(?:ba)?sh\b", first) is not None

    def _bash(self, edited: list[str]) -> list[str] | None:
        scripts = [rel for rel in edited if self._is_shell(rel)]
        if not scripts:
            return None
        issues = []
        for rel in scripts:
            code, _, err = self._run([BASH, "-n", rel], self.work, 20)
            if code in (0, None):
                continue
            if (self.repo_dir / rel).is_file():
                before, _, _ = self._run([BASH, "-n", rel], self.repo_dir, 20)
                if before != 0:
                    continue  # it didn't parse before the edit either
            first = next((line for line in err.splitlines() if line.strip()), "syntax error")
            issues.append(f"{rel}: bash -n: {first.strip()}")
        return issues

    # ------------------------------------------------------------------ Go

    def _go(self, edited: list[str]) -> list[str] | None:
        go_files = [rel for rel in edited if rel.endswith(".go")]
        if not go_files:
            return None
        if not (Path(GO).exists() and Path(GOFMT).exists()):
            self.skipped.add("go (not installed)")
            return None
        for rel in go_files:
            if rel in self.formatted:
                continue
            if (self.repo_dir / rel).is_file():
                code, out, _ = self._run([GOFMT, "-l", rel], self.repo_dir, 30)
                if code != 0 or out.strip():
                    continue  # it wasn't gofmt-clean before: leave its formatting alone
            code, out, _ = self._run([GOFMT, "-l", rel], self.work, 30)
            if code == 0 and out.strip() and self._run([GOFMT, "-w", rel], self.work, 30)[0] == 0:
                self.formatted.append(rel)
        issues: list[str] = []
        for module in sorted({_nearest(self.work, rel, "go.mod") for rel in go_files} - {None}):
            after = self._go_vet(self.work, module)
            if after is None:
                self.skipped.add(f"go vet ({module or '.'})")
                continue
            before = self._baseline(("go", module), lambda m=module: self._go_vet(self.repo_dir, m))
            issues += [shown for key, shown in sorted(after.items()) if key not in before]
        return issues

    def _go_vet(self, root: Path, module: str) -> dict[str, str] | None:
        """go vet's findings keyed without line numbers (so moved code still matches), or None
        if it failed without findings (a module download, a bad go.mod)."""
        if not (root / module / "go.mod").is_file():
            return {}
        code, out, err = self._run([GO, "vet", "./..."], root / module, 300, go_env(self.deps_root))
        if code is None:
            return None
        found = {}
        for line in (err + out).splitlines():
            match = _GO_LINE.match(line.strip())
            if match:
                path = f"{module}/{match[1]}".lstrip("/")
                found[f"{path}: {match[3]}"] = f"{path}:{match[2]}: go vet: {match[3]}"
        if code != 0 and not found:
            log.info("go vet failed in %s/%s: %s", root.name, module, err.strip()[-300:])
            return None
        return found

    # ------------------------------------------------------------------ TypeScript

    def _tsc(self, edited: list[str]) -> list[str] | None:
        ts = [rel for rel in edited if rel.endswith(_TS_SUFFIXES)]
        projects = sorted({_nearest(self.work, rel, "tsconfig.json") for rel in ts} - {None})
        if not projects:
            return None
        if len(projects) > 1:
            self.skipped.add(f"tsc (only {projects[0] or '.'} checked of {len(projects)})")
        project = projects[0]
        tsc = self.install_packages(project)
        if tsc is None:
            return None
        after = self._tsc_run(tsc, self.work, project)
        if after is None:
            self.skipped.add("tsc (didn't finish)")
            return None
        before = self._baseline(
            ("tsc", project), lambda: self._tsc_run(tsc, self.repo_dir, project)
        )
        return [shown for key, shown in sorted(after.items()) if key not in before]

    def install_packages(self, project: str) -> Path | None:
        """Install the project's packages in the workspace folder (an ancestor of both trees, so
        both resolve them and they never show up as changed files). Returns tsc's path."""
        tsc = self.deps_root / "node_modules" / ".bin" / "tsc"
        if tsc.exists():
            return tsc
        folder = self.repo_dir / project
        lock = next((name for name in _INSTALLERS if (folder / name).is_file()), None)
        if lock is None or not (folder / "package.json").is_file():
            self.skipped.add(f"tsc ({project or '.'}: no pnpm or npm lockfile)")
            return None
        installer = _INSTALLERS[lock]
        if not Path(installer[0]).exists():
            self.skipped.add(f"tsc ({Path(installer[0]).name} not installed)")
            return None
        for name in ("package.json", lock):
            shutil.copyfile(folder / name, self.deps_root / name)
            (self.deps_root / name).chmod(0o644)
            self._installed.append(self.deps_root / name)
            shutil.chown(self.deps_root / name, AGENT_UID, AGENT_GID)
        self._installed.append(self.deps_root / "node_modules")
        env = {"COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", "CI": "true"}
        code, _, err = self._run(installer, self.deps_root, 300, env)
        if code != 0 or not tsc.exists():
            log.info("package install failed for %s: %s", project or ".", err.strip()[-300:])
            self.skipped.add(f"tsc ({project or '.'}: package install failed)")
            return None
        return tsc

    def _tsc_run(self, tsc: Path, root: Path, project: str) -> dict[str, str] | None:
        cmd = [str(tsc), "--noEmit", "--pretty", "false", "-p", "tsconfig.json"]
        code, out, _ = self._run(cmd, root / project, 300)
        if code is None:
            return None
        found = {}
        for line in out.splitlines():
            match = _TSC_LINE.match(line.strip())
            if match:
                path = f"{project}/{match[1]}".lstrip("/")
                found[f"{path}: {match[3]}"] = f"{path}:{match[2]}: tsc {match[3]}"
        return found


def checks_prompt(issues: list[str]) -> str:
    return (
        "The worker's checks (the repo's own ruff config, bash -n, go vet, tsc) found these "
        "problems in your changes:\n" + "\n".join(f"- {i}" for i in issues) + "\n\nFix them, "
        "touching only what is needed. Then reply with the complete PR description again "
        "(updated if anything changed), in the same format as before."
    )
