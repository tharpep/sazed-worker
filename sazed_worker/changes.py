"""The files a change job edited, as the finish payload's path -> content map."""

import base64
import os
from pathlib import Path

from .agent_process import JobFailed

_MAX_CHANGED_FILES = 200
# Never part of a PR: git internals, and caches a test or check run can leave in the working copy.
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}
_MAX_CHANGED_BYTES = 5 * 1024 * 1024


def changed_files(repo_dir: Path, work: Path) -> dict[str, str | None]:
    """Files the agent changed: path -> new content (base64), or None if deleted. Symlinks skip."""

    def files(root: Path) -> dict[str, Path]:
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                if not path.is_symlink():
                    out[path.relative_to(root).as_posix()] = path
        return out

    before, after = files(repo_dir), files(work)
    changes: dict[str, str | None] = {}
    total = 0
    for rel in sorted(before.keys() | after.keys()):
        if rel not in after:
            changes[rel] = None
            continue
        data = after[rel].read_bytes()
        if rel in before and before[rel].read_bytes() == data:
            continue
        total += len(data)
        changes[rel] = base64.b64encode(data).decode()
    if len(changes) > _MAX_CHANGED_FILES or total > _MAX_CHANGED_BYTES:
        raise JobFailed(f"too many changes for one PR ({len(changes)} files, {total} bytes)")
    return changes
