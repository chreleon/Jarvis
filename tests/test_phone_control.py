"""Phone control (ADB over Wi-Fi) — connection flow, dispatch, and the
safety blocklist. ADB calls are mocked; nothing here touches a real device."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import actions.phone_control as pc


class _FakeSock:
    """Stand-in for a connected socket (context manager) in subnet-scan
    fakes — create_connection returns a socket, not a bare value."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_proc(stdout="", stderr="", returncode=0, binary=False):
    """Mimic _run_adb: str stdout in text mode, bytes when binary."""
    if binary:
        return subprocess.CompletedProcess(
            args=[], returncode=returncode,
            stdout=(stdout if isinstance(stdout, bytes) else stdout.encode()),
            stderr=(stderr if isinstance(stderr, bytes) else stderr.encode()))
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=str(stdout), stderr=str(stderr))


class AdbDiscoveryTests(unittest.TestCase):

    def setUp(self):
        pc._adb_path = None

    def tearDown(self):
        pc._adb_path = None

    @patch("shutil.which", return_value="C:/tools/adb.exe")
    def test_finds_adb_on_path(self, _):
        self.assertEqual(pc._find_adb(), "C:/tools/adb.exe")

    @patch("shutil.which", return_value=None)
    @patch.object(Path, "is_dir", return_value=False)  # no WinGet packages
    def test_returns_none_when_missing(self, _, __):
        self.assertIsNone(pc._find_adb())

    def test_run_adb_raises_when_missing(self):
        with patch("actions.phone_control._find_adb", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "adb not found"):
                pc._run_adb(["devices"])


class UsbDeviceParsingTests(unittest.TestCase):

    def test_parses_authorized_device(self):
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc(
                              "List of devices attached\n"
                              "zhcmvkaitgheoby5\tdevice\n")):
            self.assertEqual(pc._usb_device(),
                             ("zhcmvkaitgheoby5", "device"))

    def test_parses_unauthorized_device(self):
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc(
                              "List of devices attached\n"
                              "zhcmvkaitgheoby5\tunauthorized\n")):
            self.assertEqual(pc._usb_device(),
                             ("zhcmvkaitgheoby5", "unauthorized"))

    def test_no_device(self):
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc("List of devices attached\n")):
            self.assertEqual(pc._usb_device(), (None, None))

    def test_phone_ip_parsed_from_wlan0(self):
        out = ("3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
               "    inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n")
        with patch.object(pc, "_run_adb", return_value=_fake_proc(out)):
            self.assertEqual(pc._phone_ip("serial"), "192.168.1.50")


