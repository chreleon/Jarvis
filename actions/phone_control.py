# actions/phone_control.py
# Phone control — a wireless bridge to your Android phone through Jeeves.
#
# Built on ADB (Android Debug Bridge), which is already on this machine
# (scrcpy's bundled adb v37). The flow (all verified against current ADB
# practice, Aug 2026):
#   1. Phone plugged in via USB with USB debugging enabled.
#   2. `adb tcpip 5555`   — tell the phone's adbd to also listen on TCP 5555
#      (one-time per boot; REQUIRES the authorized USB connection).
#   3. `adb connect <ip>:5555` — connect over Wi-Fi; the session persists
#      until the phone reboots or the Wi-Fi changes, so `phone connect` is
#      idempotent (safe to re-run anytime).
#   4. Every action then runs over Wi-Fi — no cable, no screen needed on the
#      PC side. If the wireless link drops, commands fall back to USB.
#
# SAFETY (daktari): the user asked for full control WITHOUT risking the
# phone. So: every ADB call is bounded (never hangs), destructive actions
# (uninstall, reboot, wipe, rm, ...) are NOT exposed as actions at all, and
# the arbitrary `shell` action rejects a blocklist of destructive commands.
# Everything else — see the screen, tap/swipe/type, launch/stop apps, move
# files, read info — is safe and reversible.

import json
import os
import re
import select
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.utils import CONFIG_PATH, subprocess_no_window_kwargs

# ── ADB binary discovery (cached) ────────────────────────────────────────────
# adb ships with Android platform-tools OR is bundled with scrcpy (WinGet
# installs it under LocalAppData). `shutil.which` covers PATH; the explicit
# candidates cover the common non-PATH installs on Windows.
_ADB_CANDIDATES = [
    "adb",
    str(Path.home() / "AppData" / "Local" / "Android" / "Sdk"
        / "platform-tools" / "adb.exe"),
]
_adb_path: str | None | bool = None   # None = not probed yet


def _find_adb():
    """Locate the adb binary (cached). Returns the path string or None."""
    global _adb_path
    if _adb_path is not None:
        return _adb_path or None
    found = shutil.which("adb") or shutil.which("adb.exe")
    if not found:
        # scrcpy WinGet bundle — search the packages dir for adb.exe
        try:
            wg = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" \
                / "Packages"
            hits = sorted(wg.glob("Genymobile.scrcpy*/**/adb.exe")) if wg.is_dir() else []
            if hits:
                found = str(hits[0])
        except Exception:
            pass
    if not found:
        for cand in _ADB_CANDIDATES[1:]:
            if Path(cand).is_file():
                found = cand
                break
    _adb_path = found or ""
    return found or None


# ── Safe command runner ──────────────────────────────────────────────────────
_DEFAULT_TIMEOUT = 20

_CONNECTED_ENDPOINT: str | None = None      # "ip:port" when wireless is up
_USB_SERIAL: str | None = None              # serial of the authorized USB device


def _run_adb(args, timeout: float = _DEFAULT_TIMEOUT, target=None,
             binary: bool = False) -> subprocess.CompletedProcess:
    """Run one adb command. `target` is the -s serial/endpoint when a
    specific device is meant; server-level commands (devices, tcpip,
    connect) pass target=None. Bounded timeout + no console window, so a
    wedged adb can never hang or flash a window over the user's work."""
    adb = _find_adb()
    if not adb:
        raise RuntimeError(
            "adb not found — install Android platform-tools or scrcpy "
            "(it bundles adb) and make sure 'adb' is on your PATH")
    cmd = [adb]
    if target:
        cmd += ["-s", target]
    cmd += list(args)
    kwargs = dict(capture_output=True, timeout=timeout)
    if not binary:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    kwargs.update(subprocess_no_window_kwargs())
    return subprocess.run(cmd, **kwargs)


def _live_endpoint() -> str | None:
    """A wireless endpoint (ip:port) already connected in the adb server.
    The wireless link lives in the adb server — it survives across Jeeves
    processes — so a fresh process rediscovers it instead of forgetting it
    ('phone connect' is only needed once per phone boot).
    Only state=='device' counts; adb marks dropped links 'offline'."""
    _, wireless = _devices_table()
    return wireless


def _target() -> str | None:
    """The device to run commands against: the wireless endpoint when it's
    connected, else the authorized USB serial (so everything also works on
    the cable until the first `phone connect`)."""
    global _CONNECTED_ENDPOINT
    if _CONNECTED_ENDPOINT:
        return _CONNECTED_ENDPOINT
    ep = _live_endpoint()          # rediscover across processes
    if ep:
        _CONNECTED_ENDPOINT = ep
        return ep
    return _USB_SERIAL


def _devices_table() -> tuple:
    """(usb, wireless) from ONE `adb devices` call — the same output the
    old code read twice (once in _usb_device, once in _live_endpoint), each
    spawning a fresh adb process. usb = (serial, state) of the physical
    phone; wireless = 'ip:port' endpoint when a link is live."""
    try:
        out = _run_adb(["devices"], timeout=10).stdout or ""
    except Exception:
        return ((None, None), None)
    usb = (None, None)
    wireless = None
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List"):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] == "List" or parts[0] == "of":
            continue
        serial, state = parts[0], parts[1]
        # A wireless endpoint ("192.168.1.103:5555") is NOT the USB device —
        # tcpip/authorization must target the physical phone.
        if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", serial):
            if state == "device":
                wireless = serial
            continue
        if usb[0] is None and state in ("device", "unauthorized", "offline"):
            usb = (serial, state)
    return (usb, wireless)


def _usb_device() -> tuple:
    """(serial, state) of the USB device, from `adb devices`. state is
    'device' (authorized + ready), 'unauthorized', or None (not present)."""
    usb, _ = _devices_table()
    return usb


def _phone_ip(serial: str) -> str | None:
    """The phone's Wi-Fi IP address, read from its own network stack."""
    for cmd in (["shell", "ip", "-f", "inet", "addr", "show", "wlan0"],
                ["shell", "ip", "route"]):
        try:
            out = (_run_adb(cmd, timeout=10, target=serial).stdout or "")
        except Exception:
            continue
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
        m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    return None


def _shell(serial, *cmd, timeout=_DEFAULT_TIMEOUT) -> str:
    """Run a shell command on the device and return trimmed stdout."""
    r = _run_adb(["shell"] + list(cmd), timeout=timeout, target=serial)
    return (r.stdout or "").strip()


_BATCH_RE = re.compile(r"__JEEVES_\d+__")


def _shell_batch(target, cmds, timeout=_DEFAULT_TIMEOUT) -> list:
    """Run several shell commands in ONE adb call, returning their outputs.

    Every `_shell()` spawns a fresh adb.exe subprocess (~100-200 ms on
    Windows), so sequential reads (getprops, settings) used to pay that
    cost N times. This bakes the commands into a single `adb shell` call
    separated by echo markers and splits the output back out — N spawns
    become 1. Each command is wrapped in `( ) || true` so one failure
    yields an empty string for that slot instead of aborting the batch
    (same tolerance the old per-call try/except had).
    """
    parts = []
    for i, c in enumerate(cmds):
        parts.append(f"echo __JEEVES_{i}__")
        parts.append(f"({c}) || true")
    try:
        out = _run_adb(["shell", "; ".join(parts)], timeout=timeout,
                       target=target).stdout or ""
    except Exception:
        return [""] * len(cmds)
    segs = _BATCH_RE.split(out)
    # segs[0] is anything before the first marker (normally empty)
    results = []
    for i in range(len(cmds)):
        seg = segs[i + 1] if i + 1 < len(segs) else ""
        results.append((seg or "").replace("\r", "").strip())
    return results


def _connected(serial) -> bool:
    """True when this serial answers a trivial shell echo."""
    try:
        return _shell(serial, "echo", "ok", timeout=10) == "ok"
    except Exception:
        return False


# ── Stable phone identity (serial) + subnet re-find ──────────────────────────
# The phone's IP changes (DHCP), but its SERIAL never does — that's the
# static identifier. We persist a small local profile keyed by serial and,
# when the saved IP goes stale, re-find the phone on the local subnet by
# scanning adb's own port (5555) and verifying ro.serialno. Bounded and
# safe: it only ever connects to hosts that answer the adb port.
_PROFILE_PATH = CONFIG_PATH.parent / "phone_profile.json"


