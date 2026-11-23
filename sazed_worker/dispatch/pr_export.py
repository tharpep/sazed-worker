"""What an export sends: the PR body, and on an unattended run the changed-files map to Sazed."""

import base64
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import repo_name
from sazed_worker.dispatch.plan import Plan
from sazed_worker.dispatch.workspace import GitError, Workspace, git

HEAD_PREFIX = "sazed/plan-"
PLAN_ID_MAX = 75
# pinned by test_each_refusal_prints_one_sentence_with_its_reason_and_sends_nothing
PLAN_ID = re.compile(rf"^(?!.*\.\.)(?!.*\.$)(?!.*\.lock$)[A-Za-z0-9._-]{{1,{PLAN_ID_MAX}}}$")
GATEWAY_MAX_FILES = 200
GATEWAY_MAX_BASE64_CHARS = 5 * 1024 * 1024 * 4 // 3
SAZED_MAX_TITLE = 256
SAZED_MAX_BODY = 60_000
WORKFLOWS = ".github/workflows/"
BOT_UNWRITABLE = (".github", ".github/workflows")
ABSENT, PLAIN, SUBMODULE = "000000", "100644", "160000"
SAZED_TIMEOUT_S = 300
UNREACHABLE_STATUS = 424


class ExportRefused(RuntimeError):
    def __init__(self, reason: str, sentence: str):
        super().__init__(sentence)
        self.reason = reason


class ExportUnclear(RuntimeError):
    pass


def check_plan_id(plan_id: str) -> None:
    if not PLAN_ID.match(plan_id):
        raise ExportRefused(
            "refused",
            f"plan id {plan_id!r} cannot form the branch {HEAD_PREFIX}<id>, which allows 1 to "
            f"{PLAN_ID_MAX} letters, digits, dots, dashes and underscores, with no '..' and no "
            "trailing '.' or '.lock'",
        )


def base_sha(ws: Workspace, base: str) -> str:
    return git("merge-base", base, ws.integ_branch, cwd=ws.repo)


def body(plan: Plan, ws: Workspace, board: Board, start: str) -> str:
    """The plan id, then each landed task's commit subjects in landing order."""
    landed = []
    for t in plan.tasks:
        rows = board.rows(t.id)
        if rows and rows[-1]["state"] == "landed":
            landed.append((rows[-1]["ts"], t.id, rows[-1]["ref"]))
    lines = [f"Dispatch plan {plan.id}."]
    if landed:
        lines.append("")
    for _, task_id, ref in sorted(landed):
        log = git("log", "--reverse", "--format=%s", f"{start}..{ref}", cwd=ws.repo)
        ours = (f"Interfaces for {plan.id}", f"regen after {task_id}")
        lines.append(f"- {task_id}: {'; '.join(s for s in log.splitlines() if s not in ours)}")
        start = ref
    return "\n".join(lines)


def hand_body(plan: Plan, ws: Workspace, board: Board, base: str) -> str:
    """The body from the local base, else origin/<base>, else the body hand exports had."""
    for ref in (base, f"origin/{base}"):
        try:
            return body(plan, ws, board, base_sha(ws, ref))
        except GitError:
            continue
    return f"Dispatch plan {plan.id}."


