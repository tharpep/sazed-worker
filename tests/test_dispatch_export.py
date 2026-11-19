"""Dispatch export both ways with the fake agent: by hand through git and gh, unattended
through Sazed's worker route with a changed-files map (seam 6)."""

import base64
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from sazed_worker import sazed_api
from sazed_worker.dispatch import integrate, pr_export
from sazed_worker.dispatch.__main__ import main
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import repo_name
from sazed_worker.dispatch.workspace import Workspace, git
from tests import registry
from tests.test_dispatch_engine import CHECK, FAKE, _plan

GH = [sys.executable, "-c", "print('https://pr/1')"]
GIT_READS = {"log", "merge-base", "diff", "cat-file", "remote"}
BODY = "Dispatch plan p1.\n\n- api: api: work\n- docs: docs: work\n- ui: ui: work"
PLAN_JSON = {
    "id": "p1",
    "check": CHECK,
    "interface_files": {"src/shared/types.txt": "shared\n"},
    "tasks": [
        {"id": "api", "goal": "build the api", "done": "api file", "owns": ["src/api/**"]},
        {
            "id": "ui",
            "goal": "build the ui",
            "done": "ui file",
            "owns": ["src/ui/**"],
            "depends_on": ["api"],
        },
        {"id": "docs", "goal": "write docs", "done": "docs file", "owns": ["docs/**"]},
    ],
}


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-export-"))
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
        self.remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(self.remote), cwd=self.tmp)
        git("remote", "add", "origin", str(self.remote), cwd=self.repo)
        env = {
            "DISPATCH_HOME": str(self.tmp / "home"),
            "DISPATCH_CLAUDE": f"{sys.executable} {FAKE}",
            "DISPATCH_AGY": f"{sys.executable} {FAKE}",
            "DISPATCH_FAKE_MODES": "{}",
            "DISPATCH_GH": shlex.join(GH),
        }
        old = {k: os.environ.get(k) for k in (*env, "DISPATCH_SAZED_URL")}
        self.addCleanup(self._restore, old)
        os.environ.pop("DISPATCH_SAZED_URL", None)
        os.environ.update(env)
        registry.install()
        registry.write_cache(self.tmp / "home")
        self.home = self.tmp / "home" / "p1"
        self.ws = Workspace(self.repo, "p1", self.home)
        self.board = Board(self.home)
        self.plan_file = self.tmp / "plan.json"
        self.plan_file.write_text(json.dumps(PLAN_JSON))

    @staticmethod
    def _restore(old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main([*args, str(self.plan_file), "--repo", str(self.repo)])
        return code, out.getvalue()

    def recorded_export(self, *args: str) -> tuple[int, str, list[list[str]]]:
        """Runs export through the CLI and returns every git and gh argv that is not a read."""
        calls: list[list[str]] = []
        real = subprocess.run

        def record(argv, *a, **kw):
            calls.append(list(argv))
            return real(argv, *a, **kw)

        with mock.patch("subprocess.run", side_effect=record):
            code, out = self.cli("export", *args)
        acting = [c for c in calls if not (c[0] == "git" and c[1] in GIT_READS)]
        return code, out, acting


class HandExport(ExportCase):
    def test_a_hand_export_pushes_integ_then_opens_one_draft_pr_with_gh(self):
        """Pins the argv, in order, of every git and gh call a hand export makes that is not a
        read: one push of integ/<plan>, then one `gh pr create --draft`."""
        integrate.go(_plan(), self.ws, self.board, "main")
        code, out, acting = self.recorded_export()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "https://pr/1")
        self.assertEqual(
            acting,
            [
                ["git", "push", "-q", "-u", "origin", "integ/p1"],
                [
                    *GH,
                    "pr",
                    "create",
                    "--draft",
                    "--base",
                    "main",
                    "--head",
                    "integ/p1",
                    "--title",
                    "p1",
                    "--body",
                    BODY,
                ],
            ],
        )
        self.assertTrue(git("rev-parse", "--verify", "-q", "refs/heads/integ/p1", cwd=self.remote))

    def test_a_hand_export_never_imports_config(self):
        """A hand export succeeds where the worker's agent user does not exist, so it never
        imports sazed_worker.config, which looks that user up at import."""
        integrate.go(_plan(), self.ws, self.board, "main")
        env = os.environ | {"SAZED_AGENT_USER": "no-such-user-seam6"}
        proc = subprocess.run(
            [sys.executable, "-m", "sazed_worker.dispatch", "export", str(self.plan_file)]
            + ["--repo", str(self.repo)],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "https://pr/1")


class FakeSazed(BaseHTTPRequestHandler):
    def do_POST(self):
        server = self.server
        size = int(self.headers["Content-Length"])
        server.seen.append((self.path, dict(self.headers), json.loads(self.rfile.read(size))))
        status, answer = server.answer
        raw = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


OK = {
    "url": "https://github.com/o/r/pull/7",
    "number": 7,
    "branch": "sazed/plan-p1",
    "head_sha": "f" * 40,
    "updated": False,
    "base_moved": False,
}


