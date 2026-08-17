"""Unit tests for cli.py — the spawnable-agent entry point.

Covers the fixes for the "spawn Jeeves" caveats:
  • direct tool invocation (--tool / --args / --raw) — deterministic, no LLM
  • _process_turn structured results (reply + tool + result)
  • warm daemon (--daemon / --send) JSON-lines protocol round-trip
"""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
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

    def test_call_tool_screen_process_returns_analysis_text(self):
        # The remote WhatsApp dashboard needs the real description, not the
        # old "Vision module activated" stub — so _call_tool must return
        # whatever screen_process analyzed. A fake module keeps the test
        # fast (the real one costs ~14s to import).
        import sys
        import types
        fake = types.ModuleType("actions.screen_processor")
        fake.screen_process = lambda parameters=None, response=None, \
            player=None, session_memory=None: \
            "The screen shows VS Code with the secretary running."
        sys.modules["actions.screen_processor"] = fake
        try:
            out = cli._call_tool("screen_process",
                                 {"text": "describe", "angle": "screen"},
                                 cli.ConsolePlayer())
        finally:
            del sys.modules["actions.screen_processor"]
        self.assertIn("VS Code", out)
        self.assertNotIn("Vision module activated", out)

    def test_call_tool_screen_process_failure_message(self):
        import sys
        import types
        fake = types.ModuleType("actions.screen_processor")
        fake.screen_process = lambda parameters=None, response=None, \
            player=None, session_memory=None: False
        sys.modules["actions.screen_processor"] = fake
        try:
            out = cli._call_tool("screen_process",
                                 {"text": "describe", "angle": "screen"},
                                 cli.ConsolePlayer())
        finally:
            del sys.modules["actions.screen_processor"]
        self.assertIn("Vision analysis failed", out)


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

    def test_brain_error_is_reported_when_no_fallback(self):
        class Boom:
            def multi_turn(self, messages, **kwargs):
                raise RuntimeError("boom")
        with patch("cli._meta_ai_fallback", return_value=None):
            out = cli._process_turn("hi", cli.ConsolePlayer(), [], Boom())
        self.assertIn("Brain error", out["reply"])
        self.assertIsNone(out["tool"])

    def test_brain_error_falls_back_to_meta_ai(self):
        class Boom:
            def multi_turn(self, messages, **kwargs):
                raise RuntimeError("boom")
        conv = []
        with patch("cli._meta_ai_fallback", return_value="META answer"):
            out = cli._process_turn("hi", cli.ConsolePlayer(), conv, Boom())
        self.assertEqual(out["reply"], "[Meta AI] META answer")
        self.assertEqual(conv[-1]["content"], "[Meta AI] META answer")

    def test_meta_ai_fallback_returns_answer(self):
        with patch("actions.meta_ai._ask_bridge", return_value="the answer"):
            self.assertEqual(cli._meta_ai_fallback("q"), "the answer")

    def test_meta_ai_fallback_none_when_unavailable(self):
        with patch("actions.meta_ai._ask_bridge", side_effect=RuntimeError("no link")):
            self.assertIsNone(cli._meta_ai_fallback("q"))

    def test_handle_text_compat_returns_string(self):
        brain = FakeBrain(["plain reply"])
        reply = cli.handle_text("hi", cli.ConsolePlayer(), [], brain)
        self.assertIsInstance(reply, str)
        self.assertEqual(reply, "plain reply")


