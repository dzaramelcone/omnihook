import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class UATIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="omnihook-uat-home-"))
        self.project = Path(tempfile.mkdtemp(prefix="omnihook-uat-project-"))
        self.bin_dir = self.home / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.bin_dir}:{self.env['PATH']}"
        self.env["OMNIHOOK_REPO"] = ROOT.as_uri()
        self.env["OMNIHOOK_PORT"] = str(self.port)
        self._write_fake_claude()

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.project, ignore_errors=True)

    def _write_fake_claude(self) -> None:
        fake = self.bin_dir / "claude"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "echo invoked >> \"$HOME/claude-invocations.log\"\n"
            "exit 0\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def _run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(ROOT / script)],
            cwd=self.project,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=90,
        )

    def _health(self) -> int:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/health",
            timeout=5,
        ) as resp:
            return resp.status

    def test_quickstart_and_uninstall_are_clean_in_isolation(self) -> None:
        install = self._run("quickstart.sh")
        self.assertEqual(install.returncode, 0, install.stderr or install.stdout)

        settings_path = self.project / ".claude" / "settings.json"
        launcher_path = self.project / ".claude" / "hooks" / "ensure_omnihook.sh"
        install_dir = self.home / ".claude" / "omnihook-src"
        state_dir = self.home / ".claude" / "omnihook"

        self.assertTrue(settings_path.exists())
        self.assertTrue(launcher_path.exists())
        self.assertTrue(os.access(launcher_path, os.X_OK))
        self.assertTrue(install_dir.exists())
        self.assertTrue(state_dir.exists())
        self.assertEqual(self._health(), 200)

        settings = json.loads(settings_path.read_text())
        allowed = settings.get("allowedHttpHookUrls", [])
        self.assertIn(f"http://127.0.0.1:{self.port}/*", allowed)
        startup_hooks = settings["hooks"]["SessionStart"][0]["hooks"]
        self.assertEqual(
            startup_hooks[0]["command"],
            "$CLAUDE_PROJECT_DIR/.claude/hooks/ensure_omnihook.sh",
        )
        self.assertEqual(
            startup_hooks[1]["url"],
            f"http://127.0.0.1:{self.port}/hook",
        )

        uninstall = self._run("uninstall.sh")
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr or uninstall.stdout)

        self.assertFalse(launcher_path.exists())
        self.assertFalse(install_dir.exists())
        self.assertFalse(state_dir.exists())

        cleaned = json.loads(settings_path.read_text())
        self.assertFalse(cleaned.get("allowedHttpHookUrls"))
        hooks = cleaned.get("hooks", {})
        for groups in hooks.values():
            for group in groups:
                for hook in group.get("hooks", []):
                    self.assertNotIn("ensure_omnihook", hook.get("command", ""))
                    self.assertNotIn(
                        f"127.0.0.1:{self.port}",
                        hook.get("url", ""),
                    )


if __name__ == "__main__":
    unittest.main()
