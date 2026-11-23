"""deploy.sh ships the whole package and refuses without SAZED_URL; ssh is a stand-in (D7 W-8)."""

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SSH = """#!/bin/sh
echo "$*" >> "$FAKE/calls"
case "$*" in
  *SAZED_URL*) exit "$SAZED_URL_STATUS";;
  *) cat > "$FAKE/sent.tar";;
esac
"""


class Deploy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deploy-"))
        self.tree = self.tmp / "tree"
        shutil.copytree(
            ROOT, self.tree,
            ignore=shutil.ignore_patterns(".git", ".deploy.env", "__pycache__", "node_modules"),
        )  # fmt: skip
        script = self.tree / "deploy.sh"
        script.write_bytes(script.read_bytes().replace(b"\r\n", b"\n"))
        cache = self.tree / "sazed_worker" / "dispatch" / "__pycache__"
        cache.mkdir()
        (cache / "stale.py").write_text("")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("ssh", SSH), ("python", "#!/bin/sh\nexit 0\n")):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)

    def deploy(self, sazed_url_status: int) -> subprocess.CompletedProcess:
        env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE": str(self.tmp),
            "SAZED_URL_STATUS": str(sazed_url_status),
            "PROXMOX_SSH": "root@proxmox.test",
            "HOME": str(self.tmp),
        }
        return subprocess.run(["bash", str(self.tree / "deploy.sh")], env=env,
                              capture_output=True, text=True)  # fmt: skip

    def test_it_ships_every_module_the_entry_point_and_the_unit_and_nothing_else(self):
        out = self.deploy(0)
        self.assertEqual(out.returncode, 0, out.stderr)
        with tarfile.open(self.tmp / "sent.tar") as tar:
            sent = sorted(tar.getnames())
        package = {p.relative_to(ROOT).as_posix() for p in (ROOT / "sazed_worker").rglob("*.py")}
        self.assertIn("sazed_worker/dispatch/job.py", package)
        self.assertEqual(sent, sorted(package | {"worker.py", "sazed-worker.service"}))

    def test_it_refuses_without_sazed_url_before_it_copies_anything(self):
        out = self.deploy(1)
        self.assertEqual(out.returncode, 1)
        self.assertIn("refused: SAZED_URL is not set", out.stderr)
        self.assertFalse((self.tmp / "sent.tar").exists())

    def test_the_remote_makes_the_package_readable_by_the_agent(self):
        self.deploy(0)
        remote = (self.tmp / "calls").read_text()
        for line in ("-type d -exec chmod 755 {} +", "-type f -exec chmod 644 {} +"):
            self.assertIn(line, remote)


if __name__ == "__main__":
    unittest.main()
