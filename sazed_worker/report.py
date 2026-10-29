"""The JSON block a change job's reply ends with: a shape check and the one repair prompt.

Sazed validates the same block (app/jobs/change_result.py) and renders the PR description from
it; this check only decides whether the worker spends one repair round. Keep the two in step.
"""

import json
import re

_BLOCK_RE = re.compile(r"```json[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
_TRAILING_BLOCK_RE = re.compile(r"```json[ \t]*\n.*?\n[ \t]*```\s*$", re.DOTALL)
_LISTS = ("files", "decisions", "not_verified", "needed_elsewhere", "manual_steps")


def _asks(reply: str) -> bool:
    """The reply stops on a NEEDS_INPUT question (a trailing JSON block doesn't hide it)."""
    lines = [line.strip("*`_ ") for line in _TRAILING_BLOCK_RE.sub("", reply).splitlines()]
    lines = [line for line in lines if line]
    return bool(lines) and lines[-1].startswith("NEEDS_INPUT:")


def report_problem(reply: str) -> str | None:
    """What's wrong with the reply's last JSON block, or None if it's fine or the reply asks."""
    if _asks(reply):
        return None
    blocks = _BLOCK_RE.findall(reply)
    if not blocks:
        return "it has no ```json block"
    try:
        data = json.loads(blocks[-1])
    except ValueError as e:
        return f"the JSON block doesn't parse ({e})"
    if not isinstance(data, dict):
        return "the JSON block isn't an object"
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return "summary must be a non-empty string"
    for key in _LISTS:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return f"{key} must be a list of strings"
    if not isinstance(data.get("inert", ""), str):
        return "inert must be a string"
    return None


def repair_prompt(problem: str) -> str:
    return (
        f"Your reply has to end with the ```json block from your instructions, but {problem}. "
        "Don't change any files. Reply again with the complete block: summary and inert are "
        "strings; files, decisions, not_verified, needed_elsewhere and manual_steps are lists of "
        "one-line strings ([] if empty)."
    )
