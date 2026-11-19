import unittest
from pathlib import Path

from sazed_worker.dispatch.run_agent import task_spec
from sazed_worker.run import launch_args

CFG = {"model": "haiku", "effort": "max", "budget_usd": 0.5, "timeout_s": 600}


def command(cfg: dict, read_only: bool, grant=None) -> list[str]:
    spec = task_spec(cfg, Path("/tmp/wt"), "p", Path("/tmp/s.json"), read_only, grant)
    return launch_args(spec)[0]


class AgentCommand(unittest.TestCase):
    def test_agents_get_no_mcp_servers_of_their_own(self):
        """A dispatch agent never inherits the operator account's MCP connectors."""
        cmd = command(CFG, read_only=False)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--mcp-config", cmd)

    def test_profile_model_and_effort_reach_the_command(self):
        cmd = command(CFG, read_only=True)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
        self.assertIn("--disallowedTools", cmd)


if __name__ == "__main__":
    unittest.main()
