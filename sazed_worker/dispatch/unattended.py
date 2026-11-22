"""The unattended boundary (seam 3): its refusals, its workspace, and the wrapper's commit."""

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from sazed_worker import config
from sazed_worker.agent_process import kill_group
from sazed_worker.config import BWRAP
from sazed_worker.dispatch import mcp, profiles
from sazed_worker.dispatch.halt import Halt
from sazed_worker.dispatch.plan import Plan, Task, resolve
from sazed_worker.dispatch.rules import edit_reason, folded, glob_match
from sazed_worker.dispatch.workspace import GitError, Landing, Workspace, git
from sazed_worker.jail import home_for, run_jailed

CHECK_TOOL = Path(__file__).resolve().parent / "check_tool.py"
PACKAGE = CHECK_TOOL.parents[2]
CHECK_SERVER = "check"
RUN_CHECK = f"mcp__{CHECK_SERVER}__run_check"
SHELL = "/bin/sh"
IDENTITY = ("sazed-dispatch", "dispatch@sazed.invalid")
ROOT_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": IDENTITY[0],
    "GIT_AUTHOR_EMAIL": IDENTITY[1],
    "GIT_COMMITTER_NAME": IDENTITY[0],
    "GIT_COMMITTER_EMAIL": IDENTITY[1],
}
SUMMARY_MAX = 200
PROBE_TIMEOUT_S = 30
SHIELDED = (".git", ".claude")
REGEN_NEVER_TOP = (".github",)
REGEN_NEVER_ANYWHERE = (".git", ".claude", ".gitattributes", ".gitmodules")
FARM = "node_modules"
FROM = {"cache": "the cache", "none": "nowhere"}
TRAVERSE_ONLY = 0o711
ROOT_ONLY = 0o700
PRIVATE_FILE = 0o600
SHARED_FILE = 0o644
CLAUDE_CONFIG = "claude-config"
TRANSCRIPTS = ("agent.jsonl", "agent.stderr")


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
    if plan.regen and not plan.regen_outputs:
        return "the plan runs regen but names no regen_outputs, the only paths regen may commit"
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
    if uid == 0:
        return f"the agent user {config.AGENT_USER} is uid 0, and agents never run as root"
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


def _traverse_only(*dirs: Path) -> None:
    """Dirs the agent passes through to a known path but cannot list."""
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(TRAVERSE_ONLY)


def _root_git_env() -> dict[str, str]:
    """Root's git sees no system or global config, so only the repository's own config, which
    the agent cannot write, can define a filter, a merge driver or a hook path."""
    return dict(os.environ) | ROOT_GIT_ENV