class ConnectFlowTests(unittest.TestCase):

    def setUp(self):
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None

    def tearDown(self):
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None

    def _fake_adb(self, args, timeout=20, target=None, binary=False):
        cmd = " ".join(args)
        if cmd == "devices":
            return _fake_proc("List of devices attached\nserial1\tdevice\n")
        if cmd == "shell ip -f inet addr show wlan0":
            return _fake_proc("    inet 192.168.1.50/24 scope global wlan0\n")
        if cmd == "tcpip 5555":
            return _fake_proc("restarting in TCP mode port: 5555")
        if cmd == "connect 192.168.1.50:5555":
            return _fake_proc("connected to 192.168.1.50:5555")
        if cmd == "shell echo ok":
            return _fake_proc("ok")
        return _fake_proc("")

    def test_connect_full_flow(self):
        with patch.object(pc, "_run_adb", side_effect=self._fake_adb), \
             patch.object(pc, "time", **{"sleep.return_value": None}):
            out = pc._action_connect()
        self.assertIn("Wireless connected", out)
        self.assertIn("192.168.1.50:5555", out)
        self.assertEqual(pc._CONNECTED_ENDPOINT, "192.168.1.50:5555")

    def test_connect_requires_authorized_usb(self):
        def fake(args, timeout=20, target=None, binary=False):
            if " ".join(args) == "devices":
                return _fake_proc("List of devices attached\ns1\tunauthorized\n")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc._action_connect()
        self.assertIn("Allow", out)            # points at the phone prompt
        self.assertIsNone(pc._CONNECTED_ENDPOINT)

    def test_connect_no_device(self):
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc("List of devices attached\n")), \
             patch.object(pc, "_profile_load", return_value={}):
            out = pc._action_connect()
        self.assertIn("no saved phone profile", out)

    def test_target_rediscovers_live_endpoint(self):
        """A fresh process must rediscover the wireless link from the adb
        server (it persists across processes) — not demand 'phone connect'
        again."""
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc(
                              "List of devices attached\n"
                              "serial1\tdevice\n"
                              "192.168.1.50:5555\tdevice\n")):
            self.assertEqual(pc._target(), "192.168.1.50:5555")
        self.assertEqual(pc._CONNECTED_ENDPOINT, "192.168.1.50:5555")

    def test_target_ignores_offline_endpoint_falls_back_to_usb(self):
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = "serial1"
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc(
                              "List of devices attached\n"
                              "serial1\tdevice\n"
                              "192.168.1.50:5555\toffline\n")):
            self.assertEqual(pc._target(), "serial1")

    def test_profile_save_load_round_trip(self):
        with patch.object(pc, "_PROFILE_PATH",
                          Path(__file__).parent / "_profile_tmp.json"), \
             patch.object(pc, "_shell", return_value="Redmi"):
            pc._profile_save("serial1", "192.168.1.50:5555")
            p = pc._profile_load()
        self.assertEqual(p["serial"], "serial1")
        self.assertEqual(p["endpoint"], "192.168.1.50:5555")
        self.assertEqual(p["model"], "Redmi")
        Path(__file__).parent.joinpath("_profile_tmp.json").unlink(
            missing_ok=True)

    def test_connect_and_verify_matches_serial(self):
        def fake(args, timeout=20, target=None, binary=False):
            if " ".join(args) == "connect 192.168.1.50:5555":
                return _fake_proc("connected to 192.168.1.50:5555")
            if "ro.serialno" in " ".join(args):
                return _fake_proc("serial1")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake), \
             patch.object(pc, "time", **{"sleep.return_value": None}):
            self.assertTrue(pc._connect_and_verify("192.168.1.50:5555",
                                                   "serial1"))
            self.assertFalse(pc._connect_and_verify("192.168.1.50:5555",
                                                    "other_serial"))

    def test_scan_subnet_finds_phone_by_serial(self):
        def fake_connect(addr, timeout=0.4):
            ip = addr[0]
            # only .77 is open; a connected socket is a context manager
            return _FakeSock() if ip == "192.168.1.77" else None
        calls = []
        def fake_adb(args, timeout=20, target=None, binary=False):
            calls.append(" ".join(args))
            if "connect " in " ".join(args):
                return _fake_proc("connected")
            if "ro.serialno" in " ".join(args):
                return _fake_proc("serial1")
            return _fake_proc("")
        with patch.object(pc, "_pc_local_ip", return_value="192.168.1.50"), \
             patch("actions.phone_control.socket.create_connection",
                   side_effect=fake_connect), \
             patch.object(pc, "_run_adb", side_effect=fake_adb), \
             patch.object(pc, "time", **{"sleep.return_value": None}):
            ep = pc._scan_subnet_for_serial("serial1")
        self.assertEqual(ep, "192.168.1.77:5555")

    def test_scan_subnet_rejects_wrong_serial(self):
        def fake_connect(addr, timeout=0.4):
            return _FakeSock()                # every host "open"
        def fake_adb(args, timeout=20, target=None, binary=False):
            if "connect " in " ".join(args):
                return _fake_proc("connected")
            if "ro.serialno" in " ".join(args):
                return _fake_proc("someone_elses_phone")
            return _fake_proc("")
        with patch.object(pc, "_pc_local_ip", return_value="192.168.1.50"), \
             patch("actions.phone_control.socket.create_connection",
                   side_effect=fake_connect), \
             patch.object(pc, "_run_adb", side_effect=fake_adb), \
             patch.object(pc, "time", **{"sleep.return_value": None}):
            self.assertIsNone(pc._scan_subnet_for_serial("serial1"))

    def test_connect_reconnects_by_saved_profile(self):
        """No USB, profile present, saved endpoint still live → reconnect
        without a cable and without scanning."""
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None
        def fake(args, timeout=20, target=None, binary=False):
            if " ".join(args) == "devices":
                return _fake_proc("List of devices attached\n")
            if "connect 192.168.1.99:5555" in " ".join(args):
                return _fake_proc("connected")
            if "ro.serialno" in " ".join(args):
                return _fake_proc("serial1")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake), \
             patch.object(pc, "_profile_load",
                          return_value={"serial": "serial1",
                                        "endpoint": "192.168.1.99:5555"}), \
             patch.object(pc, "time", **{"sleep.return_value": None}):
            out = pc._action_connect()
        self.assertIn("Reconnected", out)
        self.assertIn("192.168.1.99:5555", out)
        self.assertEqual(pc._CONNECTED_ENDPOINT, "192.168.1.99:5555")

    def test_connect_scans_when_saved_endpoint_stale(self):
        """No USB, profile present, saved IP dead → subnet scan finds the
        phone at its NEW IP by serial."""
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None
        def fake_connect(addr, timeout=0.4):
            return _FakeSock() if addr[0] == "192.168.1.88" else None
        def fake(args, timeout=20, target=None, binary=False):
            cmd = " ".join(args)
            if cmd == "devices":
                return _fake_proc("List of devices attached\n")
            if "connect 192.168.1.99:5555" in cmd:
                return _fake_proc("failed to connect")
            if "connect " in cmd:
                return _fake_proc("connected")
            if "ro.serialno" in cmd:
                return _fake_proc("serial1")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake), \
             patch.object(pc, "_profile_load",
                          return_value={"serial": "serial1",
                                        "endpoint": "192.168.1.99:5555"}), \
             patch.object(pc, "_pc_local_ip", return_value="192.168.1.50"), \
             patch("actions.phone_control.socket.create_connection",
                   side_effect=fake_connect), \
             patch.object(pc, "time", **{"sleep.return_value": None}):
            out = pc._action_connect()
        self.assertIn("by its serial", out)
        self.assertIn("192.168.1.88:5555", out)
        self.assertEqual(pc._CONNECTED_ENDPOINT, "192.168.1.88:5555")

    def test_connect_no_profile_explains(self):
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc("List of devices attached\n")), \
             patch.object(pc, "_profile_load", return_value={}):
            out = pc._action_connect()
        self.assertIn("no saved phone profile", out)

    def test_status_shows_rediscovered_wireless(self):
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None
        calls = []
        def fake(args, timeout=20, target=None, binary=False):
            calls.append(" ".join(args))
            if " ".join(args) == "devices":
                return _fake_proc(
                    "List of devices attached\n"
                    "serial1\tdevice\n"
                    "192.168.1.50:5555\tdevice\n")
            if "echo ok" in " ".join(args):
                return _fake_proc("ok")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc._action_status()
        self.assertIn("192.168.1.50:5555", out)
        self.assertIn("no cable needed", out)


