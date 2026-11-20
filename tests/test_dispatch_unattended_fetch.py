"""An unattended run reads profiles and the MCP registry from Sazed's worker routes only."""

import contextlib
import io
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from sazed_worker import sazed_api
from sazed_worker.dispatch import mcp, profiles
from sazed_worker.dispatch.__main__ import main
from tests import registry
from tests.test_dispatch_profiles import Env

PROFILES = "/worker/agents/profiles"
SERVERS = "/worker/agents/mcp-servers"
CONTEXT7 = {"name": "context7", "transport": "http", "url": "https://c7.test/mcp", "headers": {}}
WORKER_KEY = "worker-key-for-tests"
CONSOLE_KEY = "console-key-for-tests"


class Routes(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, self.headers))
        answer = self.server.routes.get(self.path, (404, {"detail": "Not Found"}))
        if answer == "hang":
            time.sleep(1)
            return
        status, body = answer
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class Case(Env):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, mcp, "_active", ({}, "none"))
        self.worker = self.start({PROFILES: (200, registry.ROWS), SERVERS: (200, [CONTEXT7])})
        self.console = self.start({})
        os.environ["DISPATCH_SAZED_URL"] = self.console.url
        os.environ["DISPATCH_SAZED_KEY"] = CONSOLE_KEY
        for name, value in (("SAZED_URL", self.worker.url), ("WORKER_KEY", WORKER_KEY)):
            patcher = mock.patch.object(sazed_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, routes: dict) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Routes)
        server.seen = []
        server.routes = routes
        server.url = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def refusal(self, load) -> str:
        with self.assertRaises(profiles.ProfilesError) as caught:
            load(unattended=True)
        return str(caught.exception)


class Fetched(Case):
    def test_both_come_from_the_worker_routes_with_the_worker_key_only(self):
        """Proves the unattended loads call only the worker routes, send X-Worker-Key, never
        send the console key, and never contact DISPATCH_SAZED_URL."""
        table, source = profiles.load(unattended=True)
        servers, mcp_source = mcp.load(unattended=True)
        self.assertEqual((source, mcp_source), ("sazed", "sazed"))
        self.assertEqual(set(table), {r["name"] for r in registry.ROWS})
        self.assertEqual(list(servers), ["context7"])
        self.assertEqual([path for path, _ in self.worker.seen], [PROFILES, SERVERS])
        for _, headers in self.worker.seen:
            self.assertEqual(headers.get("X-Worker-Key"), WORKER_KEY)
            self.assertNotIn("X-API-Key", headers)
            self.assertNotIn(CONSOLE_KEY, headers.as_string())
        self.assertEqual(self.worker.seen[0][1].get("X-Sazed-Shape"), "2")
        self.assertEqual(self.console.seen, [])

    def test_a_successful_fetch_neither_writes_nor_refreshes_the_cache(self):
        profiles.load(unattended=True)
        mcp.load(unattended=True)
        self.assertFalse(profiles.cache_path().exists())
        self.assertFalse(mcp.cache_path().exists())
        registry.write_cache(profiles.cache_path().parent, [registry.ROWS[0]])
        mcp.cache_path().write_text("[]")
        profiles.load(unattended=True)
        mcp.load(unattended=True)
        self.assertEqual(json.loads(profiles.cache_path().read_text()), [registry.ROWS[0]])
        self.assertEqual(mcp.cache_path().read_text(), "[]")

    def test_a_hand_run_still_asks_dispatch_sazed_url_with_the_console_key(self):
        self.console.routes.update(
            {"/agents/profiles": (200, registry.ROWS), "/agents/mcp-servers": (200, [CONTEXT7])}
        )
        self.assertEqual(profiles.load()[1], "sazed")
        self.assertEqual(mcp.load()[1], "sazed")
        sent = [(path, h.get("X-API-Key"), h.get("X-Worker-Key")) for path, h in self.console.seen]
        self.assertEqual(
            sent,
            [("/agents/profiles", CONSOLE_KEY, None), ("/agents/mcp-servers", CONSOLE_KEY, None)],
        )
        self.assertEqual(self.worker.seen, [])


class Refused(Case):
    def test_each_failure_refuses_in_one_sentence_and_never_reads_the_cache(self):
        """Proves a 401, a timeout, an empty list, a non-list body and a malformed entry each
        raise one sentence, for both registries, while a valid cache sits on disk unread."""
        registry.write_cache(profiles.cache_path().parent)
        mcp.cache_path().write_text(json.dumps([CONTEXT7]))
        cases = {
            "401": ((401, {"detail": "bad worker key"}), "answered 401"),
            "empty": ((200, []), "did not answer a non-empty list"),
            "not a list": ((200, {"rows": []}), "did not answer a non-empty list"),
            "malformed": ((200, ["context7"]), "answered a malformed entry"),
            "timeout": ("hang", "could not be reached (TimeoutError: timed out)"),
        }
        loads = {
            PROFILES: (profiles, "agent profiles"),
            SERVERS: (mcp, "the MCP registry"),
        }
        with mock.patch.object(profiles, "TIMEOUT_S", 0.2):
            for route, (module, what) in loads.items():
                for name, (answer, fragment) in cases.items():
                    with self.subTest(route=route, case=name):
                        self.worker.routes[route] = answer
                        sentence = self.refusal(module.load)
                        self.assertTrue(
                            sentence.startswith(
                                f"an unattended run needs {what} fresh from Sazed, and "
                            ),
                            sentence,
                        )
                        self.assertIn(fragment, sentence)
                        self.assertNotIn("\n", sentence)
                        self.assertNotIn(WORKER_KEY, sentence)
                rows = registry.ROWS if route == PROFILES else [CONTEXT7]
                self.worker.routes[route] = (200, rows)
        self.assertEqual(self.console.seen, [])

    def test_the_cli_refuses_to_start_when_a_registry_fetch_fails(self):
        self.worker.routes[SERVERS] = (401, {"detail": "bad worker key"})
        plan_file = self.tmp / "plan.json"
        plan_file.write_text(json.dumps({"id": "p1", "tasks": []}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["run", str(plan_file), "--repo", str(self.tmp), "--unattended"])
        self.assertEqual(
            (code, out.getvalue().strip()),
            (
                1,
                "refused: an unattended run needs the MCP registry fresh from Sazed, "
                f"and {SERVERS} answered 401",
            ),
        )
        self.assertFalse((self.tmp / "home" / "p1").exists())


if __name__ == "__main__":
    unittest.main()