class UnattendedExport(ExportCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSazed)
        self.server.seen = []
        self.server.answer = (200, OK)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        url = f"http://127.0.0.1:{self.server.server_port}"
        for name, value in (("SAZED_URL", url), ("WORKER_KEY", "worker-key-for-tests")):
            patcher = mock.patch.object(sazed_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_it_sends_the_contract_request_and_pushes_nothing(self):
        integrate.go(_plan(), self.ws, self.board, "main")
        code, out, acting = self.recorded_export("--unattended")
        self.assertEqual((code, out.strip()), (0, OK["url"]))
        self.assertEqual(acting, [])
        self.assertFalse(
            git("rev-parse", "--verify", "-q", "integ/p1", cwd=self.remote, check=False)
        )
        [(path, headers, sent)] = self.server.seen
        self.assertEqual(path, "/worker/dispatch/plans/p1/export")
        self.assertEqual(headers["X-Worker-Key"], "worker-key-for-tests")
        self.assertEqual(set(sent), {"repo", "base", "base_sha", "title", "body", "files", "modes"})
        self.assertEqual(sent["repo"], repo_name(self.repo))
        self.assertEqual((sent["base"], sent["title"], sent["body"]), ("main", "p1", BODY))
        self.assertEqual(sent["base_sha"], git("rev-parse", "main", cwd=self.repo))
        self.assertEqual(
            {p: base64.b64decode(c) for p, c in sent["files"].items()},
            {
                "src/shared/types.txt": b"shared\n",
                "src/api/api.txt": b"api\n",
                "src/ui/ui.txt": b"ui\n",
                "docs/docs.txt": b"docs\n",
            },
        )
        self.assertEqual(sent["modes"], {})

    def test_hand_and_unattended_exports_send_the_same_body(self):
        integrate.go(_plan(), self.ws, self.board, "main")
        self.assertEqual(self.recorded_export("--unattended")[0], 0)
        _, _, acting = self.recorded_export()
        self.assertEqual(acting[1][acting[1].index("--body") + 1], self.server.seen[0][2]["body"])

    def test_it_says_so_when_sazed_reports_the_base_moved(self):
        integrate.go(_plan(), self.ws, self.board, "main")
        self.server.answer = (200, OK | {"base_moved": True, "updated": True})
        code, out = self.cli("export", "--unattended")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], OK["url"])
        self.assertRegex(out.splitlines()[1], r"^base_moved: main has moved past [0-9a-f]{12}, ")

    def test_it_prints_sazeds_sentence_and_reason_on_a_refusal(self):
        integrate.go(_plan(), self.ws, self.board, "main")
        self.server.answer = (
            409,
            {"detail": "The plan's PR #7 was closed.", "reason": "pr_closed"},
        )
        code, out = self.cli("export", "--unattended")
        self.assertEqual(
            (code, out.strip()), (1, "refused: The plan's PR #7 was closed. (reason: pr_closed)")
        )

    def test_an_unreachable_sazed_is_export_unreachable(self):
        integrate.go(_plan(), self.ws, self.board, "main")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{s.getsockname()[1]}"
        with mock.patch.object(sazed_api, "SAZED_URL", closed):
            code, out = self.cli("export", "--unattended")
        self.assertEqual(code, 1)
        self.assertRegex(
            out.strip(), r"^refused: Sazed could not be reached .*\(reason: export_unreachable\)$"
        )

    def test_each_refusal_prints_one_sentence_with_its_reason_and_sends_nothing(self):
        cases = {
            "unlanded": "refused",
            "plan_id": "refused",
            "submodule": "refused",
            "workflow": "refused",
            "too_many_files": "export_too_large",
            "too_large": "export_too_large",
        }
        integrate.init(_plan(), self.ws, self.board, "main")
        code, out = self.cli("export", "--unattended")
        outputs = {"unlanded": (code, out)}
        self.plan_file.write_text(json.dumps(PLAN_JSON | {"id": "p" * 76}))
        outputs["plan_id"] = self.cli("export", "--unattended")
        self.plan_file.write_text(json.dumps(PLAN_JSON))
        integrate.go(_plan(), self.ws, self.board, "main")
        tip = self.ws.tip()
        sha = git("rev-parse", "HEAD", cwd=self.ws.integ_dir)
        edits = {
            "submodule": lambda: ["update-index", "--add", "--cacheinfo", f"160000,{sha},lib"],
            "workflow": lambda: ["add", "--", self.write(".github/workflows/ci.yml", b"on:\n")],
            "too_many_files": lambda: [
                "add",
                "--",
                *[self.write(f"many/{i}.txt", b"x") for i in range(201)],
            ],
            "too_large": lambda: ["add", "--", self.write("big.bin", b"x" * (5 * 1024 * 1024 + 1))],
        }
        for name, index_args in edits.items():
            git(*index_args(), cwd=self.ws.integ_dir)
            git("commit", "-q", "-m", name, cwd=self.ws.integ_dir)
            outputs[name] = self.cli("export", "--unattended")
            git("reset", "-q", "--hard", tip, cwd=self.ws.integ_dir)
        for name, reason in cases.items():
            with self.subTest(name):
                code, out = outputs[name]
                self.assertEqual(code, 1)
                self.assertEqual(len(out.strip().splitlines()), 1, out)
                self.assertRegex(out.strip(), rf"^refused: \S.* \(reason: {reason}\)$")
        self.assertEqual(self.server.seen, [])

    def write(self, rel: str, data: bytes) -> str:
        path = self.ws.integ_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return rel


