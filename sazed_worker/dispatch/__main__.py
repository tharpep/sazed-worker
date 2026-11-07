"""Dispatch's command line, driven from a Claude Code session (plans/17)."""

import argparse
import json
import sys
from pathlib import Path

from sazed_worker.dispatch import plan as plans


def _validate(args: argparse.Namespace) -> int:
    try:
        loaded = plans.load(Path(args.plan))
    except (OSError, ValueError, KeyError) as e:
        print(f"can't read the plan: {e}")
        return 2
    config = plans.load_config(Path(args.config) if args.config else None)
    problems = plans.validate(loaded, config)
    if problems:
        print("\n".join(f"- {p}" for p in problems))
        return 1
    resolved = {t.id: plans.resolve(t, config) for t in loaded.tasks}
    print(json.dumps({"plan": loaded.id, "ok": True, "tasks": resolved}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sazed_worker.dispatch")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="check a plan before anything runs")
    v.add_argument("plan")
    v.add_argument("--config", help="a workstream TOML with defaults, profiles and check")
    v.set_defaults(func=_validate)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
