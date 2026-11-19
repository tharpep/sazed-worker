"""Dispatch reads agent profiles from Sazed, then its cache, else refuses, and checks evidence."""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from sazed_worker.dispatch import integrate, profiles
from sazed_worker.dispatch.__main__ import main
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import Mirror
from sazed_worker.dispatch.plan import Plan, PlanError, Task, load, resolve, validate
from sazed_worker.dispatch.run_agent import agent_command, run_task
from sazed_worker.dispatch.workspace import Workspace, git
from tests import registry
from tests.test_dispatch_mirror import Recorder

FAKE = Path(__file__).resolve().parents[1] / "sazed_worker" / "dispatch" / "fake_agent.py"

KEYS = ("model", "effort", "budget_usd", "timeout_s", "read_only", "evidence_required", "engine")
TABLE = {
    "smoke": ("haiku", "medium", 0.5, 600, True, False, "claude"),
    "sweep": ("haiku", "max", 0.5, 600, True, False, "claude"),
    "verify": ("haiku", "max", 0.5, 600, True, True, "claude"),
    "implement": ("sonnet", "high", 2.0, 1200, False, False, "claude"),
    "design": ("opus", "xhigh", 4.0, 1200, False, False, "claude"),
    "flash": ("gemini-3.8-flash-high", "high", 0.5, 1200, False, False, "agy"),
}

CHANGE_JOB = {
    "name": "change",
    "engine": "claude",
    "capability": "change",
    "model": "claude-sonnet-5-5",
    "effort": None,
    "budget_usd": None,
    "timeout_s": 1800,
    "max_tool_calls": 200,
    "evidence_required": False,
    "mcp": {},
    "read_only": False,
}
RESEARCH_JOB = CHANGE_JOB | {
    "name": "research_quick",
    "engine": "agy",
    "capability": "research",
    "model": "gemini-3.8-flash-high",
    "timeout_s": 300,
    "max_tool_calls": 30,
    "read_only": True,
}


class Registry(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("X-API-Key")))
        self.server.shape = self.headers.get("X-Sazed-Shape")
        body = json.dumps(self.server.profiles).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _row(name: str, **fields) -> dict:
    """A row in the shape Sazed served before migration 035: read_only, no capability."""
    return {
        "name": name,
        "model": "haiku",
        "effort": "low",
        "budget_usd": 0.1,
        "timeout_s": 60,
        "read_only": True,
        "evidence_required": False,
        "description": "",
        "updated_by": "t",
        "updated_at": "now",
        "version": 1,
    } | fields


def _task(profile: str, **kw) -> Task:
    return Task(id="t", goal="g", done="d", profile=profile, **kw)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-profiles-"))
        names = ("DISPATCH_HOME", "DISPATCH_SAZED_URL", "DISPATCH_SAZED_KEY", "DISPATCH_CLAUDE")
        saved = {k: os.environ.pop(k, None) for k in (*names, "DISPATCH_FAKE_MODES")}
        os.environ["DISPATCH_HOME"] = str(self.tmp / "home")
        self.addCleanup(self.restore, saved)
        self.addCleanup(setattr, profiles, "_active", profiles.current())
        registry.install()

    def restore(self, saved):
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def serve(self, handler, **attrs) -> str:
        server = HTTPServer(("127.0.0.1", 0), handler)
        server.seen = []
        for k, v in attrs.items():
            setattr(server, k, v)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server
        return f"http://127.0.0.1:{server.server_port}"


