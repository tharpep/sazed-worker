"""The ruff pre-check on lines a change job edited, and the prompt for its one fix round."""

import difflib
import json
import subprocess
from pathlib import Path

from .agent_process import agent_env, as_agent, kill_group
from .config import RUFF


def _changed_lines(before: bytes | None, after: bytes) -> set[int]:
    """1-based line numbers in `after` that are new or edited relative to `before`."""
    old = (before or b"").decode(errors="replace").splitlines()
    new = after.decode(errors="replace").splitlines()
    lines: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag in ("replace", "insert"):
            lines.update(range(j1 + 1, j2 + 1))
    return lines


def lint_changed(repo_dir: Path, work: Path, changes: dict[str, str | None]) -> list[str]:
    """Ruff findings on lines the agent changed, using the repo's own ruff config.

    Run by the worker with fixed arguments (as the agent user), so the agent never needs a shell.
    Old code isn't held to the rules, only the agent's own lines.
    """
    py = [path for path, content in changes.items() if content is not None and path.endswith(".py")]
    if not py or not Path(RUFF).exists():
        return []
    proc = subprocess.Popen(
        [RUFF, "check", "--output-format=json", "--no-cache", "--quiet", "--exit-zero", *py],
        cwd=work,
        env=agent_env("ruff"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **as_agent(),
    )
    try:
        out, _ = proc.communicate(timeout=60)
        findings = json.loads(out or "[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        kill_group(proc)
        return []
    issues = []
    touched: dict[str, set[int]] = {}
    for f in findings if isinstance(findings, list) else []:
        try:
            rel = Path(f["filename"]).resolve().relative_to(work.resolve()).as_posix()
            row = int(f["location"]["row"])
        except (KeyError, ValueError, TypeError):
            continue
        if rel not in touched:
            before = repo_dir / rel
            touched[rel] = _changed_lines(
                before.read_bytes() if before.is_file() else None, (work / rel).read_bytes()
            )
        if row in touched[rel]:
            issues.append(f"{rel}:{row}: {f.get('code')} {f.get('message')}")
    return issues[:40]


def lint_prompt(issues: list[str]) -> str:
    return (
        "The repo's linter (ruff, with its own config) flagged these problems on lines you "
        "changed:\n" + "\n".join(f"- {i}" for i in issues) + "\n\nFix them, touching only what "
        "is needed. Then reply with the complete PR description again (updated if anything "
        "changed), in the same format as before."
    )
