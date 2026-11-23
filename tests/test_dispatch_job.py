"""A dispatch job end to end against a fake Sazed, with the fake agent (D7 W-3, W-4, W-6, W-9)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from sazed_worker import config as worker_config
from sazed_worker import jobs, sazed_api, scrub
from sazed_worker import main as main_module
from sazed_worker import repo as repo_module
from sazed_worker.agent_process import JobFailed
from sazed_worker.config import stop
from sazed_worker.dispatch import admission, unattended
from sazed_worker.dispatch import job as dispatch_job
from sazed_worker.dispatch.workspace import git
from tests import registry
from tests.test_dispatch_unattended import BUSY, FAKE, Case, needs_jail, needs_root

WORKER_KEY = "worker-key-for-tests"
CONSOLE_KEY = "console-key-for-tests"
CLONE_TOKEN = "clone-token-for-tests"
PR = "https://github.com/o/r/pull/7"
RECORDER = """import json, os, runpy, sys
with open({record!r}, "a") as f:
    f.write(json.dumps(dict(os.environ)) + chr(10))
runpy.run_path({fake!r}, run_name="__main__")
"""


class Sazed(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, dict(self.headers), None))
        answers = {"/worker/agents/profiles": self.server.profiles,
                   "/worker/agents/mcp-servers": []}  # fmt: skip
        self.reply(200, answers[self.path])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, dict(self.headers), body))
        if self.path.endswith("/events"):
            self.reply(200, {"cancel": self.server.cancel.is_set()})
        elif self.path.endswith("/export"):
            self.reply(*self.server.export)
        elif self.server.cancel.is_set():
            self.reply(403, {"detail": "no running dispatch job"})
        else:
            self.reply(200, {})

    def reply(self, status, answer):
        raw = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class JobCase(Case):
    """The real host preflight needs root; AsRoot below runs it."""

    def setUp(self):
        super().setUp()
        self.source = self.tmp / "o" / "r"
        self.source.parent.mkdir()
        self.repo.rename(self.source)
        self.jobs_dir = self.tmp / "jobs"
        self.jobs_dir.mkdir()
        self.record = self.tmp / "agent-env.jsonl"
        self.record.touch()
        self.record.chmod(0o666)
        recorder = self.tmp / "recorder.py"
        recorder.write_text(RECORDER.format(record=str(self.record), fake=str(FAKE)))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Sazed)
        self.server.seen, self.server.cancel = [], threading.Event()
        self.server.profiles = registry.ROWS
        self.server.export = (200, {"url": PR, "number": 7, "base_moved": False})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(scrub.extra_secrets.clear)
        self.addCleanup(stop.clear)
        paths = worker_config._agent_paths() | {"JOBS_DIR": self.jobs_dir}
        for patcher in (
            mock.patch.dict(os.environ, {
                "DISPATCH_CLAUDE": f"{sys.executable} {recorder}",
                "DISPATCH_SAZED_URL": "http://127.0.0.1:9",
                "DISPATCH_SAZED_KEY": CONSOLE_KEY,
                "SAZED_WORKER_KEY": WORKER_KEY,
            }),
            mock.patch.object(sazed_api, "SAZED_URL", f"http://127.0.0.1:{self.server.server_port}"),
            mock.patch.object(sazed_api, "WORKER_KEY", WORKER_KEY),
            mock.patch.object(worker_config, "_agent_paths", return_value=paths),
            mock.patch.object(repo_module, "clone_url", return_value=str(self.source)),
            mock.patch.object(dispatch_job, "BEAT_S", 0.2),
            mock.patch.object(dispatch_job, "TICK_S", 0.1),
        ):  # fmt: skip
            patcher.start()
            self.addCleanup(patcher.stop)
        self.unprivileged()
        self.job_id = str(uuid.uuid4())
        self.job_dir = self.jobs_dir / self.job_id

    def unprivileged(self) -> None:
        self.as_unprivileged_agent()
        patcher = mock.patch.object(unattended, "host_refusal", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, tasks=("api", "ui"), plan=None, config=None, **params) -> dict:
        plan = {
            "id": "p1",
            "check": "env && sh check.sh",
            "tasks": [{"id": t, "goal": "g", "done": "d", "owns": [f"src/{t}/**"]} for t in tasks],
        } | (plan or {})
        config = config or {}
        return {
            "id": self.job_id, "kind": "dispatch", "agent": "claude", "profile": "dispatch",
            "model": "", "prompt": "The PR", "resume_session_id": None, "parent_id": None,
            "clone_token": CLONE_TOKEN,
            "params": {
                "plan_id": plan["id"], "plan": plan, "config": config,
                "plan_hash": admission.plan_hash(plan, config), "repo": "o/r", "base": "main",
                "private": True, "budget_usd": 5.0, "task_caps": {t: 2.0 for t in tasks},
                "timeout_s": 600, "parallel": 1, "title": "The PR", "body": "Why this.",
                "board_card": None,
            } | params,
        }  # fmt: skip

    def finish(self, job: dict) -> dict:
        self.err = io.StringIO()
        with contextlib.redirect_stderr(self.err):
            return jobs.run_job(job)

    def posts(self, path: str) -> list[dict]:
        return [body for p, _, body in self.server.seen if p == path]

    def rows(self, task: str) -> list[dict]:
        return [r for r in self.posts("/worker/dispatch/rows") if r["task"] == task]

    def assert_failed(self, payload: dict, reason: str, says: str) -> None:
        self.assertEqual((payload["status"], payload["meta"]["reason"]), ("failed", reason))
        self.assertIn(says, payload["error"])
        self.assertIn("cost_usd", payload["meta"])
        self.assertIn("tasks", payload["meta"])
        self.assertEqual(self.posts("/worker/dispatch/plans/p1/export"), [])


class Routing(unittest.TestCase):
    def test_only_the_dispatch_kind_reaches_the_handler(self):
        with mock.patch.object(jobs, "run_dispatch", return_value={"status": "x"}) as handler:
            self.assertEqual(jobs.run_job({"kind": "dispatch", "agent": "claude"}), {"status": "x"})
            for kind in ("research", "second_opinion", "code_task", None):
                with (
                    self.subTest(kind=kind),
                    mock.patch.object(jobs, "workspace", side_effect=JobFailed("as before")),
                    self.assertRaisesRegex(JobFailed, "as before"),
                ):
                    jobs.run_job({"id": "x", "kind": kind, "agent": "claude"})
        self.assertEqual(handler.call_count, 1)


    def test_the_claim_names_the_kinds_this_worker_runs(self):
        """A8: Sazed gives a dispatch job only to a worker that lists the kind."""
        bodies = []

        def post(path, body):
            bodies.append((path, body))
            stop.set()
            return 204, None

        self.addCleanup(stop.clear)
        with (
            mock.patch.object(main_module, "post", post),
            mock.patch.object(main_module, "SAZED_URL", "http://s.test"),
            mock.patch.object(main_module, "WORKER_KEY", "k"),
            mock.patch.object(main_module.os, "geteuid", return_value=0),
            mock.patch.object(main_module, "JOBS_DIR"),
            mock.patch.object(main_module.shutil, "chown"),
            mock.patch.object(main_module.signal, "signal"),
            mock.patch.object(main_module.limits, "probe"),
            mock.patch.object(main_module, "_cleanup_workspaces"),
        ):
            main_module.main()
        ((path, body),) = bodies
        self.assertEqual(path, "/worker/claim")
        self.assertEqual(body["kinds"],
                         ["research", "second_opinion", "code_task", "dispatch"])
        self.assertEqual(sorted(body), ["agents", "kinds", "worker"])


class Refused(JobCase):
    def test_a_hash_mismatch_is_refused_before_anything_is_read_or_written(self):
        job = self.job()
        job["params"]["plan"]["tasks"][0]["owns"] = ["**"]
        payload = self.finish(job)
        self.assert_failed(payload, "refused", "the plan's hash is not the approved one")
        self.assertEqual((self.server.seen, list(self.jobs_dir.iterdir())), ([], []))

    def test_a_changed_config_is_a_hash_mismatch_too(self):
        job = self.job()
        job["params"]["config"] = {"check": "true"}
        self.assert_failed(self.finish(job), "refused", "the plan's hash")

    def test_params_that_are_no_plan_or_could_leave_the_job_folder_are_refused(self):
        bad_task = {"id": "../../x", "goal": "g", "done": "d", "owns": ["a/**"]}
        for job, says in (
            (self.job() | {"params": {}}, "not a dispatch plan"),
            (self.job(plan_id="p2"), "names another plan"),
            (self.job(parallel=9), "parallel 9 is outside"),
            (self.job(plan={"id": "../p1"}, plan_id="../p1"), "plan id '../p1'"),
            (self.job(plan={"tasks": [bad_task]}), "task id '../../x'"),
            (self.job(plan={"interface_files": {"../x": ""}}), "interface file '../x'"),
            (self.job(base="--upload-pack=x"), "invalid base"),
        ):
            with self.subTest(says=says):
                payload = self.finish(job)
                self.assertEqual(payload["status"], "failed")
                self.assertIn(says, payload["error"])
                self.assertFalse((self.job_dir / "repo").exists())
                if self.job_dir.exists():
                    self.job_dir.rmdir()

    def test_a_job_folder_that_already_exists_is_never_reused(self):
        self.job_dir.mkdir()
        with self.assertLogs("sazed-worker", "ERROR"):
            payload = self.finish(self.job())
        self.assert_failed(payload, "wrapper_failed", "FileExistsError")
        self.assertEqual(list(self.job_dir.iterdir()), [])

    def test_a_cap_over_the_approved_one_is_refused_before_any_task(self):
        payload = self.finish(self.job(task_caps={"api": 2.0, "ui": 1.99}))
        self.assert_failed(payload, "budget", "task ui now caps at $2.00, over its approved $1.99")
        self.assertEqual(payload["meta"]["tasks"], {"api": "not started", "ui": "not started"})
        self.assertEqual(self.posts("/worker/dispatch/rows"), [])
        self.assertFalse((self.job_dir / "dispatch" / "p1" / "integ").exists())

    def test_a_task_with_no_budget_or_no_approved_cap_or_a_sum_over_the_budget(self):
        self.assert_failed(self.finish(self.job(task_caps={"api": 2.0})), "budget",
                           "task ui has no approved cap")
        self.job_id = str(uuid.uuid4())
        self.assert_failed(self.finish(self.job(budget_usd=3.9)), "budget", "add up to $4.00")
        self.job_id = str(uuid.uuid4())
        self.server.profiles = [r | {"budget_usd": None} for r in registry.ROWS]
        self.assert_failed(self.finish(self.job()), "budget", "task api has no budget_usd")

    def test_caps_that_only_meet_the_budget_after_rounding_pass(self):
        caps = {"api": 1.1, "ui": 2.2}
        plan = {"tasks": [{"id": t, "goal": "g", "done": "d", "owns": [f"src/{t}/**"],
                           "budget_usd": c} for t, c in caps.items()]}  # fmt: skip
        work = dispatch_job.approved(
            self.job(plan=plan, task_caps=caps, budget_usd=3.3)["params"]
        )
        registry.install()
        self.assertIsNone(dispatch_job.over_cap(work, {}))

    def test_preflight_s_refusals_are_the_job_s(self):
        self.assert_failed(self.finish(self.job(config={"profiles": {"implement": {
            "engine": "agy"}}})), "refused", "runs on agy")  # fmt: skip
        self.job_id = str(uuid.uuid4())
        self.server.profiles = []
        self.assert_failed(self.finish(self.job()), "refused", "fresh from Sazed")

    def test_an_interface_file_behind_a_link_in_the_repository_is_refused(self):
        (self.source / "out").symlink_to("/tmp")
        git("add", "out", cwd=self.source)
        git("commit", "-q", "-m", "link", cwd=self.source)
        for rel in ("out/x.txt", "out"):
            self.job_id = str(uuid.uuid4())
            job = self.job(plan={"interface_files": {rel: "x"}})
            self.assert_failed(self.finish(job), "refused", "is not a plain file")

    def test_a_clone_that_fails_is_the_worker_s_failure_and_never_shows_the_token(self):
        payload = self.finish(self.job(base="absent"))
        self.assert_failed(payload, "wrapper_failed", "git clone failed")
        self.assertIn(CLONE_TOKEN, scrub.extra_secrets)


class Clone(JobCase):
    def test_root_s_git_has_the_token_in_its_environment_and_no_worker_secret(self):
        seen = {}

        def popen(cmd, **kwargs):
            seen.update(cmd=cmd, **kwargs)
            raise OSError("stop here")

        with (
            mock.patch.object(repo_module.subprocess, "Popen", popen),
            self.assertLogs("sazed-worker", "ERROR"),
        ):
            self.finish(self.job())
        self.assertEqual(seen["cmd"][:4], ["git", "-c", "core.hooksPath=/dev/null", "clone"])
        self.assertNotIn(CLONE_TOKEN, " ".join(seen["cmd"]))
        self.assertEqual(seen["env"]["GIT_TOKEN"], CLONE_TOKEN)
        self.assertEqual(seen["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(seen["env"]["GIT_CEILING_DIRECTORIES"], str(self.jobs_dir))
        self.assertNotIn(WORKER_KEY, str(seen["env"]))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen["env"])
        self.assertNotIn("user", seen)
        self.assertEqual(seen["cwd"], self.job_dir)

    def test_the_url_comes_from_a_valid_repo_name_only(self):
        mock.patch.stopall()
        for bad in ("o", "o/r/x", "../r/x", "o/r\n", "https://evil/x", "-o/"):
            with self.subTest(bad=bad), self.assertRaises(JobFailed):
                repo_module.clone_url(bad)
        self.assertEqual(repo_module.clone_url("o/r"), "https://github.com/o/r.git")


class Stops(JobCase):
    """No jail is needed: the agent hangs until the stop."""

    def agent_is_running(self) -> bool:
        return any(r["state"] == "running" for r in self.posts("/worker/dispatch/rows"))

    def when_running(self, act) -> None:
        def wait() -> None:
            while not self.agent_is_running():
                time.sleep(0.02)
            act()

        threading.Thread(target=wait, daemon=True).start()

    def assert_agents_gone(self) -> None:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
        for pid in pids:
            with contextlib.suppress(OSError), open(f"/proc/{pid}/cmdline") as f:
                self.assertNotIn(str(self.tmp / "recorder.py"), f.read())

    def test_a_cancel_kills_the_agent_starts_nothing_else_and_finishes_cancelled(self):
        self.modes(api="hang", ui="hang")
        self.when_running(self.server.cancel.set)
        payload = self.finish(self.job())
        self.assert_failed(payload, "cancelled", "cancelled")
        self.assertEqual(payload["meta"]["tasks"], {"api": "failed", "ui": "not started"})
        self.assert_agents_gone()
        self.assertEqual(self.rows("api")[-1]["meta"]["reason"], "cancelled")

    def test_the_wall_clock_cap_kills_the_agents_and_writes_timed_out(self):
        self.modes(api="hang", ui="hang")
        started = time.monotonic()
        payload = self.finish(self.job(timeout_s=4, parallel=2))
        self.assertLess(time.monotonic() - started, 40)
        self.assert_failed(payload, "timeout", "passed its wall-clock cap of 4 s")
        self.assertEqual(payload["meta"]["tasks"], {"api": "timed_out", "ui": "timed_out"})
        self.assert_agents_gone()
        for task in ("api", "ui"):
            row = self.rows(task)[-1]
            self.assertEqual((row["state"], row["meta"]["reason"]), ("timed_out", "timeout"))

    def test_a_worker_shutdown_stops_the_plan(self):
        self.modes(api="hang")
        self.when_running(stop.set)
        payload = self.finish(self.job(tasks=("api",)))
        self.assert_failed(payload, "cancelled", "the worker shut down")
        self.assert_agents_gone()

    def test_a_plan_limit_sets_rate_limited_as_claude_jobs_do(self):
        self.modes(api="ratelimit")
        payload = self.finish(self.job())
        self.assert_failed(payload, "plan_limit", "five hour plan limit; it resets at")
        self.assertEqual(payload["meta"]["rate_limited"],
                         {"window": "five_hour", "resets_at": 1794103800})
        self.assertEqual(payload["meta"]["tasks"], {"api": "failed", "ui": "not started"})

    def test_a_task_that_waits_ends_the_job_without_a_pr(self):
        self.modes(api="waiting")
        payload = self.finish(self.job(tasks=("api",)))
        self.assert_failed(payload, "no_done_state", "task api is waiting: Which file")

    def test_an_error_mid_run_leaves_no_task_running(self):
        with (
            mock.patch.object(dispatch_job.integrate, "go", side_effect=self.crash),
            self.assertLogs("sazed-worker", "ERROR"),
        ):
            payload = self.finish(self.job())
        self.assert_failed(payload, "wrapper_failed", "RuntimeError: boom")
        self.assertEqual(payload["meta"]["tasks"], {"api": "failed", "ui": "not started"})
        self.assertEqual(self.rows("api")[-1]["state"], "failed")

    def crash(self, plan, ws, board, *args):
        board.write("api", "running", "started")
        raise RuntimeError("boom")

    def test_the_job_beats_the_whole_time_and_the_process_is_put_back(self):
        self.modes(api="hang")
        before = {k: v for k, v in os.environ.items()}
        umask = os.umask(0o027)
        self.addCleanup(os.umask, umask)
        self.finish(self.job(tasks=("api",), timeout_s=3))
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(os.umask(0o027), 0o027)
        beats = self.posts(f"/worker/jobs/{self.job_id}/events")
        self.assertGreater(len(beats), 5)
        self.assertEqual(beats[0], {"events": []})
        self.assertIn("/events", self.server.seen[0][0])

    def test_the_beat_is_inside_sazed_s_grace(self):
        mock.patch.stopall()
        self.assertLessEqual(dispatch_job.BEAT_S, 30)


class HappyPath:
    """Shared by the unprivileged run and the one as root."""

    def lands_and_exports(self) -> None:
        self.modes(api="noshell", ui="noshell")
        payload = self.finish(self.job(plan={"interface_files": {"src/types.txt": "shared\n"}}))
        self.assertEqual(payload, {
            "status": "succeeded",
            "result": f"Draft pull request: {PR}",
            "meta": {
                "pr_url": PR, "pr_number": 7, "cost_usd": 0.0,
                "tasks": {"api": "landed", "ui": "landed"},
                "plan_hash": self.job(
                    plan={"interface_files": {"src/types.txt": "shared\n"}}
                )["params"]["plan_hash"],
            },
        }, self.err.getvalue())  # fmt: skip
        (sent,) = self.posts("/worker/dispatch/plans/p1/export")
        self.assertEqual(sent["plan_hash"], payload["meta"]["plan_hash"])
        self.assertEqual((sent["repo"], sent["base"], sent["title"]), ("o/r", "main", "The PR"))
        self.assertTrue(sent["body"].startswith("Why this.\n\nDispatch plan p1.\n\n- api: "))
        self.assertEqual(sorted(sent["files"]),
                         ["src/api/api.txt", "src/types.txt", "src/ui/ui.txt"])
        self.assertEqual([r["state"] for r in self.rows("api")], ["running", "done", "landed"])
        for path, headers, _ in self.server.seen:
            self.assertTrue(path.startswith("/worker/"), path)
            self.assertEqual(headers["X-Worker-Key"], WORKER_KEY)
            if path.startswith("/worker/dispatch/"):
                self.assertEqual(headers["X-Dispatch-Job"], self.job_id)
            self.assertNotIn(CONSOLE_KEY, str(headers))

    def no_secret_reaches_an_agent_or_the_jail(self) -> None:
        self.lands_and_exports()
        log = self.job_dir / "dispatch" / "p1" / "runs" / "api" / "agent.jsonl"
        jail = log.read_text()
        self.assertIn("RUFF_CACHE_DIR=", jail)
        agents = self.record.read_text()
        self.assertEqual(agents.count("\n"), 2)
        for text in (jail, agents):
            for secret in (WORKER_KEY, CONSOLE_KEY, CLONE_TOKEN, "SAZED_WORKER_KEY",
                           "DISPATCH_SAZED", "SAZED_URL", "GIT_TOKEN"):
                self.assertNotIn(secret, text)
        config = (self.job_dir / "repo" / ".git" / "config").read_text()
        self.assertNotIn(CLONE_TOKEN, config)
        self.assertEqual(oct((self.job_dir / "plan.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.job_dir.stat().st_mode & 0o777), "0o711")


@needs_jail
@unittest.skipIf(os.geteuid() == 0, "AsRoot covers root")
@unittest.skipIf(BUSY, "this user runs too many threads for run_check's process cap")
class EndToEnd(JobCase, HappyPath):
    def test_an_approved_plan_lands_and_exports_with_its_hash(self):
        self.no_secret_reaches_an_agent_or_the_jail()

    def test_a_failing_check_is_the_job_s_reason_and_nothing_is_exported(self):
        self.modes(api="noshell", ui="noshell-nochange")
        payload = self.finish(self.job())
        self.assert_failed(payload, "no_change", "task ui is failed: changed nothing")
        self.assertEqual(payload["meta"]["tasks"], {"api": "landed", "ui": "failed"})
        self.job_id = str(uuid.uuid4())
        self.modes(api="noshell")
        payload = self.finish(self.job(tasks=("api",), plan={"check": "exit 3"}))
        self.assert_failed(payload, "check_failed", "check failed after landing")

    def test_an_export_sazed_refuses_fails_the_job_with_its_reason(self):
        self.modes(api="noshell")
        for reason in ("refused", "left_owns"):
            with self.subTest(reason=reason):
                self.job_id = str(uuid.uuid4())
                self.server.export = (403, {"detail": "not approved", "reason": reason})
                payload = self.finish(self.job(tasks=("api",)))
                self.assertEqual(
                    (payload["status"], payload["meta"]["reason"], payload["error"]),
                    ("failed", reason, "not approved"),
                )
                self.assertEqual(payload["meta"]["tasks"], {"api": "landed"})

    def test_a_base_that_moved_is_said_in_the_result_only(self):
        self.modes(api="noshell")
        self.server.export = (200, {"url": PR, "base_moved": True})
        payload = self.finish(self.job(tasks=("api",)))
        self.assertEqual((payload["meta"]["pr_url"], payload["meta"]["pr_number"]), (PR, 7))
        self.assertIn("base_moved", payload["result"])


@needs_root
class AsRoot(JobCase, HappyPath):
    """Run as root in WSL with SAZED_AGENT_USER naming an ordinary user (never in CI)."""

    def unprivileged(self) -> None:
        for path in (self.tmp, self.jobs_dir):
            path.chmod(0o755)
        os.chown(self.jobs_dir, worker_config.AGENT_UID, worker_config.AGENT_GID)

    def test_the_job_runs_behind_the_real_preflight_with_a_root_owned_clone(self):
        self.no_secret_reaches_an_agent_or_the_jail()
        for path in (self.job_dir, self.job_dir / "repo", self.job_dir / "repo" / ".git",
                     self.job_dir / "repo" / "check.sh"):  # fmt: skip
            self.assertEqual(path.stat().st_uid, 0, path)
        agent = worker_config.AGENT_UID
        for command in (["ls", str(self.job_dir)], ["cat", str(self.job_dir / "plan.json")],
                        ["touch", str(self.job_dir / "repo" / "x")]):  # fmt: skip
            run = subprocess.run(command, user=agent, capture_output=True, cwd="/")
            self.assertNotEqual(run.returncode, 0, command)

    def test_the_wall_clock_cap_kills_agents_that_run_as_the_agent_user(self):
        Stops.test_the_wall_clock_cap_kills_the_agents_and_writes_timed_out(self)

    assert_agents_gone = Stops.assert_agents_gone


if __name__ == "__main__":
    unittest.main()
