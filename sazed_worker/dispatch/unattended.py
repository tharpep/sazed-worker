"""The unattended boundary (seam 3): its refusals, its workspace, and the wrapper's commit."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from sazed_worker import config
from sazed_worker.config import BWRAP
from sazed_worker.dispatch import mcp, profiles
from sazed_worker.dispatch.plan import Plan, Task, resolve
from sazed_worker.dispatch.rules import edit_reason
from sazed_worker.dispatch.workspace import GitError, Workspace, git
from sazed_worker.jail import home_for, run_jailed

CHECK_TOOL = Path(__file__).resolve().parent / "check_tool.py"
PACKAGE = CHECK_TOOL.parents[2]
CHECK_SERVER = "check"
RUN_CHECK = f"mcp__{CHECK_SERVER}__run_check"
SHELL = "/bin/sh"
IDENTITY = ("sazed-dispatch", "dispatch@sazed.invalid")
SUMMARY_MAX = 200
PROBE_TIMEOUT_S = 30
SHIELDED = (".git", ".claude")
FARM = "node_modules"
FROM = {"cache": "the cache", "none": "nowhere"}


class Refused(RuntimeError):
    def __init__(self, sentence: str):
        super().__init__(sentence)
        self.reason = "refused"


def plan_refusal(plan: Plan, config_: dict[str, Any]) -> str | None:
    """Why this plan cannot run unattended, read from the plan and its rules alone."""
    source = profiles.current()[1]
    if source != "sazed":
        return (
            "an unattended run needs agent profiles fresh from Sazed, "
            f"and these came from {FROM.get(source, source)}"
        )
    mcp_source = mcp.current()[1]
    if mcp_source != "sazed":
        return (
            "an unattended run needs the MCP registry fresh from Sazed, "
            f"and it came from {FROM.get(mcp_source, mcp_source)}"
        )
    for task in plan.tasks:
        if resolve(task, config_).get("engine") == "agy":
            return (
                f"task {task.id} runs on agy, which keeps a shell an unattended run cannot remove"
            )
        if not (task.check or plan.check or config_.get("check")):
            return f"task {task.id} has no check for run_check to run"
    return None


def _can_reach(path: Path, uid: int, gids: set[int]) -> bool:
    for part in [path, *path.parents]:
        st = os.stat(part)
        if st.st_uid == uid:
            bit = stat.S_IXUSR if stat.S_ISDIR(st.st_mode) else stat.S_IRUSR
        elif st.st_gid in gids:
            bit = stat.S_IXGRP if stat.S_ISDIR(st.st_mode) else stat.S_IRGRP
        else:
            bit = stat.S_IXOTH if stat.S_ISDIR(st.st_mode) else stat.S_IROTH
        if not st.st_mode & bit:
            return False
    return True


def host_refusal(ws: Workspace) -> str | None:
    """Why this host cannot start an unattended run: root, bubblewrap, the agent user, paths."""
    if os.geteuid() != 0:
        return "an unattended run must start as root, so that its agents run as the agent user"
    if not Path(BWRAP).exists():
        return f"an unattended run needs bubblewrap at {BWRAP}, and it is not installed"
    try:
        uid, gid = config.AGENT_UID, config.AGENT_GID
    except KeyError:
        return f"an unattended run needs the agent user {config.AGENT_USER}, and there is none"
    gids = set(os.getgrouplist(config.AGENT_USER, gid))
    ws.home.mkdir(parents=True, exist_ok=True)
    for path in (ws.home, ws.repo, PACKAGE, Path(sys.executable)):
        if not _can_reach(path.resolve(), uid, gids):
            return f"the agent user {config.AGENT_USER} cannot reach {path}"
    code, _, err = run_jailed(
        ["/bin/true"], ws.home, ws.home, PROBE_TIMEOUT_S, untrusted_writable=[]
    )
    if code != 0:
        return f"the jail does not start as the agent user: {err.strip()[-200:] or code}"
    return None


def preflight(plan: Plan, config_: dict[str, Any], ws: Workspace) -> None:
    sentence = plan_refusal(plan, config_) or host_refusal(ws)
    if sentence:
        raise Refused(sentence)


def check_env(scratch: Path) -> dict[str, str]:
    """The check's caches and HOME, all in scratch, so a read-only tree never stops it."""
    return {
        "HOME": str(scratch / "home"),
        "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
        "RUFF_CACHE_DIR": str(scratch / "ruff"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
    }


def _give(top: Path, skip: tuple[str, ...] = ()) -> None:
    """Chown top and everything under it to the agent, except skipped top-level names and any
    file with a second hard link, which may be a link to a file outside the tree."""
    uid, gid = config.AGENT_UID, config.AGENT_GID
    os.chown(top, uid, gid, follow_symlinks=False)
    for dirpath, dirnames, filenames in os.walk(top):
        if Path(dirpath) == top:
            dirnames[:] = [d for d in dirnames if d not in skip]
            filenames = [f for f in filenames if f not in skip]
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            st = path.lstat()
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                continue
            os.chown(path, uid, gid, follow_symlinks=False)


def _fresh(path: Path) -> Path:
    """An empty scratch folder for the agent, whose git trusts the root-owned repository."""
    shutil.rmtree(path, ignore_errors=True)
    (path / "home").mkdir(parents=True)
    (path / "home" / ".gitconfig").write_text("[safe]\n\tdirectory = *\n")
    _give(path)
    return path


def _git_z(*args: str, cwd: Path, guard: tuple[str, ...]) -> list[str]:
    proc = subprocess.run(["git", *guard, *args], cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[:400]
        raise GitError(f"git {' '.join(args)}: {detail}")
    return [p for p in proc.stdout.decode(errors="surrogateescape").split("\0") if p]


def _under_symlink(root: Path, rel: str) -> bool:
    parent = root
    for part in PurePosixPath(rel).parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            return True
    return False


class UnattendedWorkspace(Workspace):
    """A Workspace whose worktrees the agent user writes, whose checks run in the untrusted jail,
    and whose git calls in those worktrees never read their .git file, hooks or fsmonitor."""

    def guard(self, d: Path) -> tuple[str, ...]:
        return (
            f"--git-dir={self._admin(d)}",
            f"--work-tree={d}",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
        )

    def _admin(self, d: Path) -> Path:
        """The worktree's git directory, found from the main repository, never from d/.git."""
        common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.repo))
        want = os.path.realpath(d)
        for pointer in (common / "worktrees").glob("*/gitdir"):
            if os.path.realpath(os.path.dirname(pointer.read_text().strip())) == want:
                return pointer.parent
        raise GitError(f"{d} is not a worktree of {self.repo}")

    def scratch(self, name: str) -> Path:
        return self.home / "scratch" / name

    def shielded(self, d: Path) -> list[Path]:
        """Paths bound read-only in every jail that writes d."""
        return [self.repo, *(d / name for name in SHIELDED)]

    def _prepare(self, d: Path) -> None:
        """Make d writable by the agent, with .git and .claude left to root."""
        (d / ".claude").mkdir(exist_ok=True)
        farm = d / FARM
        shared = self.repo / FARM
        if shared.is_dir() and not farm.exists() and not farm.is_symlink():
            farm.mkdir()
            for entry in shared.iterdir():
                (farm / entry.name).symlink_to(entry)
        _give(d, SHIELDED)

    def create(self, task_id: str) -> Path:
        path = super().create(task_id)
        self._prepare(path)
        jail_home = home_for(self.home)
        jail_home.mkdir(exist_ok=True)
        _give(jail_home)
        _fresh(self.scratch(f"wt/{task_id}"))
        return path

    def run_code(self, command: str, cwd: Path, timeout_s: int = 1200) -> tuple[int, str]:
        scratch = _fresh(self.scratch("integ"))
        self._prepare(cwd)
        code, out, err = run_jailed(
            [SHELL, "-c", command],
            cwd,
            self.home,
            timeout_s,
            check_env(scratch),
            [cwd, scratch],
            self.shielded(cwd),
        )
        if code is None:
            return -1, err.strip()[-1500:]
        return code, (out + err)[-1500:]

    def _cleanup_hooks(self, task_dir: Path) -> None:
        """Nothing: agy never runs unattended, and root never follows a path the agent made."""

    def _scrub(self, d: Path) -> None:
        git("reset", "-q", "--hard", "HEAD", cwd=d, guard=self.guard(d))
        git("clean", "-ffdxq", "-e", f"/{FARM}", cwd=d, guard=self.guard(d))

    def land(self, task_id: str, check: str, regen: str | None, timeout_s: int):
        self._scrub(self.integ_dir)
        return super().land(task_id, check, regen, timeout_s)

    def check_tool(self, task_id: str, check: str, timeout_s: int, run_dir: Path) -> Path:
        """Write the run_check server's spec and the MCP config naming it; return the config."""
        worktree = self.task_dir(task_id)
        scratch = self.scratch(f"wt/{task_id}")
        spec = {
            "check": check,
            "cwd": str(worktree),
            "root": str(self.home),
            "writable": [str(worktree), str(scratch)],
            "read_only": [str(p) for p in self.shielded(worktree)],
            "timeout_s": timeout_s,
            "env": check_env(scratch),
        }
        spec_path = run_dir / "check.json"
        spec_path.write_text(json.dumps(spec, indent=2))
        server = {
            "command": sys.executable,
            "args": ["-I", str(CHECK_TOOL), "--spec", str(spec_path)],
            "env": {"SAZED_AGENT_USER": config.AGENT_USER},
        }
        path = run_dir / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {CHECK_SERVER: server}}, indent=2))
        for written in (spec_path, path):
            written.chmod(0o644)
        return path

    def commit_owned(self, task: Task, summary: str) -> tuple[list[str], list[str]]:
        """Commit what changed inside the task's owns under the fixed identity, then discard the
        rest; return (committed, discarded) paths."""
        d = self.task_dir(task.id)
        guard = self.guard(d)
        changed = _git_z("diff", "--name-only", "-z", "HEAD", cwd=d, guard=guard)
        changed += _git_z("ls-files", "--others", "--exclude-standard", "-z", cwd=d, guard=guard)
        owned, strays = [], []
        for rel in dict.fromkeys(changed):
            inside = (
                not rel.endswith("/")
                and ".git" not in PurePosixPath(rel).parts
                and not _under_symlink(d, rel)
                and edit_reason(rel, task.owns, str(d)) is None
            )
            (owned if inside else strays).append(rel)
        if owned:
            name, email = IDENTITY
            identity = {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
            }
            lines = summary.strip().splitlines() or ["done"]
            message = f"{task.id}: {lines[0][:SUMMARY_MAX]}"
            git("--literal-pathspecs", "add", "--", *owned, cwd=d, guard=guard)
            git(
                "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message,
                cwd=d, guard=guard, env=os.environ | identity,
            )  # fmt: skip
        self._scrub(d)
        return owned, strays
