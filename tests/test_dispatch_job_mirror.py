"""A worker job mirrors with the worker key on the worker routes only (D7 W-7)."""

import contextlib
import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from sazed_worker import sazed_api
from sazed_worker.dispatch.mirror import JobMirror, Mirror
from sazed_worker.dispatch.plan import Plan, Task
from tests import registry


class Recorder(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, dict(self.headers), body))
        self.send_response(self.server.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class JobMirroring(unittest.TestCase):
    def setUp(self):
        registry.install()
        self.worker, self.console = self.start(), self.start()
        env = {"DISPATCH_SAZED_URL": self.console.url, "DISPATCH_SAZED_KEY": "console-key-abc"}
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(sazed_api, "SAZED_URL", self.worker.url),
            mock.patch.object(sazed_api, "WORKER_KEY", "worker-key-abc"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        server.seen, server.status = [], 200
        server.url = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def send_all(self, mirror: Mirror) -> None:
        plan = Plan(id="p1", tasks=[Task(id="a", goal="g", done="d", owns=["a/**"])])
        mirror.plan(plan, {}, Path("/nowhere/repo"), "main")
        mirror.row({"state": "running", "task": "a"})
        mirror.heartbeat("a")
        mirror.steps("a", [{"kind": "text"}])
        mirror.reading({"unifiedWindows": {}})

    def test_everything_but_readings_goes_to_the_worker_routes_with_the_worker_key(self):
        self.send_all(JobMirror("p1"))
        self.assertEqual(
            [path for path, _, _ in self.worker.seen],
            [f"/worker/dispatch/{r}" for r in ("plans", "rows", "heartbeat", "steps")],
        )
        for _, headers, _ in self.worker.seen:
            self.assertEqual(headers["X-Worker-Key"], "worker-key-abc")
            self.assertNotIn("console-key-abc", str(headers))
        self.assertEqual(self.console.seen, [])

    def test_the_bodies_are_the_hand_mirror_s(self):
        self.send_all(JobMirror("p1"))
        self.send_all(Mirror.from_env("p1"))
        hand = [(p, b) for p, _, b in self.console.seen if p != "/claude-usage/readings"]
        self.assertEqual([(f"/worker{p}", b) for p, b in hand],
                         [(p, b) for p, _, b in self.worker.seen])

    def test_a_refusing_sazed_never_stops_the_run_and_settled_rows_are_still_tried(self):
        self.worker.status = 403
        mirror = JobMirror("p1")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mirror.row({"state": "running", "task": "a"})
            mirror.row({"state": "running", "task": "a"})
            mirror.heartbeat("a")
            mirror.row({"state": "done", "task": "a"})
        self.assertEqual(
            [p for p, _, _ in self.worker.seen],
            ["/worker/dispatch/rows", "/worker/dispatch/heartbeat", "/worker/dispatch/rows"],
        )
        self.assertEqual(err.getvalue().count("mirror to"), 1)


if __name__ == "__main__":
    unittest.main()
