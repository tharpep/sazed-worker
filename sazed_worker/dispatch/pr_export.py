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

GATEWAY_BRANCH_ID_MAX = 80
HEAD_PREFIX = "plan-"
PLAN_ID = re.compile(rf"^[A-Za-z0-9._-]{{1,{GATEWAY_BRANCH_ID_MAX - len(HEAD_PREFIX)}}}$")
GATEWAY_MAX_FILES = 200
GATEWAY_MAX_BYTES = 5 * 1024 * 1024
WORKFLOWS = ".github/workflows/"
ABSENT, PLAIN, SUBMODULE = "000000", "100644", "160000"
SAZED_TIMEOUT_S = 300


class ExportRefused(RuntimeError):
    def __init__(self, reason: str, sentence: str):
        super().__init__(sentence)
        self.reason = reason


def check_plan_id(plan_id: str) -> None:
    if not PLAN_ID.match(plan_id):
        raise ExportRefused(
            "refused",
            f"plan id {plan_id!r} cannot form the branch sazed/{HEAD_PREFIX}<id>, which allows "
            f"1 to {GATEWAY_BRANCH_ID_MAX - len(HEAD_PREFIX)} letters, digits, dots, dashes "
            "and underscores",
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


def _blob(repo: Path, sha: str) -> bytes:
    proc = subprocess.run(["git", "cat-file", "blob", sha], cwd=repo, capture_output=True)
    if proc.returncode != 0:
        raise GitError(f"git cat-file blob {sha}: {proc.stderr.decode(errors='replace')[:400]}")
    return proc.stdout


def changed_files(
    repo: Path, start: str, head: str
) -> tuple[dict[str, str | None], dict[str, str]]:
    """start..head as path -> base64 content (None for a delete), plus the modes the gateway
    would not infer: a new path is 100644 and an existing one keeps its mode unless listed."""
    raw = git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", start, head, cwd=repo)
    fields = raw.split("\0")
    entries = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        old_mode, new_mode, _, blob, _ = meta.lstrip(":").split()
        if SUBMODULE in (old_mode, new_mode):
            raise ExportRefused("refused", f"{path} is a submodule, which an export cannot carry")
        if path.startswith(WORKFLOWS):
            raise ExportRefused(
                "refused", f"{path} is under {WORKFLOWS}, which the gateway's bot may not write"
            )
        entries.append((path, old_mode, new_mode, blob))
    if len(entries) > GATEWAY_MAX_FILES:
        raise ExportRefused(
            "export_too_large",
            f"too many files for one PR ({len(entries)} > {GATEWAY_MAX_FILES})",
        )
    files: dict[str, str | None] = {}
    modes: dict[str, str] = {}
    total = 0
    for path, old_mode, new_mode, blob in entries:
        if new_mode == ABSENT:
            files[path] = None
            continue
        data = _blob(repo, blob)
        total += len(data)
        files[path] = base64.b64encode(data).decode()
        if new_mode != (PLAIN if old_mode == ABSENT else old_mode):
            modes[path] = new_mode
    if total > GATEWAY_MAX_BYTES:
        raise ExportRefused(
            "export_too_large",
            f"too many changes for one PR ({len(files)} files, {total} bytes)",
        )
    return files, modes


def request(ws: Workspace, base: str, start: str, title: str, text: str) -> dict[str, Any]:
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


def send(plan_id: str, payload: dict[str, Any]) -> str:
    """POST the request to Sazed with the worker key; the PR URL, or ExportRefused."""
    from sazed_worker import sazed_api  # pinned by test_a_hand_export_never_imports_config

    path = f"/worker/dispatch/plans/{plan_id}/export"
    status, answer = sazed_api.post(path, payload, timeout=SAZED_TIMEOUT_S)
    if 200 <= status < 300 and isinstance(answer, dict):
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
        reason = "export_unreachable" if status >= 500 else "refused"
        detail = f"Sazed answered {status}: {detail}"
    raise ExportRefused(reason, detail)
