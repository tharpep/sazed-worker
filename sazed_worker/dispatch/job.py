"""A claimed dispatch job: one approved plan, run unattended from its hash check to its draft PR."""

import contextlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sazed_worker import config, repo
from sazed_worker.agent_process import JobFailed, kill_group
from sazed_worker.dispatch import (
    admission,
    integrate,
    mcp,
    pr_export,
    profiles,
    session,
    unattended,
)
from sazed_worker.dispatch import plan as plans
from sazed_worker.dispatch.board import RED, Board
from sazed_worker.dispatch.halt import ROWS, Halt
from sazed_worker.dispatch.mirror import JobMirror
from sazed_worker.dispatch.reasons import REASONS
from sazed_worker.dispatch.run_agent import limit_line
from sazed_worker.run import JobEvents

BEAT_S = 20
TICK_S = 1
TRAVERSE_ONLY = 0o711
ROOT_ONLY = 0o600
ERROR_MAX = 500
PARALLEL_MAX = 4
PR_NUMBER = re.compile(r"/pull/(\d+)")


class Stopped(RuntimeError):
    """The job ends without a PR, for a reason code of the worker's and in one sentence."""

    def __init__(self, reason: str, sentence: str):
        super().__init__(sentence)
        self.reason = reason


@dataclass(frozen=True)
class Approved:
    """A job's params, whose plan and config hash to what Pryce approved."""

    plan: plans.Plan
    plan_json: dict[str, Any]
    config: dict[str, Any]
    plan_hash: str
    repo: str
    base: str
    task_caps: dict[str, float]
    budget_usd: float
    timeout_s: int
    parallel: int
    title: str
    body: str | None


def approved(params: dict[str, Any]) -> Approved:
    """The params once their hash matches, or Stopped; it reads nothing else and writes nothing."""
    try:
        digest = admission.plan_hash(params["plan"], params["config"])
        if digest != params["plan_hash"]:
            raise Stopped("refused", "the plan's hash is not the approved one")
        plan = plans.parse(params["plan"])
        found = Approved(
            plan=plan,
            plan_json=params["plan"],
            config=dict(params["config"]),
            plan_hash=digest,
            repo=params["repo"],
            base=params["base"],
            task_caps={t: float(cap) for t, cap in params["task_caps"].items()},
            budget_usd=float(params["budget_usd"]),
            timeout_s=int(params["timeout_s"]),
            parallel=int(params["parallel"]),
            title=params["title"],
            body=params.get("body"),
        )
        same_id = params["plan_id"] == plan.id
        sentence = admission.name_refusal(plan)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise Stopped("refused", f"the job's params are not a dispatch plan: {e!r}") from e
    if not same_id:
        sentence = "the job names another plan than the one it carries"
    elif not 1 <= found.parallel <= PARALLEL_MAX:
        sentence = f"parallel {found.parallel} is outside 1 to {PARALLEL_MAX}"
    if sentence:
        raise Stopped("refused", sentence)
    return found


def over_cap(work: Approved, settings: dict[str, Any]) -> str | None:
    """Why the caps resolved now exceed what was approved; budgets can change after a submit."""
    try:
        resolved = admission.caps(work.plan, settings)
    except plans.PlanError as e:
        return str(e)
    for task, cap in resolved.items():
        allowed = work.task_caps.get(task)
        if allowed is None:
            return f"task {task} has no approved cap"
        if cap > allowed:
            return f"task {task} now caps at ${cap:.2f}, over its approved ${allowed:.2f}"
    return admission.over_budget(work.task_caps, work.budget_usd)


