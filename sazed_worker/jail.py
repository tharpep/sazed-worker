"""Commands that run repo-controlled code outside the agent's sandbox, jailed with bubblewrap.

Dependency installs (poetry, pnpm, npm, go mod download) and the checks (tsc and go vet load
package code; ruff and bash -n don't, but share the path) run before or after the agent, with
network. They run as the agent user inside bubblewrap, so they can't read the agent user's
home: the Claude session data in ~/.claude, agy's login in ~/.gemini, other jobs' workspaces.
They see a read-only system, their own workspace (writable) and a private /tmp, and no other
processes. The OAuth token never reaches them (agent_env gives it only to Claude).

The repo's tests (tests.py) run code the agent just wrote, so they get the untrusted mode: the
workspace read-only except a scratch copy and a throwaway cache (no cache a later step reads can be
poisoned), no network and an empty /run (no system sockets), and a cap on processes.
"""

import resource
import shutil
import subprocess
from pathlib import Path

from .agent_process import agent_env, as_agent, kill_group
from .config import AGENT_HOME, AGENT_USER, BWRAP


def home_for(root: Path) -> Path:
    """The jailed commands' HOME, inside the workspace (caches land there)."""
    return root / "home"


def run_jailed(
    cmd: list[str],
    cwd: Path,
    root: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    untrusted_writable: list[Path] | None = None,
) -> tuple[int | None, str, str]:
    """(exit code, stdout, stderr) of cmd run in the jail with `root` (the job's workspace)
    writable; exit code None if it couldn't run or finish.

    With `untrusted_writable` (the test run): only those paths are writable, there's no network
    and no /run, and the process count is capped."""
    if not Path(BWRAP).exists():
        return None, "", "bubblewrap isn't installed"
    home = home_for(root)
    if not home.is_dir():
        home.mkdir(parents=True)
        shutil.chown(home, AGENT_USER, AGENT_USER)
    jail = [
        BWRAP,
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", str(AGENT_HOME),  # hides ~/.claude, ~/.gemini and every other job
        *(
            ["--ro-bind", str(root), str(root)]
            + [arg for path in untrusted_writable for arg in ("--bind", str(path), str(path))]
            + ["--unshare-net", "--tmpfs", "/run"]
            if untrusted_writable is not None
            else ["--bind", str(root), str(root)]
        ),
        "--unshare-pid",
        "--unshare-ipc",
        "--die-with-parent",
        "--new-session",
        "--chdir", str(cwd),
        "--",
    ]  # fmt: skip
    full_env = agent_env("check") | {"HOME": str(home)} | (env or {})
    try:
        proc = subprocess.Popen(
            [*jail, *cmd],
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=_cap_processes if untrusted_writable is not None else None,
            **as_agent(),
        )
    except OSError as e:
        return None, "", str(e)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        return None, "", f"{Path(cmd[0]).name} timed out"
    return proc.returncode, out, err


MAX_TEST_PROCESSES = 512  # per the agent user; nothing else of the agent's runs during tests


def _cap_processes() -> None:
    # A fork bomb in a test hits this instead of CT 110; the timeout and process-group kill end it.
    resource.setrlimit(resource.RLIMIT_NPROC, (MAX_TEST_PROCESSES, MAX_TEST_PROCESSES))


def go_env(root: Path) -> dict[str, str]:
    """Go's environment for the checks and the test run: module and build caches in the workspace,
    no toolchain downloads (a go.mod "toolchain" line would fetch and run one), no go.mod
    edits."""
    return {
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=readonly",
        "GOPATH": str(root / "cache" / "go"),
        "GOCACHE": str(root / "cache" / "go-build"),
        "CGO_ENABLED": "0",
    }
