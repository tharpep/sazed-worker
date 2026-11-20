import getpass
import os
import unittest

os.environ.setdefault("SAZED_AGENT_USER", getpass.getuser())

from sazed_worker.adapters import agy
from sazed_worker.run import JobEvents, Run


class AgyAdapter(unittest.TestCase):
    def test_handle_event_counts_regular_tool(self):
        run = Run(JobEvents("j1"))
        event = {
            "step_update": {
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "read_file",
            }
        }
        agy.handle_event(run, event)
        self.assertEqual(run.tool_calls, 1)
        self.assertEqual(run.tools_used, ["read_file"])

    def test_handle_event_extracts_inner_mcp_tool_name(self):
        run = Run(JobEvents("j1"))
        event = {
            "step_update": {
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "call_mcp_tool",
                "tool_info": {
                    "name": "call_mcp_tool",
                    "parameters": {
                        "Arguments": {},
                        "ServerName": "api-gateway",
                        "ToolName": "homelab_overview",
                    },
                },
            }
        }
        agy.handle_event(run, event)
        self.assertEqual(run.tool_calls, 1)
        self.assertEqual(run.tools_used, ["homelab_overview"])

    def test_handle_event_records_result_and_session(self):
        run = Run(JobEvents("j1"))
        event = {
            "event": "result",
            "result": {
                "conversation_id": "sess-123",
                "status": "SUCCESS",
                "response": "Done.",
                "duration_seconds": 1.2,
            },
        }
        agy.handle_event(run, event)
        self.assertEqual(run.session_id, "sess-123")
        self.assertIsNotNone(run.result)
        self.assertTrue(run.result["ok"])
        self.assertEqual(run.result["response"], "Done.")


if __name__ == "__main__":
    unittest.main()