@contextlib.contextmanager
def _process_state(dispatch_home: Path) -> Iterator[None]:
    """This process's umask and environment for one plan, put back for the next job."""
    names = ("DISPATCH_HOME", *unattended.ROOT_GIT_ENV)
    before = {name: os.environ.get(name) for name in names}
    umask = os.umask(session.SHARED_UMASK)
    os.environ["DISPATCH_HOME"] = str(dispatch_home)
    try:
        yield
    finally:
        os.umask(umask)
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def _watched(job_id: str, halt: Halt, timeout_s: int) -> Iterator[None]:
    """Beat to Sazed, and stop the plan on a cancel, the wall-clock cap or a shutdown. A beat can
    wait on Sazed, so the cap and the shutdown have a thread of their own."""
    done = threading.Event()
    deadline = time.monotonic() + timeout_s
    events = JobEvents(job_id)

    def beat() -> None:
        while True:
            if events.beat(force=True):
                halt.stop("cancelled")
            if done.wait(BEAT_S):
                return

    def tick() -> None:
        while not done.wait(TICK_S):
            if time.monotonic() >= deadline:
                halt.stop("timed_out")
            if config.stop.is_set():
                halt.stop("shutdown")

    threads = [threading.Thread(target=target, daemon=True) for target in (beat, tick)]
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        done.set()
        for thread in threads:
            thread.join(timeout=5)


def _job_dir(job_id: str) -> Path:
    """A fresh root-owned folder the agent may pass through; one that exists is not ours."""
    path = config.JOBS_DIR / job_id
    path.mkdir()
    path.chmod(TRAVERSE_ONLY)
    return path


def _keep(path: Path, data: Any) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, ROOT_ONLY), "w") as f:
        json.dump(data, f, indent=2)


def _first_miss(plan: plans.Plan, board: Board, settings: dict[str, Any]) -> Stopped | None:
    """Why a code task did not land: a red task first, else the first one held back."""
    missed = [
        t.id
        for t in plan.tasks
        if not plans.resolve(t, settings)["read_only"] and board.state(t.id) != "landed"
    ]
    if not missed:
        return None
    latest = board.latest()
    everyone = [t.id for t in plan.tasks if t.id in latest]
    red = [t for t in everyone if latest[t]["state"] in RED]
    task = next(iter([*red, *(t for t in missed if t in latest)]), None)
    if task is None:
        sentence = f"no task started, so {', '.join(missed)} never landed"
        return Stopped("dependency_pending", sentence)
    row = latest[task]
    reason = row["meta"].get("reason") or "no_done_state"
    return Stopped(reason, f"task {task} is {row['state']}: {row['line']}")