class ActionDispatchTests(unittest.TestCase):

    def setUp(self):
        pc._CONNECTED_ENDPOINT = "192.168.1.50:5555"

    def tearDown(self):
        pc._CONNECTED_ENDPOINT = None

    def test_unknown_action_help(self):
        out = pc.phone_control({"action": "warp"})
        self.assertIn("Unknown phone action", out)

    def test_tap_validates_coords(self):
        self.assertIn("integer", pc.phone_control({"action": "tap",
                                                   "x": "a", "y": 5}))
        with patch.object(pc, "_run_adb", return_value=_fake_proc("")):
            out = pc.phone_control({"action": "tap", "x": 100, "y": 200})
        self.assertIn("Tapped (100, 200)", out)

    def test_no_device_message(self):
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc("List of devices attached\n")):
            out = pc.phone_control({"action": "tap", "x": 1, "y": 2})
        self.assertIn("No phone connected", out)

    def test_text_escapes_spaces(self):
        captured = {}
        def fake(args, timeout=20, target=None, binary=False):
            captured["args"] = args
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc.phone_control({"action": "text",
                                    "text": "hello world"})
        self.assertIn("Typed: hello world", out)
        self.assertIn("%s", captured["args"][-1])   # space → %s

    def test_key_mapping(self):
        with patch.object(pc, "_run_adb", return_value=_fake_proc("")) as m:
            pc.phone_control({"action": "key", "key": "home"})
        self.assertEqual(m.call_args.args[0][-1], "3")
        out = pc.phone_control({"action": "key", "key": "bogus_key"})
        self.assertIn("Unknown key", out)

    def test_screenshot_saves_png(self):
        png = b"\x89PNG\r\n\x1a\n" + b"fakedata"
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc(png, binary=True)), \
             patch.object(pc, "time", **{"strftime.return_value": "20260816_120000"}):
            out = pc.phone_control({"action": "screenshot"})
        self.assertIn("Saved", out)
        p = Path("phone_shots/phone_20260816_120000.png")
        self.assertTrue(p.is_file())
        p.unlink(missing_ok=True)

    def test_screenshot_rejects_non_png(self):
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc(b"notpng", binary=True)):
            out = pc.phone_control({"action": "screenshot"})
        self.assertIn("unexpected format", out)

    def test_info_reads_device(self):
        """_action_info now batches its reads: 4 getprops in one adb call,
        then battery/wm/df in a second — the fake must answer each batch
        with marker-separated output."""
        def fake(args, timeout=20, target=None, binary=False):
            cmd = " ".join(args)
            if "ro.product.model" in cmd and "__JEEVES_" in cmd:
                return _fake_proc(
                    "__JEEVES_0__\nPixel 7\n"
                    "__JEEVES_1__\n14\n"
                    "__JEEVES_2__\nserial9\n"
                    "__JEEVES_3__\nTQ3A.230805\n")
            if "dumpsys battery" in cmd and "__JEEVES_" in cmd:
                return _fake_proc(
                    "__JEEVES_0__\n  level: 82\n"
                    "__JEEVES_1__\nPhysical size: 1080x2400\n"
                    "__JEEVES_2__\n/sdcard 12G 40G 52G 23% /sdcard\n")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc.phone_control({"action": "info"})
        self.assertIn("Pixel 7", out)
        self.assertIn("82%", out)
        self.assertIn("1080x2400", out)
        self.assertIn("serial9", out)
        self.assertIn("storage: 12G used of 52G", out)

    def test_ring_starts_and_stops(self):
        with patch.object(pc, "_ring_worker",
                          side_effect=lambda t, p, s: None), \
             patch.object(pc, "_resolve_sound_path",
                          return_value="/system/alarm.ogg"):
            out = pc.phone_control({"action": "ring"})
        self.assertIn("Ringing", out)
        self.assertIn("25s", out)
        # already ringing guard
        with patch.object(pc, "_ring_worker",
                          side_effect=lambda t, p, s: None), \
             patch.object(pc, "_resolve_sound_path",
                          return_value="/system/alarm.ogg"):
            out = pc.phone_control({"action": "ring"})
        self.assertIn("already ringing", out)
        # stop
        pc._ring_stop = False
        with patch.object(pc, "_run_adb", return_value=_fake_proc("")):
            out = pc.phone_control({"action": "ring", "stop": True})
        self.assertIn("stopped", out)

    def test_ring_needs_phone(self):
        pc._CONNECTED_ENDPOINT = None
        pc._USB_SERIAL = None
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc("List of devices attached\n")):
            out = pc.phone_control({"action": "ring"})
        self.assertIn("No phone connected", out)

    def test_ring_no_sound_found(self):
        with patch.object(pc, "_resolve_sound_path", return_value=None):
            out = pc.phone_control({"action": "ring"})
        self.assertIn("playable sound", out)

    def test_resolve_sound_path_parses_content_query(self):
        def fake(args, timeout=20, target=None, binary=False):
            if "settings get system alarm_alert" in " ".join(args):
                return _fake_proc(
                    "content://media/internal/audio/media/40?title=Carousel")
            if "content query" in " ".join(args):
                return _fake_proc(
                    "Row: 0 _data=/product/media/audio/ringtones/Carousel.ogg")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake):
            path = pc._resolve_sound_path("192.168.1.50:5555")
        self.assertEqual(path, "/product/media/audio/ringtones/Carousel.ogg")

    def test_ring_worker_raises_volume_plays_then_restores(self):
        """Full worker contract: wake + VIEW + max volume, bounded wait,
        then dispatch stop + volume restore + active flag cleared."""
        pc._ring_stop = False
        calls = []
        def fake(args, timeout=20, target=None, binary=False):
            calls.append((target, " ".join(args)))
            return _fake_proc("8")
        with patch.object(pc, "_run_adb", side_effect=fake), \
             patch.object(pc, "time", **{"sleep.return_value": None}):
            pc._ring_worker("192.168.1.50:5555", "/system/alarm.ogg", 5)
        self.assertFalse(pc._ring_active)
        cmds = [c[1] for c in calls]
        self.assertTrue(any("keyevent KEYCODE_WAKEUP" in c for c in cmds))
        self.assertTrue(any("android.intent.action.VIEW" in c
                            and "file:///system/alarm.ogg" in c for c in cmds))
        self.assertTrue(any("dispatch stop" in c for c in cmds))
        puts = [c for c in cmds if "settings put system volume_" in c]
        self.assertEqual(
            sum(1 for c in puts if "volume_alarm 15" in c), 1)
        self.assertTrue(any("volume_alarm 8" in c for c in puts))

    def test_macro_list_reports_setup(self):
        def fake(args, timeout=20, target=None, binary=False):
            cmd = " ".join(args)
            if "pm list packages" in cmd:
                return _fake_proc("package:com.arlosoft.macrodroid")
            if "dumpsys activity services" in cmd:
                return _fake_proc("ServiceRecord{... com.arlosoft.macrodroid/...}")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake), \
             patch.object(pc, "_cfg_extra",
                          return_value={"phone_macros": {"find": "com.x.F"}}):
            out = pc._action_macro(do_list=True)
        self.assertIn("installed and running", out)
        self.assertIn("com.x.F", out)

    def test_macro_fires_intent(self):
        captured = {}
        def fake(args, timeout=20, target=None, binary=False):
            captured["cmd"] = " ".join(args)
            if "pm list packages" in " ".join(args):
                return _fake_proc("package:com.arlosoft.macrodroid")
            if "dumpsys activity services" in " ".join(args):
                return _fake_proc("ServiceRecord{... macrodroid ...}")
            if "am broadcast" in " ".join(args):
                return _fake_proc("Broadcast completed: result=0")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake), \
             patch.object(pc, "_cfg_extra",
                          return_value={"phone_macros":
                                        {"find": "com.jeeves.macro.FIND"}}):
            out = pc._action_macro("find", "kitchen")
        self.assertIn("Fired macro", out)
        self.assertIn("-a com.jeeves.macro.FIND", captured["cmd"])
        self.assertIn("--es value kitchen", captured["cmd"])

    def test_macro_fires_http_path(self):
        captured = {}
        def fake(args, timeout=20, target=None, binary=False):
            captured["cmd"] = " ".join(args)
            if "pm list packages" in captured["cmd"]:
                return _fake_proc("package:com.arlosoft.macrodroid")
            if "dumpsys activity services" in captured["cmd"]:
                return _fake_proc("ServiceRecord{... macrodroid ...}")
            return _fake_proc("")
        class _Resp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self, n):
                return b"ok"
        with patch.object(pc, "_run_adb", side_effect=fake), \
             patch.object(pc, "_cfg_extra",
                          return_value={"phone_macros":
                                        {"flash": "/toggle_flashlight"},
                                        "phone_macrodroid_port": 8080}), \
             patch("urllib.request.urlopen", return_value=_Resp()):
            out = pc._action_macro("flash")
        self.assertIn("HTTP macro fired (200)", out)

    def test_macro_unknown_name(self):
        with patch.object(pc, "_cfg_extra", return_value={"phone_macros": {}}):
            out = pc._action_macro("bogus")
        self.assertIn("No macro named 'bogus'", out)

    def test_dev_status_no_phone(self):
        with patch.object(pc, "_target", return_value=None):
            out = pc._action_dev("status")
        self.assertIn("No phone connected", out)

    def test_dev_status_reads_settings(self):
        """_dev_settings batches all 7 settings reads into ONE adb call,
        so the fake must answer with marker-separated output."""
        keys = ("stay_on_while_plugged_in", "adb_wifi_timeout_ms",
                "adb_authorization_timeout", "development_settings_enabled",
                "window_animation_scale", "transition_animation_scale",
                "animator_duration_scale")
        vals = {"development_settings_enabled": "1",
                "stay_on_while_plugged_in": "15",
                "adb_wifi_timeout_ms": "0",
                "adb_authorization_timeout": "0",
                "window_animation_scale": "0.0",
                "transition_animation_scale": "0.0",
                "animator_duration_scale": "0.0"}
        def fake(args, timeout=20, target=None, binary=False):
            cmd = " ".join(args)
            if "settings get global" in cmd and "__JEEVES_" in cmd:
                parts = []
                for i, k in enumerate(keys):
                    parts.append(f"__JEEVES_{i}__")
                    parts.append(vals.get(k, "(default)"))
                return _fake_proc("\n".join(parts))
            return _fake_proc("(default)")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc._action_dev("status")
        self.assertIn("Developer Options", out)
        self.assertIn("never (0 ms)", out)      # wifi timeout line
        self.assertIn("ON (all power sources)", out)

    def test_dev_on_sets_keys(self):
        captured, vals = [], {}
        def fake(args, timeout=20, target=None, binary=False):
            cmd = " ".join(args)
            captured.append(cmd)
            if "put global" in cmd:
                parts = cmd.split()
                vals[parts[4]] = parts[5]
                return _fake_proc(parts[5])
            if "get global" in cmd:
                return _fake_proc(vals.get(cmd.split()[4], ""))
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc._action_dev("on")
        self.assertIn("optimised", out)
        joined = " ".join(captured)
        self.assertIn("put global adb_wifi_timeout_ms 0", joined)
        self.assertIn("put global adb_authorization_timeout 0", joined)
        self.assertIn("put global stay_on_while_plugged_in 15", joined)
        self.assertIn("put global window_animation_scale 0", joined)

    def test_dev_off_deletes_keys(self):
        captured = []
        def fake(args, timeout=20, target=None, binary=False):
            cmd = " ".join(args)
            captured.append(cmd)
            if "put global" in cmd:
                parts = cmd.split()
                return _fake_proc(parts[5])
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc._action_dev("off")
        self.assertIn("restored", out)
        joined = " ".join(captured)
        self.assertIn("delete global adb_wifi_timeout_ms", joined)
        self.assertIn("delete global adb_authorization_timeout", joined)

    def test_dev_unknown_mode(self):
        with patch.object(pc, "_target",
                          return_value="192.168.1.50:5555"):
            out = pc._action_dev("bogus")
        self.assertIn("Unknown 'phone dev' mode", out)

    def test_launch_uses_monkey(self):
        def fake(args, timeout=20, target=None, binary=False):
            if "monkey" in " ".join(args):
                return _fake_proc("Events injected: 1")
            return _fake_proc("")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc.phone_control({"action": "launch", "pkg": "com.whatsapp"})
        self.assertIn("Launched com.whatsapp", out)

    def test_termux_no_key(self):
        from pathlib import Path
        with patch.object(pc, "_TERMUX_KEY",
                          Path(pc.CONFIG_PATH).parent.parent
                          / "config" / "nope" / "missing_key"):
            out = pc._action_termux(cmd="uname -a")
        self.assertIn("phone termux setup", out)

    def test_termux_status_reachable(self):
        with patch.object(pc, "_target", return_value="192.168.1.50:5555"), \
             patch.object(pc, "_termux_reachable", return_value=True), \
             patch.object(pc, "_termux_ssh",
                          return_value="/data/data/com.termux/files/usr/bin/"
                          "termux-battery-status"):
            out = pc._action_termux("status")
        self.assertIn("SSH shell up", out)
        self.assertIn("Termux:API installed", out)

    def test_termux_runs_command(self):
        captured = {}
        def fake_ssh(cmd, timeout=30):
            captured["cmd"] = cmd
            return "hello"
        with patch.object(pc, "_termux_ssh", side_effect=fake_ssh):
            out = pc._action_termux(cmd="echo hi")
        self.assertEqual(out, "hello")
        self.assertIn("echo hi", captured["cmd"])

    def test_termux_refuses_dangerous(self):
        with patch.object(pc, "_termux_ssh", return_value=""):
            out = pc._action_termux(cmd="rm -rf /")
        self.assertIn("Refused", out)

    def test_termux_friendly_maps_to_api(self):
        captured = {}
        def fake_ssh(cmd, timeout=30):
            captured["cmd"] = cmd
            return "{\"battery\": 41}"
        with patch.object(pc, "_termux_ssh", side_effect=fake_ssh):
            out = pc._action_termux(cmd="battery")
        self.assertIn("termux-battery-status", captured["cmd"])
        self.assertIn("battery", out)

    def test_termux_clipboard_set_maps(self):
        captured = {}
        def fake_ssh(cmd, timeout=30):
            captured["cmd"] = cmd
            return ""
        with patch.object(pc, "_termux_ssh", side_effect=fake_ssh):
            pc._action_termux(cmd="clipboard set hi there")
        self.assertIn("termux-clipboard-set hi there", captured["cmd"])

    def test_termux_api_missing_hint(self):
        with patch.object(pc, "_termux_ssh",
                          return_value="termux-battery-status: "
                          "command not found"):
            out = pc._action_termux(cmd="battery")
        self.assertIn("Termux:API", out)

    def test_notify_pushes(self):
        def fake(args, timeout=20, target=None, binary=False):
            return _fake_proc("posting: Notification(...)")
        with patch.object(pc, "_run_adb", side_effect=fake):
            out = pc.phone_control({"action": "notify",
                                    "text": "hello", "title": "Jeeves"})
        self.assertIn("Notification pushed", out)

    def test_notify_no_text(self):
        with patch.object(pc, "_target", return_value=None):
            out = pc._action_notify("")
        self.assertIn("notify needs a message", out)

    def test_battery_parses_dumpsys(self):
        sample = ("Current Battery Service state:\n"
                  "  AC powered: false\n  USB powered: true\n"
                  "  status: 2\n  level: 57\n  temperature: 310\n"
                  "  technology: Li-poly\n")
        with patch.object(pc, "_run_adb", return_value=_fake_proc(sample)):
            out = pc._action_battery()
        self.assertIn("57%", out)
        self.assertIn("charging", out)
        self.assertIn("31.0°C", out)
        self.assertIn("USB", out)

    def test_battery_no_phone(self):
        with patch.object(pc, "_target", return_value=None):
            out = pc._action_battery()
        self.assertIn("No phone connected", out)

    def test_push_restricted_to_shared_storage(self):
        out = pc.phone_control({"action": "push",
                                "local": "C:/x.txt", "remote": "/system/x"})
        self.assertIn("shared storage", out)


