"""Every git call dispatch makes: a worktree per task, the plan's integ branch, landing, export."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout.strip()


def run_code(command: str, cwd: Path, timeout_s: int = 1200) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout_s} s"
    return proc.returncode, (proc.stdout + proc.stderr)[-1500:]


def run_shell(command: str, cwd: Path, timeout_s: int = 1200) -> tuple[bool, str]:
    code, out = run_code(command, cwd, timeout_s)
    return code == 0, out


@dataclass(frozen=True)
class Landing:
    state: str
    line: str
    ref: str | None = None


class Workspace:
    def __init__(self, repo: Path, plan_id: str, home: Path):
        self.repo = repo
        self.plan_id = plan_id
        self.home = home
        self.integ_branch = f"integ/{plan_id}"
        self.integ_dir = home / "integ"

    def task_branch(self, task_id: str) -> str:
        return f"dispatch/{self.plan_id}/{task_id}"

    def task_dir(self, task_id: str) -> Path:
        return self.home / "wt" / task_id

    def integ_exists(self) -> bool:
        ref = f"refs/heads/{self.integ_branch}"
        return bool(git("rev-parse", "--verify", "-q", ref, cwd=self.repo, check=False))

    def open(self, base: str) -> str:
        if not self.integ_exists():
            git("branch", self.integ_branch, base, cwd=self.repo)
        if not self.integ_dir.exists():
            self.home.mkdir(parents=True, exist_ok=True)
            git("worktree", "add", "-q", str(self.integ_dir), self.integ_branch, cwd=self.repo)
        return self.tip()

    def tip(self) -> str:
        return git("rev-parse", self.integ_branch, cwd=self.repo)

    def commit_all(self, message: str, paths: list[str]) -> bool:
        git("add", "--", *paths, cwd=self.integ_dir)
        if not git("diff", "--cached", "--name-only", cwd=self.integ_dir):
            return False
        git("commit", "-q", "-m", message, cwd=self.integ_dir)
        return True

    def create(self, task_id: str) -> Path:
        path = self.task_dir(task_id)
        if path.exists():
            self.remove(task_id)
        git(
            "worktree",
            "add",
            "-q",
            "-B",
            self.task_branch(task_id),
            str(path),
            self.tip(),
            cwd=self.repo,
        )
        self._exclude_agents()
        return path

    def _exclude_agents(self) -> None:
        rel_exclude = git("rev-parse", "--git-path", "info/exclude", cwd=self.repo)
        exclude = (self.repo / rel_exclude).resolve()
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text() if exclude.exists() else ""
        if ".agents" not in text:
            exclude.write_text(text.rstrip() + ("\n.agents\n" if text else ".agents\n"))

    def commits(self, task_id: str) -> int:
        span = f"{self.integ_branch}..{self.task_branch(task_id)}"
        return int(git("rev-list", "--count", span, cwd=self.repo) or 0)

    def changed_paths(self, task_id: str) -> list[str]:
        base = git("merge-base", self.integ_branch, self.task_branch(task_id), cwd=self.repo)
        out = git("diff", "--name-only", base, self.task_branch(task_id), cwd=self.repo)
        return [p for p in out.splitlines() if p]

    def land(self, task_id: str, check: str, regen: str | None, timeout_s: int) -> Landing:
        before = self.tip()
        task_dir = self.task_dir(task_id)
        self._cleanup_hooks(task_dir)
        rebase = subprocess.run(
            ["git", "rebase", "-q", self.integ_branch], cwd=task_dir, capture_output=True, text=True
        )
        if rebase.returncode != 0:
            git("rebase", "--abort", cwd=task_dir, check=False)
            return Landing("conflict", f"rebase onto {self.integ_branch} conflicted")
        git("merge", "-q", "--ff-only", self.task_branch(task_id), cwd=self.integ_dir)
        if regen:
            ok, out = run_shell(regen, self.integ_dir, timeout_s)
            if not ok:
                self._reset(before)
                return Landing("failed", f"regen failed: {out[-300:]}")
            git("add", "-A", cwd=self.integ_dir)
            if git("diff", "--cached", "--name-only", cwd=self.integ_dir):
                git("commit", "-q", "-m", f"regen after {task_id}", cwd=self.integ_dir)
        ok, out = run_shell(check, self.integ_dir, timeout_s)
        if not ok:
            self._reset(before)
            return Landing("failed", f"check failed after landing: {out[-300:]}")
        return Landing("landed", f"landed on {self.integ_branch}", self.tip())

    def _cleanup_hooks(self, task_dir: Path) -> None:
        hook_file = task_dir / ".agents" / "hooks.json"
        if hook_file.exists():
            hook_file.unlink()
            git("checkout", "-q", "--", ".agents/hooks.json", cwd=task_dir, check=False)
            try:
                (task_dir / ".agents").rmdir()
            except OSError:
                pass

    def _reset(self, sha: str) -> None:
        git("reset", "-q", "--hard", sha, cwd=self.integ_dir)

    def export(self, remote: str = "origin") -> str:
        git("push", "-q", "-u", remote, self.integ_branch, cwd=self.integ_dir)
        return self.integ_branch

    def remove(self, task_id: str) -> None:
        path = self.task_dir(task_id)
        git("worktree", "remove", "--force", str(path), cwd=self.repo, check=False)
        shutil.rmtree(path, ignore_errors=True)
        git("worktree", "prune", cwd=self.repo, check=False)

    def discard(self, task_ids: list[str]) -> None:
        for task_id in task_ids:
            self.remove(task_id)
            git("branch", "-q", "-D", self.task_branch(task_id), cwd=self.repo, check=False)
        git("worktree", "remove", "--force", str(self.integ_dir), cwd=self.repo, check=False)
        shutil.rmtree(self.integ_dir, ignore_errors=True)
        git("worktree", "prune", cwd=self.repo, check=False)
        git("branch", "-q", "-D", self.integ_branch, cwd=self.repo, check=False)


def default_home(plan_id: str) -> Path:
    root = Path(os.environ.get("DISPATCH_HOME", Path.home() / ".dispatch"))
    return root / plan_id