def _profile_load() -> dict:
    try:
        return json.loads(_PROFILE_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _profile_save(serial: str, endpoint: str) -> None:
    """Persist the phone's stable identity so a later 'phone connect' can
    re-find it by serial even after its IP changes."""
    try:
        model = _shell(serial, "getprop", "ro.product.model", timeout=10)
        build = _shell(serial, "getprop", "ro.build.id", timeout=10)
    except Exception:
        model = build = ""
    try:
        _PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROFILE_PATH.write_text(json.dumps({
            "serial": str(serial or "").strip(),
            "model": str(model or "").strip(),
            "build": str(build or "").strip(),
            "endpoint": str(endpoint or "").strip(),
            "saved_at": time.strftime("%Y-%m-%d %H:%M"),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _serial_of(endpoint: str) -> str:
    try:
        return _shell(endpoint, "getprop", "ro.serialno", timeout=10).strip()
    except Exception:
        return ""


def _connect_and_verify(endpoint: str, serial: str) -> bool:
    """adb-connect to an endpoint and confirm it is really OUR phone (its
    serial matches the profile) — never blind-trusts a port 5555 hit."""
    try:
        out = (_run_adb(["connect", endpoint], timeout=15).stdout or "")
        if "connected" not in out.lower():
            return False
        time.sleep(0.5)
        return bool(serial) and _serial_of(endpoint) == serial
    except Exception:
        return False


def _pc_local_ip() -> str | None:
    """The PC's own LAN IP (for subnet scanning)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))     # no packets sent — just routing
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def _scan_subnet_for_serial(serial: str, port: int = 5555,
                            timeout: float = 0.4,
                            workers: int = 32) -> str | None:
    """Find the phone on the local subnet by its STABLE serial when its IP
    changed: TCP-scan the /24 on adb's port, adb-connect each hit, verify
    ro.serialno. Bounded (seconds when nothing is open; first match wins).
    Returns the 'ip:port' endpoint, or None."""
    base = _pc_local_ip()
    if not base:
        return None
    prefix = ".".join((base or "").split(".")[:3])

    def _probe(i: int) -> str | None:
        ip = f"{prefix}.{i}"
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return ip
        except Exception:
            return None

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            hits = [r for r in pool.map(_probe, range(1, 255)) if r]
    except Exception:
        return None
    for ip in hits:                     # try in scan order
        ep = f"{ip}:{port}"
        if _connect_and_verify(ep, serial):
            return ep
    return None


# ── MacroDroid integration (phone-side automation) ───────────────────────────
# MacroDroid (com.arlosoft.macrodroid) adds triggers/actions adb can't do
# (sensors, toggles, on-phone events) and gives Jeeves two clean fire paths:
#   • Intent Received trigger — `am broadcast -a <action>` from adb fires the
#     macro (extras pass data). Requires MacroDroid's service to be running.
#   • HTTP Server Request trigger — GET/POST http://<phone-ip>:<port>/<path>
#     fires the macro (works even without an adb link). Default port 8080.
# Config (config/api_keys.json):
#   "phone_macros": {"find": "com.jeeves.macro.FIND",
#                     "flash": "/toggle_flashlight"}   # "/path" = HTTP
#   "phone_macrodroid_port": 8080
_MACRODROID_PKG = "com.arlosoft.macrodroid"
_MACRODROID_PORT_DEFAULT = 8080


def _macrodroid_running() -> bool:
    """Is MacroDroid's automation service up (its receivers are registered)?"""
    try:
        t = _target()
        if not t:
            return False
        # Filter the dump to the package: the unfiltered form dumps EVERY
        # service (big payload over Wi-Fi) and runs inside a 10-iteration
        # poll loop in _macrodroid_start.
        out = _shell(t, "dumpsys", "activity", "services",
                     _MACRODROID_PKG, timeout=15)
        return "macrodroid" in (out or "").lower()
    except Exception:
        return False


def _macrodroid_start() -> str:
    """Launch MacroDroid so its service starts registering its receivers."""
    t = _target()
    if not t:
        return "No phone connected — run 'phone connect' first."
    if _macrodroid_running():
        return f"✅ MacroDroid is already running."
    try:
        out = _shell(t, "monkey", "-p", _MACRODROID_PKG, "1", timeout=25)
        if "No activities found" in (out or ""):
            return ("MacroDroid is installed but can't be launched — open it "
                    "once on the phone (grant it the accessibility/"
                    "overlay permissions it asks for).")
        for _ in range(10):
            if _macrodroid_running():
                return "✅ MacroDroid started — macros are now reachable."
            time.sleep(1)
        return ("Launched MacroDroid, but its service isn't showing yet — "
                "open the app once on the phone so it can start.")
    except Exception as e:
        return f"Could not start MacroDroid: {e}"


def _macro_fire_intent(action: str, value: str = "") -> str:
    """Fire a macro via the Intent Received trigger (am broadcast)."""
    t = _target()
    cmd = ["shell", "am", "broadcast", "-a", action]
    if value:
        cmd += ["--es", "value", value]
    try:
        out = _shell(t, *cmd[1:], timeout=15)
    except Exception as e:
        return f"Broadcast failed: {e}"
    if "Broadcast completed" in (out or "") or "result=" in (out or ""):
        return (f"⚡ Fired macro '{action}'"
                + (f" (data: {value})" if value else "") + ".")
    return f"Broadcast sent: {(out or 'no reply').strip()[:120]}"


def _macro_fire_http(path: str, value: str = "") -> str:
    """Fire a macro via the HTTP Server Request trigger (GET to the phone)."""
    from urllib.request import urlopen
    from urllib.error import URLError
    t = _target()
    host = ""
    if t and ":" in t and "." in t.split(":")[0]:
        host = t.split(":")[0]          # wireless endpoint ip:port
    else:
        prof = _profile_load()
        ep = str(prof.get("endpoint") or "")
        if ep and ":" in ep:
            host = ep.split(":")[0]
    if not host:
        return ("Can't reach the phone over the network — connect it "
                "wirelessly first ('phone connect').")
    cfg = _cfg_extra()
    port = int(cfg.get("phone_macrodroid_port") or _MACRODROID_PORT_DEFAULT)
    url = f"http://{host}:{port}{path}"
    if value:
        url += f"?value={value}" if "?" not in path else f"&value={value}"
    try:
        with urlopen(url, timeout=5) as resp:
            status = getattr(resp, "status", 200)
            body = (resp.read(200) or b"").decode("utf-8", "replace")[:120]
        return (f"⚡ HTTP macro fired ({status}) at {path}"
                + (f" (data: {value})" if value else "") + ".")
    except URLError as e:
        return (f"HTTP macro failed — MacroDroid's server isn't answering at "
                f"{host}:{port} ({e.reason}). Is MacroDroid running with an "
                f"HTTP Server Request macro on port {port}?")
    except Exception as e:
        return f"HTTP macro failed: {e}"


# mtime-keyed config cache: _cfg_extra() used to re-read + re-parse
# config/api_keys.json on EVERY macro call (disk I/O per request). Same
# pattern as or_client._load_config_cached: edits bump mtime → picked up
# immediately; steady-state macro fires do zero disk I/O.
_cfg_cache: dict | None = None
_cfg_mtime_ns: int = -1
_cfg_size: int = -1


def _cfg_extra() -> dict:
    """The phone-related config extras (macros map, HTTP port) — cached."""
    global _cfg_cache, _cfg_mtime_ns, _cfg_size
    try:
        st = CONFIG_PATH.stat()
        if (_cfg_cache is not None
                and st.st_mtime_ns == _cfg_mtime_ns
                and st.st_size == _cfg_size):
            return _cfg_cache
    except OSError:
        pass
    try:
        from core.utils import get_api_config
        cfg = dict(get_api_config())
    except Exception:
        cfg = {}
    try:
        st = CONFIG_PATH.stat()
        _cfg_mtime_ns, _cfg_size = st.st_mtime_ns, st.st_size
    except OSError:
        pass
    _cfg_cache = cfg
    return cfg


def _action_macro(name: str = "", value: str = "", start: bool = False,
                  do_list: bool = False) -> str:
    """Drive MacroDroid macros on the phone. name is a friendly key from
    config 'phone_macros'; the value is either an intent action
    ('com.jeeves.macro.X') or an HTTP path ('/flash')."""
    t = _target()
    if not t:
        return "No phone connected — run 'phone connect' first."
    if start:
        return _macrodroid_start()
    installed = _shell(t, "pm", "list", "packages", _MACRODROID_PKG,
                       timeout=15).strip()
    has_md = _MACRODROID_PKG in (installed or "")
    running = _macrodroid_running()
    cfg = _cfg_extra()
    macros = dict(cfg.get("phone_macros", {}) or {})
    if do_list or not name:
        lines = [f"🤖 MacroDroid: {'installed' if has_md else 'NOT installed'}"
                 + (" and running" if running else "") + "."]
        lines.append("  Two ways to fire macros:")
        lines.append("    • Intent:  in MacroDroid add a macro with the "
                     "'Intent Received' trigger on an action like "
                     "'com.jeeves.macro.X', then map it here:")
        lines.append("      phone_macros: {\"name\": \"com.jeeves.macro.X\"}")
        lines.append("    • HTTP:    add a macro with the 'HTTP Server "
                     "Request' trigger on a path like '/flash', then map "
                     "it here:")
        port = int(cfg.get("phone_macrodroid_port") or _MACRODROID_PORT_DEFAULT)
        lines.append("      phone_macros: {\"name\": \"/flash\"}  "
                     f"(server port {port})")
        if macros:
            lines.append(f"  Configured ({len(macros)}): "
                         + ", ".join(f"{k} → {v}" for k, v in macros.items()))
        if not running:
            lines.append("  ⚠ MacroDroid is NOT running — macros won't fire "
                         "until 'phone macro start' (or you open the app).")
        return "\n".join(lines)
    target = macros.get(name)
    if not target:
        known = ", ".join(sorted(macros)) or "(none configured)"
        return (f"No macro named '{name}' — configured macros: {known}. "
                f"Add it to 'phone_macros' in config/api_keys.json.")
    if not running:
        msg = _macrodroid_start()
        if "running" not in msg and "started" not in msg.lower():
            return msg + "\n(create the macro in MacroDroid first: trigger " \
                   "'Intent Received' on the action, or 'HTTP Server " \
                   "Request' on the path.)"
    if str(target).strip().startswith("/"):
        return _macro_fire_http(str(target).strip(), value)
    return _macro_fire_intent(str(target).strip(), value)


# ── Developer Options tuning (safe, reversible) ──────────────────────────────
# These use the phone's own Developer Options settings to make the phone tools
# reliable and keep the wireless adb link alive. Everything here is a
# `settings put/delete` — no root, no data risk, all reversible with
# 'phone dev off'. Deliberately NOT touched: OEM unlock / bootloader unlock
# (wipes the phone) and any 'adb root' escalation.
_DEV_ANIMATION_KEYS = ("window_animation_scale",
                        "transition_animation_scale",
                        "animator_duration_scale")


def _dev_settings(t: str) -> dict:
    """Read the developer-option settings we manage, as {key: value}.

    All 7 keys are read in ONE adb call (was 7 sequential spawns).
    """
    keys = ("stay_on_while_plugged_in", "adb_wifi_timeout_ms",
            "adb_authorization_timeout", "development_settings_enabled",
            "window_animation_scale", "transition_animation_scale",
            "animator_duration_scale")
    vals = _shell_batch(t, [f"settings get global {k}" for k in keys],
                        timeout=10)
    return {k: (v if v and v.lower() not in ("null", "") else "(default)")
            for k, v in zip(keys, vals)}


def _dev_put(t: str, key: str, value: str) -> str:
    _shell(t, "settings", "put", "global", key, value, timeout=10)
    got = _shell(t, "settings", "get", "global", key, timeout=10).strip()
    return got


def _action_dev(mode: str = "") -> str:
    """Developer Options tuning for reliable phone control.

    'phone dev' / 'phone dev status' — report the current values.
    'phone dev on'  — keep the screen on while charging, kill UI animation
                      (snappier taps/screenshots), and most importantly set
                      adb_wifi_timeout_ms=0 so the wireless adb link never
                      expires (default is 10 minutes — that's why the phone
                      kept dropping off).
    'phone dev off' — restore the system defaults for the keys we changed.
    """
    t = _target()
    if not t:
        return "No phone connected — run 'phone connect' first."
    mode = (mode or "status").strip().lower()

    if mode in ("status", ""):
        s = _dev_settings(t)
        dev_on = s.get("development_settings_enabled") == "1"
        lines = ["🔧 Developer Options (phone)",
                 f"  Developer options: {'ON' if dev_on else 'OFF'}"]
        if not dev_on:
            lines.append("  → enable them once on the phone: Settings → "
                         "About phone → tap 'OS version' 7× (this also "
                         "turns on USB debugging for 'phone connect')")
        stay = s.get("stay_on_while_plugged_in")
        lines.append("  Stay awake while charging: "
                     + ("ON (all power sources)" if stay == "15" else stay))
        anims = [s.get(k) for k in _DEV_ANIMATION_KEYS]
        off = all(v == "0.0" for v in anims)
        lines.append("  UI animations: "
                     + ("OFF (fast automation)" if off
                        else ", ".join(f"{k.split('_')[0]}="
                                       f"{s.get(k)}" for k in _DEV_ANIMATION_KEYS)))
        wifi = s.get("adb_wifi_timeout_ms")
        lines.append("  Wireless-adb session timeout: "
                     + ("never (0 ms)" if wifi == "0" else
                        f"{wifi} ms — default 600000 (10 min) means the "
                        f"wireless link silently expires when idle; "
                        f"'phone dev on' sets it to 0"))
        auth = s.get("adb_authorization_timeout")
        lines.append("  USB-debug authorization timeout: "
                     + ("never (0 ms)" if auth == "0" else
                        f"{auth} ms — default 604800000 (7 days); "
                        f"'phone dev on' sets it to 0"))
        lines.append("  → 'phone dev on' applies the safe optimisations; "
                     "'phone dev off' restores defaults.")
        return "\n".join(lines)

    if mode in ("on", "optimize", "enable"):
        applied = []
        got = _dev_put(t, "stay_on_while_plugged_in", "15")
        applied.append(f"stay awake on any power source (now {got})")
        for k in _DEV_ANIMATION_KEYS:
            got = _dev_put(t, k, "0")
        applied.append("UI animations off (faster taps/screenshots)")
        got = _dev_put(t, "adb_wifi_timeout_ms", "0")
        applied.append(f"wireless adb never expires (adb_wifi_timeout_ms="
                       f"{got}) — no more silent drops")
        got = _dev_put(t, "adb_authorization_timeout", "0")
        applied.append(f"debug authorization never expires ("
                       f"adb_authorization_timeout={got})")
        return ("✅ Developer Options optimised —\n  • "
                + "\n  • ".join(applied)
                + "\n(Safe: no root, no data risk. 'phone dev off' restores "
                  "defaults.)")

    if mode in ("off", "restore", "reset"):
        restored = []
        got = _dev_put(t, "stay_on_while_plugged_in", "0")
        restored.append(f"stay-awake restored (now {got})")
        for k in _DEV_ANIMATION_KEYS:
            got = _dev_put(t, k, "1")
        restored.append("UI animations restored (1.0)")
        for k in ("adb_wifi_timeout_ms", "adb_authorization_timeout"):
            _shell(t, "settings", "delete", "global", k, timeout=10)
            restored.append(f"{k} back to system default")
        return ("♻️ Developer Options restored to defaults —\n  • "
                + "\n  • ".join(restored))

    return ("Unknown 'phone dev' mode — try 'phone dev', "
            "'phone dev on', 'phone dev off'.")


# ── Termux SSH bridge (a real shell into the phone) ──────────────────────────
# The Google Play build of Termux ships no RUN_COMMAND service (Play Store
# policy), so the reliable path is the classic one: openssh inside Termux,
# bootstrapped once by driving the Termux terminal via adb input (proven on
# this device), then Jeeves talks to the phone over SSH with a key pair kept
# only on the PC (config/termux_keys/, gitignored). From that shell, the
# Termux:API commands (battery/location/clipboard/sensors/SMS/camera/volume)
# become reachable — things adb alone can't do. Key-only auth, non-default
# port, no root — nothing is opened beyond a local SSH listener.
_TERMUX_SSH_PORT = 8022
_TERMUX_SSH_USER = "u0_a353"          # auto-detected during setup on other phones
_TERMUX_KEY_DIR = Path(CONFIG_PATH).parent.parent / "config" / "termux_keys"
_TERMUX_KEY = _TERMUX_KEY_DIR / "jeeves_ed25519"
_TERMUX_KEY_PUB = _TERMUX_KEY_DIR / "jeeves_ed25519.pub"
_TERMUX_APP_DATA = "/sdcard/Android/data/com.termux/files"  # shared bridge dir


def _termux_host() -> str:
    """The phone's LAN IP (from the wireless endpoint or saved profile)."""
    t = _target()
    if t and ":" in t and "." in t.split(":")[0]:
        return t.split(":")[0]
    prof = _profile_load()
    ep = str(prof.get("endpoint") or "")
    if ep and ":" in ep:
        return ep.split(":")[0]
    return ""


def _termux_ssh(cmd: str, timeout: int = 30) -> str:
    """Run a command inside Termux over SSH; returns stdout (or a helpful
    error when the bridge isn't set up / reachable)."""
    key = str(_TERMUX_KEY)
    if not Path(key).exists():
        return ("Termux bridge key missing — run 'phone termux setup' to "
                "generate it and install openssh on the phone (one-time).")
    host = _termux_host()
    if not host:
        return ("Can't reach the phone's IP — connect it wirelessly "
                "('phone connect') first.")
    user = _TERMUX_SSH_USER
    cmdline = ["ssh", "-i", key, "-p", str(_TERMUX_SSH_PORT),
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=8",
               f"{user}@{host}", cmd]
    try:
        r = subprocess.run(cmdline, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace",
                           **subprocess_no_window_kwargs())
    except Exception as e:
        return f"Termux bridge failed: {e}"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode == 0:
        return out or "(no output)"
    if "Permission denied" in err or "publickey" in err:
        return ("Termux bridge auth failed — run 'phone termux setup' to "
                "refresh the SSH key on the phone.")
    if ("Connection refused" in err or "timed out" in err.lower()
            or "No route" in err):
        return ("Termux's sshd isn't reachable — run 'phone termux start' "
                "(or 'phone termux setup') to launch it.")
    return (out + ("\n" + err if err else "")).strip() or "(no output)"


def _termux_reachable() -> bool:
    """Quick probe: does the Termux sshd answer on the LAN?"""
    return _termux_ssh("echo ok", timeout=10).strip() == "ok"


def _termux_setup_script() -> str:
    """The bootstrap script pushed into Termux's app-data dir and run once
    inside the app (installs openssh + termux-services, drops the PC's public
    key in authorized_keys, starts sshd on 8022 with key-only auth)."""
    pub = ""
    try:
        pub = _TERMUX_KEY_PUB.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return (f"#!/data/data/com.termux/files/usr/bin/bash\n"
            f"export PATH=/data/data/com.termux/files/usr/bin:$PATH\n"
            f"log -t JEEVES_SETUP start\n"
            f"if ! command -v sshd >/dev/null 2>&1; then\n"
            f"  pkg update -y >/dev/null 2>&1 || true\n"
            f"  pkg install -y openssh >/dev/null 2>&1 || true\n"
            f"fi\n"
            f"if ! command -v sv >/dev/null 2>&1; then\n"
            f"  pkg install -y termux-services >/dev/null 2>&1 || true\n"
            f"fi\n"
            f"if ! command -v sshd >/dev/null 2>&1; then\n"
            f"  log -t JEEVES_SETUP NO_SSHD\n"
            f"  exit 1\n"
            f"fi\n"
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
            f"echo '{pub}' > ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\n"
            f"SSHD_CFG=\"$PREFIX/etc/ssh/sshd_config\"\n"
            f"grep -q '^Port 8022' \"$SSHD_CFG\" 2>/dev/null "
            f"|| echo 'Port 8022' >> \"$SSHD_CFG\"\n"
            f"grep -q '^PasswordAuthentication' \"$SSHD_CFG\" 2>/dev/null "
            f"|| echo 'PasswordAuthentication no' >> \"$SSHD_CFG\"\n"
            f"grep -q '^PubkeyAuthentication' \"$SSHD_CFG\" 2>/dev/null "
            f"|| echo 'PubkeyAuthentication yes' >> \"$SSHD_CFG\"\n"
            f"grep -q '^PermitRootLogin' \"$SSHD_CFG\" 2>/dev/null "
            f"|| echo 'PermitRootLogin no' >> \"$SSHD_CFG\"\n"
            f"pkill -f sshd 2>/dev/null || true\n"
            f"sleep 1\n"
            f"sshd\n"
            f"sleep 2\n"
            f"sv-enable sshd >/dev/null 2>&1 || true\n"
            f"log -t JEEVES_SETUP done\n")


def _termux_setup() -> str:
    """Bootstrap the Termux SSH bridge by driving the Termux terminal via
    adb (input text + logcat markers). Idempotent; safe to re-run."""
    if not _TERMUX_KEY.exists():
        _TERMUX_KEY_DIR.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["ssh-keygen", "-t", "ed25519",
                            "-f", str(_TERMUX_KEY), "-N", "",
                            "-C", "jeeves-phone", "-q"],
                           check=True, timeout=30,
                           **subprocess_no_window_kwargs())
        except Exception as e:
            return f"Could not generate the SSH key: {e}"
    t = _target()
    if not t:
        return "No phone connected — run 'phone connect' first."
    script = _termux_setup_script()
    local = _TERMUX_KEY_DIR / "jeeves_setup.sh"
    local.write_text(script, encoding="utf-8")
    try:
        r = _run_adb(["push", str(local),
                      f"{_TERMUX_APP_DATA}/jeeves_setup.sh"],
                     timeout=30, target=t)
        if r.returncode != 0:
            return ("Could not push the setup script into Termux — the "
                    "app-data bridge dir may be blocked on this device.")
        _run_adb(["shell", "am", "start",
                  "-n", "com.termux/.app.TermuxActivity"],
                 timeout=20, target=t)
        time.sleep(3)
        _run_adb(["shell", "input", "keyevent", "66"], timeout=15, target=t)
        time.sleep(1)
        _run_adb(["shell", "logcat", "-c"], timeout=15, target=t)
        typed = (f"input text 'bash {_TERMUX_APP_DATA}/jeeves_setup.sh'")
        _run_adb(["shell", "am", "start",
                  "-n", "com.termux/.app.TermuxActivity"],
                 timeout=20, target=t)
        time.sleep(2)
        _run_adb(["shell"] + typed.split(), timeout=15, target=t)
        _run_adb(["shell", "input", "keyevent", "66"], timeout=15, target=t)
    except Exception as e:
        return f"Could not drive the Termux setup: {e}"
    # poll logcat for the setup markers (pkg install can take a while)
    deadline = time.time() + 300
    seen = set()
    while time.time() < deadline:
        try:
            out = _run_adb(["shell", "logcat", "-d"], timeout=20,
                           target=t).stdout or ""
        except Exception:
            out = ""
        for line in out.splitlines():
            if "JEEVES_SETUP" in line:
                tag = line.split("JEEVES_SETUP", 1)[1].strip()
                if tag and tag not in seen:
                    seen.add(tag)
                    if "done" in tag:
                        return ("✅ Termux bridge ready — openssh installed, "
                                "sshd on port 8022 (key-only).")
                    if "NO_SSHD" in tag:
                        return ("Termux setup couldn't install openssh "
                                "(offline?). Open Termux and run "
                                "'pkg install openssh' once.")
        time.sleep(5)
    return ("Termux setup is still running (pkg install can take minutes) — "
            "wait for it, then 'phone termux status'.")


def _action_termux(mode: str = "", cmd: str = "") -> str:
    """Drive the phone through the Termux shell.

    'phone termux' / 'status'  — is the bridge up? Termux:API installed?
    'phone termux setup'       — one-time bootstrap (installs openssh, keys,
                                 starts sshd; idempotent).
    'phone termux start'       — start sshd if it died (e.g. after reboot).
    'phone termux stop'        — stop sshd (turn the bridge off).
    'phone termux <command>'   — run any safe command inside Termux
                                 (dangerous ones refused).
    """
    mode = (mode or "").strip().lower()
    cmd = (cmd or "").strip()
    if mode and mode not in ("status", "check", "setup", "start", "stop"):
        cmd = f"{mode} {cmd}".strip()     # CLI generic form: mode carries cmd
        mode = ""
    if not mode and cmd:
        first = cmd.split()[0].lower()
        if first in ("status", "check"):
            mode, cmd = "status", ""
        elif first == "setup":
            mode, cmd = "setup", ""
        elif first == "start":
            mode, cmd = "start", ""
        elif first == "stop":
            mode, cmd = "stop", ""
        else:
            mode = "run"
    if mode in ("", "status", "check"):
        t = _target()
        reachable = _termux_reachable() if t else False
        api = ""
        if reachable:
            api = _termux_ssh("command -v termux-battery-status "
                              "|| echo missing", timeout=10).strip()
        lines = ["🤖 Termux bridge"]
        if not t:
            lines.append("  ✗ No phone connected — run 'phone connect' first.")
        elif not reachable:
            lines.append("  ✗ sshd not reachable — run 'phone termux setup' "
                         "(one-time) or 'phone termux start'.")
        else:
            lines.append("  ✅ SSH shell up (port 8022, key-only).")
            if "missing" in api or not api:
                lines.append("  ⚠ Termux:API not installed — GPS, clipboard, "
                             "SMS, sensors, camera need it (install the "
                             "Termux:API app once, then 'phone termux status' "
                             "re-checks).")
            else:
                lines.append("  ✅ Termux:API installed — GPS/clipboard/SMS/"
                             "sensors/camera available.")
        return "\n".join(lines)
    if mode == "setup":
        return _termux_setup()
    if mode == "start":
        out = _termux_ssh("sshd && echo started || echo failed", timeout=15)
        return "✅ sshd started." if "started" in out else \
            f"sshd didn't start: {out}"
    if mode == "stop":
        _termux_ssh("pkill -f sshd 2>/dev/null; echo stopped", timeout=15)
        return "🛑 Termux sshd stopped — the bridge is off."
    # mode == "run": a command inside Termux
    if not cmd:
        return ("Termux bridge: try 'phone termux status | setup | start | "
                "stop', or 'phone termux <command>'.")
    if _DANGEROUS_SHELL.search(cmd):
        return ("Refused: that command can damage the phone or wipe data. "
                "The phone tool keeps those off-limits.")
    # Friendly names → the Termux:API command they map to (these need the
    # Termux:API app; adb alone can't do GPS/clipboard/SMS/sensors/camera).
    first, _, rest = cmd.partition(" ")
    friendly = {
        "battery": "termux-battery-status",
        "gps": "termux-location -p once",
        "location": "termux-location -p once",
        "clipboard": ("termux-clipboard-get"
                       if rest.lower() in ("", "get") else
                       f"termux-clipboard-set "
                       + (rest[4:].strip() if rest.lower().startswith("set ")
                          else rest)),
        "sensors": "termux-sensor -n 1 -d 250",
        "camera": (f"termux-camera-photo {_TERMUX_APP_DATA}/jeeves_cam.jpg"),
        "torch": ("termux-torch on" if rest.lower() in ("", "on")
                   else "termux-torch off"),
        "volume": "termux-volume",
        "vibrate": "termux-vibrate",
        "wifi": ("termux-wifi-enable" if rest.lower() in ("", "on")
                  else "termux-wifi-disable"),
    }
    if first.lower() in friendly:
        cmd = friendly[first.lower()]
    out = _termux_ssh(cmd)
    if ("command not found" in out and "termux-" in cmd) or \
            ("needs to be installed" in out):
        return (f"'{first}' needs the Termux:API app — install it once "
                "(F-Droid or Termux GitHub releases), grant the permission "
                "it asks for, then it works. ({out})")
    return out


def _action_notify(text: str, title: str = "") -> str:
    """Push a notification to the phone's shade via `cmd notification post`
    (native, no Termux needed)."""
    text = (text or "").strip()
    if not text:
        return "notify needs a message ('phone notify hello')."
    title = (title or "Jeeves").strip()
    t = _target()
    if not t:
        return "No phone connected — run 'phone connect' first."
    try:
        out = _run_adb(["shell", "cmd", "notification", "post",
                        "-S", "bigtext", "-t", title,
                        "jeeves", text],
                       timeout=20, target=t).stdout or ""
    except Exception as e:
        return f"notify failed: {e}"
    if "posting" in out or "Notification" in out or not out.strip():
        return f"🔔 Notification pushed: {title} — {text}"
    return out.strip()[:200] or "🔔 Notification pushed."


def _action_battery() -> str:
    """Formatted live battery report (native dumpsys — no Termux needed)."""
    t = _target()
    if not t:
        return "No phone connected — run 'phone connect' first."
    try:
        out = _run_adb(["shell", "dumpsys", "battery"], timeout=20,
                       target=t).stdout or ""
    except Exception as e:
        return f"battery check failed: {e}"
    vals = {}
    for line in out.splitlines():
        line = line.strip()
        if ":" in line:
            k, _, v = line.partition(":")
            vals[k.strip().lower()] = v.strip()
    level = vals.get("level", "?")
    status = vals.get("status", "").lower()
    state = {"1": "unknown", "2": "charging", "3": "discharging",
             "4": "not charging", "5": "full"}.get(
        vals.get("status", ""), status)
    temp = vals.get("temperature", "")
    if temp:
        try:
            temp = f"{int(temp) / 10.0:.1f}°C"
        except ValueError:
            pass
    tech = vals.get("technology", "")
    parts = [f"🔋 Battery: {level}% ({state})"]
    if temp and temp != "0.0°C":
        parts.append(f"Temp: {temp}")
    if tech:
        parts.append(f"Type: {tech}")
    ac = vals.get("ac powered") == "true"
    usb = vals.get("usb powered") == "true"
    parts.append(f"Power: {'AC' if ac else ''}{'+' if ac and usb else ''}"
                 f"{'USB' if usb else 'battery'}")
    return " · ".join(parts)


# ── Actions ──────────────────────────────────────────────────────────────────

def _action_status() -> str:
    adb = _find_adb()
    lines = ["📱 Phone status"]
    if not adb:
        lines.append("  ✗ adb not found — install Android platform-tools "
                     "or scrcpy and add adb to PATH")
        return "\n".join(lines)
    # ONE `adb devices` call feeds both the USB line and the wireless line
    # (the old code read the same output twice, spawning adb twice).
    (serial, state), ep = _devices_table()
    if not serial:
        lines.append("  ✗ No phone over USB — plug the phone in and enable "
                     "USB debugging (Settings → Developer options)")
    elif state == "unauthorized":
        lines.append(f"  ⚠ Phone connected ({serial}) but NOT authorized — "
                     "unlock the phone and tap 'Allow' on the 'Allow USB "
                     "debugging?' prompt, then run 'phone connect'")
    elif state == "offline":
        lines.append(f"  ⚠ Phone ({serial}) is offline — try replugging the "
                     "USB cable")
    else:
        lines.append(f"  ✓ USB: {serial} (authorized)")
        ep = _CONNECTED_ENDPOINT or ep
        if ep and _connected(ep):
            lines.append(f"  ✓ Wireless: {ep} (no cable needed)")
        else:
            lines.append("  ~ Wireless: not connected yet — say 'phone "
                         "connect' (one-time USB step, then it's wireless)")
        info = _action_info(target=serial)
        lines.append("  " + info.replace("\n", "\n  "))
    return "\n".join(lines)


def _action_pair(code: str = "", target: str = "") -> str:
    """Pair with a phone using Android 11+ wireless debugging.

    Usage: phone connect pair <code> <ip:port>
    Example: phone connect pair 482931 192.168.1.5:37000

    On the phone:
      1. Settings → Developer options → Wireless debugging → ON
      2. Tap 'Pair device with pairing code'
      3. Note the 6-digit code and IP:port shown
      4. Run: phone connect pair <code> <ip:port>
    """
    code = (code or "").strip()
    target = (target or "").strip()
    if not code:
        return (
            "Usage: phone connect pair <code> <ip:port>\n\n"
            "On the phone:\n"
            "  Settings → Developer options → Wireless debugging → ON\n"
            "  Tap 'Pair device with pairing code'\n"
            "  Note the 6-digit code and IP:port shown\n\n"
            "Then: phone connect pair 482931 192.168.1.5:37000")
    adb = _find_adb()
    if not adb:
        return "adb not found — install Android platform-tools or scrcpy first."
    # `adb pair <ip:port> <code>` — Android 11+ wireless pairing
    try:
        cmd = [adb, "pair", target, code] if target else [adb, "pair", code]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                           **subprocess_no_window_kwargs())
        out = (r.stdout or "") + (r.stderr or "")
        if "successfully paired" in out.lower() or "paired" in out.lower():
            # After pairing, connect to the device
            # The pairing port is different from the connect port
            # Extract IP from target if provided
            if target and ":" in target:
                ip = target.split(":")[0]
                # Try common ports (5555 is default for wireless ADB)
                connect_endpoint = f"{ip}:5555"
                try:
                    cr = _run_adb(["connect", connect_endpoint], timeout=10)
                    if "connected" in (cr.stdout or "").lower():
                        _CONNECTED_ENDPOINT = connect_endpoint
                        _profile_save("", connect_endpoint)
                        # Auto-configure developer settings
                        try:
                            _action_dev("on")
                        except Exception:
                            pass
                        return (
                            f"✅ Paired and connected: {connect_endpoint}\n"
                            f"  Wireless debugging is active. No cable needed.\n"
                            f"  Developer settings auto-optimized.")
                except Exception:
                    pass
            return f"✅ Paired successfully! Now run: phone connect"
        else:
            return (f"Pairing failed: {out.strip()[:200]}\n\n"
                    "Make sure:\n"
                    "  1. Wireless debugging is ON\n"
                    "  2. You tapped 'Pair device with pairing code'\n"
                    "  3. The code and IP:port match what's shown on the phone")
    except Exception as e:
        return f"Pairing failed: {e}"


def _action_connect(port: int = 5555) -> str:
    """USB → wireless, and RECONNECT by stable serial. Requires the phone
    plugged in + authorized ONCE; afterwards everything runs over Wi-Fi.
    When the phone's IP changes (DHCP), re-running 'phone connect' finds it
    again by its serial — saved endpoint first, then a bounded local-subnet
    scan of adb's port. Idempotent."""
    global _CONNECTED_ENDPOINT, _USB_SERIAL
    adb = _find_adb()
    if not adb:
        return "adb not found — install Android platform-tools or scrcpy first."
    serial, state = _usb_device()
    profile = _profile_load()

    # Already wirelessly connected? Confirm (idempotent, no re-setup).
    live = _CONNECTED_ENDPOINT or _live_endpoint()
    if live and _connected(live):
        _CONNECTED_ENDPOINT = live
        return f"✅ Already wireless-connected: {live}."

    if serial and state == "device":
        _USB_SERIAL = serial
        ip = _phone_ip(serial)
        if not ip:
            return ("Could not read the phone's Wi-Fi IP — is Wi-Fi on and "
                    "connected to the same network as this computer?")
        # 1. Tell the phone's adbd to listen on TCP (needs the authorized
        #    USB link; one-time per boot). MUST target the USB serial — with
        #    a wireless endpoint already live, a bare `adb tcpip` fails with
        #    "more than one device/emulator". Restarts adbd — brief blip.
        try:
            r = _run_adb(["tcpip", str(port)], timeout=15, target=serial)
            if "restarting in TCP mode" not in (r.stdout or "").lower():
                if r.returncode != 0:
                    return (f"adb tcpip failed: "
                            f"{(r.stderr or r.stdout or '').strip()}")
        except Exception as e:
            return f"adb tcpip failed: {e}"
        time.sleep(1.5)  # adbd restart
        # 2. Connect over Wi-Fi.
        endpoint = f"{ip}:{port}"
        try:
            out = (_run_adb(["connect", endpoint], timeout=15).stdout or "")
        except Exception as e:
            return f"adb connect failed: {e}"
        if "connected" not in out.lower():
            return (f"Could not connect to {endpoint}: {out.strip() or 'no reply'}."
                    f" Make sure the phone and computer are on the SAME Wi-Fi.")
        # 3. Verify the wireless link actually answers.
        time.sleep(1.0)
        if not _connected(endpoint):
            return (f"adb reported a connection to {endpoint} but it isn't "
                    f"answering yet — wait a moment and run 'phone status'.")
        _CONNECTED_ENDPOINT = endpoint
        _profile_save(serial, endpoint)
        # Auto-configure developer settings for reliable wireless ADB.
        dev_msg = ""
        try:
            dev_result = _action_dev("on")
            if dev_result and "applied" in dev_result.lower():
                dev_msg = ("\n  ⚙ Developer settings optimized: stay-awake, "
                           "no animations, wireless adb never expires.")
        except Exception:
            pass
        return (f"✅ Wireless connected: {endpoint}. The phone is now reachable "
                f"over Wi-Fi — no cable needed — until it reboots or changes "
                f"networks. Try 'phone screenshot'.{dev_msg}")

    # USB attached but not yet authorized — point at the phone prompt.
    if serial:
        return ("📱 Phone detected but not authorized.\n\n"
                "On your phone, you should see a popup:\n"
                "  \"Allow USB debugging?\"\n\n"
                "Tap \"Allow\" (check \"Always allow\" if shown), then run\n"
                "  phone connect\n\n"
                "If you don't see the popup:\n"
                "  1. Make sure the phone is UNLOCKED\n"
                "  2. Disconnect and reconnect the USB cable\n"
                "  3. Settings → Developer options → USB debugging must be ON\n"
                "     (if Developer options isn't visible:\n"
                "      Settings → About phone → tap \"Build number\" 7×)")

    # ── No usable USB: re-find the phone by its STABLE serial ──
    pserial = str(profile.get("serial") or "").strip()
    if not pserial:
        # Check Android version for wireless debugging support
        android_ver = 0
        try:
            import subprocess as _sp
            r = _sp.run(["adb", "shell", "getprop", "ro.build.version.release"],
                        capture_output=True, text=True, timeout=5)
            android_ver = int((r.stdout or "").strip().split(".")[0])
        except Exception:
            pass

        if android_ver >= 11:
            return (
                "📱 No phone connected.\n\n"
                "EASY SETUP (Android 11+ — no USB cable needed!):\n\n"
                "  On the phone:\n"
                "    1. Settings → Developer options\n"
                "       (if hidden: About phone → tap \"Build number\" 7×)\n"
                "    2. Turn ON \"Wireless debugging\"\n"
                "    3. Tap \"Pair device with pairing code\"\n"
                "    4. Note the 6-digit code + IP:port shown\n\n"
                "  Then on PC, run:\n"
                "    phone connect pair <code> <ip:port>\n\n"
                "Example:\n"
                "    phone connect pair 482931 192.168.1.5:37000\n\n"
                "After pairing, it's wireless forever — no cable, no USB debugging.")
        else:
            return (
                "📱 No phone connected and no saved profile.\n\n"
                "FIRST-TIME SETUP (one-time, ~2 minutes):\n\n"
                "  On the phone:\n"
                "    1. Settings → About phone\n"
                "    2. Tap \"Build number\" 7× → Developer options enabled\n"
                "    3. Settings → Developer options → ON \"USB debugging\"\n\n"
                "  Then:\n"
                "    4. Plug phone into PC via USB cable\n"
                "    5. On phone: tap \"Allow\" on the USB debugging popup\n"
                "    6. Run: phone connect\n\n"
                "After that, it's wireless forever (until reboot/network change).\n"
                "Jeeves auto-optimizes developer settings on first connect.")
    # 1) The last-known endpoint (fast path when the IP didn't change).
    pep = str(profile.get("endpoint") or "").strip()
    if pep and _connect_and_verify(pep, pserial):
        _CONNECTED_ENDPOINT = pep
        _USB_SERIAL = pserial
        return (f"✅ Reconnected to your phone (serial {pserial}) at "
                f"{pep} — still wireless, no cable needed.")
    # 2) IP changed: scan the local subnet for adb's port and verify serial.
    print("[phone] saved endpoint is stale — scanning the subnet for "
          f"serial {pserial}...")
    ep = _scan_subnet_for_serial(pserial, port=port)
    if ep:
        _CONNECTED_ENDPOINT = ep
        _USB_SERIAL = pserial
        _profile_save(pserial, ep)
        return (f"✅ Found your phone by its serial ({pserial}) at its new "
                f"IP {ep} — reconnected over Wi-Fi, no cable needed.")
    return (f"Couldn't reach the saved phone ({pserial}) — it may be off, "
            f"on a different network, or adb debugging was re-disabled. Plug "
            f"it in via USB once to reconnect, or check it's on the same "
            f"Wi-Fi.")


def _action_info(target=None) -> str:
    target = target or _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    bits = []
    # 4 getprops in ONE adb call (was 4 sequential subprocess spawns).
    model, ver, serial, build = _shell_batch(target, [
        "getprop ro.product.model",
        "getprop ro.build.version.release",
        "getprop ro.serialno",
        "getprop ro.build.id",
    ])
    if model:
        bits.append(f"model: {model}")
    if ver:
        bits.append(f"Android: {ver}")
    if serial:
        bits.append(f"serial: {serial}")
    if build:
        bits.append(f"build: {build}")
    # battery + screen + storage in a SECOND adb call (was 3 more spawns).
    bat, size, df = _shell_batch(target, [
        "dumpsys battery",
        "wm size",
        "df -h /sdcard",
    ])
    m = re.search(r"level:\s*(\d+)", bat or "")
    if m:
        bits.append(f"battery: {m.group(1)}%")
    m = re.search(r"(\d+x\d+)", size or "")
    if m:
        bits.append(f"screen: {m.group(1)}")
    for line in (df or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "/sdcard":
            bits.append(f"storage: {parts[1]} used of {parts[3]}")
            break
    return "; ".join(bits) if bits else "connected (no details readable)"


def _action_screenshot(analyze: bool = False, text: str = "",
                       save_dir: str = "phone_shots") -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        r = _run_adb(["exec-out", "screencap", "-p"], timeout=30,
                     target=target, binary=True)
    except Exception as e:
        return f"Screenshot failed: {e}"
    if r.returncode != 0 or not r.stdout:
        return "Screenshot failed — the phone returned nothing."
    data = r.stdout
    if not data.startswith(b"\x89PNG"):
        return "Screenshot failed — unexpected format from the phone."
    d = Path(save_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"phone_{time.strftime('%Y%m%d_%H%M%S')}.png"
    path.write_bytes(data)
    base = f"📸 Saved: {path}"
    if analyze:
        try:
            # Vision is a heavy import (~14s cold) — lazy, only on demand.
            from actions.screen_processor import _analyze_still
            desc = _analyze_still(data, "image/png",
                                  text or "Describe what is on this phone "
                                          "screen right now.")
            return f"{base}\n🔍 {desc}"
        except Exception as e:
            return f"{base}\n(could not analyze: {e})"
    return base


def _input(target, *args) -> str:
    try:
        out = (_run_adb(["shell", "input"] + list(args), timeout=15,
                        target=target).stdout or "").strip()
    except Exception as e:
        return f"Input failed: {e}"
    return "ok" if not out else out


def _action_tap(x: int, y: int) -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return "tap needs integer x and y coordinates (pixels)."
    res = _input(target, "tap", str(x), str(y))
    return f"👆 Tapped ({x}, {y})" if res == "ok" else res


def _action_swipe(x1, y1, x2, y2, duration_ms: int = 300) -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        vals = [int(v) for v in (x1, y1, x2, y2)]
        ms = max(0, int(duration_ms or 300))
    except (TypeError, ValueError):
        return "swipe needs integer x1,y1,x2,y2 (and optional duration_ms)."
    res = _input(target, "swipe", *[str(v) for v in vals], str(ms))
    return (f"👉 Swiped ({vals[0]},{vals[1]}) → ({vals[2]},{vals[3]})"
            if res == "ok" else res)


def _action_text(text: str) -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    if not (text or "").strip():
        return "text needs the text to type."
    # adb shell input treats space as a separator — %s is the standard
    # escape; also neutralize shell metacharacters inside the quotes.
    escaped = (str(text).replace(" ", "%s")
               .replace("\\", "\\\\").replace('"', '\\"')
               .replace("`", "\\`").replace("$", "\\$"))
    res = _input(target, "text", f'"{escaped}"')
    return f"⌨️ Typed: {text[:40]}{'…' if len(text) > 40 else ''}" \
        if res == "ok" else res


_KEYCODES = {
    "home": 3, "back": 4, "call": 5, "endcall": 6, "volume_up": 24,
    "volume_down": 25, "power": 26, "camera": 27, "enter": 66, "menu": 82,
    "search": 84, "recents": 187, "app_switch": 187, "notifications": 130,
    "sleep": 223, "wakeup": 224, "mute": 164, "play": 126, "pause": 127,
    "media_play_pause": 85, "brightness_up": 220, "brightness_down": 221,
}


def _action_key(key: str) -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    k = (key or "").strip().lower()
    code = _KEYCODES.get(k)
    if code is None and k.isdigit():
        code = int(k)
    if code is None:
        return (f"Unknown key '{key}' — try: "
                + ", ".join(sorted(_KEYCODES.keys())))
    res = _input(target, "keyevent", str(code))
    return f"🔘 Key: {k}" if res == "ok" else res


def _action_apps(query: str = "") -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        out = _shell(target, "pm", "list", "packages", "-3", timeout=30)
    except Exception as e:
        return f"Could not list apps: {e}"
    pkgs = [l.replace("package:", "").strip()
            for l in (out or "").splitlines() if l.startswith("package:")]
    pkgs.sort()
    q = (query or "").strip().lower()
    if q:
        pkgs = [p for p in pkgs if q in p.lower()]
    if not pkgs:
        return f"No apps found" + (f" matching '{query}'" if q else "") + "."
    shown = pkgs[:40]
    head = f"📱 {len(pkgs)} app(s)" + (f" matching '{query}'" if q else "")
    return head + "\n" + "\n".join("  " + p for p in shown) \
        + ("\n  …" if len(pkgs) > 40 else "")


def _resolve_package(target: str, query: str) -> str | None:
    """Resolve a partial app name to its full package name via pm list."""
    q = query.strip().lower()
    try:
        out = _shell(target, "pm", "list", "packages", timeout=15) or ""
    except Exception:
        return None
    matches = []
    for line in out.splitlines():
        pkg = line.replace("package:", "").strip()
        if q == pkg.lower():
            return pkg            # exact match
        if q in pkg.lower():
            matches.append(pkg)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # prefer the one that ends with the query (e.g. "spotify" → com.spotify.music)
        for m in matches:
            if m.lower().endswith(q):
                return m
        return matches[0]         # first fuzzy match
    return None


def _action_launch(pkg: str, stop: bool = False) -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    pkg = (pkg or "").strip()
    if not pkg:
        return "launch needs the package name (see 'phone apps')."
    try:
        if stop:
            _shell(target, "am", "force-stop", pkg, timeout=20)
            return f"⏹ Stopped {pkg}"
        # monkey launches an app without needing the exact activity name.
        out = (_run_adb(["shell", "monkey", "-p", pkg, "1"], timeout=20,
                        target=target).stdout or "")
        if "Events injected" in out or "No activities found" not in out:
            return f"🚀 Launched {pkg}"
        # If monkey failed, try resolving partial name → full package
        if "No activities found" in out:
            resolved = _resolve_package(target, pkg)
            if resolved and resolved != pkg:
                out2 = (_run_adb(["shell", "monkey", "-p", resolved, "1"],
                                timeout=20, target=target).stdout or "")
                if "Events injected" in out2 or "No activities found" not in out2:
                    return f"🚀 Launched {resolved}"
                return f"Could not launch {pkg} ({resolved}): {out2.strip()[:120]}"
            # suggest similar packages
            try:
                all_pkgs = _shell(target, "pm", "list", "packages", timeout=15) or ""
                suggestions = [l.replace("package:", "").strip()
                               for l in all_pkgs.splitlines()
                               if pkg.lower() in l.lower()][:5]
                if suggestions:
                    return (f"Could not launch '{pkg}' — did you mean: "
                            + ", ".join(suggestions) + "?")
            except Exception:
                pass
        return f"Could not launch {pkg}: {out.strip()[:120]}"
    except Exception as e:
        return f"Could not launch {pkg}: {e}"


def _action_files(path: str = "/sdcard") -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    path = (path or "/sdcard").strip()
    try:
        out = _shell(target, "ls", "-la", path, timeout=15)
    except Exception as e:
        return f"Could not list {path}: {e}"
    lines = [l for l in (out or "").splitlines() if l.strip()]
    if not lines:
        return f"(empty) {path}"
    head = f"📂 {path}"
    return head + "\n" + "\n".join("  " + l for l in lines[:40]) \
        + ("\n  …" if len(lines) > 40 else "")


def _action_pull(remote: str, local: str = "") -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    remote = (remote or "").strip()
    if not remote:
        return "pull needs a remote path on the phone (e.g. /sdcard/DCIM/x.jpg)."
    local = (local or "").strip() or str(Path.cwd() / "phone_pulls")
    Path(local).mkdir(parents=True, exist_ok=True) if not local.endswith(
        (".png", ".jpg", ".jpeg", ".mp4", ".txt", ".pdf", ".docx", ".zip")) \
        else None
    try:
        # A directory of photos over Wi-Fi can take a while — 180s, still
        # bounded (a wedged adb can never hang forever).
        r = _run_adb(["pull", remote, local], timeout=180, target=target)
    except Exception as e:
        return f"pull failed: {e}"
    return (r.stdout or "").strip() or f"📥 Pulled {remote} → {local}"


def _action_push(local: str, remote: str) -> str:
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    local = (local or "").strip()
    remote = (remote or "").strip()
    if not local or not remote:
        return "push needs local=<path on PC> and remote=<path on phone>."
    if not (remote.startswith("/sdcard/") or remote.startswith(
            "/storage/emulated/0/")):
        return ("push only writes to the phone's shared storage (remote must "
                "start with /sdcard/) — never into system directories.")
    try:
        r = _run_adb(["push", local, remote], timeout=180, target=target)
    except Exception as e:
        return f"push failed: {e}"
    return (r.stdout or "").strip() or f"📤 Pushed {local} → {remote}"


# ── Shell guard (daktari: accidental destruction is the real risk) ───────────
# The `shell` action gives full reach into the device, so commands that can
# damage the phone (or silently wipe data) are refused outright. This is a
# guard against accidents — the phone is the user's own, so there's no
# adversarial threat model here, just no footguns.
_DANGEROUS_SHELL = re.compile(
    r"(^|[\s;&|])(reboot|shutdown|recovery|fastboot|wipe|format|mkfs|fdisk|"
    r"parted|flash|odin|su\b|magisk|dd\b|shred|pv\b|iptables|"
    r"\brm\b|\brmdir\b|\bdel\b|mv\s+/system|mount\s+-o\s+rw|"
    r"pm\s+uninstall|pm\s+clear|am\s+force-stop\s+com\.android\.settings|"
    r"settings\s+put\s+global|chmod\s+777\s+/|chown\s+-R)|"
    r"(rm\s+-[a-z]*rf?|rm\s+-[a-z]*r\s+/|rm\s+/\s*-)",
    re.IGNORECASE,
)


def _action_shell(cmd: str) -> str:
    # The blocklist runs FIRST — a destructive command is refused no matter
    # what (never "connect first, then we'll let you rm"); daktari.
    cmd = (cmd or "").strip()
    if not cmd:
        return "shell needs a command (e.g. 'getprop ro.build.version.release')."
    if _DANGEROUS_SHELL.search(cmd):
        return ("Refused: that command can damage the phone or wipe data "
                "(reboot/wipe/rm/uninstall/...). The phone tool keeps those "
                "off-limits — ask Jeeves for a safe way to do what you need.")
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        r = _run_adb(["shell", cmd], timeout=30, target=target)
    except Exception as e:
        return f"shell failed: {e}"
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    return out or "(no output)"


# ── Ring / find-my-phone (daktari: loud, sustained, and self-cleaning) ───────
# Rings the phone at max volume using the device's OWN default alarm sound
# (resolved to a real file, then played through the default audio player via a
# VIEW intent — the `media play` binary no longer exists on Android 14+).
# Verified live on this device: the alarm file plays for its full length even
# over the lock screen. The ring runs on a daemon thread so the action returns
# instantly, auto-stops after `seconds`, restores the volumes it raised, and
# `ring stop` cancels early — no runaway ring, no permanent volume change.

_ring_lock = threading.Lock()
_ring_active = False
_ring_stop = False

_VOLUME_SETTINGS = ("volume_alarm", "volume_music", "volume_ring")
_VOLUME_STREAMS = {"volume_alarm": 4, "volume_music": 3, "volume_ring": 2}
_SOUND_SETTING = {"alarm": "alarm_alert",
                  "notification": "notification_sound"}


def _resolve_sound_path(target: str, kind: str = "alarm") -> str | None:
    """Resolve the device's default alarm/notification sound to a real file
    path: settings gives a content:// URI, content query gives `_data`."""
    key = _SOUND_SETTING.get(kind, "alarm_alert")
    try:
        uri = _shell(target, "settings", "get", "system", key, timeout=10)
    except Exception:
        uri = ""
    uri = (uri or "").strip()
    if uri.startswith("content://"):
        base = uri.split("?", 1)[0]
        try:
            out = _shell(target, "content", "query", "--uri", base,
                         "--projection", "_data", timeout=10)
            m = re.search(r"_data=(\S+)", out or "")
            if m and m.group(1).startswith("/"):
                return m.group(1)
        except Exception:
            pass
    # Fallback: first file in the system alarm ringtone dirs.
    for d in ("/product/media/audio/alarms", "/system/media/audio/alarms",
              "/product/media/audio/ringtones"):
        try:
            out = _shell(target, "ls", d, timeout=10)
        except Exception:
            continue
        for name in (out or "").splitlines():
            name = name.strip()
            if name and not name.startswith("-"):
                return f"{d}/{name}"
    return None


def _set_volume(target: str, key: str, value: str) -> None:
    """Best-effort volume change: persisted setting + live AudioService.
    MIUI may cap the live index below the requested max — still loud."""
    try:
        _shell(target, "settings", "put", "system", key, value, timeout=10)
    except Exception:
        pass
    try:
        _run_adb(["shell", "cmd", "media_session", "volume",
                  "--stream", str(_VOLUME_STREAMS[key]), "--set", value],
                 timeout=10, target=target)
    except Exception:
        pass


def _ring_worker(target: str, path: str, seconds: int) -> None:
    global _ring_active
    original = {}
    try:
        for key in _VOLUME_SETTINGS:
            try:
                original[key] = _shell(target, "settings", "get", "system",
                                       key, timeout=10).strip() or ""
            except Exception:
                original[key] = ""
        for key in _VOLUME_SETTINGS:
            _set_volume(target, key, "15")
        # Wake the screen so the user can also spot it, then play the sound
        # through the default player (works over the lock screen).
        try:
            _shell(target, "input", "keyevent", "KEYCODE_WAKEUP", timeout=10)
        except Exception:
            pass
        try:
            _shell(target, "am", "start", "-a", "android.intent.action.VIEW",
                   "-d", f"file://{path}", "-t", "audio/*", timeout=15)
        except Exception:
            pass
        elapsed = 0
        while elapsed < seconds and not _ring_stop:
            time.sleep(1)
            elapsed += 1
    finally:
        # Silence + restore the volumes the ring raised.
        try:
            _shell(target, "cmd", "media_session", "dispatch", "stop",
                   timeout=10)
        except Exception:
            pass
        for key, val in original.items():
            if val:
                _set_volume(target, key, val)
        with _ring_lock:
            _ring_active = False


def _action_ring(seconds: int = 25, stop: bool = False) -> str:
    """Ring the phone at max volume so it can be found when misplaced.
    Returns immediately; the ring self-stops after `seconds` (5–120) and
    `phone ring stop` silences it early."""
    global _ring_active, _ring_stop
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    if stop:
        with _ring_lock:
            _ring_stop = True
        try:
            _shell(target, "cmd", "media_session", "dispatch", "stop",
                   timeout=10)
        except Exception:
            pass
        return "🔕 Ringing stopped."
    with _ring_lock:
        if _ring_active:
            return ("The phone is already ringing — say 'phone ring stop' "
                    "to silence it early.")
        _ring_stop = False
        _ring_active = True
    try:
        seconds = max(5, min(int(seconds or 25), 120))
    except (TypeError, ValueError):
        seconds = 25
    path = _resolve_sound_path(target)
    if not path:
        with _ring_lock:
            _ring_active = False
        return ("Could not find a playable sound on the phone — check that "
                "it has a default alarm ringtone set.")
    threading.Thread(target=_ring_worker, args=(target, path, seconds),
                     daemon=True).start()
    return (f"🔔 Ringing for {seconds}s at max volume — go find it! "
            f"('phone ring stop' silences it early)")


# ── Phantom Droid-style extras: live screen, device manager, diagnostics ──────
# Ideas borrowed from HexSec's Phantom Droid / DroidHunter (authorized-testing
# frameworks) and kept inside Jeeves' SAFE envelope: everything here is a
# read/mirror — no destructive reach. scrcpy mirrors the screen (view +
# control, like Phantom Droid's remote view); devices/logcat/wifi/network/
# report/top/storage are all read-only diagnostics.


def _find_scrcpy() -> str | None:
    """Locate the scrcpy binary (PATH or the WinGet bundle). Returns path
    or None. scrcpy mirrors the phone's screen live and lets you control
    it — the remote-view feature (safe: read-only mirror + input)."""
    found = shutil.which("scrcpy") or shutil.which("scrcpy.exe")
    if not found:
        try:
            wg = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" \
                / "Packages"
            hits = sorted(wg.glob("Genymobile.scrcpy*/**/scrcpy.exe")) \
                if wg.is_dir() else []
            if hits:
                found = str(hits[0])
        except Exception:
            pass
    return found or None


def _action_screen() -> str:
    """Open a LIVE mirror of the phone's screen via scrcpy (the phone's own
    screen stays on; you see and can tap/type into it from the PC).
    Returns immediately — the scrcpy window runs on its own."""
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    scrcpy = _find_scrcpy()
    if not scrcpy:
        return ("scrcpy not found — install it (WinGet: 'winget install "
                "Genymobile.scrcpy') to mirror the phone's screen live.")
    adb = _find_adb()
    cmd = [scrcpy]
    if target:
        cmd += ["--serial", target]
    # scrcpy 4.0+: set ADB path via environment variable
    # ADB=full path to adb.exe (not just the directory)
    import os as _os
    env = dict(_os.environ)
    if adb:
        adb_path = Path(adb)
        env["ADB"] = str(adb_path)  # full path to adb.exe
        # Also add adb's directory to PATH so scrcpy can find it
        adb_dir = str(adb_path.parent)
        env["PATH"] = adb_dir + _os.pathsep + env.get("PATH", "")
    try:
        # Deliberately NO subprocess_no_window_kwargs(): scrcpy is a GUI
        # binary and CREATE_NO_WINDOW would hide its mirror window.
        subprocess.Popen(cmd, env=env)
    except Exception as e:
        return f"Could not start scrcpy: {e}"
    return (f"📺 Live screen opened for {target} — a scrcpy window should be "
            f"up now. You can see and tap/type into the phone from it. "
            f"(Close the window to stop; say 'phone screenshot' for a "
            f"static capture Jeeves can describe.)")


def _action_devices() -> str:
    """Device manager: every device the adb server sees (USB + wireless
    endpoints), with state and model — like Phantom Droid's device list."""
    try:
        out = (_run_adb(["devices", "-l"], timeout=10).stdout or "")
    except Exception as e:
        return f"Could not list devices: {e}"
    lines = [l for l in (out or "").splitlines() if l.strip()]
    rows = []
    for line in lines:
        if line.startswith("List") or "\t" not in line:
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        extra = " ".join(parts[2:])
        model = ""
        for kv in extra.split():
            if kv.startswith("model="):
                model = kv.split("=", 1)[1].replace("_", " ")
        kind = "wireless" if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", serial) \
            else "usb"
        rows.append((serial, state, kind, model))
    if not rows:
        return ("No devices seen by adb. Plug the phone in (USB debugging "
                "on) and run 'phone connect', or check the phone is on the "
                "same Wi-Fi.")
    head = f"🔌 {len(rows)} device(s) seen by adb:"
    body = [
        f"  {kind:8} {state:12} {serial}  {model}".rstrip()
        for serial, state, kind, model in rows
    ]
    return head + "\n" + "\n".join(body)


def _action_logcat(lines: int = 120, query: str = "") -> str:
    """Recent logcat (last N lines, optionally filtered) — read-only
    diagnostics from the phone's own log buffer."""
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        lines = max(5, min(int(lines or 120), 1000))
    except (TypeError, ValueError):
        lines = 120
    try:
        out = _run_adb(["logcat", "-d", "-t", str(lines)], timeout=20,
                       target=target).stdout or ""
    except Exception as e:
        return f"logcat failed: {e}"
    q = (query or "").strip().lower()
    if q:
        out = "\n".join(l for l in out.splitlines() if q in l.lower())
    out = out.strip()
    if not out:
        return f"(no log lines" + (f" matching '{query}'" if q else "") + ")"
    shown = out.splitlines()[:60]
    head = f"📜 logcat" + (f" matching '{query}'" if q else "") \
           + f" ({len(out.splitlines())} lines)"
    return head + "\n" + "\n".join("  " + l for l in shown) \
        + ("\n  …" if len(out.splitlines()) > 60 else "")


def _action_wifi() -> str:
    """Wi-Fi info from the phone's own radio: SSID, signal, link speed,
    IP — read-only (dumpsys wifi)."""
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        out = _shell(target, "dumpsys", "wifi", timeout=20)
    except Exception as e:
        return f"wifi check failed: {e}"
    out = out or ""
    info = {}
    m = re.search(r'mWifiInfo SSID: "([^"]+)"', out)
    if m:
        info["SSID"] = m.group(1)
    m = re.search(r"SSID: ([^,\s]+)", out)
    if m and "SSID" not in info:
        info["SSID"] = m.group(1)
    m = re.search(r"Link speed: (\d+)", out)
    if m:
        info["link speed"] = f"{m.group(1)} Mbps"
    m = re.search(r"RSSI: (-?\d+)", out)
    if m:
        info["signal"] = f"{m.group(1)} dBm"
    m = re.search(r"IP address: (\d+\.\d+\.\d+\.\d+)", out)
    if m:
        info["ip"] = m.group(1)
    m = re.search(r"Freq: (\d+)", out)
    if m:
        info["band"] = ("5 GHz" if int(m.group(1)) > 4900 else "2.4 GHz")
    if not info:
        ssid, ip = _shell_batch(target, [
            'dumpsys wifi | grep -Eo "SSID: \"[^\"]*" | head -1',
            "ip -f inet addr show wlan0 | grep -oE 'inet [0-9.]+' | head -1",
        ], timeout=15)
        if ssid:
            info["SSID"] = ssid.split('"', 1)[-1] if '"' in ssid else ssid
        if ip:
            info["ip"] = ip.replace("inet ", "")
    if not info:
        return "Wi-Fi info unavailable — is Wi-Fi on? (try 'phone network')"
    return "📶 Wi-Fi: " + " · ".join(f"{k}: {v}" for k, v in info.items())


def _action_network() -> str:
    """Full network view: all interface IPs, gateway, DNS — read-only."""
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        addrs = _shell(target, "ip", "-f", "inet", "addr", "show", timeout=15)
        route = _shell(target, "ip", "route", timeout=15)
        dns = _shell(target, "getprop", "net.dns1", timeout=10)
        dns2 = _shell(target, "getprop", "net.dns2", timeout=10)
    except Exception as e:
        return f"network check failed: {e}"
    lines = []
    cur = None
    for line in (addrs or "").splitlines():
        line = line.strip()
        m = re.match(r"^(\w+):", line)
        if m:
            cur = m.group(1)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if m and cur:
            lines.append(f"  {cur}: {m.group(1)}/{m.group(2)}")
    gw = ""
    for line in (route or "").splitlines():
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", line)
        if m:
            gw = m.group(1)
            break
    if not lines and not gw:
        return "Network info unavailable — is the phone online?"
    head = "🌐 Network"
    body = [f"  gateway: {gw}"] if gw else []
    body += lines[:8]
    dnss = [d for d in (dns, dns2) if d and d.strip()]
    if dnss:
        body.append("  dns: " + ", ".join(d.strip() for d in dnss))
    return head + "\n" + "\n".join(body)


def _action_top(limit: int = 15) -> str:
    """Running processes sorted by CPU (top) — read-only."""
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        limit = max(5, min(int(limit or 15), 40))
    except (TypeError, ValueError):
        limit = 15
    try:
        out = _shell(target, "top", "-n", "1", "-b", timeout=20)
    except Exception as e:
        return f"top failed: {e}"
    lines = [l for l in (out or "").splitlines() if l.strip()]
    # Drop header lines; keep the process table (starts with PID column).
    proc = [l for l in lines if re.match(r"^\s*\d+", l)]
    if not proc:
        return out[:600] or "(no process table)"
    head = f"⚙️ Top {min(limit, len(proc))} processes by CPU:"
    return head + "\n" + "\n".join("  " + l for l in proc[:limit])


def _action_storage() -> str:
    """Storage usage per mount (df -h) — read-only."""
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    try:
        out = _shell(target, "df", "-h", timeout=15)
    except Exception as e:
        return f"storage check failed: {e}"
    lines = [l for l in (out or "").splitlines()
             if l.strip() and not l.lower().startswith("filesystem")]
    keep = [l for l in lines
            if re.search(r"(/sdcard|/data$|/system$|/storage)", l)][:8]
    if not keep:
        keep = lines[:6]
    return "💾 Storage:\n" + "\n".join("  " + l for l in keep)


def _action_report() -> str:
    """One-shot device health report — battery, storage, memory, CPU,
    uptime, screen, top apps. The safe, phone-side cousin of DroidHunter's
    report generator (read-only, nothing is written or changed)."""
    target = _target()
    if not target:
        return "No phone connected — run 'phone connect' first."
    bat, mem, up, size, df = _shell_batch(target, [
        "dumpsys battery",
        "cat /proc/meminfo",
        "cat /proc/uptime",
        "wm size",
        "df -h /sdcard",
    ], timeout=20)
    model, ver = _shell_batch(target, [
        "getprop ro.product.model",
        "getprop ro.build.version.release",
    ], timeout=10)
    lines = [f"📋 Phone report"]
    lines.append(f"  Device: {model or '?'}  (Android {ver or '?'})")
    m = re.search(r"level:\s*(\d+)", bat or "")
    status = re.search(r"status:\s*(\d+)", bat or "")
    state = {"2": "charging", "3": "discharging", "5": "full"}.get(
        status.group(1) if status else "", "?") if status else "?"
    if m:
        lines.append(f"  Battery: {m.group(1)}% ({state})")
    m = re.search(r"MemTotal:\s*(\d+)\s*kB", mem or "")
    ma = re.search(r"MemAvailable:\s*(\d+)\s*kB", mem or "")
    if m and ma:
        tot = int(m.group(1)) / 1024 / 1024
        av = int(ma.group(1)) / 1024 / 1024
        lines.append(f"  RAM: {av:.1f} GB free of {tot:.1f} GB")
    m = re.search(r"^(\d+)\.(\d+) (\d+)\.(\d+)", up or "")
    if m:
        secs = int(m.group(1))
        lines.append(f"  Uptime: {secs // 86400}d {(secs % 86400) // 3600}h "
                     f"{(secs % 3600) // 60}m")
    m = re.search(r"(\d+x\d+)", size or "")
    if m:
        lines.append(f"  Screen: {m.group(1)}")
    for line in (df or "").splitlines():
        parts = line.split()
        # /sdcard is the first column on some builds, the mount column on
        # others — accept either position.
        if len(parts) >= 5 and ("/sdcard" in (parts[0], parts[-1])):
            lines.append(f"  Storage: {parts[1]} used of {parts[3]}")
            break
    top = _action_top(6)
    top_lines = [l for l in top.splitlines() if l.strip()]
    if len(top_lines) > 1:
        lines.append("  Top processes:")
        for l in top_lines[1:4]:
            lines.append("    " + l.strip())
    return "\n".join(lines)



# ── Tool entry point ─────────────────────────────────────────────────────────

def phone_control(parameters: dict, player=None) -> str:
    """Tool dispatcher. Actions:
      status      — connection state (USB/wireless) + phone info
      devices     — every phone adb sees (USB + wireless, state, model)
      connect     — one-time USB→wireless setup (then it's cable-free)
      info        — model, Android version, battery, screen, storage
      screen      — LIVE mirror + control via scrcpy (Phantom Droid-style)
      screenshot  — capture the screen (analyze=true → Jeeves describes it)
      logcat      — recent phone logs (lines=, query=)
      wifi/network/report/top/storage — read-only diagnostics
      ring        — find-my-phone: ring at max volume (seconds=, stop=true)
      tap/swipe/text/key — control the screen (pixel coords; key=home/back/...)
      apps/launch/stop  — list (query=), open, force-stop apps by package
      files/pull/push   — browse /sdcard, copy files either way (push is
                          restricted to shared storage)
      shell       — any safe shell command (destructive ones are refused)
    """
    params = parameters or {}
    action = str(params.get("action", "status")).strip().lower()

    if action in ("status", "state"):
        return _action_status()
    if action in ("connect", "wireless", "reconnect"):
        return _action_connect(int(params.get("port", 5555) or 5555))
    if action == "pair":
        return _action_pair(params.get("code") or params.get("pairing_code"),
                           params.get("ip") or params.get("address") or params.get("target"))
    if action in ("info", "device"):
        return _action_info()
    if action == "devices":
        return _action_devices()
    if action == "screen":
        return _action_screen()
    if action == "logcat":
        return _action_logcat(params.get("lines"), params.get("query") or "")
    if action == "wifi":
        return _action_wifi()
    if action == "network":
        return _action_network()
    if action == "top":
        return _action_top(params.get("limit"))
    if action == "storage":
        return _action_storage()
    if action == "report":
        return _action_report()
    if action in ("screenshot", "shot"):
        analyze = str(params.get("analyze", "")).lower() in ("1", "true", "yes")
        return _action_screenshot(analyze=analyze,
                                  text=str(params.get("text") or ""))
    if action == "tap":
        return _action_tap(params.get("x"), params.get("y"))
    if action == "swipe":
        return _action_swipe(params.get("x1"), params.get("y1"),
                             params.get("x2"), params.get("y2"),
                             params.get("duration_ms"))
    if action in ("text", "type"):
        return _action_text(params.get("text") or params.get("message"))
    if action in ("key", "keyevent"):
        return _action_key(params.get("key"))
    if action in ("apps", "list_apps"):
        return _action_apps(params.get("query") or "")
    if action == "launch":
        return _action_launch(params.get("pkg"))
    if action == "stop":
        return _action_launch(params.get("pkg"), stop=True)
    if action == "files":
        return _action_files(params.get("path") or "/sdcard")
    if action == "pull":
        return _action_pull(params.get("remote"), params.get("local") or "")
    if action == "push":
        return _action_push(params.get("local"), params.get("remote"))
    if action == "shell":
        return _action_shell(params.get("cmd") or params.get("command"))
    if action in ("ring", "locate", "find"):
        stop = str(params.get("stop", "")).lower() in ("1", "true", "yes")
        return _action_ring(params.get("seconds"), stop=stop)
    if action in ("macro", "macrodroid"):
        return _action_macro(str(params.get("name") or ""),
                             str(params.get("value") or ""),
                             start=str(params.get("start", "")).lower()
                             in ("1", "true", "yes"),
                             do_list=str(params.get("list", "")).lower()
                             in ("1", "true", "yes"))
    if action in ("dev", "developer", "devopts"):
        return _action_dev(str(params.get("mode") or "status"))
    if action in ("termux", "term"):
        return _action_termux(str(params.get("mode") or ""),
                              str(params.get("cmd") or ""))
    if action in ("notify", "notification"):
        return _action_notify(str(params.get("text") or ""),
                              str(params.get("title") or ""))
    if action in ("battery", "batt"):
        return _action_battery()
    if action in ("unlock", "pin", "password"):
        # Local-only fail-safe (security-question-gated PIN vault). The
        # module is gitignored on purpose — never committed.
        try:
            from actions.phone_unlock import phone_unlock
        except Exception:
            return ("phone unlock isn't available here (it lives in "
                    "actions/phone_unlock.py, a local-only file kept out "
                    "of git).")
        return phone_unlock(params)
    return ("Unknown phone action. Try: status | devices | connect | "
            "pair <code> <ip:port> (Android 11+ wireless debugging) | "
            "info | screen (live mirror) | screenshot | logcat [lines] | "
            "wifi | network | report | top | storage | "
            "ring [seconds] | unlock | "
            "dev [on|off|status] | termux [status|setup|start|stop|<cmd>] | "
            "notify '<text>' | battery | tap x y | swipe x1 y1 x2 y2 | "
            "text '...' | key home | apps | launch pkg | stop pkg | files | "
            "pull remote | push local remote | shell 'cmd' | macro <name>")
