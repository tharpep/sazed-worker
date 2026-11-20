"""The unattended boundary (seam 3, PR B): refusals, run_check, the jail, the wrapper's commit."""

import io
import json
import os
import resource
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sazed_worker import config as worker_config
from sazed_worker import jail
from sazed_worker import run as run_module
from sazed_worker.adapters import claude
from sazed_worker.config import BWRAP
from sazed_worker.dispatch import check_tool, integrate, mcp, profiles, unattended
from sazed_worker.dispatch.__main__ import main
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.plan import Plan, Task
from sazed_worker.dispatch.run_agent import run_task
from sazed_worker.dispatch.unattended import UnattendedWorkspace, plan_refusal
from sazed_worker.dispatch.workspace import git
from tests import registry

FAKE = Path(__file__).resolve().parents[1] / "sazed_worker" / "dispatch" / "fake_agent.py"
CHECK = "sh check.sh"
SHELL_VARS = {"PWD", "SHLVL", "_", "OLDPWD"}
JAIL_ENV = {
    "HOME", "USER", "PATH", "LANG", "GIT_TERMINAL_PROMPT",
    "PYTHONPYCACHEPREFIX", "RUFF_CACHE_DIR", "XDG_CACHE_HOME",
}  # fmt: skip


def _jail_works() -> bool:
    if not Path(BWRAP).exists():
        return False
    probe = [BWRAP, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "--unshare-net", "--unshare-pid", "--", "/bin/true"]  # fmt: skip
    return subprocess.run(probe, capture_output=True).returncode == 0


JAIL = _jail_works()
needs_jail = unittest.skipUnless(JAIL, "needs a working bubblewrap (bwrap) jail")
needs_root = unittest.skipUnless(
    os.geteuid() == 0 and JAIL and worker_config.AGENT_UID != 0,
    "needs root, bubblewrap and SAZED_AGENT_USER naming a user other than root",
)