class ShortcutTests(unittest.TestCase):
    """Plain-language shortcuts must map to direct tool calls (no LLM)."""

    def setUp(self):
        self.calls = []
        self._orig_call_tool = cli._call_tool
        cli._call_tool = self._fake_call_tool

    def tearDown(self):
        cli._call_tool = self._orig_call_tool

    def _fake_call_tool(self, name, args, player):
        self.calls.append((name, args))
        return f"RAN:{name}"

    def _match(self, text):
        return cli._try_shortcut(text, cli.ConsolePlayer())

    def test_vision_screen_phrases(self):
        for text in ("what's on my screen", "whats on my screen", "screenshot",
                     "look at my screen", "see my screen"):
            out = self._match(text)
            self.assertIsNotNone(out, text)
            self.assertEqual(self.calls[-1][0], "screen_process", text)
            self.assertEqual(self.calls[-1][1]["angle"], "screen", text)

    def test_vision_camera_phrases(self):
        out = self._match("take a picture")
        self.assertEqual(self.calls[-1][0], "screen_process")
        self.assertEqual(self.calls[-1][1]["angle"], "camera")

    def test_open_app_extracts_name(self):
        out = self._match("open notepad")
        self.assertEqual(self.calls[-1], ("open_app", {"app_name": "Notepad"}))
        self._match("open the terminal")
        self.assertEqual(self.calls[-1][1]["app_name"], "Terminal")

    def test_search_extracts_query(self):
        self._match("search python 3.13")
        self.assertEqual(self.calls[-1], ("web_search",
                                          {"query": "python 3.13", "mode": "search"}))
        self._match("google bitcoin price")
        self.assertEqual(self.calls[-1][1]["query"], "bitcoin price")

    def test_play_extracts_query(self):
        self._match("play despacito")
        self.assertEqual(self.calls[-1], ("youtube_video",
                                          {"action": "play", "query": "despacito"}))

    def test_weather_extracts_city(self):
        self._match("weather in paris")
        self.assertEqual(self.calls[-1], ("weather_report", {"city": "paris"}))
        # no city -> empty string, tool decides
        self._match("what's the weather like")
        self.assertEqual(self.calls[-1], ("weather_report", {"city": ""}))

    def test_system_status(self):
        self._match("system status")
        self.assertEqual(self.calls[-1], ("system_status", {}))
        self._match("cpu usage")
        self.assertEqual(self.calls[-1][0], "system_status")

    def test_instant_time_and_date_need_no_tool(self):
        self.assertIn("It's", self._match("what time is it"))
        self.assertIn("Today is", self._match("what's the date"))
        self.assertEqual(self.calls, [])  # no tool touched

    def test_msg_shortcut_sends_message(self):
        self._match("msg alixon: hi there")
        self.assertEqual(self.calls[-1], ("send_message", {
            "receiver": "alixon", "message_text": "hi there",
            "platform": "whatsapp"}))
        self._match("text mom hey how are you")
        self.assertEqual(self.calls[-1][1], {
            "receiver": "mom", "message_text": "hey how are you",
            "platform": "whatsapp"})
        self._match("message bob hello")
        self.assertEqual(self.calls[-1][1]["receiver"], "bob")

    def test_platform_shortcut_selects_app(self):
        self._match("whatsapp alixon hi")
        self.assertEqual(self.calls[-1][1]["platform"], "whatsapp")
        self._match("telegram bro yo")
        self.assertEqual(self.calls[-1][1]["platform"], "telegram")
        self._match("ig sis hello")
        self.assertEqual(self.calls[-1][1]["platform"], "instagram")

    def test_tell_shortcut_sends_but_me_us_fall_through(self):
        self._match("tell alixon hi there")
        self.assertEqual(self.calls[-1][1]["receiver"], "alixon")
        # "tell me ..." must stay a chat request, never a real message
        self.assertIsNone(self._match("tell me a joke"))
        self.assertIsNone(self._match("msg me hello"))
        # "text me the weather" → the guard blocks the message, so the
        # weather shortcut (not a send) handles it instead
        self._match("text me the weather")
        self.assertNotEqual(self.calls[-1][0], "send_message")

    def test_msg_quoted_receiver_keeps_spaces(self):
        # 'omoke jr' is one receiver — the parser must not split mid-name
        self._match("msg 'omoke jr' hi")
        self.assertEqual(self.calls[-1][1], {
            "receiver": "omoke jr", "message_text": "hi",
            "platform": "whatsapp"})
        self._match('msg "alixon the boss" hello there')
        self.assertEqual(self.calls[-1][1], {
            "receiver": "alixon the boss", "message_text": "hello there",
            "platform": "whatsapp"})
        self._match("text 'omoke jr': hey")
        self.assertEqual(self.calls[-1][1], {
            "receiver": "omoke jr", "message_text": "hey",
            "platform": "whatsapp"})

    def test_msg_needs_both_receiver_and_text(self):
        self.assertIsNone(self._match("msg alixon"))
        self.assertIsNone(self._match("msg"))

    def test_briefing_and_balance_shortcuts(self):
        self._match("good morning")
        self.assertEqual(self.calls[-1], ("daily_briefing", {}))
        self._match("briefing")
        self.assertEqual(self.calls[-1][0], "daily_briefing")
        self._match("my balance")
        self.assertEqual(self.calls[-1], ("business_tracker", {"action": "balance"}))
        self._match("how much money do i have")
        self.assertEqual(self.calls[-1][0], "business_tracker")
        # natural phrasing stays with the LLM (the brain calls the tool)
        self.assertIsNone(self._match("track 50 income from freelancing"))

    def test_anime_shortcuts(self):
        self._match("new anime")
        self.assertEqual(self.calls[-1], ("anime_watch", {"action": "new"}))
        self._match("trending anime")
        self.assertEqual(self.calls[-1], ("anime_watch", {"action": "trending"}))
        self._match("what anime should i watch")
        self.assertEqual(self.calls[-1], ("anime_watch", {"action": "new"}))

    def test_secretary_shortcuts(self):
        self._match("secretary on")
        self.assertEqual(self.calls[-1], ("secretary", {"action": "on"}))
        self._match("secretary mode on")
        self.assertEqual(self.calls[-1], ("secretary", {"action": "on"}))
        self._match("secretary off")
        self.assertEqual(self.calls[-1], ("secretary", {"action": "off"}))
        self._match("secretary status")
        self.assertEqual(self.calls[-1], ("secretary", {"action": "status"}))
        self._match("secretary state")
        self.assertEqual(self.calls[-1], ("secretary", {"action": "status"}))
        self._match("any messages for me")
        self.assertEqual(self.calls[-1], ("secretary", {"action": "inbox"}))
        self._match("check my inbox")
        self.assertEqual(self.calls[-1], ("secretary", {"action": "inbox"}))

    def test_secretary_feed_shortcuts(self):
        # every natural way of relaying an incoming message → secretary handle
        for text, sender, msg in (
            ("mom says: dinner at 7?", "mom", "dinner at 7?"),
            ("mom says dinner at 7?", "mom", "dinner at 7?"),
            ("handle from mom: dinner at 7?", "mom", "dinner at 7?"),
            ("incoming from mom: dinner at 7?", "mom", "dinner at 7?"),
            ("message from mom: dinner at 7?", "mom", "dinner at 7?"),
            ("text from alixon: are you there?", "alixon", "are you there?"),
        ):
            self._match(text)
            self.assertEqual(self.calls[-1], ("secretary", {
                "action": "handle", "sender": sender, "message": msg,
            }), text)

    def test_secretary_reply_shortcut(self):
        self._match("reply to mom: yes sounds good")
        self.assertEqual(self.calls[-1], ("secretary", {
            "action": "reply", "sender": "mom", "text": "yes sounds good"}))
        self._match("reply mom: no thanks")
        self.assertEqual(self.calls[-1][1]["text"], "no thanks")

    def test_message_from_relays_not_sends(self):
        # "message from mom: ..." must route to the secretary, not send a
        # WhatsApp message to a contact named "from mom".
        self._match("message from mom: dinner at 7?")
        self.assertEqual(self.calls[-1][0], "secretary")

    def test_meta_ai_shortcut(self):
        self._match("ask meta ai what is 2+2")
        self.assertEqual(self.calls[-1], ("meta_ai", {"question": "what is 2+2"}))
        self._match("meta ai: explain photosynthesis")
        self.assertEqual(self.calls[-1], ("meta_ai", {"question": "explain photosynthesis"}))
        self._match("ask meta ai - what is the weather like in mars")
        self.assertEqual(self.calls[-1][1]["question"],
                         "what is the weather like in mars")

    def test_meta_ai_shortcut_does_not_collide(self):
        # plain questions about AI must NOT route to the Meta AI tool
        for text in ("what do you think about ai", "tell me about ai",
                     "metaphorically speaking"):
            self.assertIsNone(self._match(text), text)
        # "what time is it" is its own instant shortcut — no tool at all
        out = self._match("what time is it")
        self.assertTrue("AM" in out or "PM" in out, out)
        self.assertEqual(self.calls, [])

    def test_unmatched_input_falls_through(self):
        for text in ("hi there", "tell me a joke", "exit",
                     "what do you think about ai"):
            self.assertIsNone(self._match(text), text)
        self.assertEqual(self.calls, [])

    def test_handle_text_intercepts_before_brain(self):
        class BoomBrain:
            def multi_turn(self, *a, **k):
                raise AssertionError("brain must not run for shortcuts")
        reply = cli.handle_text("system status", cli.ConsolePlayer(), [], BoomBrain())
        self.assertIn("RAN:system_status", reply)


