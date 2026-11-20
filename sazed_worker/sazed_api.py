"""Sazed's worker API: claim, events and finish go through `post`, registry reads through `get`."""

import json
import urllib.error
import urllib.request
from typing import Any

from .config import SAZED_URL, WORKER_KEY
from .scrub import scrub


def post(path: str, body: dict[str, Any], timeout: float = 30) -> tuple[int, Any]:
    """POST to Sazed. Never raises: network or decode failures come back as status 0."""
    req = urllib.request.Request(
        f"{SAZED_URL}{path}",
        data=scrub(json.dumps(body)).encode(),
        headers={"Content-Type": "application/json", "X-Worker-Key": WORKER_KEY},
        method="POST",
    )
    return _send(req, timeout)


def get(path: str, headers: dict[str, str] | None = None, timeout: float = 30) -> tuple[int, Any]:
    """GET from Sazed with the worker key. Never raises, like `post`."""
    req = urllib.request.Request(
        f"{SAZED_URL}{path}", headers=(headers or {}) | {"X-Worker-Key": WORKER_KEY}
    )
    return _send(req, timeout)


def _send(req: urllib.request.Request, timeout: float) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]
    except (OSError, ValueError) as e:  # URLError, timeouts, resets, bad JSON
        return 0, f"{type(e).__name__}: {e}"