def _plan(check: str = CHECK) -> Plan:
    return Plan(
        id="p1",
        check=check,
        tasks=[
            Task(id="api", goal="build the api", done="api file", owns=["src/api/**"]),
            Task(id="ui", goal="build the ui", done="ui file", owns=["src/ui/**"],
                 depends_on=["api"]),
            Task(id="docs", goal="write docs", done="docs file", owns=["docs/**"]),
        ],
    )  # fmt: skip


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seam3b-"))
        self.tmp.chmod(0o755)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["config", "commit.gpgsign", "false"],
        ):
            git(*args, cwd=self.repo)
        (self.repo / "check.sh").write_text("! grep -rqs --include=*.txt FAIL .\n")
        (self.repo / "README.md").write_text("# repo\n")
        git("add", "check.sh", "README.md", cwd=self.repo)
        git("commit", "-q", "-m", "seed", cwd=self.repo)
        self.env = {
            "DISPATCH_HOME": str(self.tmp / "home"),
            "DISPATCH_CLAUDE": f"{sys.executable} {FAKE}",
            "DISPATCH_FAKE_MODES": "{}",
        }
        old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(self.restore, old)
        registry.install()
        self.addCleanup(setattr, mcp, "_active", ({}, "none"))
        mcp._active = ({}, "sazed")
        self.home = self.tmp / "home" / "p1"
        self.ws = UnattendedWorkspace(self.repo, "p1", self.home)
        self.board = Board(self.home)
        self.config = {"unattended": True}
        hard = resource.getrlimit(resource.RLIMIT_NPROC)[1]
        patcher = mock.patch.object(jail, "MAX_TEST_PROCESSES", hard)
        patcher.start()
        self.addCleanup(patcher.stop)

    def restore(self, old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def modes(self, **modes) -> None:
        """The fake agent's modes reach it through the one env channel the minimal env keeps."""
        extra = claude.extra_env() | {"DISPATCH_FAKE_MODES": json.dumps(modes)}
        patcher = mock.patch.object(claude, "extra_env", return_value=extra)
        patcher.start()
        self.addCleanup(patcher.stop)

    def as_unprivileged_agent(self) -> None:
        """Without root the launch cannot switch user; the tests' agent user is this user."""
        patcher = mock.patch.object(run_module, "as_agent", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def agent_events(self, task: str) -> list[dict]:
        log = self.home / "runs" / task / "agent.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    def check_results(self, task: str) -> list[dict]:
        return [
            block
            for e in self.agent_events(task)
            if e.get("type") == "user"
            for block in e["message"]["content"]
            if block.get("type") == "tool_result"
        ]


class Refusals(Case):
    def test_profiles_from_the_cache_are_refused(self):
        profiles._active = (profiles._active[0], "cache")
        self.assertEqual(
            plan_refusal(_plan(), self.config),
            "an unattended run needs agent profiles fresh from Sazed, "
            "and these came from the cache",
        )

    def test_an_mcp_registry_from_the_cache_or_nowhere_is_refused(self):
        for source, where in (("cache", "the cache"), ("none", "nowhere")):
            mcp._active = ({}, source)
            with self.subTest(source=source):
                self.assertEqual(
                    plan_refusal(_plan(), self.config),
                    f"an unattended run needs the MCP registry fresh from Sazed, "
                    f"and it came from {where}",
                )

    def test_an_agy_task_is_refused(self):
        plan = Plan(id="p1", check=CHECK,
                    tasks=[Task(id="a", goal="g", done="d", owns=["src/**"], profile="flash")])
        self.assertEqual(
            plan_refusal(plan, self.config),
            "task a runs on agy, which keeps a shell an unattended run cannot remove",
        )

    def test_a_task_with_no_check_is_refused(self):
        plan = Plan(id="p1", tasks=[Task(id="r", goal="g", done="d", profile="sweep")])
        self.assertEqual(
            plan_refusal(plan, self.config), "task r has no check for run_check to run"
        )

    def test_a_clean_plan_passes(self):
        self.assertIsNone(plan_refusal(_plan(), self.config))

    @unittest.skipIf(os.geteuid() == 0, "the refusal is for a caller that is not root")
    def test_not_root_is_refused(self):
        self.assertEqual(
            unattended.host_refusal(self.ws),
            "an unattended run must start as root, so that its agents run as the agent user",
        )

    def cli(self, *args: str) -> tuple[int, str]:
        plan_file = self.tmp / "plan.json"
        plan_file.write_text(json.dumps({
            "id": "p1", "check": CHECK,
            "tasks": [{"id": "api", "goal": "g", "done": "d", "owns": ["src/api/**"]}],
        }))  # fmt: skip
        out = io.StringIO()
        with (
            redirect_stdout(out),
            mock.patch.object(profiles, "load"),
            mock.patch.object(mcp, "load"),
        ):
            code = main([*args, str(plan_file), "--repo", str(self.repo), "--unattended"])
        return code, out.getvalue()

    def test_the_cli_prints_one_sentence_and_its_reason_and_launches_nothing(self):
        mcp._active = ({}, "cache")
        for command in ("init", "run", "land", "go"):
            with self.subTest(command=command):
                code, out = self.cli(command)
                self.assertEqual(code, 1)
                self.assertEqual(
                    out.strip(),
                    "refused: an unattended run needs the MCP registry fresh from Sazed, "
                    "and it came from the cache (reason: refused)",
                )
        self.assertFalse((self.home / "integ").exists())
        self.assertFalse((self.home / "wt").exists())

    @unittest.skipIf(os.geteuid() == 0, "the refusal is for a caller that is not root")
    def test_the_cli_refuses_a_caller_that_is_not_root(self):
        code, out = self.cli("run")
        self.assertEqual(
            (code, out.strip()),
            (1, "refused: an unattended run must start as root, so that its agents run as the "
                "agent user (reason: refused)"),
        )  # fmt: skip

    def test_export_and_board_never_ask_for_root(self):
        mcp._active = ({}, "cache")
        code, out = self.cli("board")
        self.assertEqual(code, 0, out)


class CheckToolProtocol(Case):
    SPEC = {"check": "exit 3", "cwd": "/", "root": "/", "writable": [], "read_only": [],
            "timeout_s": 30, "env": {}}  # fmt: skip

    def call(self, message: dict, ran=(0, "ok\n", "")) -> dict | None:
        with mock.patch.object(check_tool, "run_jailed", return_value=ran) as jailed:
            reply = check_tool.handle(message, self.SPEC)
        self.jailed = jailed
        return reply

    def test_it_lists_one_tool_that_takes_no_arguments(self):
        tools = self.call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["run_check"])
        self.assertEqual(
            tools[0]["inputSchema"],
            {"type": "object", "properties": {}, "additionalProperties": False},
        )

    def test_a_passing_check_is_a_result_and_the_command_comes_from_the_spec(self):
        reply = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "run_check", "arguments": {}}})  # fmt: skip
        text = {"type": "text", "text": "check passed (exit 0)\nok\n"}
        self.assertEqual(reply["result"], {"content": [text], "isError": False})
        self.assertEqual(self.jailed.call_args.args[0], ["/bin/sh", "-c", "exit 3"])

    def test_a_failing_check_is_a_tool_error_with_its_output(self):
        reply = self.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "run_check"}}, ran=(1, "", "E402 boom\n"))
        self.assertTrue(reply["result"]["isError"])
        text = reply["result"]["content"][0]["text"]
        self.assertEqual(text, "check failed (exit 1)\nE402 boom\n")

    def test_arguments_are_refused_and_nothing_runs(self):
        reply = self.call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                           "params": {"name": "run_check", "arguments": {"cmd": "id"}}})
        self.assertEqual(reply["result"]["content"][0]["text"], "run_check takes no arguments")
        self.assertTrue(reply["result"]["isError"])
        self.jailed.assert_not_called()

    def test_a_hung_check_is_stopped_and_reported_as_failed(self):
        reply = self.call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                           "params": {"name": "run_check"}}, ran=(None, "", "sh timed out"))
        self.assertEqual(
            reply["result"]["content"][0]["text"], "check failed: it was stopped after 30 s"
        )

    def test_notifications_get_no_reply_and_unknown_methods_an_error(self):
        self.assertIsNone(self.call({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        reply = self.call({"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_over_stdio_a_failing_check_comes_back_as_a_tool_error(self):
        spec = self.tmp / "check.json"
        spec.write_text(json.dumps(self.SPEC | {"cwd": str(self.tmp), "root": str(self.tmp)}))
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "run_check", "arguments": {}}},
        ]  # fmt: skip
        proc = subprocess.run(
            [sys.executable, "-I", str(unattended.CHECK_TOOL), "--spec", str(spec)],
            input="".join(json.dumps(m) + "\n" for m in lines),
            capture_output=True, text=True, timeout=60,
            env={"SAZED_AGENT_USER": worker_config.AGENT_USER, "PATH": os.environ["PATH"]},
        )  # fmt: skip
        if os.geteuid() == 0:
            self.assertIn("never as root", proc.stderr)
            return
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(replies[1]["result"]["isError"])


class WrapperCommit(Case):
    def setUp(self):
        super().setUp()
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {self.tmp / 'hook-ran'}\n")
        hook.chmod(0o755)
        integrate.init(_plan(), self.ws, self.board, "main", self.config)
        self.wt = self.ws.create("api")

    def test_only_owned_paths_are_committed_under_the_fixed_identity_and_strays_go(self):
        (self.wt / "src" / "api").mkdir(parents=True)
        (self.wt / "src" / "api" / "a.txt").write_text("api\n")
        (self.wt / "README.md").write_text("strayed\n")
        (self.wt / "stray.txt").write_text("stray\n")
        (self.wt / "src" / "api" / "link").symlink_to("/etc/hostname")
        task = _plan().tasks[0]
        committed, discarded = self.ws.commit_owned(task, "built the api\nsecond line")
        self.assertEqual(sorted(committed), ["src/api/a.txt", "src/api/link"])
        self.assertEqual(sorted(discarded), ["README.md", "stray.txt"])
        log = git("log", "-1", "--format=%an <%ae>|%cn <%ce>|%s", "dispatch/p1/api", cwd=self.repo)
        self.assertEqual(
            log,
            "sazed-dispatch <dispatch@sazed.invalid>|sazed-dispatch <dispatch@sazed.invalid>|"
            "api: built the api",
        )
        self.assertEqual((self.wt / "README.md").read_text(), "# repo\n")
        self.assertFalse((self.wt / "stray.txt").exists())
        self.assertFalse((self.tmp / "hook-ran").exists())
        self.assertEqual(git("status", "--porcelain", cwd=self.repo), "")

    def test_protected_paths_and_paths_under_a_symlink_are_strays_even_inside_owns(self):
        task = Task(id="api", goal="g", done="d", owns=["**"])
        (self.wt / ".claude" / "settings.json").write_text("{}")
        (self.wt / "src").mkdir()
        (self.wt / "src" / "x").symlink_to(self.tmp)
        (self.tmp / "outside.txt").write_text("x\n")
        (self.wt / "src" / "ok.txt").write_text("ok\n")
        committed, discarded = self.ws.commit_owned(task, "s")
        self.assertEqual(committed, ["src/ok.txt", "src/x"])
        self.assertIn(".claude/settings.json", discarded)
        self.assertFalse((self.wt / ".claude" / "settings.json").exists())

    def test_a_rewritten_dot_git_file_never_steers_the_wrapper(self):
        decoy = self.tmp / "decoy"
        git("init", "-q", "-b", "main", str(decoy), cwd=self.tmp)
        (self.wt / ".git").write_text(f"gitdir: {decoy / '.git'}\n")
        (self.wt / "src" / "api").mkdir(parents=True)
        (self.wt / "src" / "api" / "a.txt").write_text("api\n")
        self.ws.commit_owned(_plan().tasks[0], "s")
        self.assertEqual(self.ws.commits("api"), 1)
        self.assertEqual(git("rev-list", "--all", cwd=decoy, check=False), "")

    def test_nothing_inside_owns_commits_nothing(self):
        (self.wt / "README.md").write_text("strayed\n")
        self.assertEqual(self.ws.commit_owned(_plan().tasks[0], "s"), ([], ["README.md"]))
        self.assertEqual(self.ws.commits("api"), 0)


@needs_jail
class Jail(Case):
    def setUp(self):
        super().setUp()
        self.ws.open("main")

    def test_the_jail_sees_only_its_own_environment_and_cannot_write_outside(self):
        os.environ["SAZED_WORKER_KEY"] = "worker-secret"
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-secret"
        self.addCleanup(os.environ.pop, "SAZED_WORKER_KEY")
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_OAUTH_TOKEN")
        integ = self.ws.integ_dir
        script = (
            "python3 -c 'import os; print(\"ENV\", \" \".join(sorted(os.environ)))'; "
            f"touch {integ}/ok && echo wrote-worktree; "
            f"touch {self.repo}/x 2>/dev/null || echo repo-read-only; "
            f"touch {self.home}/x 2>/dev/null || echo home-read-only; "
            f"echo x > {integ}/.git 2>/dev/null || echo dot-git-read-only; "
            f"touch {integ}/.claude/settings.json 2>/dev/null || echo dot-claude-read-only; "
            "python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 53), 2)' "
            "2>/dev/null || echo no-network; "
            "git log -1 --format=%s"
        )
        code, out = self.ws.run_code(script, integ, 60)
        self.assertEqual(code, 0, out)
        names = set(next(line for line in out.splitlines() if line.startswith("ENV")).split()[1:])
        self.assertEqual(names - SHELL_VARS, JAIL_ENV)
        for line in ("wrote-worktree", "repo-read-only", "home-read-only", "dot-git-read-only",
                     "dot-claude-read-only", "no-network", "seed"):
            self.assertIn(line, out.splitlines())
        self.assertNotIn("secret", out)

    def test_a_python_check_writes_its_caches_to_scratch(self):
        (self.ws.integ_dir / "mod.py").write_text("X = 1\n")
        code, out = self.ws.run_code(
            "python3 -m compileall -q mod.py && test ! -e __pycache__ && echo $RUFF_CACHE_DIR",
            self.ws.integ_dir,
            60,
        )
        self.assertEqual(code, 0, out)
        self.assertTrue(list((self.home / "scratch" / "integ" / "pycache").rglob("*.pyc")))
        self.assertIn(str(self.home / "scratch" / "integ" / "ruff"), out)

    def test_node_modules_is_a_farm_of_links_with_a_writable_top_level(self):
        shared = self.repo / "node_modules" / "pkg"
        shared.mkdir(parents=True)
        (shared / "index.js").write_text("module.exports = 1\n")
        (self.repo / "node_modules" / ".bin").mkdir()
        wt = self.ws.create("api")
        farm = wt / "node_modules"
        self.assertTrue(farm.is_dir() and not farm.is_symlink())
        self.assertEqual(sorted(p.name for p in farm.iterdir()), [".bin", "pkg"])
        self.assertTrue((farm / "pkg").is_symlink())
        code, out = self.ws.run_code(
            "cat node_modules/pkg/index.js && mkdir node_modules/.vite-temp && "
            "{ touch node_modules/pkg/poison 2>/dev/null && echo poisoned || echo shared-ro; }",
            self.ws.integ_dir,
            60,
        )
        self.assertEqual(code, 0, out)
        self.assertIn("module.exports = 1", out)
        self.assertIn("shared-ro", out)

    def test_a_hung_check_is_stopped_at_its_timeout(self):
        spec = json.loads(self.ws.check_tool("t", "sleep 60", 2, self.tmp).with_name(
            "check.json").read_text())
        self.ws.create("t")
        passed, text = check_tool.run(spec | {"timeout_s": 2})
        self.assertEqual((passed, text), (False, "check failed: it was stopped after 2 s"))


@needs_jail
@unittest.skipIf(os.geteuid() == 0, "without root the agent is this user; AsRoot covers root")
class UnattendedPlan(Case):
    """S3-21: the plan run unattended with the fake agent's no-shell modes, as far as no root."""

    def setUp(self):
        super().setUp()
        self.as_unprivileged_agent()

    def test_the_plan_lands_owned_work_and_reports_each_outcome(self):
        Outcomes.go_and_check_outcomes(self)

    def test_run_check_counts_toward_the_tool_cap(self):
        self.modes(api="noshell")
        plan = _plan()
        object.__setattr__(plan.tasks[0], "overrides", {"max_tool_calls": 3})
        integrate.init(plan, self.ws, self.board, "main", self.config)
        row = run_task(plan, plan.tasks[0], self.ws, self.board, self.config)
        self.assertEqual((row["state"], row["meta"]["reason"]), ("failed", "tool_cap"))


class Outcomes:
    """The plan both outcome tests run: unprivileged, and as root with a real user switch."""

    def go_and_check_outcomes(self: Case) -> None:
        self.modes(api="noshell", ui="noshell-fix", docs="noshell-nochange")
        states = integrate.go(_plan(), self.ws, self.board, "main", self.config)
        self.assertEqual(states, {"api": "landed", "ui": "landed", "docs": "failed"})
        docs = self.board.rows("docs")[-1]
        self.assertEqual(docs["line"], "changed nothing inside owns: docs noshell-nochange")
        self.assertEqual((docs["meta"]["reason"], docs["meta"]["discarded"]),
                         ("no_change", ["stray.txt"]))
        api_done = [r for r in self.board.rows("api") if r["state"] == "done"][-1]
        self.assertEqual(api_done["meta"]["discarded"], ["README.md"])
        tree = git("ls-tree", "-r", "--name-only", "integ/p1", cwd=self.repo).splitlines()
        self.assertEqual(sorted(tree), ["README.md", "check.sh", "src/api/api.txt",
                                        "src/ui/ui.txt"])
        self.assertEqual(git("show", "integ/p1:README.md", cwd=self.repo), "# repo")
        subjects = git("log", "--format=%an|%s", "integ/p1", cwd=self.repo).splitlines()
        self.assertEqual(subjects[:2], ["sazed-dispatch|ui: ui noshell-fix",
                                        "sazed-dispatch|api: api noshell"])
        ui = self.check_results("ui")
        self.assertEqual([r["is_error"] for r in ui], [True, False])
        self.assertTrue(ui[0]["content"][0]["text"].startswith("check failed (exit 1)"))
        self.assertTrue(ui[1]["content"][0]["text"].startswith("check passed (exit 0)"))


@needs_root
class AsRoot(Case):
    """Run as root in WSL with SAZED_AGENT_USER naming an ordinary user (never in CI)."""

    def test_preflight_passes_and_the_task_runs_as_the_agent(self):
        unattended.preflight(_plan(), self.config, self.ws)
        self.modes(api="noshell")
        plan = _plan("id -u && sh check.sh")
        integrate.init(plan, self.ws, self.board, "main", self.config)
        row = run_task(plan, plan.tasks[0], self.ws, self.board, self.config)
        self.assertEqual(row["state"], "done", row)
        text = self.check_results("api")[0]["content"][0]["text"]
        self.assertIn(f"\n{worker_config.AGENT_UID}\n", text)
        self.assertEqual(integrate.land(plan, self.ws, self.board, self.config)[0]["state"],
                         "landed")
        self.assertEqual(git("status", "--porcelain", cwd=self.ws.integ_dir,
                             guard=self.ws.guard(self.ws.integ_dir)), "")

    def test_the_landing_check_runs_as_the_agent_in_the_untrusted_jail(self):
        self.ws.open("main")
        code, out = self.ws.run_code(f"id -u; touch {self.repo}/x || echo repo-read-only",
                                     self.ws.integ_dir, 60)
        self.assertEqual(out.splitlines()[0], str(worker_config.AGENT_UID))
        self.assertIn("repo-read-only", out)

    def test_the_plan_lands_owned_work_as_the_agent_user(self):
        Outcomes.go_and_check_outcomes(self)

    def test_a_home_the_agent_cannot_reach_is_refused(self):
        self.tmp.chmod(0o700)
        self.addCleanup(self.tmp.chmod, 0o755)
        self.assertEqual(
            unattended.host_refusal(self.ws),
            f"the agent user {worker_config.AGENT_USER} cannot reach {self.home}",
        )


if __name__ == "__main__":
    unittest.main()
