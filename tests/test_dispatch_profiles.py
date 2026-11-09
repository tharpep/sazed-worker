"""Dispatch reads agent profiles from Sazed, then a cache, then built-ins, and checks evidence."""

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
from sazed_worker.dispatch.plan import Plan, Task, load, resolve
from sazed_worker.dispatch.run_agent import run_task
from sazed_worker.dispatch.workspace import Workspace, git
from tests.test_dispatch_mirror import Recorder

FAKE = Path(__file__).resolve().parents[1] / "sazed_worker" / "dispatch" / "fake_agent.py"

KEYS = ("model", "effort", "budget_usd", "timeout_s", "read_only", "evidence_required")
TABLE = {
    "smoke": ("haiku", "medium", 0.5, 600, True, False),
    "sweep": ("haiku", "max", 0.5, 600, True, False),
    "verify": ("haiku", "max", 0.5, 600, True, True),
    "implement": ("sonnet", "high", 2.0, 1200, False, False),
    "design": ("opus", "xhigh", 4.0, 1200, False, False),
    "flash": ("gemini-3.8-flash-high", "high", 0.5, 1200, False, False),
}


class Registry(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("X-API-Key")))
        body = json.dumps(self.server.profiles).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _row(name: str, **fields) -> dict:
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


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-profiles-"))
        names = ("DISPATCH_HOME", "DISPATCH_SAZED_URL", "DISPATCH_SAZED_KEY", "DISPATCH_CLAUDE")
        saved = {k: os.environ.pop(k, None) for k in (*names, "DISPATCH_FAKE_MODES")}
        os.environ["DISPATCH_HOME"] = str(self.tmp / "home")
        self.addCleanup(self.restore, saved)
        self.addCleanup(setattr, profiles, "_active", (profiles.BUILTIN, "builtin"))

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
        got = resolve(Task(id="v", goal="g", done="d", profile="verify"), {})
        self.assertEqual((got["model"], got["effort"]), ("sonnet", "xhigh"))
        self.assertEqual(got["profile_source"], "sazed")
        self.assertEqual(json.loads(profiles.cache_path().read_text()), rows)
        self.assertEqual(table["sweep"], profiles.BUILTIN["sweep"])

    def test_the_cache_is_used_when_sazed_is_down(self):
        profiles.cache_path().parent.mkdir(parents=True)
        profiles.cache_path().write_text(json.dumps([_row("verify", model="opus")]))
        os.environ["DISPATCH_SAZED_URL"] = "http://127.0.0.1:1"
        with contextlib.redirect_stderr(io.StringIO()):
            _, source = profiles.load()
        got = resolve(Task(id="v", goal="g", done="d", profile="verify"), {})
        self.assertEqual((source, got["model"], got["profile_source"]), ("cache", "opus", "cache"))

    def test_built_ins_are_used_with_neither_and_match_the_table(self):
        _, source = profiles.load()
        self.assertEqual(source, "builtin")
        for name, want in TABLE.items():
            with self.subTest(name=name):
                got = resolve(Task(id="t", goal="g", done="d", profile=name), {})
                self.assertEqual(tuple(got[k] for k in KEYS), want)
                self.assertEqual(got["profile_source"], "builtin")

    def test_a_task_override_still_beats_the_profile(self):
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=[_row("verify")])
        profiles.load()
        overrides = {"effort": "max", "model": "opus"}
        task = Task(id="v", goal="g", done="d", profile="verify", overrides=overrides)
        got = resolve(task, {"profiles": {"verify": {"effort": "high"}}})
        self.assertEqual((got["effort"], got["model"]), ("max", "opus"))

    def test_the_profiles_command_lists_them_with_their_source(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["profiles"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["source"], "builtin")
        self.assertEqual(set(data["profiles"]), set(TABLE))

    def test_flash_profile_resolves_with_agy_engine(self):
        """Proves the flash profile specifies the agy execution engine and model."""
        got = resolve(Task(id="f", goal="g", done="d", profile="flash"), {})
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
