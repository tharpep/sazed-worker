import unittest
from pathlib import Path

from sazed_worker.dispatch.run_agent import agent_command

CFG = {"model": "haiku", "effort": "max", "budget_usd": 0.5}


class AgentCommand(unittest.TestCase):
    def test_agents_get_no_mcp_servers_of_their_own(self):
        """A dispatch agent never inherits the operator account's MCP connectors."""
        cmd = agent_command(CFG, Path("/tmp/s.json"), read_only=False)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--mcp-config", cmd)

    def test_profile_model_and_effort_reach_the_command(self):
        cmd = agent_command(CFG, Path("/tmp/s.json"), read_only=True)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
        self.assertIn("--disallowedTools", cmd)


if __name__ == "__main__":
    unittest.main()