class ManageMonitorDispatchTests(unittest.TestCase):
    """manage_monitor must be registered and dispatchable in the CLI."""

    def setUp(self):
        self._orig_imports = cli._load_runtime_imports
        self.monitors = ["OpenAI news"]

        def fake_imports():
            return {
                "add_monitor":    lambda topic: f"Now monitoring: {topic}",
                "remove_monitor": lambda topic: f"Stopped monitoring: {topic}",
                "list_monitors":  lambda: list(self.monitors),
            }
        cli._load_runtime_imports = fake_imports

    def tearDown(self):
        cli._load_runtime_imports = self._orig_imports

    def test_add_monitor(self):
        result = cli._call_tool(
            "manage_monitor", {"action": "add", "topic": "PS5 restock"},
            cli.ConsolePlayer())
        self.assertIn("Now monitoring: PS5 restock", result)

    def test_remove_monitor(self):
        result = cli._call_tool(
            "manage_monitor", {"action": "remove", "topic": "OpenAI news"},
            cli.ConsolePlayer())
        self.assertIn("Stopped monitoring: OpenAI news", result)

    def test_list_monitors(self):
        result = cli._call_tool(
            "manage_monitor", {"action": "list"}, cli.ConsolePlayer())
        self.assertIn("OpenAI news", result)

    def test_list_empty(self):
        self.monitors = []
        result = cli._call_tool(
            "manage_monitor", {"action": "list"}, cli.ConsolePlayer())
        self.assertIn("No topics", result)

    def test_registered_in_tool_definitions(self):
        from config.tool_definitions import TOOL_DECLARATIONS, TOOL_REGISTRY
        self.assertIn("manage_monitor", [t["name"] for t in TOOL_DECLARATIONS])
        self.assertIn("manage_monitor", [t["name"] for t in TOOL_REGISTRY])


class BusinessBriefingDispatchTests(unittest.TestCase):
    """business_tracker + daily_briefing must be dispatchable in the CLI."""

    def setUp(self):
        self._orig_imports = cli._load_runtime_imports
        cli._load_runtime_imports = lambda: {
            "business_tracker": lambda parameters, player=None:
                f"BT:{parameters.get('action')}",
            "daily_briefing": lambda parameters, player=None: "BRIEFING",
            "anime_watch": lambda parameters, player=None:
                f"ANIME:{parameters.get('action')}",
        }

    def tearDown(self):
        cli._load_runtime_imports = self._orig_imports

    def test_business_tracker_dispatch(self):
        out = cli._call_tool("business_tracker", {"action": "balance"},
                             cli.ConsolePlayer())
        self.assertEqual(out, "BT:balance")

    def test_daily_briefing_dispatch(self):
        out = cli._call_tool("daily_briefing", {}, cli.ConsolePlayer())
        self.assertEqual(out, "BRIEFING")

    def test_anime_watch_dispatch(self):
        out = cli._call_tool("anime_watch", {"action": "trending"},
                             cli.ConsolePlayer())
        self.assertEqual(out, "ANIME:trending")

    def test_registered_in_tool_definitions(self):
        from config.tool_definitions import TOOL_DECLARATIONS, TOOL_REGISTRY
        for tool in ("business_tracker", "daily_briefing", "anime_watch"):
            self.assertIn(tool, [t["name"] for t in TOOL_DECLARATIONS])
            self.assertIn(tool, [t["name"] for t in TOOL_REGISTRY])


class BusinessTrackerModuleTests(unittest.TestCase):
    """business_tracker math, validation, and storage against a temp file."""

    def setUp(self):
        import actions.business_tracker as _bt
        global bt
        bt = _bt
        import memory.memory_manager as mm
        self._mem_patch = patch.object(
            mm, "MEMORY_PATH",
            Path(tempfile.mkdtemp()) / "long_term.json",
        )
        self._mem_patch.start()

    def tearDown(self):
        self._mem_patch.stop()

    def test_add_and_balance(self):
        bt.add_entry("income", 100, "freelance", "2026-08-01")
        bt.add_entry("expense", 25.5, "software", "2026-08-02")
        out = bt.balance()
        self.assertIn("$74.50", out)
        self.assertIn("Income $100.00", out)
        self.assertIn("Expenses $25.50", out)

    def test_validation(self):
        self.assertIn("kind must be", bt.add_entry("gift", 10))
        self.assertIn("positive amount", bt.add_entry("income", 0))
        self.assertIn("positive amount", bt.add_entry("income", -5))
        self.assertIn("positive amount", bt.add_entry("income", "abc"))

    def test_monthly_report(self):
        bt.add_entry("income", 100, "a", "2026-08-01")
        bt.add_entry("expense", 30, "b", "2026-07-20")
        out = bt.monthly_report("2026-08")
        self.assertIn("Net $100.00", out)
        self.assertNotIn("for b", out)

    def test_import_csv(self):
        out = bt.import_csv(
            "2026-08-01,income,50,freelance\n2026-08-02,expense,10,coffee")
        self.assertIn("Imported 2", out)
        self.assertIn("$40.00", bt.balance())

    def test_remove_by_index_and_clear(self):
        bt.add_entry("income", 10, "a")
        bt.add_entry("income", 20, "b")
        out = bt.remove_entry(index=1)   # newest first → removes "b"
        self.assertIn("for b", out)
        self.assertIn("$10.00", bt.balance())
        self.assertIn("confirm", bt.clear())
        self.assertIn("cleared", bt.clear(confirm="yes"))
        self.assertIn("No entries yet", bt.balance())