class DispatchJob:
    def __init__(self, job: dict[str, Any], work: Approved):
        self.id = str(uuid.UUID(str(job["id"])))
        self.token = job.get("clone_token") or None
        self.work = work
        self.halt = Halt()
        self.board: Board | None = None
        self.settings = work.config | {"unattended": True}

    def run(self) -> dict[str, Any]:
        with _watched(self.id, self.halt, self.work.timeout_s):
            try:
                with _process_state(config.JOBS_DIR / self.id / "dispatch"):
                    url = self._run()
            except Exception as e:
                ended = self._ended(e)
                self._settle(ended)
                return self._failed(ended)
        return self._succeeded(url)

    def _check_halt(self) -> None:
        if config.stop.is_set():
            self.halt.stop("shutdown")
        if self.halt.reason:
            raise Stopped(ROWS[self.halt.reason][1], self._halt_sentence())

    def _halt_sentence(self) -> str:
        if self.halt.reason == "timed_out":
            return f"passed its wall-clock cap of {self.work.timeout_s} s"
        if self.halt.reason == "rate_limit":
            return limit_line(self.halt.rate_limited)
        return "the worker shut down" if self.halt.reason == "shutdown" else "cancelled"

    def _run(self) -> str:
        work, plan = self.work, self.work.plan
        job_dir = _job_dir(self.id)
        checkout = job_dir / "repo"
        repo.clone_as_caller(
            repo.clone_url(work.repo),
            work.base,
            checkout,
            self.token,
            unattended.ROOT_GIT_ENV,
            lambda proc: self.halt.over(lambda _: kill_group(proc)),
        )
        self._check_halt()
        _keep(job_dir / "plan.json", work.plan_json)
        _keep(job_dir / "config.json", work.config)
        sentence = unattended.interface_refusal(plan, checkout)
        if sentence:
            raise Stopped("refused", sentence)
        profiles.load(unattended=True)
        mcp.load(unattended=True)
        home = job_dir / "dispatch" / plan.id
        ws, self.board = session.prepare(
            plan, self.settings, checkout, home, JobMirror(plan.id)
        )
        assert isinstance(ws, unattended.UnattendedWorkspace)
        ws.halt = self.halt
        sentence = over_cap(work, self.settings)
        if sentence:
            raise Stopped("budget", sentence)
        integrate.go(plan, ws, self.board, work.base, self.settings, work.parallel, self.halt)
        self._check_halt()
        missed = _first_miss(plan, self.board, self.settings)
        if missed:
            raise missed
        generated = pr_export.body(plan, ws, self.board, pr_export.base_sha(ws, work.base))
        body = "\n\n".join(part for part in (work.body, generated) if part)
        return integrate.export(
            plan, ws, self.board, work.base, work.title, body, self.settings,
            plan_hash=work.plan_hash,
        )  # fmt: skip

    def _ended(self, e: Exception) -> Stopped:
        """Every way the run ends, as a reason code; a stop explains whatever it broke."""
        if isinstance(e, pr_export.ExportUnclear):
            return Stopped("export_unreachable", str(e))
        if self.halt.reason:
            return Stopped(ROWS[self.halt.reason][1], self._halt_sentence())
        if isinstance(e, pr_export.ExportRefused):
            return Stopped(e.reason, str(e))
        if isinstance(e, Stopped):
            return e
        if isinstance(e, unattended.Refused | profiles.ProfilesError | plans.PlanError):
            return Stopped("refused", str(e))
        if isinstance(e, integrate.IntegrateError):
            return Stopped("check_failed", str(e))
        config.log.error("job %s: dispatch stopped", self.id, exc_info=e)
        sentence = str(e) if isinstance(e, JobFailed) else f"worker error: {type(e).__name__}: {e}"
        return Stopped("wrapper_failed", sentence)

    def _settle(self, ended: Stopped) -> None:
        """No task is left running once its job is over; an error between rows can leave one."""
        reason = ended.reason if ended.reason in REASONS else "wrapper_failed"
        for task in self.work.plan.tasks if self.board else ():
            if self.board.state(task.id) == "running":
                self.board.write(task.id, "failed", str(ended)[:ERROR_MAX], reason=reason)

    def _meta(self) -> dict[str, Any]:
        plan, board = self.work.plan, self.board
        rows = [r for t in plan.tasks for r in board.rows(t.id)] if board else []
        return {
            "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in rows), 6),
            "tasks": {
                t.id: (board.state(t.id) if board else None) or "not started" for t in plan.tasks
            },
            "plan_hash": self.work.plan_hash,
        }

    def _succeeded(self, url: str) -> dict[str, Any]:
        number = PR_NUMBER.search(url)
        pr = {
            "pr_url": url.splitlines()[0],
            "pr_number": int(number.group(1)) if number else None,
        }
        return {
            "status": "succeeded",
            "result": f"Draft pull request: {url}",
            "meta": pr | self._meta(),
        }

    def _failed(self, ended: Stopped) -> dict[str, Any]:
        meta = {"reason": ended.reason} | self._meta()
        if ended.reason == "plan_limit" and self.halt.rate_limited:
            meta["rate_limited"] = self.halt.rate_limited
        return {"status": "failed", "error": str(ended)[:ERROR_MAX], "meta": meta}


def run_dispatch(job: dict[str, Any]) -> dict[str, Any]:
    """Run a claimed dispatch job and return its finish payload."""
    try:
        work = approved(job.get("params") or {})
    except Stopped as e:
        meta = {"reason": e.reason, "cost_usd": 0.0, "tasks": {}}
        return {"status": "failed", "error": str(e)[:ERROR_MAX], "meta": meta}
    return DispatchJob(job, work).run()
