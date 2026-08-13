"""Unit tests for cli.py — the spawnable-agent entry point.

Covers the fixes for the "spawn Jeeves" caveats:
  • direct tool invocation (--tool / --args / --raw) — deterministic, no LLM
  • _process_turn structured results (reply + tool + result)
  • warm daemon (--daemon / --send) JSON-lines protocol round-trip
"""

import contextlib
import io
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on sys.path so cli is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import cli  # noqa: E402


class FakeBrain:
    """Scripted multi_turn: pops responses from a list in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def multi_turn(self, messages, **kwargs):
        self.calls += 1
        if not self.responses:
            raise RuntimeError("no more scripted responses")
        return self.responses.pop(0)


class ToolDispatchTests(unittest.TestCase):
    """_call_tool() dispatch + the shared _load_runtime_imports contract."""

    def setUp(self):
        self._orig_imports = cli._load_runtime_imports
        self.calls = []
        self._fake_imports = {
            "open_app": self._make_tool("open_app"),
            "run_agentic_task": None,
        }
        cli._load_runtime_imports = lambda: self._fake_imports

    def tearDown(self):
        cli._load_runtime_imports = self._orig_imports

    def _make_tool(self, name):
        def tool(parameters=None, player=None):
            self.calls.append((name, parameters or {}))
            return f"{name} ran"
        return tool

    def test_call_tool_dispatches_with_parameters(self):
        player = cli.ConsolePlayer()
        result = cli._call_tool("open_app", {"app_name": "Notepad"}, player)
        self.assertEqual(result, "open_app ran")
        self.assertEqual(self.calls, [("open_app", {"app_name": "Notepad"})])

    def test_call_tool_unknown_returns_message(self):
        result = cli._call_tool("definitely_not_a_tool", {}, cli.ConsolePlayer())
        self.assertIn("Unknown tool", result)


class ParserTests(unittest.TestCase):
    """New CLI flags must parse correctly."""

    def test_tool_flag(self):
        args = cli._build_parser().parse_args(
            ["--tool", "open_app", "--args", '{"app_name": "Notepad"}', "--raw"]
        )
        self.assertEqual(args.tool, "open_app")
        self.assertEqual(json.loads(args.args), {"app_name": "Notepad"})
        self.assertTrue(args.raw)

    def test_send_flags(self):
        args = cli._build_parser().parse_args(["--send", "hi", "--port", "9999", "--reset"])
        self.assertEqual(args.send, "hi")
        self.assertEqual(args.port, 9999)
        self.assertTrue(args.reset)
        self.assertIsNone(args.send_tool)

    def test_send_tool_flags(self):
        args = cli._build_parser().parse_args(
            ["--send-tool", "system_status", "--send-args", "{}"]
        )
        self.assertEqual(args.send_tool, "system_status")
        self.assertIsNone(args.send)

    def test_daemon_flags(self):
        args = cli._build_parser().parse_args(["--daemon", "--port", "7777"])
        self.assertTrue(args.daemon)
        self.assertEqual(args.port, 7777)
        args2 = cli._build_parser().parse_args(["--daemon-stop"])
        self.assertTrue(args2.daemon_stop)


class MainToolFlowTests(unittest.TestCase):
    """main() with --tool must run the tool and print the raw result."""

    def test_main_tool_raw(self):
        with patch("cli._call_tool", return_value="RAN:open_app") as mock_call:
            with patch("sys.argv", ["cli.py", "--tool", "open_app",
                                    "--args", '{"app_name": "X"}', "--raw"]):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cli.main()
        mock_call.assert_called_once_with("open_app", {"app_name": "X"}, unittest.mock.ANY)
        self.assertEqual(buf.getvalue().strip(), "RAN:open_app")

    def test_main_tool_bad_json(self):
        with patch("sys.argv", ["cli.py", "--tool", "open_app", "--args", "not json"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main()
        self.assertIn("Invalid --args JSON", buf.getvalue())


class ProcessTurnTests(unittest.TestCase):
    """_process_turn must return structured {reply, tool, result}."""

    def setUp(self):
        self._orig_sys_prompt = cli._build_system_prompt
        self._orig_memory = cli._update_memory_async
        self._orig_call_tool = cli._call_tool
        cli._build_system_prompt = lambda: "SYS"
        cli._update_memory_async = lambda *a, **k: None
        cli._call_tool = lambda name, args, player: f"RESULT:{name}"

    def tearDown(self):
        cli._build_system_prompt = self._orig_sys_prompt
        cli._update_memory_async = self._orig_memory
        cli._call_tool = self._orig_call_tool

    def test_tool_path_returns_tool_and_result(self):
        brain = FakeBrain([
            json.dumps({"tool_call": {"name": "open_app", "args": {"app_name": "Notepad"}}}),
            "Done!",
        ])
        conv = []
        out = cli._process_turn("open notepad", cli.ConsolePlayer(), conv, brain)
        self.assertEqual(out["tool"], "open_app")
        self.assertEqual(out["result"], "RESULT:open_app")
        self.assertEqual(out["reply"], "Done!")
        self.assertEqual(brain.calls, 2)

    def test_plain_path(self):
        brain = FakeBrain(["hello there"])
        out = cli._process_turn("hi", cli.ConsolePlayer(), [], brain)
        self.assertEqual(out["reply"], "hello there")
        self.assertIsNone(out["tool"])
        self.assertIsNone(out["result"])

    def test_brain_error_is_reported(self):
        class Boom:
            def multi_turn(self, messages, **kwargs):
                raise RuntimeError("boom")
        out = cli._process_turn("hi", cli.ConsolePlayer(), [], Boom())
        self.assertIn("Brain error", out["reply"])
        self.assertIsNone(out["tool"])

    def test_handle_text_compat_returns_string(self):
        brain = FakeBrain(["plain reply"])
        reply = cli.handle_text("hi", cli.ConsolePlayer(), [], brain)
        self.assertIsInstance(reply, str)
        self.assertEqual(reply, "plain reply")


class DaemonTests(unittest.TestCase):
    """Full daemon protocol round-trip on a localhost socket."""

    @classmethod
    def setUpClass(cls):
        cls._patches = [
            patch("cli._daemon_token", return_value="test-token"),
            patch("cli._get_brain_client",
                  return_value=FakeBrain(["daemon says hi"])),
            patch("cli._call_tool",
                  side_effect=lambda name, args, player: f"RESULT:{name}:{json.dumps(args)}"),
            patch("cli._build_system_prompt", return_value="SYS"),
            patch("cli._update_memory_async", lambda *a, **k: None),
        ]
        for p in cls._patches:
            p.start()

        # Find a free ephemeral port
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()

        cls.thread = threading.Thread(
            target=cli._daemon_run, args=(cls.port,), daemon=True
        )
        cls.thread.start()

        deadline = time.time() + 15
        while time.time() < deadline:
            resp = cli._daemon_request({"type": "ping"}, cls.port, timeout=2.0)
            if resp.get("pong"):
                break
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        try:
            cli._daemon_request({"type": "shutdown"}, cls.port, timeout=5.0)
        except Exception:
            pass
        cls.thread.join(timeout=10)
        for p in cls._patches:
            p.stop()

    def test_ping(self):
        resp = cli._daemon_request({"type": "ping"}, self.port, timeout=5.0)
        self.assertTrue(resp.get("pong"))

    def test_tool_request(self):
        resp = cli._daemon_request(
            {"type": "tool", "name": "open_app", "args": {"app_name": "X"}},
            self.port, timeout=10.0,
        )
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp["tool"], "open_app")
        self.assertEqual(resp["result"], 'RESULT:open_app:{"app_name": "X"}')

    def test_chat_request(self):
        resp = cli._daemon_request({"type": "chat", "text": "hi"}, self.port, timeout=10.0)
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp["reply"], "daemon says hi")

    def test_reset(self):
        resp = cli._daemon_request({"type": "reset"}, self.port, timeout=5.0)
        self.assertTrue(resp.get("ok"))

    def test_bad_token_rejected(self):
        resp = cli._daemon_request(
            {"type": "ping", "token": "wrong"}, self.port, timeout=5.0
        )
        self.assertFalse(resp.get("ok"))
        self.assertIn("invalid token", resp.get("error", ""))

    def test_missing_tool_name(self):
        resp = cli._daemon_request({"type": "tool"}, self.port, timeout=5.0)
        self.assertFalse(resp.get("ok"))
        self.assertIn("requires 'name'", resp.get("error", ""))


if __name__ == "__main__":
    unittest.main()