class DailyBriefingTests(unittest.TestCase):
    """daily_briefing aggregates local state and degrades gracefully."""

    def _empty(self):
        return [
            patch("actions.daily_briefing._finance_snapshot", return_value=""),
            patch("actions.daily_briefing._monitors_snapshot", return_value=""),
            patch("actions.daily_briefing._reminders_snapshot", return_value=""),
        ]

    def test_empty_briefing_has_greeting_and_hint(self):
        from actions.daily_briefing import daily_briefing
        with patch("actions.daily_briefing._finance_snapshot", return_value=""):
            with patch("actions.daily_briefing._monitors_snapshot", return_value=""):
                with patch("actions.daily_briefing._reminders_snapshot", return_value=""):
                    out = daily_briefing({})
        self.assertIn("Good ", out)
        self.assertIn("Nothing on the books", out)

    def test_briefing_includes_finances(self):
        from actions.daily_briefing import daily_briefing
        with patch("actions.daily_briefing._finance_snapshot",
                   return_value="  Finances:\n    Balance $74.50"):
            with patch("actions.daily_briefing._monitors_snapshot", return_value=""):
                with patch("actions.daily_briefing._reminders_snapshot", return_value=""):
                    out = daily_briefing({})
        self.assertIn("$74.50", out)

    def test_include_email_calls_agent(self):
        import types
        from actions.daily_briefing import daily_briefing
        fake = types.SimpleNamespace(run_agentic_task=lambda *a, **k: "Email summary")
        with patch.dict(sys.modules, {"composio_agent": fake}):
            with patch("actions.daily_briefing._finance_snapshot", return_value=""):
                with patch("actions.daily_briefing._monitors_snapshot", return_value=""):
                    with patch("actions.daily_briefing._reminders_snapshot", return_value=""):
                        out = daily_briefing({"include_email": "true"})
        self.assertIn("Email summary", out)

    def test_reminders_snapshot_parses_schtasks(self):
        from actions.daily_briefing import _reminders_snapshot
        fake = type("CP", (), {"returncode": 0, "stdout": (
            '"MARKReminder_20260801_0900","08/01/2026 09:00 AM","Ready","x"\n'
            '"Other","08/02/2026 09:00 AM","Ready","x"\n'
        )})()
        with patch("actions.daily_briefing.subprocess.run", return_value=fake):
            out = _reminders_snapshot()
        self.assertIn("Reminders (1", out)
        self.assertIn("Aug 01, 09:00 AM", out)
        self.assertNotIn("Other", out)


class AnimeWatchTests(unittest.TestCase):
    """anime_watch rendering, Netflix flags, and the trending fallback."""

    def setUp(self):
        import actions.anime_watch as _aw
        global aw
        aw = _aw
        self._cache_patch = patch("actions.anime_watch._anilist_cached")
        self._cache = self._cache_patch.start()
        self._net_patch = patch("actions.anime_watch._netflix_check")
        self._net = self._net_patch.start()

    def tearDown(self):
        self._net_patch.stop()
        self._cache_patch.stop()

    def _media(self, title, status="FINISHED", episodes=24, season="SPRING",
               year=2026, score=85, popularity=900000):
        return {
            "title": {"romaji": title, "english": title},
            "episodes": episodes, "status": status, "season": season,
            "seasonYear": year, "format": "TV", "genres": ["Action", "Drama"],
            "averageScore": score, "popularity": popularity,
            "siteUrl": f"https://anilist.co/anime/{title.lower().replace(' ', '')}",
        }

    def test_new_releases_renders_details_and_netflix_flags(self):
        from actions.anime_watch import _new_releases
        self._cache.return_value = [
            self._media("Show A", status="RELEASING", episodes=12),
            self._media("Show B"),
        ]
        self._net.side_effect = lambda t: t == "Show A"
        out = _new_releases()
        self.assertIn("New anime airing this", out)
        self.assertIn("Show A", out)
        self.assertIn("[ON NETFLIX]", out)
        self.assertIn("ongoing", out)          # RELEASING
        self.assertIn("fully released", out)   # FINISHED
        self.assertIn("12 episodes", out)
        self.assertIn("Genre: Action", out)
        self.assertIn("aired Spring 2026", out)
        self.assertIn("On Netflix: Show A", out)
        self.assertIn("Show B", out)           # non-Netflix never dropped

    def test_empty_season_falls_back_to_trending(self):
        from actions.anime_watch import _new_releases
        def side(key, *a, **k):
            return [] if key.startswith("new:") else [self._media("Top Pick")]
        self._cache.side_effect = side
        self._net.return_value = False
        out = _new_releases()
        self.assertIn("No new anime airing", out)
        self.assertIn("Top Pick", out)

    def test_check_title_reports_netflix_availability(self):
        from actions.anime_watch import _check_title
        self._cache.return_value = [self._media("Demon Slayer")]
        self._net.return_value = True
        out = _check_title("Demon Slayer")
        self.assertIn("Demon Slayer", out)
        self.assertIn("Available on Netflix", out)
        self._net.return_value = False
        out = _check_title("Some Obscure Show")
        self.assertIn("Not found on Netflix", out)

    def test_unknown_action(self):
        from actions.anime_watch import anime_watch
        out = anime_watch({"action": "bogus"})
        self.assertIn("Unknown action", out)


