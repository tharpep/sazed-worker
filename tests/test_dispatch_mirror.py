"""Dispatch mirrors its plan and board rows to Sazed over HTTP, best-effort."""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from sazed_worker.dispatch import integrate
from sazed_worker.dispatch.__main__ import main
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import Mirror
from sazed_worker.dispatch.plan import Plan, Task
from sazed_worker.dispatch.workspace import Workspace, git


class Recorder(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, self.headers.get("X-API-Key"), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class Mirroring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-mirror-"))
        self.server = HTTPServer(("127.0.0.1", 0), Recorder)
        self.server.seen = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["config", "commit.gpgsign", "false"],
            ["remote", "add", "origin", "git@github.com:someone/thing.git"],
        ):
            git(*args, cwd=self.repo)
        (self.repo / "README.md").write_text("# repo\n")
        git("add", "README.md", cwd=self.repo)
        git("commit", "-q", "-m", "seed", cwd=self.repo)
        self.plan = Plan(
            id="m1",
            tasks=[
                Task(id="a", goal="do a", done="", owns=["a/**"]),
                Task(
                    id="b", goal="do b", done="", owns=["b/**"], depends_on=["a"], profile="design"
                ),
            ],
        )
        self.home = self.tmp / "home" / "m1"
        self.ws = Workspace(self.repo, "m1", self.home)
        self.saved = {
            k: os.environ.pop(k, None) for k in ("DISPATCH_SAZED_URL", "DISPATCH_SAZED_KEY")
        }
        self.addCleanup(self.restore)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def restore(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def board(self, url: str | None = "") -> Board:
        url = self.url if url == "" else url
        return Board(self.home, Mirror(url, "m1", "k1") if url else None)

    def test_a_board_write_posts_the_row_with_the_plan_id_and_key(self):
        row = self.board().write("a", "running", "started", ref="r1", cost_usd=0.5, extra=1)
        path, key, body = self.server.seen[0]
        self.assertEqual((path, key), ("/api/dispatch/rows", "k1"))
        self.assertEqual(body, {"plan": "m1", **row})
        self.assertEqual(body["meta"], {"extra": 1})

    def test_init_posts_the_plan_with_resolved_model_and_profile(self):
        integrate.init(self.plan, self.ws, self.board(), "main")
        path, _, body = self.server.seen[0]
        self.assertEqual(path, "/api/dispatch/plans")
        self.assertEqual(
            (body["plan"], body["repo"], body["base"]), ("m1", "someone/thing", "main")
        )
        self.assertTrue(body["host"])
        by_id = {t["id"]: t for t in body["tasks"]}
        self.assertEqual((by_id["a"]["model"], by_id["a"]["profile"]), ("sonnet", "implement"))
        self.assertEqual((by_id["b"]["model"], by_id["b"]["profile"]), ("opus", "design"))
        self.assertEqual(by_id["b"]["depends_on"], ["a"])
        self.assertEqual(by_id["a"]["owns"], ["a/**"])

    def test_nothing_is_sent_when_the_env_var_is_unset(self):
        self.assertIsNone(Mirror.from_env("m1"))
        board = self.board(None)
        integrate.init(self.plan, self.ws, board, "main")
        board.write("a", "running", "started")
        self.assertEqual(self.server.seen, [])
        self.assertEqual(len(board.rows("a")), 1)

    def test_an_unreachable_url_does_not_raise_and_the_local_row_is_written(self):
        board = self.board("http://127.0.0.1:1/api")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            board.write("a", "running", "one")
            board.write("a", "done", "two")
        self.assertEqual([r["state"] for r in board.rows("a")], ["running", "done"])
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_sync_replays_the_plan_and_all_rows(self):
        board = self.board(None)
        board.write("a", "running", "go")
        board.write("a", "done", "ok")
        board.write("b", "blocked", "waiting on a")
        plan_file = self.tmp / "plan.json"
        plan_file.write_text(
            json.dumps(
                {
                    "id": "m1",
                    "check": "true",
                    "tasks": [
                        {"id": "a", "goal": "do a", "done": "a", "owns": ["a/**"]},
                        {
                            "id": "b",
                            "goal": "do b",
                            "done": "b",
                            "owns": ["b/**"],
                            "depends_on": ["a"],
                        },
                    ],
                }
            )
        )
        os.environ["DISPATCH_SAZED_URL"] = self.url
        os.environ["DISPATCH_HOME"] = str(self.tmp / "home")
        self.addCleanup(os.environ.pop, "DISPATCH_HOME", None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", str(plan_file), "--repo", str(self.repo)]), 0)
        paths = [p for p, _, _ in self.server.seen]
        self.assertEqual(paths, ["/api/dispatch/plans"] + ["/api/dispatch/rows"] * 3)
        rows = [b for p, _, b in self.server.seen if p.endswith("rows")]
        self.assertEqual(
            {(r["plan"], r["task"], r["state"]) for r in rows},
            {("m1", "a", "running"), ("m1", "a", "done"), ("m1", "b", "blocked")},
        )


if __name__ == "__main__":
    unittest.main()