class ShellSafetyTests(unittest.TestCase):

    def setUp(self):
        pc._CONNECTED_ENDPOINT = "192.168.1.50:5555"

    def tearDown(self):
        pc._CONNECTED_ENDPOINT = None

    def test_dangerous_commands_refused(self):
        for cmd in ("reboot", "wipe data", "rm -rf /sdcard/Important",
                    "pm uninstall com.whatsapp", "format", "su",
                    "settings put global airplane_mode_on 1",
                    "dd if=/dev/zero of=/dev/block/sda"):
            out = pc._action_shell(cmd)
            self.assertIn("Refused", out, cmd)

    def test_safe_commands_allowed(self):
        with patch.object(pc, "_run_adb",
                          return_value=_fake_proc("14")):
            out = pc._action_shell("getprop ro.build.version.release")
        self.assertEqual(out, "14")


class PhoneShortcutTests(unittest.TestCase):
    """'phone ...' routes to phone_control (before the PC-screen 'screenshot'
    shortcut can steal it)."""

    def test_phone_screenshot_shortcut(self):
        from cli import _try_shortcut
        calls = []
        with patch("cli._run_shortcut",
                   side_effect=lambda name, args, player:
                   calls.append((name, args)) or "ok"):
            _try_shortcut("phone screenshot", None)
        self.assertEqual(calls[0][0], "phone_control")
        self.assertEqual(calls[0][1]["action"], "screenshot")
        self.assertTrue(calls[0][1]["analyze"])

    def test_phone_status_shortcut(self):
        from cli import _try_shortcut
        calls = []
        with patch("cli._run_shortcut",
                   side_effect=lambda name, args, player:
                   calls.append((name, args)) or "ok"):
            _try_shortcut("phone status", None)
        self.assertEqual(calls[0][0], "phone_control")
        self.assertEqual(calls[0][1]["action"], "status")

    def test_phone_dev_shortcut(self):
        from cli import _try_shortcut
        calls = []
        with patch("cli._run_shortcut",
                   side_effect=lambda name, args, player:
                   calls.append((name, args)) or "ok"):
            _try_shortcut("phone dev on", None)
            self.assertEqual(calls[0][0], "phone_control")
            self.assertEqual(calls[0][1]["action"], "dev")
            self.assertEqual(calls[0][1]["mode"], "on")
            _try_shortcut("phone dev", None)
            self.assertEqual(calls[1][1]["mode"], "status")

    def test_phone_termux_shortcuts(self):
        from cli import _try_shortcut
        calls = []
        with patch("cli._run_shortcut",
                   side_effect=lambda name, args, player:
                   calls.append((name, args)) or "ok"):
            _try_shortcut("phone termux ls /sdcard", None)
            _try_shortcut("phone termux status", None)
            _try_shortcut("phone gps", None)
            _try_shortcut("phone clipboard set yo", None)
        self.assertEqual(calls[0][1], {"action": "termux",
                                       "cmd": "ls /sdcard"})
        self.assertEqual(calls[1][1], {"action": "termux",
                                       "mode": "status"})
        self.assertEqual(calls[2][1], {"action": "termux", "cmd": "gps"})
        self.assertEqual(calls[3][1], {"action": "termux",
                                       "cmd": "clipboard set yo"})

    def test_phone_notify_battery_shortcuts(self):
        from cli import _try_shortcut
        calls = []
        with patch("cli._run_shortcut",
                   side_effect=lambda name, args, player:
                   calls.append((name, args)) or "ok"):
            _try_shortcut("phone notify hello there", None)
            _try_shortcut("phone battery", None)
        self.assertEqual(calls[0][1], {"action": "notify",
                                       "text": "hello there"})
        self.assertEqual(calls[1][1], {"action": "battery"})

    def test_whats_on_my_phone_shortcut(self):
        from cli import _try_shortcut
        calls = []
        with patch("cli._run_shortcut",
                   side_effect=lambda name, args, player:
                   calls.append((name, args)) or "ok"):
            _try_shortcut("what's on my phone", None)
        self.assertEqual(calls[0][0], "phone_control")
        self.assertEqual(calls[0][1]["action"], "screenshot")

    def _run(self, text):
        """Run a 'phone ...' shortcut; returns the captured (name, args)."""
        from cli import _try_shortcut
        calls = []
        with patch("cli._run_shortcut",
                   side_effect=lambda name, args, player:
                   calls.append((name, args)) or "ok"):
            _try_shortcut(text, None)
        self.assertEqual(calls[0][0], "phone_control")
        return calls[0][1]

    def test_phone_tap_forwards_coordinates(self):
        self.assertEqual(self._run("phone tap 540 1200"),
                         {"action": "tap", "x": 540, "y": 1200})

    def test_phone_swipe_forwards_coordinates(self):
        self.assertEqual(self._run("phone swipe 200 800 200 200"),
                         {"action": "swipe", "x1": 200, "y1": 800,
                          "x2": 200, "y2": 200})
        self.assertEqual(
            self._run("phone swipe 1 2 3 4 500"),
            {"action": "swipe", "x1": 1, "y1": 2, "x2": 3, "y2": 4,
             "duration_ms": 500})

    def test_phone_text_keeps_whole_message(self):
        self.assertEqual(self._run("phone text hello world"),
                         {"action": "text", "text": "hello world"})

    def test_phone_key_launch_stop(self):
        self.assertEqual(self._run("phone key home"),
                         {"action": "key", "key": "home"})
        self.assertEqual(self._run("phone launch com.whatsapp"),
                         {"action": "launch", "pkg": "com.whatsapp"})
        self.assertEqual(self._run("phone stop com.whatsapp"),
                         {"action": "stop", "pkg": "com.whatsapp"})

    def test_phone_files_pull_push(self):
        self.assertEqual(self._run("phone files /sdcard/DCIM"),
                         {"action": "files", "path": "/sdcard/DCIM"})
        self.assertEqual(self._run("phone files"),
                         {"action": "files", "path": "/sdcard"})
        self.assertEqual(self._run("phone pull /sdcard/DCIM/x.jpg"),
                         {"action": "pull", "remote": "/sdcard/DCIM/x.jpg"})
        self.assertEqual(self._run("phone push C:/x.txt /sdcard/x.txt"),
                         {"action": "push", "local": "C:/x.txt",
                          "remote": "/sdcard/x.txt"})

    def test_phone_shell_keeps_whole_command(self):
        self.assertEqual(self._run("phone shell dumpsys battery"),
                         {"action": "shell", "cmd": "dumpsys battery"})

    def test_phone_malformed_numbers_left_to_tool(self):
        self.assertEqual(self._run("phone tap abc def"),
                         {"action": "tap"})

    def test_phone_ring_shortcut(self):
        self.assertEqual(self._run("phone ring"), {"action": "ring"})
        self.assertEqual(self._run("phone ring 30"),
                         {"action": "ring", "seconds": 30})
        self.assertEqual(self._run("phone ring stop"),
                         {"action": "ring", "stop": True})
        self.assertEqual(self._run("phone locate"), {"action": "ring"})

    def test_find_my_phone_phrases_ring(self):
        for phrase in ("find my phone", "ring my phone",
                       "make my phone ring", "locate my phone",
                       "where is my phone"):
            self.assertEqual(self._run(phrase), {"action": "ring"}, phrase)


class PhoneRegistrationTests(unittest.TestCase):
    """phone_control must be registered for both the LLM and the CLI."""

    def test_registered_in_tool_definitions(self):
        from config.tool_definitions import TOOL_DECLARATIONS, TOOL_REGISTRY
        self.assertIn("phone_control",
                      [t["name"] for t in TOOL_DECLARATIONS])
        self.assertIn("phone_control",
                      [t["name"] for t in TOOL_REGISTRY])

    def test_dispatchable_in_cli(self):
        from cli import _call_tool
        with patch("cli._load_runtime_imports",
                   return_value={"phone_control": pc.phone_control}), \
             patch("cli._maybe_show_tool_tip", lambda name: None):
            out = _call_tool("phone_control",
                             {"action": "status", "x": 1, "y": 2}, None)
        self.assertIn("Phone status", out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    unittest.main()