class DdgHtmlFallbackTests(unittest.TestCase):
    """requests-only DDG fallback used by search + the monitor."""

    def test_parses_result_links(self):
        from actions.web_search import _ddg_html
        fake = ('<a rel="nofollow" class="result__a" '
                'href="https://example.com/x">Title 1</a>'
                '<a class="result__a" href="https://example.com/y">'
                'Title <b>2</b></a>')
        with patch("requests.get") as get:
            get.return_value.raise_for_status = lambda: None
            get.return_value.text = fake
            out = _ddg_html("q")
        self.assertEqual([r["title"] for r in out], ["Title 1", "Title 2"])
        self.assertEqual(out[0]["url"], "https://example.com/x")

    def test_network_failure_returns_empty(self):
        from actions.web_search import _ddg_html
        with patch("requests.get", side_effect=OSError("down")):
            self.assertEqual(_ddg_html("q"), [])

    def test_bing_redirect_decodes_real_url(self):
        from actions.web_search import _bing_redirect_target
        import base64
        target = "https://www.netflix.com/title/81091393"
        href = "https://www.bing.com/ck/a?!&&p=abc" \
               "&u=a1" + base64.b64encode(target.encode()).decode() + "&ntb=1"
        self.assertEqual(_bing_redirect_target(href), target)
        # non-redirect hrefs pass through unchanged
        self.assertEqual(_bing_redirect_target("https://plain.example/x"),
                         "https://plain.example/x")


class CheckAllParallelTests(unittest.TestCase):
    """check_all runs topics in parallel and dedupes per day."""

    def setUp(self):
        import memory.memory_manager as mm
        import actions.background_monitor as _bm
        global bm
        bm = _bm
        self._mem_patch = patch.object(
            mm, "MEMORY_PATH",
            Path(tempfile.mkdtemp()) / "long_term.json",
        )
        self._mem_patch.start()
        bm.add_monitor("Alpha topic")
        bm.add_monitor("Beta topic")

    def tearDown(self):
        self._mem_patch.stop()

    def test_parallel_check_all_alerts_and_dedupes(self):
        import actions.background_monitor as bm
        def fake_news(topic, max_results=5):
            return [{"title": f"Headline about {topic}", "snippet": "snip"}]
        with patch("actions.web_search._ddg_news", side_effect=fake_news) as news:
            alerts = bm.check_all()
        self.assertEqual(len(alerts), 2)
        self.assertEqual(news.call_count, 2)
        self.assertTrue(any("Alpha topic" in a for a in alerts))
        # same day, second run → nothing new, no network calls
        with patch("actions.web_search._ddg_news", side_effect=fake_news) as news2:
            self.assertEqual(bm.check_all(), [])
            news2.assert_not_called()


class SubcommandTests(unittest.TestCase):
    """Friendly one-shot subcommands (ask / daemon / tool / reset)."""

    def _run(self, argv):
        buf = io.StringIO()
        with patch("sys.argv", ["cli.py"] + argv):
            with contextlib.redirect_stdout(buf):
                handled = cli._main_subcommands()
        return handled, buf.getvalue()

    def test_ask_without_text_shows_usage(self):
        handled, out = self._run(["ask"])
        self.assertTrue(handled)
        self.assertIn("Usage: python cli.py ask", out)

    def test_ask_routes_chat_to_daemon(self):
        with patch("cli._daemon_send_or_spawn",
                   return_value={"ok": True, "reply": "hi back"}) as m:
            handled, out = self._run(["ask", "hello there"])
        self.assertTrue(handled)
        m.assert_called_once_with(
            {"type": "chat", "text": "hello there"}, cli.DAEMON_DEFAULT_PORT)
        self.assertIn("hi back", out)

    def test_daemon_status_running(self):
        with patch("cli._daemon_request", return_value={"ok": True, "pong": True}):
            handled, out = self._run(["daemon", "status"])
        self.assertTrue(handled)
        self.assertIn("is running", out)

    def test_daemon_status_not_running(self):
        with patch("cli._daemon_request",
                   return_value={"ok": False, "error": "unreachable"}):
            _, out = self._run(["daemon", "status"])
        self.assertIn("not running", out)

    def test_daemon_stop_sends_shutdown(self):
        with patch("cli._daemon_request",
                   return_value={"ok": True, "reply": "bye"}) as m:
            handled, out = self._run(["daemon", "stop"])
        self.assertTrue(handled)
        m.assert_called_once_with(
            {"type": "shutdown"}, cli.DAEMON_DEFAULT_PORT, timeout=10.0)
        self.assertIn("stopped", out)

    def test_daemon_unknown_verb_shows_usage(self):
        handled, out = self._run(["daemon", "bogus"])
        self.assertTrue(handled)
        self.assertIn("Usage: python cli.py daemon", out)

    def test_tool_with_args_calls_tool(self):
        with patch("cli._call_tool", return_value="RAN:open_app") as m:
            handled, out = self._run(["tool", "open_app", '{"app_name": "Notepad"}'])
        self.assertTrue(handled)
        m.assert_called_once_with("open_app", {"app_name": "Notepad"}, unittest.mock.ANY)
        self.assertIn("RAN:open_app", out)

    def test_tool_without_name_shows_usage(self):
        handled, out = self._run(["tool"])
        self.assertTrue(handled)
        self.assertIn("Usage: python cli.py tool", out)

    def test_tool_bad_json_reports_error(self):
        handled, out = self._run(["tool", "open_app", "not json"])
        self.assertTrue(handled)
        self.assertIn("Invalid args JSON", out)

    def test_reset_clears_daemon_conversation(self):
        with patch("cli._daemon_send_or_spawn", return_value={"ok": True}) as m:
            handled, out = self._run(["reset"])
        self.assertTrue(handled)
        m.assert_called_once_with({"type": "reset"}, cli.DAEMON_DEFAULT_PORT)
        self.assertIn("reset", out)

    def test_unknown_args_fall_through_to_flag_parser(self):
        self.assertFalse(self._run(["-c", "hi"])[0])

    def test_daemon_chat_handles_shortcuts_without_brain(self):
        """ask 'system status' must run the tool directly, never the LLM."""
        class BoomBrain:
            def multi_turn(self, *a, **k):
                raise AssertionError("brain must not run for daemon shortcuts")
        with patch("cli._call_tool",
                   side_effect=lambda name, args, player: f"RAN:{name}"):
            resp = cli._daemon_handle(
                {"type": "chat", "text": "system status"},
                cli.ConsolePlayer(),
                {"client": BoomBrain()},
                [],
                threading.Lock(),
            )
        self.assertTrue(resp.get("ok"))
        self.assertIn("RAN:system_status", resp["reply"])