def _git_z(*args: str, cwd: Path, guard: tuple[str, ...]) -> list[str]:
    proc = subprocess.run(
        ["git", *guard, *args], cwd=cwd, capture_output=True, env=_root_git_env()
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[:400]
        raise GitError(f"git {' '.join(args)}: {detail}")
    return [p for p in proc.stdout.decode(errors="surrogateescape").split("\0") if p]


def _changed(d: Path, guard: tuple[str, ...]) -> list[str]:
    """Paths changed against HEAD or new and not ignored, relative to d."""
    changed = _git_z("diff", "--name-only", "-z", "HEAD", cwd=d, guard=guard)
    changed += _git_z("ls-files", "--others", "--exclude-standard", "-z", cwd=d, guard=guard)
    return list(dict.fromkeys(changed))


def _storable(root: Path, rel: str) -> bool:
    """A path git can take: deleted, a regular file or a symlink, under no symlinked folder."""
    if rel.endswith("/") or ".git" in {folded(p) for p in PurePosixPath(rel).parts}:
        return False
    parent = root
    for part in PurePosixPath(rel).parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            return False
    try:
        mode = (root / rel).lstat().st_mode
    except FileNotFoundError:
        return True
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _regen_may_write(rel: str) -> bool:
    parts = tuple(folded(p) for p in PurePosixPath(rel).parts)
    return parts[0] not in REGEN_NEVER_TOP and not set(parts) & set(REGEN_NEVER_ANYWHERE)


def _commit(d: Path, guard: tuple[str, ...], paths: list[str], message: str) -> None:
    env = _root_git_env()
    git("--literal-pathspecs", "add", "--", *paths, cwd=d, guard=guard, env=env)
    git(
        "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message,
        cwd=d, guard=guard, env=env,
    )  # fmt: skip


class UnattendedWorkspace(Workspace):
    """A Workspace whose worktrees the agent user writes, whose checks run in the untrusted jail,
    and whose git calls in those worktrees never read their .git file, hooks or fsmonitor."""

    def __init__(
        self, repo: Path, plan_id: str, home: Path, regen_outputs: Sequence[str] = ()
    ):
        super().__init__(repo, plan_id, home)
        self.regen_outputs = list(regen_outputs)
        self.halt: Halt | None = None

    def guard(self, d: Path) -> tuple[str, ...]:
        return (
            f"--git-dir={self._admin(d)}",
            f"--work-tree={d}",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
        )

    def git_env(self) -> dict[str, str]:
        return _root_git_env()

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

    def seal(self) -> None:
        """Close the dispatch home to the agent: it may pass through, never list or read."""
        _traverse_only(self.home.parent, self.home)
        board = self.home / "board"
        if board.is_dir():
            board.chmod(ROOT_ONLY)
        for cache in ("profiles.json", "mcp-servers.json"):
            if (self.home.parent / cache).is_file():
                (self.home.parent / cache).chmod(PRIVATE_FILE)

    def _prepare(self, d: Path) -> None:
        """Make d writable by the agent, with .git and .claude left to root and a link folder
        rebuilt from the clone, so nothing an earlier jail wrote under node_modules survives."""
        (d / ".claude").mkdir(exist_ok=True)
        if stat.S_ISREG((d / ".git").lstat().st_mode):
            (d / ".git").chmod(SHARED_FILE)
        farm = d / FARM
        if farm.is_symlink() or farm.is_file():
            farm.unlink()
        elif farm.exists():
            shutil.rmtree(farm)
        shared = self.repo / FARM
        if shared.is_dir():
            farm.mkdir()
            for entry in shared.iterdir():
                (farm / entry.name).symlink_to(entry)
        _give(d, SHIELDED)

    def create(self, task_id: str) -> Path:
        home = self.home
        _traverse_only(home, home / "wt", home / "runs", home / "scratch", home / "scratch" / "wt")
        path = super().create(task_id)
        self._prepare(path)
        jail_home = home_for(home)
        jail_home.mkdir(exist_ok=True)
        _give(jail_home)
        _fresh(self.scratch(f"wt/{task_id}"))
        return path

    def run_code(self, command: str, cwd: Path, timeout_s: int = 1200) -> tuple[int, str]:
        _traverse_only(self.home, self.home / "scratch")
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
            self._stoppable if self.halt else None,
        )
        if code is None:
            return -1, err.strip()[-1500:]
        return code, (out + err)[-1500:]

    def _stoppable(self, proc: subprocess.Popen):
        assert self.halt
        return self.halt.over(lambda _: kill_group(proc))

    def _cleanup_hooks(self, task_dir: Path) -> None:
        """Nothing: agy never runs unattended, and root never follows a path the agent made."""

    def _scrub(self, d: Path) -> None:
        guard, env = self.guard(d), self.git_env()
        git("reset", "-q", "--hard", "HEAD", cwd=d, guard=guard, env=env)
        git("clean", "-ffdxq", "-e", f"/{FARM}", cwd=d, guard=guard, env=env)

    def land(self, task_id: str, check: str, regen: str | None, timeout_s: int):
        self._scrub(self.integ_dir)
        return super().land(task_id, check, regen, timeout_s)

    def _commit_regen(self, task_id: str, before: str) -> Landing | None:
        """Commit regen output inside regen_outputs only; anything else fails the landing."""
        d = self.integ_dir
        guard = self.guard(d)
        changed = _changed(d, guard)
        refused = [
            rel
            for rel in changed
            if not (
                _storable(d, rel)
                and _regen_may_write(rel)
                and any(glob_match(rel, g) for g in self.regen_outputs)
            )
        ]
        if refused:
            self._reset(before)
            self._scrub(d)
            line = f"regen changed paths outside regen_outputs: {', '.join(refused[:10])}"
            return Landing("failed", line, reason="regen_failed")
        if changed:
            _commit(d, guard, changed, f"regen after {task_id}")
        self._scrub(d)
        return None

    def prepare_run(self, task_id: str, check: str, timeout_s: int, run_dir: Path) -> Path:
        """Write the run_check spec, the MCP config naming it, the agent's own Claude config
        folder and private transcripts; return the MCP config."""
        _traverse_only(run_dir)
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
            written.chmod(SHARED_FILE)
        claude_config = self.claude_config(run_dir)
        shutil.rmtree(claude_config, ignore_errors=True)
        claude_config.mkdir()
        _give(claude_config)
        for name in TRANSCRIPTS:
            os.close(os.open(run_dir / name, os.O_WRONLY | os.O_CREAT, PRIVATE_FILE))
            (run_dir / name).chmod(PRIVATE_FILE)
        return path

    def claude_config(self, run_dir: Path) -> Path:
        return run_dir / CLAUDE_CONFIG

    def commit_owned(self, task: Task, summary: str) -> tuple[list[str], list[str]]:
        """Commit what changed inside the task's owns under the fixed identity, then discard the
        rest; return (committed, discarded) paths."""
        d = self.task_dir(task.id)
        guard = self.guard(d)
        try:
            owned, strays = [], []
            for rel in _changed(d, guard):
                inside = _storable(d, rel) and edit_reason(rel, task.owns, str(d)) is None
                (owned if inside else strays).append(rel)
            if owned:
                lines = summary.strip().splitlines() or ["done"]
                _commit(d, guard, owned, f"{task.id}: {lines[0][:SUMMARY_MAX]}")
        finally:
            self._scrub(d)
        return owned, strays