def _run(args: list[str], cwd: Path, env: dict | None = None, data: bytes | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, env=env, input=data, capture_output=True, check=True)
    return proc.stdout.decode().strip()


class ChangedFilesMap(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="dispatch-map-"))
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["config", "commit.gpgsign", "false"],
        ):
            git(*args, cwd=self.repo)

    def put(self, rel: str, data: bytes) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def apply(self, start: str, files: dict, modes: dict) -> str:
        """The gateway's rule: start's tree plus the files; a listed mode wins, an existing path
        keeps its mode, and a new path is 100644."""
        env = os.environ | {"GIT_INDEX_FILE": str(self.repo / ".git" / "apply-index")}
        _run(["git", "read-tree", start], self.repo, env)
        for path, content in files.items():
            if content is None:
                _run(["git", "update-index", "--force-remove", "--", path], self.repo, env)
                continue
            blob = _run(
                ["git", "hash-object", "-w", "--stdin"], self.repo, env, base64.b64decode(content)
            )
            existing = _run(["git", "ls-tree", start, "--", path], self.repo).split(" ")[0]
            mode = modes.get(path) or existing or "100644"
            _run(
                ["git", "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}"],
                self.repo,
                env,
            )
        return _run(["git", "write-tree"], self.repo, env)

    def test_applying_the_map_to_base_sha_reproduces_integs_tree(self):
        """An added, a modified and a deleted file, an executable bit set and one cleared on
        unchanged content, a new executable, a deleted executable, a new symlink, a binary file
        and a rename: base_sha plus the map has integ's tree, and no deleted path has a mode."""
        for rel, data in {
            "keep.txt": b"keep\n",
            "mod.txt": b"before\n",
            "gone.txt": b"gone\n",
            "run.sh": b"#!/bin/sh\necho hi\n",
            "was-exec.sh": b"#!/bin/sh\n",
            "gone-exec.sh": b"#!/bin/sh\n",
            "old-name.txt": b"moved\n",
        }.items():
            self.put(rel, data)
        git("add", "-A", cwd=self.repo)
        git("update-index", "--chmod=+x", "was-exec.sh", "gone-exec.sh", cwd=self.repo)
        git("commit", "-q", "-m", "base", cwd=self.repo)
        start = git("rev-parse", "HEAD", cwd=self.repo)
        git("checkout", "-q", "-b", "integ/p1", cwd=self.repo)
        self.put("new.txt", b"new\n")
        self.put("mod.txt", b"after\n")
        (self.repo / "gone.txt").unlink()
        os.symlink("mod.txt", self.repo / "link")
        self.put("image.bin", bytes(range(256)) * 4)
        (self.repo / "old-name.txt").rename(self.repo / "new-name.txt")
        (self.repo / "gone-exec.sh").unlink()
        self.put("tool.sh", b"#!/bin/sh\n")
        git("add", "-A", cwd=self.repo)
        git("update-index", "--chmod=+x", "run.sh", "tool.sh", cwd=self.repo)
        git("update-index", "--chmod=-x", "was-exec.sh", cwd=self.repo)
        git("commit", "-q", "-m", "integ", cwd=self.repo)

        files, modes = pr_export.changed_files(self.repo, start, "integ/p1")

        self.assertEqual(
            self.apply(start, files, modes), git("rev-parse", "integ/p1^{tree}", cwd=self.repo)
        )
        self.assertEqual(
            {p for p, c in files.items() if c is None}, {"gone.txt", "gone-exec.sh", "old-name.txt"}
        )
        self.assertEqual(
            set(files),
            {
                "new.txt",
                "mod.txt",
                "gone.txt",
                "gone-exec.sh",
                "link",
                "image.bin",
                "old-name.txt",
                "new-name.txt",
                "run.sh",
                "was-exec.sh",
                "tool.sh",
            },
        )
        self.assertEqual(
            modes,
            {"run.sh": "100755", "tool.sh": "100755", "was-exec.sh": "100644", "link": "120000"},
        )
        self.assertEqual(base64.b64decode(files["link"]), b"mod.txt")
        self.assertEqual(base64.b64decode(files["image.bin"]), bytes(range(256)) * 4)
        self.assertEqual(base64.b64decode(files["run.sh"]), b"#!/bin/sh\necho hi\n")


if __name__ == "__main__":
    unittest.main()
