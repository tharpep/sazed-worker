"""Pins how a worker job starts its agent: argv, environment, user and cwd per job kind (S2-7).

Written against the launch code before seam 2 and kept unchanged after it.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from sazed_worker import config, jobs
from sazed_worker.agent_process import JobFailed
from sazed_worker.checks import checks_prompt
from sazed_worker.report import repair_prompt, report_problem

REAL_POPEN = subprocess.Popen
SECRET_READS = [
    "Read(~/.claude/**)",
    "Read(~/.claude.json)",
    "Read(~/.gemini/**)",
    "Read(//proc/**)",
    "Read(//etc/sazed-eco/**)",
]
CLAUDE_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "the answer",
    "session_id": "s1",
    "num_turns": 2,
}


def agy_result(response: str, denied: list[str] | None = None) -> dict:
    result = {"conversation_id": "c1", "status": "SUCCESS", "response": response}
    if denied:
        result["denied_actions"] = denied
    return {"event": "result", "result": result}


def tool_use(i: int) -> dict:
    block = {"type": "tool_use", "id": f"toolu_{i}", "name": "Read", "input": {"file_path": "a"}}
    return {"type": "assistant", "message": {"content": [block]}}


def base_env(**extra: str) -> dict[str, str]:
    home = str(config.AGENT_HOME)
    return {
        "HOME": home,
        "USER": config.AGENT_USER,
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
        **extra,
    }


CLAUDE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": config.CLAUDE_TOKEN, "DISABLE_AUTOUPDATER": "1"}


class FakePopen:
    """Records each launch, then starts a python process that prints the scripted events."""

    def __init__(self, *outputs: list[dict], hang: bool = False):
        self.outputs = list(outputs)
        self.hang = hang
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        events = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        text = "".join(json.dumps(e) + "\n" for e in events)
        script = f"import sys, time; sys.stdout.write({text!r}); sys.stdout.flush()"
        if self.hang:
            script += "; time.sleep(60)"
        for key in ("user", "group", "extra_groups"):
            kwargs.pop(key, None)
        return REAL_POPEN([sys.executable, "-c", script], **kwargs)


class WorkerLaunch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seam2-worker-"))
        self.ws = self.tmp / "ws"
        self.repo = self.tmp / "repo"
        self.work = self.tmp / "work"
        for d in (self.ws, self.repo, self.work):
            d.mkdir()
        self.posts: list[tuple[str, dict]] = []
        self.checks = mock.MagicMock()
        self.checks.run.return_value = []
        self.checks.formatted = False
        self.checks.meta.return_value = {}
        self.tests = mock.MagicMock()
        self.tests.run.return_value = []
        self.tests.meta.return_value = {}
        patches = [
            mock.patch.object(jobs, "workspace", return_value=self.ws),
            mock.patch.object(jobs, "repo_instructions", return_value=""),
            mock.patch.object(jobs, "pr_diff", return_value=""),
            mock.patch.object(jobs, "pr_checks", return_value=("", {})),
            mock.patch.object(jobs, "changed_files", return_value={}),
            mock.patch.object(jobs, "Checks", return_value=self.checks),
            mock.patch.object(jobs, "TestRun", return_value=self.tests),
            mock.patch.object(jobs, "JOBS_DIR", self.tmp),
            mock.patch("sazed_worker.run.post", side_effect=self.post),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, path, body, timeout=30):
        self.posts.append((path, body))
        return 200, {}

    def run_job(self, job: dict, fake: FakePopen, prepared=None) -> dict:
        prepared = prepared or (self.repo, self.work, job.get("resume_session_id"))
        with (
            mock.patch.object(jobs, "prepare_repo", return_value=prepared),
            mock.patch("subprocess.Popen", fake),
        ):
            return jobs.run_job(job)

    def popen_kwargs(self, cwd: Path, env: dict) -> dict:
        user = config.AGENT_USER
        return {
            "cwd": cwd,
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "start_new_session": True,
            "user": user,
            "group": user,
            "extra_groups": [],
        }

    def claude_argv(self, tools: str, allowed: list[str], prompt: str, resume=None) -> list:
        argv = [
            config.CLAUDE,
            "--print",
            "--output-format=stream-json",
            "--verbose",
            "--model=sonnet",
            "--permission-mode=dontAsk",
            "--setting-sources=user",
            "--strict-mcp-config",
            f"--tools={tools}",
            "--allowedTools",
            *allowed,
            "--disallowedTools",
            *SECRET_READS,
        ]
        if resume:
            argv.append(f"--resume={resume}")
        return [*argv, "--", prompt]

    def job(self, **fields) -> dict:
        return {"id": str(uuid.uuid4()), "model": "sonnet", "params": {}, **fields}

    def test_claude_review_with_a_pr_starts_read_only_as_the_agent_user(self):
        job = self.job(agent="claude", profile="review", prompt="Review it.",
                       params={"pr_number": 7})
        fake = FakePopen([CLAUDE_RESULT])
        out = self.run_job(job, fake)
        self.assertEqual(out["status"], "succeeded")
        argv, kwargs = fake.calls[0]
        tools, allowed = "Read,Grep,Glob", ["Read", "Grep", "Glob"]
        self.assertEqual(argv, self.claude_argv(tools, allowed, "Review it."))
        self.assertEqual(kwargs, self.popen_kwargs(self.work, base_env(**CLAUDE_ENV)))
        posted = [e for path, body in self.posts for e in body["events"]]
        self.assertEqual({p for p, _ in self.posts}, {f"/worker/jobs/{job['id']}/events"})
        self.assertEqual(posted, [CLAUDE_RESULT])

    def test_claude_resume_passes_the_session(self):
        job = self.job(agent="claude", profile="review", prompt="And then?",
                       resume_session_id="sess-1")
        fake = FakePopen([CLAUDE_RESULT])
        self.run_job(job, fake)
        tools, allowed = "Read,Grep,Glob", ["Read", "Grep", "Glob"]
        self.assertEqual(
            fake.calls[0][0], self.claude_argv(tools, allowed, "And then?", resume="sess-1")
        )

    def test_claude_change_runs_a_fix_round_and_a_repair_round_in_the_same_session(self):
        self.checks.run.side_effect = [["a.py:1: F401 unused"], []]
        job = self.job(agent="claude", profile="change", prompt="Change it.")
        fake = FakePopen([CLAUDE_RESULT])
        out = self.run_job(job, fake)
        self.assertEqual(out["meta"]["rounds"], 3)
        tools = "Read,Grep,Glob,Edit,Write"
        allowed = ["Read", "Grep", "Glob", "Edit(./**)", "Write(./**)"]
        fix = checks_prompt(["a.py:1: F401 unused"])
        repair = repair_prompt(report_problem("the answer"))
        self.assertEqual(
            [argv for argv, _ in fake.calls],
            [
                self.claude_argv(tools, allowed, "Change it."),
                self.claude_argv(tools, allowed, fix, resume="s1"),
                self.claude_argv(tools, allowed, repair, resume="s1"),
            ],
        )
        for _, kwargs in fake.calls:
            self.assertEqual(kwargs, self.popen_kwargs(self.work, base_env(**CLAUDE_ENV)))

    def test_agy_research_runs_in_the_workspace_with_effort_and_timeout(self):
        job = self.job(agent="agy", kind="research", model="gemini-3.8-pro",
                       prompt="Look it up.", params={"timeout_s": 120, "effort": "high"})
        fake = FakePopen([agy_result("found")])
        out = self.run_job(job, fake)
        self.assertEqual(out["status"], "succeeded")
        argv, kwargs = fake.calls[0]
        self.assertEqual(
            argv,
            [
                config.AGY,
                "--prompt=Look it up.",
                "--output-format=stream-json",
                "--model=gemini-3.8-pro",
                "--print-timeout=120s",
                "--effort=high",
            ],
        )
        self.assertEqual(kwargs, self.popen_kwargs(self.ws, base_env()))

    def test_agy_review_runs_in_the_checkout(self):
        job = self.job(agent="agy", profile="review", model="gemini-3.8-pro", prompt="Review.")
        fake = FakePopen([agy_result("fine")])
        self.run_job(job, fake, prepared=(self.repo, self.repo, None))
        argv, kwargs = fake.calls[0]
        self.assertEqual(
            argv,
            [
                config.AGY,
                "--prompt=Review.",
                "--output-format=stream-json",
                "--model=gemini-3.8-pro",
                "--print-timeout=300s",
            ],
        )
        self.assertEqual(kwargs["cwd"], self.repo)

    def test_agy_follow_up_resumes_in_the_parent_workspace(self):
        parent = str(uuid.uuid4())
        (self.tmp / parent).mkdir()
        job = self.job(agent="agy", model="gemini-3.8-pro", prompt="More.",
                       resume_session_id="c0", parent_id=parent)
        fake = FakePopen([agy_result("more")])
        self.run_job(job, fake)
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv[-1], "--conversation=c0")
        self.assertEqual(kwargs["cwd"], self.tmp / parent)

    def test_agy_empty_answer_after_a_denial_resumes_once(self):
        job = self.job(agent="agy", model="gemini-3.8-pro", prompt="Q?")
        fake = FakePopen([agy_result("", ["run_command"])])
        self.run_job(job, fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(
            fake.calls[1][0],
            [
                config.AGY,
                f"--prompt={jobs._EMPTY_RETRY_PROMPT}",
                "--output-format=stream-json",
                "--model=gemini-3.8-pro",
                "--print-timeout=300s",
                "--conversation=c1",
            ],
        )

    def test_the_default_tool_cap_stops_the_agent_at_40_calls(self):
        job = self.job(agent="claude", profile="review", prompt="Read a lot.")
        fake = FakePopen([tool_use(i) for i in range(45)], hang=True)
        out = self.run_job(job, fake)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["error"], "stopped at the 40-tool-call cap")
        self.assertEqual(out["meta"]["stop_reason"], "tool_cap")
        self.assertGreaterEqual(out["meta"]["tool_calls"], 40)

    def test_a_bad_model_is_refused_before_anything_starts(self):
        job = self.job(agent="claude", profile="review", prompt="x", model="bad model")
        fake = FakePopen([CLAUDE_RESULT])
        with self.assertRaises(JobFailed):
            self.run_job(job, fake)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