class IdleShutdownTests(unittest.TestCase):
    """The daemon auto-shuts down after idle_timeout with no requests."""

    def setUp(self):
        # A live secretary_mode flag in config would start the background
        # monitor, which (by design) keeps the daemon alive — these tests
        # assert idle behavior, so force the flag off.
        self._idle_patches = [
            patch("actions.secretary.is_enabled", return_value=False),
            # the always-on remote dashboard would start the WhatsApp monitor
            # even with secretary off (secretary_self_chat is configured in
            # the real config) — stub it out so no browser launches here
            patch("actions.secretary_listener.start_monitor",
                  return_value="monitor skipped (test)"),
        ]
        for p in self._idle_patches:
            p.start()

    def tearDown(self):
        for p in self._idle_patches:
            p.stop()

    def _free_port(self) -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _start(self, idle_timeout: float, port: int) -> threading.Thread:
        thread = threading.Thread(
            target=cli._daemon_run,
            args=(port, "127.0.0.1", idle_timeout),
            daemon=True,
        )
        thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            resp = cli._daemon_request({"type": "ping"}, port, timeout=2.0)
            if resp.get("pong"):
                return thread
            time.sleep(0.2)
        raise AssertionError("daemon did not start")

    def _alive(self, port: int) -> bool:
        try:
            return bool(cli._daemon_request({"type": "ping"}, port, timeout=1.5).get("pong"))
        except Exception:
            return False

    def test_shuts_down_after_idle(self):
        port = self._free_port()
        thread = self._start(0.4, port)
        # Wait silently: polling pings would count as requests and reset
        # the idle timer, which is exactly what test_requests_reset covers.
        time.sleep(3.0)
        self.assertFalse(self._alive(port))
        thread.join(timeout=5)

    def test_requests_reset_idle_timer(self):
        port = self._free_port()
        thread = self._start(1.0, port)
        try:
            t0 = time.time()
            while time.time() - t0 < 2.0:
                self.assertTrue(self._alive(port))  # pings keep it alive
                time.sleep(0.3)
        finally:
            cli._daemon_request({"type": "shutdown"}, port, timeout=5.0)
            thread.join(timeout=5)

    def test_zero_timeout_disables_shutdown(self):
        port = self._free_port()
        thread = self._start(0, port)
        try:
            time.sleep(1.2)
            self.assertTrue(self._alive(port))
        finally:
            cli._daemon_request({"type": "shutdown"}, port, timeout=5.0)
            thread.join(timeout=5)