class Sources(Env):
    def test_profiles_come_from_sazed_when_reachable_and_land_in_the_cache(self):
        rows = [_row("verify", model="sonnet", effort="xhigh", evidence_required=True)]
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=rows)
        os.environ["DISPATCH_SAZED_KEY"] = "k1"
        table, source = profiles.load()
        self.assertEqual(source, "sazed")
        self.assertEqual(self.server.seen, [("/agents/profiles", "k1")])
        self.assertEqual(self.server.shape, "2")
        got = resolve(_task("verify"), {})
        self.assertEqual((got["model"], got["effort"]), ("sonnet", "xhigh"))
        self.assertEqual(got["profile_source"], "sazed")
        self.assertEqual(json.loads(profiles.cache_path().read_text()), rows)
        self.assertEqual(set(table), {"verify"})

    def test_the_cache_is_used_when_sazed_is_down(self):
        registry.write_cache(profiles.cache_path().parent, [_row("verify", model="opus")])
        os.environ["DISPATCH_SAZED_URL"] = "http://127.0.0.1:1"
        with contextlib.redirect_stderr(io.StringIO()):
            _, source = profiles.load()
        got = resolve(_task("verify"), {})
        self.assertEqual((source, got["model"], got["profile_source"]), ("cache", "opus", "cache"))

    def test_with_neither_sazed_nor_a_cache_dispatch_refuses(self):
        """Proves no built-in table stands in: with no Sazed and no cache, load raises and the
        command prints a refusal naming the cache path."""
        with self.assertRaises(profiles.ProfilesError):
            profiles.load()
        os.environ["DISPATCH_SAZED_URL"] = "http://127.0.0.1:1"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["profiles"]), 1)
        self.assertIn("refused: no agent profiles", out.getvalue())
        self.assertIn(str(profiles.cache_path()), out.getvalue())

    def test_the_dispatch_profiles_resolve_as_before_from_the_new_shape(self):
        """Proves the six dispatch profiles, served with capability and engine, resolve to the
        values and read_only the deleted built-in table held."""
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=registry.ROWS)
        profiles.load()
        for name, want in TABLE.items():
            with self.subTest(name=name):
                got = resolve(_task(name), {})
                self.assertEqual(tuple(got[k] for k in KEYS), want)

    def test_rows_without_capability_or_engine_follow_read_only_and_run_on_claude(self):
        """Proves a cache written before migration 035 still resolves: read_only comes from the
        row and the engine defaults to claude."""
        rows = [_row("verify", read_only=True), _row("implement", read_only=False)]
        registry.write_cache(profiles.cache_path().parent, rows)
        _, source = profiles.load()
        self.assertEqual(source, "cache")
        verify, implement = resolve(_task("verify"), {}), resolve(_task("implement"), {})
        self.assertEqual((verify["read_only"], verify["engine"]), (True, "claude"))
        self.assertEqual((implement["read_only"], implement["engine"]), (False, "claude"))

    def test_capability_decides_read_only_when_present(self):
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=[CHANGE_JOB, RESEARCH_JOB])
        profiles.load()
        self.assertFalse(resolve(_task("change"), {})["read_only"])
        self.assertTrue(resolve(_task("research_quick"), {})["read_only"])

    def test_a_job_profile_with_no_effort_or_budget_runs_uncapped(self):
        """Proves a NULL effort drops the effort flag and a NULL budget drops the budget cap,
        and that max_tool_calls is read from the row."""
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=[CHANGE_JOB, RESEARCH_JOB])
        profiles.load()
        change = resolve(_task("change"), {})
        self.assertEqual((change.get("effort"), change.get("budget_usd")), (None, None))
        self.assertEqual((change["max_tool_calls"], change["capability"]), (200, "change"))
        cmd = agent_command(change, Path("/tmp/s.json"), read_only=False)
        self.assertNotIn("--effort", cmd)
        self.assertNotIn("--max-budget-usd", cmd)
        agy = agent_command(resolve(_task("research_quick"), {}), Path("/tmp/s.json"), True)
        self.assertFalse(any(a.startswith("--effort") for a in agy))

    def test_workstream_defaults_fill_a_null_effort_and_budget(self):
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=[CHANGE_JOB])
        profiles.load()
        got = resolve(_task("change"), {"defaults": {"budget_usd": 1.0, "effort": "low"}})
        self.assertEqual((got["budget_usd"], got["effort"]), (1.0, "low"))
        cmd = agent_command(got, Path("/tmp/s.json"), read_only=False)
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "1.0")

    def test_capability_wins_when_it_disagrees_with_read_only(self):
        """Proves read_only is derived from capability, not copied from the row."""
        rows = [CHANGE_JOB | {"read_only": True}, RESEARCH_JOB | {"read_only": False}]
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=rows)
        profiles.load()
        self.assertFalse(resolve(_task("change"), {})["read_only"])
        self.assertTrue(resolve(_task("research_quick"), {})["read_only"])

    def test_an_empty_answer_from_sazed_keeps_the_cache(self):
        """Proves a 200 with [] counts as a failed fetch: the cache is used and not overwritten."""
        cached = [_row("verify", model="opus")]
        registry.write_cache(profiles.cache_path().parent, cached)
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=[])
        with contextlib.redirect_stderr(io.StringIO()):
            table, source = profiles.load()
        self.assertEqual((source, set(table)), ("cache", {"verify"}))
        self.assertEqual(json.loads(profiles.cache_path().read_text()), cached)

    def test_an_empty_or_malformed_cache_is_refused(self):
        for body in ("[]", "{}", "not json"):
            with self.subTest(body=body):
                profiles.cache_path().parent.mkdir(parents=True, exist_ok=True)
                profiles.cache_path().write_text(body)
                with self.assertRaisesRegex(profiles.ProfilesError, "unreadable"):
                    profiles.load()

    def test_an_unknown_profile_from_the_cache_names_the_cache(self):
        registry.write_cache(profiles.cache_path().parent, [_row("verify")])
        profiles.load()
        with self.assertRaisesRegex(PlanError, "unknown profile 'flash'.*from the cache at .*ago"):
            resolve(_task("flash"), {})

    def test_capability_in_a_workstream_profile_is_refused(self):
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=registry.ROWS)
        profiles.load()
        config = {"profiles": {"implement": {"capability": "review"}}}
        with self.assertRaisesRegex(PlanError, "use read_only in a workstream profile"):
            resolve(_task("implement"), config)

    def test_a_workstream_profile_named_like_a_registry_row_inherits_from_it(self):
        """Proves a TOML [profiles.review] overrides only the keys it sets; the registry's
        review row fills the rest."""
        review = CHANGE_JOB | {"name": "review", "capability": "review", "read_only": True}
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=[review])
        profiles.load()
        got = resolve(_task("review"), {"profiles": {"review": {"model": "haiku"}}})
        self.assertEqual((got["model"], got["timeout_s"]), ("haiku", 1800))
        self.assertEqual((got["read_only"], got["max_tool_calls"]), (True, 200))

    def test_a_plan_may_use_any_profile_with_a_model_and_timeout(self):
        no_timeout = CHANGE_JOB | {"name": "broken", "timeout_s": None}
        rows = [CHANGE_JOB, no_timeout]
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=rows)
        profiles.load()
        task = Task(id="c", goal="g", done="d", profile="change", owns=["x"], check="true")
        self.assertEqual(validate(Plan(id="p", tasks=[task])), [])
        broken = Task(id="b", goal="g", done="d", profile="broken", owns=["x"], check="true")
        self.assertIn("task b: no timeout_s", validate(Plan(id="p", tasks=[broken])))

    def test_a_task_override_still_beats_the_profile(self):
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=[_row("verify")])
        profiles.load()
        task = _task("verify", overrides={"effort": "max", "model": "opus"})
        got = resolve(task, {"profiles": {"verify": {"effort": "high"}}})
        self.assertEqual((got["effort"], got["model"]), ("max", "opus"))

    def test_the_profiles_command_lists_them_with_their_source(self):
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=registry.ROWS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["profiles"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["source"], "sazed")
        self.assertEqual(set(data["profiles"]), set(TABLE))

    def test_flash_comes_from_the_registry_with_the_agy_engine(self):
        """Proves the flash profile's agy engine and model come from Sazed's row."""
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=registry.ROWS)
        profiles.load()
        got = resolve(_task("flash"), {})
        self.assertEqual(got["engine"], "agy")
        self.assertEqual(got["model"], "gemini-3.8-flash-high")


