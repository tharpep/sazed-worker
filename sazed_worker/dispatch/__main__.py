"""Dispatch's command line, driven from a Claude Code session (plans/17)."""

import argparse
import json
import sys
from pathlib import Path

from sazed_worker.dispatch import integrate
from sazed_worker.dispatch import plan as plans
from sazed_worker.dispatch.board import Board
from sazed_worker.dispatch.mirror import Mirror
from sazed_worker.dispatch.run_agent import run_task
from sazed_worker.dispatch.workspace import GitError, Workspace, default_home


def _load(args: argparse.Namespace) -> tuple[plans.Plan, dict]:
    loaded = plans.load(Path(args.plan))
    config = plans.load_config(Path(args.config) if args.config else None)
    return loaded, config


def _validate(args: argparse.Namespace) -> int:
    try:
        loaded, config = _load(args)
    except (OSError, ValueError, KeyError) as e:
        print(f"can't read the plan: {e}")
        return 2
    problems = plans.validate(loaded, config)
    if problems:
        print("\n".join(f"- {p}" for p in problems))
        return 1
    resolved = {t.id: plans.resolve(t, config) for t in loaded.tasks}
    print(json.dumps({"plan": loaded.id, "ok": True, "tasks": resolved}, indent=2))
    return 0


def _context(args: argparse.Namespace):
    loaded, config = _load(args)
    problems = plans.validate(loaded, config)
    if problems:
        raise plans.PlanError("the plan doesn't validate:\n" + "\n".join(problems))
    home = default_home(loaded.id)
    ws = Workspace(Path(args.repo).resolve(), loaded.id, home)
    return loaded, config, ws, Board(home, Mirror.from_env(loaded.id))


def _print_board(plan: plans.Plan, board: Board) -> None:
    latest = board.latest()
    for t in plan.tasks:
        row = latest.get(t.id)
        if row:
            cost = f" ${row['cost_usd']:.4f}" if isinstance(row.get("cost_usd"), float) else ""
            print(f"{t.id:16} {row['state']:9} {row['line'][:90]}{cost}")
        else:
            print(f"{t.id:16} {'—':9} not started")


def _act(args: argparse.Namespace) -> int:
    try:
        plan, config, ws, board = _context(args)
        if args.command == "init":
            print(integrate.init(plan, ws, board, args.base, config))
        elif args.command == "run":
            chosen = [t for t in plan.tasks if not args.task or t.id in args.task]
            for task in chosen:
                print(json.dumps(run_task(plan, task, ws, board, config)))
        elif args.command == "land":
            for row in integrate.land(plan, ws, board, config):
                print(json.dumps(row))
        elif args.command == "go":
            print(
                json.dumps(
                    integrate.go(plan, ws, board, args.base, config, args.parallel), indent=2
                )
            )
        elif args.command == "board":
            _print_board(plan, board)
        elif args.command == "export":
            print(
                integrate.export(
                    plan,
                    ws,
                    board,
                    args.base,
                    args.title or plan.id,
                    args.body or f"Dispatch plan {plan.id}.",
                    config,
                    open_pr=not args.no_pr,
                )
            )
        elif args.command == "sync":
            if not board.mirror:
                print("refused: DISPATCH_SAZED_URL is not set")
                return 1
            board.mirror.plan(plan, config, ws.repo, args.base)
            for path in sorted(board.dir.glob("*.jsonl")):
                for row in board.rows(path.stem):
                    board.mirror.row(row)
        elif args.command == "clean":
            integrate.clean(plan, ws, board)
    except (plans.PlanError, integrate.IntegrateError, GitError, OSError) as e:
        print(f"refused: {e}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sazed_worker.dispatch")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, func=_act) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("plan")
        p.add_argument("--config", help="a workstream TOML with defaults, profiles and check")
        p.set_defaults(func=func)
        if func is _act:
            p.add_argument("--repo", default=".", help="the target repo (default: here)")
            p.add_argument("--base", default="main", help="the branch integ/<plan> starts from")
        return p

    command("validate", "check a plan before anything runs", _validate)
    command("init", "open integ/<plan> and commit the interface files")
    command("run", "run tasks whose dependencies landed").add_argument("--task", nargs="*")
    command("land", "land done tasks in dependency order")
    command("go", "run, land and repeat until nothing moves").add_argument(
        "--parallel", type=int, default=3
    )
    command("board", "show each task's latest row")
    exp = command("export", "push integ/<plan> and open one draft PR")
    exp.add_argument("--title")
    exp.add_argument("--body")
    exp.add_argument("--no-pr", action="store_true", help="push only")
    command("sync", "post the plan and every board row to Sazed")
    command("clean", "remove the plan's worktrees and task branches")
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
