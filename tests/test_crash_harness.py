import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDLERS_PATH = ROOT / "omnihook" / "handlers.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveServer:
    def __init__(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="omnihook-live-"))
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen[str] | None = None

    @property
    def store_dir(self) -> Path:
        return self.home / ".claude" / "omnihook"

    @property
    def sessions_dir(self) -> Path:
        return self.store_dir / "sessions"

    @property
    def quarantine_dir(self) -> Path:
        return self.store_dir / "quarantine"

    @property
    def config_path(self) -> Path:
        return self.store_dir / "config.json"

    @property
    def machine_path(self) -> Path:
        return self.store_dir / "machine.json"

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("server already running")
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["OMNIHOOK_PORT"] = str(self.port)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "omnihook"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_healthy()

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        if self.proc.stderr is not None:
            self.proc.stderr.close()
        self.proc = None

    def kill(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        if self.proc.stderr is not None:
            self.proc.stderr.close()
        self.proc = None

    def cleanup(self) -> None:
        self.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def wait_healthy(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                stdout = self.proc.stdout.read() if self.proc.stdout else ""
                stderr = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(
                    f"server exited early with code {self.proc.returncode}\n"
                    f"stdout:\n{stdout}\n"
                    f"stderr:\n{stderr}"
                )
            try:
                resp = self.request("GET", "/health")
            except OSError:
                time.sleep(0.1)
                continue
            if resp["status"] == 200:
                return
            time.sleep(0.1)
        raise TimeoutError("server did not become healthy in time")

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: float = 5.0,
    ) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                payload = json.loads(raw) if raw else None
                return {"status": resp.status, "json": payload, "body": raw}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            payload = json.loads(raw) if raw else None
            return {"status": exc.code, "json": payload, "body": raw}

    def session_json(self, session_id: str) -> dict:
        path = self.sessions_dir / f"{session_id}.json"
        return json.loads(path.read_text())


class CrashHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = LiveServer()
        self.server.start()

    def tearDown(self) -> None:
        self.server.cleanup()

    def test_sigkill_restart_preserves_session_state(self) -> None:
        start = self.server.request(
            "POST",
            "/hook",
            {"session_id": "sigkill", "hook_event_name": "SessionStart"},
        )
        self.assertEqual(start["status"], 200)
        self.assertEqual(self.server.session_json("sigkill")["state"], "active")

        self.server.kill()
        self.server.start()

        status = self.server.request("GET", "/ctl/status")
        self.assertEqual(status["status"], 200)
        sessions = {s["session_id"]: s for s in status["json"]["sessions"]}
        self.assertIn("sigkill", sessions)
        self.assertEqual(sessions["sigkill"]["state"], "active")

    def test_sigkill_mid_handler_does_not_persist_partial_transition(self) -> None:
        for source in (
            """
def slow_promote(session, inp):
    import time
    time.sleep(3)
    session.data["slow_promote_runs"] = session.data.get("slow_promote_runs", 0) + 1
    return "active", {"systemMessage": "slow promote complete"}
""",
            """
def quick_promote(session, inp):
    session.data["quick_promote_runs"] = session.data.get("quick_promote_runs", 0) + 1
    return "active", {"systemMessage": "quick promote complete"}
""",
        ):
            added = self.server.request("POST", "/handlers", {"source": source})
            self.assertEqual(added["status"], 200, added)

        rewired = self.server.request(
            "PUT",
            "/ctl/machine/idle/Stop",
            {"handler": "slow_promote"},
        )
        self.assertEqual(rewired["status"], 200, rewired)

        result: dict[str, object] = {}

        def invoke_stop() -> None:
            try:
                result["resp"] = self.server.request(
                    "POST",
                    "/hook",
                    {"session_id": "midkill", "hook_event_name": "Stop"},
                    timeout=10.0,
                )
            except Exception as exc:  # pragma: no cover - expected transport failure path
                result["error"] = repr(exc)

        thread = threading.Thread(target=invoke_stop)
        thread.start()
        time.sleep(0.5)
        self.server.kill()
        thread.join(timeout=5)

        self.server.start()
        session_path = self.server.sessions_dir / "midkill.json"
        self.assertFalse(session_path.exists())

        rewired = self.server.request(
            "PUT",
            "/ctl/machine/idle/Stop",
            {"handler": "quick_promote"},
        )
        self.assertEqual(rewired["status"], 200, rewired)
        replay = self.server.request(
            "POST",
            "/hook",
            {"session_id": "midkill", "hook_event_name": "Stop"},
        )
        self.assertEqual(replay["status"], 200, replay)
        session = self.server.session_json("midkill")
        self.assertEqual(session["state"], "active")
        self.assertEqual(session["data"]["quick_promote_runs"], 1)
        self.assertNotIn("slow_promote_runs", session["data"])

    def test_corrupt_persisted_files_are_quarantined_on_restart(self) -> None:
        self.server.request(
            "POST",
            "/hook",
            {"session_id": "healthy", "hook_event_name": "SessionStart"},
        )
        self.server.stop()

        self.server.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.server.store_dir.mkdir(parents=True, exist_ok=True)
        (self.server.sessions_dir / "broken.json").write_text("{bad session")
        self.server.machine_path.write_text("{bad machine")
        self.server.config_path.write_text("{bad config")

        self.server.start()

        health = self.server.request("GET", "/health")
        status = self.server.request("GET", "/ctl/status")
        self.assertEqual(health["status"], 200)
        self.assertEqual(status["status"], 200)

        quarantined = {p.name.split(".")[0] for p in self.server.quarantine_dir.iterdir()}
        self.assertIn("broken", quarantined)
        self.assertIn("machine", quarantined)
        self.assertIn("config", quarantined)

    def test_reload_with_broken_handlers_returns_422(self) -> None:
        backup = HANDLERS_PATH.read_text()
        self.addCleanup(HANDLERS_PATH.write_text, backup)
        HANDLERS_PATH.write_text("def broken(:\n")

        resp = self.server.request("POST", "/ctl/machine/reload")
        self.assertEqual(resp["status"], 422, resp)
        self.assertIn("reload failed", resp["body"])


if __name__ == "__main__":
    unittest.main()
