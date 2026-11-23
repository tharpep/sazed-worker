"""Sazed's worker API: claim, events and finish go through `post`, registry reads through `get`."""

import json
import urllib.error
import urllib.request
from typing import Any

from .config import SAZED_URL, WORKER_KEY
from .scrub import scrub


def post(
    path: str,
    body: dict[str, Any],
    timeout: float = 30,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """POST to Sazed. Never raises: network or decode failures come back as status 0."""
    req = urllib.request.Request(
        f"{SAZED_URL}{path}",
        data=scrub(json.dumps(body)).encode(),
        headers=(headers or {})
        | {"Content-Type": "application/json", "X-Worker-Key": WORKER_KEY},
        method="POST",
    )
    status, raw = _send(req, timeout)
    if not isinstance(raw, bytes):
        return status, raw
    try:
        return status, json.loads(raw) if raw else None
    except ValueError as e:
        return 0, f"{type(e).__name__}: {e}"


def job_header(job_id: str) -> dict[str, str]:
    """Names the running dispatch job a worker-key write belongs to (D7 A1)."""
    return {"X-Dispatch-Job": job_id}


def get(
    path: str, headers: dict[str, str] | None = None, timeout: float = 30
) -> tuple[int, bytes | str]:
    """GET from Sazed with the worker key. Never raises; a success's body comes back undecoded."""
    req = urllib.request.Request(
        f"{SAZED_URL}{path}", headers=(headers or {}) | {"X-Worker-Key": WORKER_KEY}
    )
    return _send(req, timeout)


def _send(req: urllib.request.Request, timeout: float) -> tuple[int, bytes | str]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]
    except OSError as e:
        return 0, f"{type(e).__name__}: {e}"