def _git_bytes(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()[:400]
        raise GitError(f"git {' '.join(args)}: {stderr}")
    return proc.stdout


def _path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        shown = raw.decode("utf-8", errors="backslashreplace")
        raise ExportRefused(
            "refused", f"{shown} is not a UTF-8 path, which the PR request cannot carry"
        ) from None
    if path in BOT_UNWRITABLE or path.startswith(WORKFLOWS):
        raise ExportRefused(
            "refused", f"the gateway's bot may not write {path}, since it reaches {WORKFLOWS}"
        )
    return path


def changed_files(
    repo: Path, start: str, head: str
) -> tuple[dict[str, str | None], dict[str, str]]:
    """start..head as path -> base64 content (None for a delete), plus the modes the gateway
    would not infer: a new path is 100644 and an existing one keeps its mode unless listed."""
    raw = _git_bytes(repo, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", start, head)
    fields = raw.split(b"\0")
    entries = []
    for meta, raw_path in zip(fields[0::2], fields[1::2], strict=False):
        old_mode, new_mode, _, blob, _ = meta.decode("ascii").lstrip(":").split()
        path = _path(raw_path)
        if SUBMODULE in (old_mode, new_mode):
            raise ExportRefused("refused", f"{path} is a submodule, which an export cannot carry")
        entries.append((path, old_mode, new_mode, blob))
    if not entries:
        raise ExportRefused("refused", f"nothing to export: {head} has no changes since {start}")
    if len(entries) > GATEWAY_MAX_FILES:
        raise ExportRefused(
            "export_too_large",
            f"too many files for one PR ({len(entries)} > {GATEWAY_MAX_FILES})",
        )
    files: dict[str, str | None] = {}
    modes: dict[str, str] = {}
    for path, old_mode, new_mode, blob in entries:
        if new_mode == ABSENT:
            files[path] = None
            continue
        files[path] = base64.b64encode(_git_bytes(repo, "cat-file", "blob", blob)).decode()
        if new_mode != (PLAIN if old_mode == ABSENT else old_mode):
            modes[path] = new_mode
    total = sum(len(c or "") for c in files.values())
    if total > GATEWAY_MAX_BASE64_CHARS:
        raise ExportRefused(
            "export_too_large",
            f"too many changes for one PR ({len(files)} files, {total} base64 characters "
            f"> {GATEWAY_MAX_BASE64_CHARS}, which is 5 MiB)",
        )
    return files, modes


def request(ws: Workspace, base: str, start: str, title: str, text: str) -> dict[str, Any]:
    if len(title) > SAZED_MAX_TITLE:
        raise ExportRefused(
            "refused", f"the title is {len(title)} characters, over Sazed's {SAZED_MAX_TITLE}"
        )
    if len(text) > SAZED_MAX_BODY:
        raise ExportRefused(
            "refused", f"the body is {len(text)} characters, over Sazed's {SAZED_MAX_BODY}"
        )
    files, modes = changed_files(ws.repo, start, ws.integ_branch)
    return {
        "repo": repo_name(ws.repo),
        "base": base,
        "base_sha": start,
        "title": title,
        "body": text,
        "files": files,
        "modes": modes,
    }


def _refusal(answer: Any) -> tuple[str, str | None]:
    try:
        data = json.loads(answer) if isinstance(answer, str) else answer
    except ValueError:
        return str(answer), None
    if not isinstance(data, dict):
        return str(answer), None
    detail = data.get("detail")
    if isinstance(detail, dict):
        data, detail = detail, detail.get("detail")
    return str(detail if detail is not None else answer), data.get("reason")


def send(plan_id: str, payload: dict[str, Any], job_id: str | None = None) -> str:
    """POST the request to Sazed with the worker key; the PR URL, or ExportRefused."""
    from sazed_worker import sazed_api  # pinned by test_a_hand_export_works_without_an_agent_user

    path = f"/worker/dispatch/plans/{plan_id}/export"
    headers = sazed_api.job_header(job_id) if job_id else None
    status, answer = sazed_api.post(path, payload, SAZED_TIMEOUT_S, headers)
    if 200 <= status < 300:
        if not (isinstance(answer, dict) and isinstance(answer.get("url"), str)):
            raise ExportUnclear(
                f"Sazed answered {status} without a PR URL, so a PR may exist on "
                f"{HEAD_PREFIX}{plan_id}: check before exporting again"
            )
        out = answer["url"]
        if answer.get("base_moved"):
            out += (
                f"\nbase_moved: {payload['base']} has moved past {payload['base_sha'][:12]}, "
                "so the PR is behind its base and GitHub shows any conflict"
            )
        return out
    if status == 0:
        raise ExportRefused("export_unreachable", f"Sazed could not be reached ({answer})")
    detail, reason = _refusal(answer)
    if reason is None:
        unreachable = status >= 500 or status == UNREACHABLE_STATUS
        reason = "export_unreachable" if unreachable else "refused"
        detail = f"Sazed answered {status}: {detail}"
    raise ExportRefused(reason, detail)
