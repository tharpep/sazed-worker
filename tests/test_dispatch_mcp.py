"""Dispatch gives each agent exactly the MCP servers and tools its profile grants."""

import contextlib
import io
import json
import os
import stat
import unittest
from pathlib import Path

from sazed_worker.dispatch import mcp
from sazed_worker.dispatch.run_agent import agent_command
from tests.test_dispatch_profiles import Env, Registry

CFG = {"model": "haiku", "effort": "max", "budget_usd": 0.5}
CONTEXT7 = {"name": "context7", "transport": "http", "url": "https://c7.test/mcp", "headers": {}}
SEARCH = {
    "name": "web-search",
    "transport": "http",
    "url": "https://ws.test/mcp",
    "headers": {"Authorization": "Bearer ${WEB_SEARCH_KEY}"},
}
LOCAL = {"name": "local", "transport": "stdio", "command": "npx", "args": ["-y", "srv"]}


class Grants(Env):
    def setUp(self):
        super().setUp()
        self.run_dir = self.tmp / "run"
        self.addCleanup(setattr, mcp, "_active", ({}, "none"))
        mcp._active = ({s["name"]: s for s in (CONTEXT7, SEARCH, LOCAL)}, "sazed")

    def config(self) -> dict:
        return json.loads((self.run_dir / "mcp.json").read_text())["mcpServers"]

    def test_no_grants_means_no_mcp_config(self):
        grant = mcp.build({}, self.run_dir)
        cmd = agent_command(CFG, Path("/tmp/s.json"), False, grant.args)
        self.assertNotIn("--mcp-config", cmd)
        self.assertNotIn("--allowedTools", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(grant.note, "")
        self.assertFalse((self.run_dir / "mcp.json").exists())

    def test_a_tool_grant_becomes_exact_allowed_tools(self):
        grant = mcp.build({"context7": ["resolve-library-id", "query-docs"]}, self.run_dir)
        cmd = agent_command(CFG, Path("/tmp/s.json"), False, grant.args)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], str(self.run_dir / "mcp.json"))
        self.assertEqual(
            cmd[cmd.index("--allowedTools") + 1],
            "mcp__context7__resolve-library-id,mcp__context7__query-docs",
        )
        self.assertEqual(list(self.config()), ["context7"])
        self.assertEqual(grant.note, " · mcp: context7")

    def test_a_star_grant_becomes_the_server_rule(self):
        grant = mcp.build({"context7": ["*"], "local": ["*"]}, self.run_dir)
        self.assertEqual(grant.args[-1], "mcp__context7,mcp__local")
        self.assertEqual(self.config()["local"], {"command": "npx", "args": ["-y", "srv"]})

    def test_an_http_server_gets_its_headers_resolved_from_the_environment(self):
        mcp.build({"web-search": ["*"]}, self.run_dir, {"WEB_SEARCH_KEY": "s3cret"})
        self.assertEqual(
            self.config()["web-search"],
            {
                "type": "http",
                "url": "https://ws.test/mcp",
                "headers": {"Authorization": "Bearer s3cret"},
            },
        )

    def test_a_missing_variable_skips_the_server_and_says_so(self):
        grant = mcp.build({"context7": ["*"], "web-search": ["*"]}, self.run_dir, {})
        self.assertEqual(list(self.config()), ["context7"])
        self.assertEqual(grant.args[-1], "mcp__context7")
        self.assertEqual(grant.note, " · mcp: context7 (web-search skipped: WEB_SEARCH_KEY unset)")

    def test_only_skipped_servers_leave_no_config_and_keep_the_note(self):
        grant = mcp.build({"web-search": ["*"]}, self.run_dir, {})
        self.assertEqual(grant.args, [])
        self.assertIn("web-search skipped: WEB_SEARCH_KEY unset", grant.note)
        self.assertFalse((self.run_dir / "mcp.json").exists())

    def test_a_server_missing_from_the_registry_is_skipped_with_a_note(self):
        grant = mcp.build({"ghost": ["*"], "context7": ["*"]}, self.run_dir)
        self.assertEqual(list(self.config()), ["context7"])
        self.assertEqual(grant.note, " · mcp: context7 (ghost skipped: not in the registry)")

    def test_mcp_json_is_mode_600(self):
        mcp.build({"web-search": ["*"]}, self.run_dir, {"WEB_SEARCH_KEY": "x"})
        mode = stat.S_IMODE((self.run_dir / "mcp.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_a_rewrite_keeps_mode_600(self):
        self.run_dir.mkdir()
        (self.run_dir / "mcp.json").write_text("{}")
        os.chmod(self.run_dir / "mcp.json", 0o644)
        mcp.build({"context7": ["*"]}, self.run_dir)
        mode = stat.S_IMODE((self.run_dir / "mcp.json").stat().st_mode)
        self.assertEqual(mode, 0o600)


class RegistrySources(Env):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, mcp, "_active", ({}, "none"))

    def test_servers_come_from_sazed_and_land_in_the_cache(self):
        os.environ["DISPATCH_SAZED_URL"] = self.serve(Registry, profiles=[CONTEXT7])
        os.environ["DISPATCH_SAZED_KEY"] = "k1"
        table, source = mcp.load()
        self.assertEqual((source, list(table)), ("sazed", ["context7"]))
        self.assertEqual(self.server.seen, [("/agents/mcp-servers", "k1")])
        self.assertEqual(json.loads(mcp.cache_path().read_text()), [CONTEXT7])

    def test_the_registry_falls_back_to_its_cache_when_sazed_is_down(self):
        mcp.cache_path().parent.mkdir(parents=True)
        mcp.cache_path().write_text(json.dumps([CONTEXT7]))
        os.environ["DISPATCH_SAZED_URL"] = "http://127.0.0.1:1"
        with contextlib.redirect_stderr(io.StringIO()):
            table, source = mcp.load()
        self.assertEqual((source, list(table)), ("cache", ["context7"]))

    def test_there_are_no_built_in_servers(self):
        self.assertEqual(mcp.load(), ({}, "none"))


if __name__ == "__main__":
    unittest.main()
