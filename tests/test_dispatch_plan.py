"""Dispatch plans (plans/17 D2): validate refuses each bad shape in code, before any agent runs."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from sazed_worker.dispatch import plan as plans
from sazed_worker.dispatch.__main__ import main
from sazed_worker.dispatch.plan import Plan, Task, could_overlap, resolve, validate


def _task(tid, owns=("src/x/**",), deps=(), **kw):
    base = {"goal": f"do {tid}", "done": "tests pass", "check": "make check"}
    return Task(id=tid, owns=list(owns), depends_on=list(deps), **(base | kw))


def _plan(*tasks, **kw):
    return Plan(id="p1", tasks=list(tasks), **kw)


THREE = _plan(
    _task("api", owns=["src/api/**"]),
    _task("ui", owns=["src/ui/**"], deps=["api"]),
    _task("docs", owns=["docs/**"]),
    interfaces=["src/shared/types.py"],
)


class Validate(unittest.TestCase):
    def test_the_acceptance_shape_is_valid(self):
        self.assertEqual(validate(THREE), [])

    def assert_refused(self, plan, fragment, config=None):
        problems = validate(plan, config)
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_parallel_tasks_cannot_share_files(self):
        self.assert_refused(
            _plan(_task("a", owns=["src/**"]), _task("b", owns=["src/app.py"])),
            "can run together but both own",
        )

    def test_ordered_tasks_may_share_files(self):
        plan = _plan(_task("a", owns=["src/app.py"]), _task("b", owns=["src/app.py"], deps=["a"]))
        self.assertEqual(validate(plan), [])

    def test_ordering_is_transitive(self):
        plan = _plan(
            _task("a", owns=["x.py"]),
            _task("b", owns=["y.py"], deps=["a"]),
            _task("c", owns=["x.py"], deps=["b"]),
        )
        self.assertEqual(validate(plan), [])

    def test_cycles_are_refused(self):
        self.assert_refused(
            _plan(_task("a", deps=["b"]), _task("b", owns=["y/**"], deps=["a"])), "dependency cycle"
        )

    def test_unknown_dependencies_are_refused(self):
        self.assert_refused(_plan(_task("a", deps=["ghost"])), "unknown tasks: ghost")

    def test_duplicate_ids_are_refused(self):
        self.assert_refused(_plan(_task("a"), _task("a", owns=["y/**"])), "duplicate task ids")

    def test_a_code_task_needs_a_check(self):
        self.assert_refused(_plan(_task("a", check=None)), "no check")
        self.assertEqual(validate(_plan(_task("a", check=None), check="make check")), [])

    def test_a_task_needs_done(self):
        self.assert_refused(_plan(_task("a", done="  ")), "no done")

    def test_a_code_task_must_own_something(self):
        self.assert_refused(_plan(_task("a", owns=[])), "owns nothing")

    def test_read_only_tasks_own_nothing_and_need_no_check(self):
        self.assertEqual(validate(_plan(_task("v", owns=[], check=None, profile="verify"))), [])
        self.assert_refused(_plan(_task("v", profile="verify")), "read-only")

    def test_interfaces_are_never_owned(self):
        plan = _plan(_task("a", owns=["src/shared/**"]), interfaces=["src/shared/types.py"])
        self.assert_refused(plan, "shared interface files")

    def test_unknown_profiles_are_refused(self):
        self.assert_refused(_plan(_task("a", profile="wizard")), "unknown profile")

    def test_an_empty_plan_is_refused(self):
        self.assert_refused(_plan(), "no tasks")


class Overlap(unittest.TestCase):
    def test_globs_that_share_paths(self):
        for a, b in [
            ("src/**", "src/app.py"),
            ("src/*.py", "src/app.py"),
            ("a/b.py", "a/b.py"),
            ("src/**/test_*.py", "src/x/test_a.py"),
            ("src/*", "src/*.py"),
        ]:
            with self.subTest(a=a, b=b):
                self.assertTrue(could_overlap(a, b))

    def test_globs_that_cannot(self):
        for a, b in [
            ("src/**", "tests/**"),
            ("src/*.py", "src/sub/a.py"),
            ("a.py", "b.py"),
            ("src/api/**", "src/ui/**"),
        ]:
            with self.subTest(a=a, b=b):
                self.assertFalse(could_overlap(a, b))


class Resolve(unittest.TestCase):
    def test_order_is_defaults_profile_config_task(self):
        config = {"defaults": {"budget_usd": 1.0}, "profiles": {"verify": {"timeout_s": 60}}}
        task = _task("v", profile="verify", owns=[], overrides={"effort": "max"})
        got = resolve(task, config)
        self.assertEqual(
            (got["model"], got["budget_usd"], got["timeout_s"], got["effort"]),
            ("haiku", 0.5, 60, "max"),
        )

    def test_a_config_profile_can_be_new(self):
        got = resolve(_task("a", profile="cheap"), {"profiles": {"cheap": {"model": "haiku"}}})
        self.assertEqual(got["model"], "haiku")

    def test_a_missing_resource_is_refused(self):
        with self.assertRaises(plans.PlanError):
            resolve(_task("a"), {"defaults": {"model": ""}})


class Cli(unittest.TestCase):
    def _run(self, data, config=None):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "plan.json").write_text(json.dumps(data))
        argv = ["validate", str(tmp / "plan.json")]
        if config:
            (tmp / "ws.toml").write_text(config)
            argv += ["--config", str(tmp / "ws.toml")]
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()

    def test_a_good_plan_prints_each_tasks_settings(self):
        code, out = self._run(
            {
                "id": "p",
                "check": "make check",
                "tasks": [
                    {"id": "a", "goal": "g", "done": "d", "owns": ["src/**"], "effort": "high"}
                ],
            }
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["tasks"]["a"]["effort"], "high")

    def test_a_bad_plan_lists_its_problems(self):
        code, out = self._run({"id": "p", "tasks": [{"id": "a", "goal": "g", "owns": ["x"]}]})
        self.assertEqual(code, 1)
        self.assertIn("no done", out)
        self.assertIn("no check", out)

    def test_a_workstream_config_supplies_the_check(self):
        code, _ = self._run(
            {"id": "p", "tasks": [{"id": "a", "goal": "g", "done": "d", "owns": ["x"]}]},
            'check = "make check"\n',
        )
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