class Evidence(Env):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["config", "commit.gpgsign", "false"],
        ):
            git(*args, cwd=self.repo)
        (self.repo / "README.md").write_text("# repo\n")
        git("add", "README.md", cwd=self.repo)
        git("commit", "-q", "-m", "seed", cwd=self.repo)
        os.environ["DISPATCH_CLAUDE"] = f"{sys.executable} {FAKE}"
        self.home = self.tmp / "home" / "e1"
        self.ws = Workspace(self.repo, "e1", self.home)
        self.board = Board(self.home)

    def run_verify(self, mode: str, profile: str = "verify") -> dict:
        os.environ["DISPATCH_FAKE_MODES"] = json.dumps({"v": mode})
        task = Task(id="v", goal="check", done="a verdict", profile=profile)
        plan = Plan(id="e1", tasks=[task])
        integrate.init(plan, self.ws, self.board, "main")
        return run_task(plan, task, self.ws, self.board)

    def test_a_verify_task_with_no_read_grep_or_glob_is_failed(self):
        row = self.run_verify("nocommit")
        self.assertEqual((row["state"], row["line"]), ("failed", "no evidence read"))

    def test_a_verify_task_that_read_a_file_passes(self):
        self.assertEqual(self.run_verify("reads")["state"], "done")

    def test_a_profile_without_the_rule_passes_without_reads(self):
        self.assertEqual(self.run_verify("nocommit", "sweep")["state"], "done")


class PlanLinks(Env):
    def test_project_and_board_plan_reach_the_plan_post(self):
        url = self.serve(Recorder)
        path = self.tmp / "plan.json"
        data = {"id": "l1", "project": "agent-stack", "board_plan": "d4", "tasks": []}
        path.write_text(json.dumps(data))
        Mirror(url, "l1").plan(load(path), {}, self.tmp, "main")
        body = self.server.seen[0][2]
        self.assertEqual((body["project"], body["board_plan"]), ("agent-stack", "d4"))

    def test_both_links_are_optional(self):
        url = self.serve(Recorder)
        Mirror(url, "l2").plan(Plan(id="l2", tasks=[]), {}, self.tmp, "main")
        body = self.server.seen[0][2]
        self.assertNotIn("project", body)
        self.assertNotIn("board_plan", body)


if __name__ == "__main__":
    unittest.main()
