"""Submit a plan to Sazed as one dispatch job, which runs only after Pryce approves it (D7)."""

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sazed_worker.dispatch import admission, mcp, profiles, unattended
from sazed_worker.dispatch import plan as plans
from sazed_worker.dispatch.pr_export import SAZED_MAX_TITLE

TIMEOUT_S = 60
SAZED_MAX_SUBMIT_BYTES = 1024 * 1024


class SubmitRefused(RuntimeError):
    pass


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("plan")
    p.add_argument("--config", help="a workstream TOML with defaults, profiles and check")
    p.add_argument("--repo-name", required=True, help="owner/name of the repo on GitHub")
    p.add_argument("--base", default="main", help="the branch integ/<plan> starts from")
    p.add_argument("--budget", type=float, default=5.0, help="USD the task caps must fit in")
    p.add_argument("--timeout", type=int, default=7200, help="the plan's wall-clock cap, seconds")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--title", required=True, help="the draft PR's title")
    p.add_argument("--body", help="text put before the PR's generated body")
    p.add_argument("--card", help="the board card, item:<uuid>")


def _read(args: argparse.Namespace) -> tuple[dict[str, Any], plans.Plan, dict[str, Any]]:
    if args.config and not Path(args.config).exists():
        raise SubmitRefused(f"there is no config at {args.config}")
    try:
        raw = json.loads(Path(args.plan).read_text())
        plan = plans.parse(raw)
        config = plans.load_config(Path(args.config) if args.config else None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise SubmitRefused(f"can't read the plan: {e}") from e
    return raw, plan, config


def request(args: argparse.Namespace) -> dict[str, Any]:
    """The submit body, or SubmitRefused; profiles and the MCP registry are read from Sazed."""
    profiles.load()
    mcp.load()
    raw, plan, config = _read(args)
    problems = plans.validate(plan, config)
    if problems:
        raise SubmitRefused("the plan doesn't validate:\n" + "\n".join(problems))
    sentence = admission.name_refusal(plan) or unattended.plan_refusal(plan, config)
    if sentence:
        raise SubmitRefused(sentence)
    task_caps = admission.caps(plan, config)
    sentence = admission.over_budget(task_caps, args.budget)
    if sentence:
        raise SubmitRefused(sentence)
    if len(args.title) > SAZED_MAX_TITLE:
        raise SubmitRefused(f"the title is over {SAZED_MAX_TITLE} characters")
    return {
        "plan": raw,
        "config": config,
        "repo": args.repo_name,
        "base": args.base,
        "budget_usd": args.budget,
        "task_caps": task_caps,
        "timeout_s": args.timeout,
        "parallel": args.parallel,
        "title": args.title,
        "body": args.body,
        "board_card": args.card,
    }


def _detail(raw: str) -> str:
    try:
        detail = json.loads(raw)["detail"]
    except (ValueError, KeyError, TypeError):
        return raw[:500]
    return detail if isinstance(detail, str) else json.dumps(detail)[:500]


def _post(path: str, data: bytes) -> dict[str, Any]:
    base = os.environ.get("DISPATCH_SAZED_URL")
    if not base:
        raise SubmitRefused("DISPATCH_SAZED_URL is not set")
    headers = {"Content-Type": "application/json"}
    if key := os.environ.get("DISPATCH_SAZED_KEY"):
        headers["X-API-Key"] = key
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", data, headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            answer = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = _detail(e.read().decode(errors="replace"))
        raise SubmitRefused(f"Sazed answered {e.code}: {detail}") from None
    except (OSError, ValueError) as e:
        raise SubmitRefused(f"Sazed at {base} gave no usable answer ({e})") from e
    if not isinstance(answer, dict) or not answer.get("job_id"):
        raise SubmitRefused(f"Sazed answered without a job id: {answer!r}")
    return answer


def submit(args: argparse.Namespace) -> int:
    try:
        body = request(args)
        try:
            data = json.dumps(body, allow_nan=False).encode()
            digest = admission.plan_hash(body["plan"], body["config"])
        except (TypeError, ValueError) as e:
            raise SubmitRefused(f"the plan or config holds a value JSON can't carry: {e}") from e
        if len(data) > SAZED_MAX_SUBMIT_BYTES:
            raise SubmitRefused(f"the plan is {len(data)} bytes encoded, over Sazed's 1 MiB")
        answer = _post(f"/dispatch/plans/{body['plan']['id']}/submit", data)
    except (SubmitRefused, plans.PlanError, profiles.ProfilesError) as e:
        print(f"refused: {e}")
        return 1
    job = answer["job_id"]
    if answer.get("plan_hash") != digest:
        print(f"refused: job {job} holds another plan hash than this plan's. Cancel it.")
        return 1
    print(f"job {job} waits for approval (plan {body['plan']['id']}, hash {digest[:12]})")
    print("It runs only once Pryce approves it in the Sazed console.")
    return 0