class ToolTutorialTests(unittest.TestCase):
    """Per-tool tips + /tools <name> text tutorials."""

    def setUp(self):
        self._orig_seen = cli._SEEN_TOOL_TIPS
        cli._SEEN_TOOL_TIPS = set()

    def tearDown(self):
        cli._SEEN_TOOL_TIPS = self._orig_seen

    def test_tip_shown_once_per_session(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._maybe_show_tool_tip("send_message")
            cli._maybe_show_tool_tip("send_message")
        out = buf.getvalue()
        self.assertEqual(out.count("💡 send_message"), 1)

    def test_unknown_tool_silent(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._maybe_show_tool_tip("not_a_real_tool")
        self.assertEqual(buf.getvalue().strip(), "")

    def test_tutorial_content(self):
        from config.tool_tips import all_tutorial_names, tool_tutorial
        t = tool_tutorial("send_message")
        self.assertIn("what:", t)
        self.assertIn("try:", t)
        self.assertIn("send_message", t)
        self.assertEqual(tool_tutorial("nope"), "")
        self.assertIn("send_message", all_tutorial_names())

    def test_print_tutorial_known_and_unknown(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_tutorial("secretary")
        self.assertIn("Secretary mode", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_tutorial("nope")
        self.assertIn("No tutorial", buf.getvalue())

    def test_call_tool_prints_tip_first_use(self):
        imports = {"web_search_action": lambda parameters=None, player=None: "ok"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             patch("cli._load_runtime_imports", return_value=imports):
            cli._call_tool("web_search", {}, cli.ConsolePlayer())
            cli._call_tool("web_search", {}, cli.ConsolePlayer())
        self.assertEqual(buf.getvalue().count("💡 web_search"), 1)


class SecretaryTests(unittest.TestCase):
    """Secretary engine: triage decisions, mode toggle, inbox flow."""

    def _empty_state(self):
        return {"conversations": {}, "inbox": []}

    def _patches(self, state, enabled=True, boss="Boss"):
        return [
            patch("actions.secretary.is_enabled", return_value=enabled),
            patch("actions.secretary._state", return_value=state),
            patch("actions.secretary._save_state", lambda s: None),
            patch("actions.secretary._load_cfg", return_value={"boss_name": boss}),
            # Meta AI drafting is pinned to the deterministic draft here (its
            # own logic is covered in MetaAiDraftTests); keep the mode-toggle
            # paths hermetic: never start the real background WhatsApp
            # monitor inside unit tests
            patch("actions.secretary._meta_draft",
                   side_effect=lambda s, m, d, media_kind=None: d),
            patch("actions.secretary_listener.start_monitor",
                  return_value="monitoring active"),
            patch("actions.secretary_listener.stop_monitor",
                  return_value="stopped"),
        ]

    def test_triage_escalates_urgency(self):
        from actions.secretary import triage
        with patch("actions.secretary._state",
                   return_value=self._empty_state()):
            d = triage("Mom", "URGENT — please confirm the payment today")
        self.assertEqual(d["action"], "escalate")
        self.assertIn("urgency", " ".join(d["reasons"]))

    def test_triage_escalates_decision(self):
        from actions.secretary import triage
        with patch("actions.secretary._state",
                   return_value=self._empty_state()):
            d = triage("Client", "are you available for a meeting at 3pm?")
        self.assertEqual(d["action"], "escalate")
        self.assertIn("decision", " ".join(d["reasons"]))

    def test_triage_replies_unknown_sender(self):
        # The boss asked to monitor ALL of WhatsApp: a stranger's routine
        # message gets a polite reply, not an "unknown sender" escalation.
        from actions.secretary import triage
        with patch("actions.secretary._state",
                   return_value=self._empty_state()):
            d = triage("stranger123", "hello")
        self.assertEqual(d["action"], "reply")
        self.assertEqual(d["reasons"], [])

    def test_triage_escalates_repeats(self):
        from actions.secretary import triage
        state = {"conversations": {"Mom": [
            {"role": "incoming", "text": "a"},
            {"role": "incoming", "text": "b"},
        ]}, "inbox": []}
        with patch("actions.secretary._state", return_value=state):
            d = triage("Mom", "hello?")
        self.assertEqual(d["action"], "escalate")
        self.assertIn("unanswered", " ".join(d["reasons"]))

    def test_triage_replies_routine(self):
        from actions.secretary import triage
        with patch("actions.secretary._state",
                   return_value=self._empty_state()):
            d = triage("Mom", "how are you doing")
        self.assertEqual(d["action"], "reply")
        self.assertIn("Thanks", d["draft"])

    def test_handle_reply_sends(self):
        from actions.secretary import handle_message
        state = self._empty_state()
        with contextlib.ExitStack() as stack:
            for p in self._patches(state):
                stack.enter_context(p)
            sm = stack.enter_context(
                patch("actions.send_message.send_message", return_value="sent"))
            out = handle_message("Mom", "hey how are you")
        self.assertIn("Replied to Mom", out)
        self.assertIn("Thanks", out)
        sm.assert_called_once()
        # conversation was logged
        self.assertEqual(len(state["conversations"]["Mom"]), 2)

    def test_handle_escalates_to_inbox(self):
        from actions.secretary import handle_message
        state = self._empty_state()
        with contextlib.ExitStack() as stack:
            for p in self._patches(state):
                stack.enter_context(p)
            sm = stack.enter_context(
                patch("actions.send_message.send_message", return_value="sent"))
            out = handle_message("Mom", "URGENT — call me about the contract")
        self.assertIn("ESCALATED", out)
        sm.assert_not_called()
        self.assertEqual(len(state["inbox"]), 1)
        self.assertEqual(state["inbox"][0]["from"], "Mom")

    def test_handle_requires_mode_on(self):
        from actions.secretary import handle_message
        with contextlib.ExitStack() as stack:
            for p in self._patches(self._empty_state(), enabled=False):
                stack.enter_context(p)
            out = handle_message("Mom", "hi")
        self.assertIn("OFF", out)

    def test_mode_toggle_persists(self):
        from actions.secretary import is_enabled, secretary
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            cfg.write_text("{}", encoding="utf-8")
            with patch("actions.secretary._CFG_PATH", cfg), \
                 patch("actions.secretary_listener.start_monitor",
                       return_value="monitoring active"), \
                 patch("actions.secretary_listener.stop_monitor",
                       return_value="stopped"):
                self.assertFalse(is_enabled())
                self.assertIn("Secretary mode ON", secretary({"action": "on"}))
                self.assertTrue(is_enabled())
                self.assertIn("ON", secretary({"action": "status"}))
                self.assertIn("OFF", secretary({"action": "off"}))
                self.assertFalse(is_enabled())

    def test_load_cfg_cached_and_mutation_safe(self):
        """_load_cfg caches on (mtime, size) so the hot triage/sweep paths
        don't re-read the file every call (YinYang), returns copies so
        caller mutation can't corrupt the cache, and a save invalidates."""
        from actions.secretary import _load_cfg, _save_cfg
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            cfg.write_text(json.dumps({"a": 1}), encoding="utf-8")
            reads = []
            orig_read = Path.read_text

            def counting_read(self, *a, **k):
                reads.append(self)
                return orig_read(self, *a, **k)

            with patch("actions.secretary._CFG_PATH", cfg), \
                 patch.object(Path, "read_text", counting_read):
                self.assertEqual(_load_cfg(), {"a": 1})
                self.assertEqual(_load_cfg(), {"a": 1})
                self.assertEqual(_load_cfg(), {"a": 1})
                self.assertEqual(len(reads), 1)   # file read exactly once
                # mutation of the returned dict must not poison the cache
                first = _load_cfg()
                first["a"] = 999
                self.assertEqual(_load_cfg()["a"], 1)
                # a write invalidates the cache
                _save_cfg({"a": 2})
                self.assertEqual(_load_cfg()["a"], 2)

    def test_off_keeps_dashboard_monitor_when_self_chat_configured(self):
        """secretary off must NOT stop the WhatsApp monitor when a self-chat
        dashboard is configured — the remote dashboard is always-on, it just
        stops triaging third parties."""
        from actions.secretary import is_enabled, secretary
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            cfg.write_text(json.dumps({"secretary_self_chat": "Omoke Jr"}),
                           encoding="utf-8")
            stopped = []
            with patch("actions.secretary._CFG_PATH", cfg), \
                 patch("actions.secretary._session_overview",
                       return_value="no active session"), \
                 patch("actions.secretary_listener.stop_monitor",
                       side_effect=lambda: stopped.append(1) or "stopped"):
                out = secretary({"action": "off"})
                self.assertFalse(is_enabled())   # mode flipped off inside the patch
            self.assertIn("OFF", out)
            self.assertIn("stays connected", out)
            self.assertEqual(stopped, [])   # monitor kept alive for the dashboard

    def test_off_stops_monitor_without_self_chat(self):
        from actions.secretary import secretary
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "api_keys.json"
            cfg.write_text("{}", encoding="utf-8")
            stopped = []
            with patch("actions.secretary._CFG_PATH", cfg), \
                 patch("actions.secretary._session_overview",
                       return_value="no active session"), \
                 patch("actions.secretary_listener.stop_monitor",
                       side_effect=lambda: stopped.append(1) or "stopped"):
                out = secretary({"action": "off"})
            self.assertIn("OFF", out)
            self.assertIn("stopped", out.lower())   # no dashboard → browser stops
            self.assertEqual(stopped, [1])

    def test_reply_sends_and_clears_inbox(self):
        from actions.secretary import secretary
        state = self._empty_state()
        state["inbox"] = [{"from": "Mom", "message": "x", "reasons": ["urgent"],
                           "draft": "d", "at": "2026-08-15T10:00:00"}]
        with contextlib.ExitStack() as stack:
            for p in self._patches(state):
                stack.enter_context(p)
            sm = stack.enter_context(
                patch("actions.send_message.send_message", return_value="sent"))
            out = secretary({"action": "reply", "sender": "Mom",
                             "text": "yes, sounds good"})
        self.assertIn("Sent to Mom", out)
        sm.assert_called_once()
        self.assertEqual(state["inbox"], [])

    def test_inbox_renders(self):
        from actions.secretary import secretary
        state = self._empty_state()
        state["inbox"] = [{"from": "Mom", "message": "URGENT call",
                           "reasons": ["urgency"], "draft": "d",
                           "at": "2026-08-15T10:00:00"}]
        with contextlib.ExitStack() as stack:
            for p in self._patches(state):
                stack.enter_context(p)
            out = secretary({"action": "inbox"})
        self.assertIn("Mom", out)
        self.assertIn("URGENT call", out)
        self.assertIn("urgency", out)


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
            # daemon boots the WhatsApp monitor when secretary_mode is on OR
            # a self-chat dashboard is configured (always-on remote dashboard);
            # keep it off in tests so no real WhatsApp is opened
            patch("actions.secretary.is_enabled", return_value=False),
            patch("actions.secretary_listener.start_monitor",
                   return_value="monitor skipped (test)"),
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

    def test_incoming_secretary_replies(self):
        with patch("actions.secretary.is_enabled", return_value=True), \
             patch("actions.secretary.handle_message",
                   return_value="Replied to Mom: hi there"):
            resp = cli._daemon_request(
                {"type": "incoming", "from": "Mom", "text": "hi"},
                self.port, timeout=10.0)
        self.assertTrue(resp.get("ok"))
        self.assertIn("Replied to Mom", resp["reply"])

    def test_incoming_secretary_off(self):
        with patch("actions.secretary.is_enabled", return_value=False):
            resp = cli._daemon_request(
                {"type": "incoming", "from": "Mom", "text": "hi"},
                self.port, timeout=10.0)
        self.assertFalse(resp.get("ok"))
        self.assertIn("OFF", resp.get("error", ""))

    def test_incoming_requires_sender_and_text(self):
        with patch("actions.secretary.is_enabled", return_value=True):
            resp = cli._daemon_request(
                {"type": "incoming", "from": "Mom"}, self.port, timeout=10.0)
        self.assertFalse(resp.get("ok"))
        self.assertIn("'from'", resp.get("error", ""))


class SelfChatFileTests(unittest.TestCase):
    """The self-chat file drop: a file sent to the boss's own chat is
    downloaded via the WhatsApp bridge (like /attach) and attached so the
    next command runs against it (file_processor)."""

    def _fake_bridge(self, info=None, error=None):
        class FakeBridge:
            def __init__(self):
                self.started = False
            def start(self):
                self.started = True
            def download_last_media(self, chat_title, save_dir, timeout=60):
                if error is not None:
                    raise error
                return info or {"path": "", "kind": "document", "name": "x.pdf"}
        return FakeBridge()

    def test_receive_file_downloads_and_attaches(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test")
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        info = {"path": path, "kind": "document", "name": "notes.pdf"}
        bridge = self._fake_bridge(info=info)
        holder = {"path": None}
        with patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   return_value=(bridge, True)):
            out = cli._receive_self_chat_file("Omoke Jr", "Document", holder)
        self.assertTrue(bridge.started)
        self.assertIn("Got your file", out)
        self.assertIn("notes.pdf", out)
        self.assertEqual(holder["path"], path)   # attached for the next command

    def test_receive_file_failure_replies_clearly(self):
        bridge = self._fake_bridge(
            error=RuntimeError("voice notes and stickers aren't supported"))
        holder = {"path": None}
        with patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   return_value=(bridge, True)):
            out = cli._receive_self_chat_file("Omoke Jr", "Sticker", holder)
        self.assertIn("Couldn't download", out)
        self.assertIsNone(holder["path"])

    def test_process_attached_file_runs_instruction(self):
        with patch("actions.file_processor.file_processor",
                   return_value="SUMMARY: the CV is 2 pages"):
            out = cli._process_attached_file("C:/tmp/notes.pdf",
                                             "summarize this")
        self.assertEqual(out, "SUMMARY: the CV is 2 pages")

    def test_process_attached_file_failure(self):
        with patch("actions.file_processor.file_processor",
                   side_effect=Exception("boom")):
            out = cli._process_attached_file("C:/tmp/notes.pdf", "summarize")
        self.assertIn("Could not process", out)

    def test_bridge_sanitize_filename(self):
        from actions.whatsapp_bridge import WhatsAppBridge
        self.assertEqual(WhatsAppBridge._sanitize_filename(
            'a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(WhatsAppBridge._sanitize_filename(""), "media")
        self.assertEqual(len(WhatsAppBridge._sanitize_filename("x" * 300)), 120)


if __name__ == "__main__":
    unittest.main()
