# actions/send_message.py
# Universal messaging — WhatsApp, Telegram & Instagram
# Uses visual element detection (pyautogui + screen search) instead of
# hardcoded tab/click sequences — works on any screen resolution.
#
# Desktop-first: if the native app (WhatsApp/Telegram) is installed it is
# driven directly; otherwise the browser version (web.whatsapp.com /
# web.telegram.org) is opened in the user's default browser — which keeps
# their logged-in session — and the vision locator finds the search +
# message boxes on screen.

import os
import sys
import time
import webbrowser
from pathlib import Path

# Windows consoles default to cp1252 and crash on emoji output (the 📨/🌐
# progress prints below). Reconfigure once, matching the cli.py pattern;
# no-ops safely when stdout is unavailable (pythonw).
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

# pyautogui is heavy (~1s cold import, ~13MB) and only needed when a
# message is actually sent, so it's loaded lazily via _get_pyautogui() on
# first use. NOTE: this must be an explicit accessor — a module-level
# __getattr__ (PEP 562) can't serve bare `pyautogui` names inside this
# module's own functions (LOAD_GLOBAL never consults it), so call sites
# use `_get_pyautogui().X` instead.
_pyautogui = None


def _get_pyautogui():
    """Lazily import pyautogui on first use and return the module."""
    global _pyautogui
    if _pyautogui is None:
        import pyautogui as _pg
        _pg.FAILSAFE = True
        _pg.PAUSE = 0.08
        _pyautogui = _pg
    return _pyautogui

def _open_app(app_name: str) -> bool:
    """Opens an app via Windows search."""
    try:
        _get_pyautogui().press("win")
        time.sleep(0.4)
        _get_pyautogui().write(app_name, interval=0.04)
        time.sleep(0.5)
        _get_pyautogui().press("enter")
        time.sleep(2.0)  
        return True
    except Exception as e:
        print(f"[SendMessage] Could not open {app_name}: {e}")
        return False


def _search_contact(contact: str, platform: str):
    """
    Searches for a contact inside the messaging app.
    Uses Ctrl+F (universal search shortcut) then types contact name.
    """
    time.sleep(0.5)
    _get_pyautogui().hotkey("ctrl", "f")
    time.sleep(0.4)
    _get_pyautogui().hotkey("ctrl", "a")
    _get_pyautogui().write(contact, interval=0.04)
    time.sleep(0.8)
    _get_pyautogui().press("enter")
    time.sleep(0.6)


def _type_and_send(message: str):
    """Types message and sends it."""
    _get_pyautogui().press("tab")
    time.sleep(0.2)
    _get_pyautogui().hotkey("ctrl", "a")
    _get_pyautogui().write(message, interval=0.03)
    time.sleep(0.2)
    _get_pyautogui().press("enter")
    time.sleep(0.3)


def _is_app_installed(app_name: str) -> bool:
    """True if a desktop app is installed on this Windows machine.

    Checks the MSIX/Store app packages and the registry uninstall keys.
    Used to decide between the desktop automation path and the browser
    fallback (e.g. WhatsApp desktop missing → web.whatsapp.com).
    """
    name = (app_name or "").lower().strip()
    if not name or sys.platform != "win32":
        return False

    # MSIX / Microsoft Store apps live under %LOCALAPPDATA%\Packages
    try:
        pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
        if pkgs.exists():
            for p in pkgs.iterdir():
                if name in p.name.lower():
                    return True
    except Exception:
        pass

    # Registry uninstall entries (classic installers)
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            sub = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
            try:
                with winreg.OpenKey(root, sub) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sk:
                                display = ""
                                try:
                                    display = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                                except OSError:
                                    pass
                                if name in display.lower():
                                    return True
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        pass

    return False


def _activate_window_by_title(substr: str) -> bool:
    """Bring the first window whose title contains `substr` to the front.

    Used before the keyboard path so keystrokes never land in an unknown
    app: we only type once we've confirmed + focused the browser tab whose
    title names the messaging web app. Cheap and local (win32), no vision.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        found: list[int] = []

        def _cb(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if substr.lower() in buf.value.lower():
                    found.append(hwnd)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if found:
            user32.ShowWindow(found[0], 9)  # SW_RESTORE
            user32.SetForegroundWindow(found[0])
            return True
    except Exception:
        pass
    return False


def _open_or_focus_webapp(url: str, title_substr: str,
                          load_wait: float = 8.0) -> bool:
    """Reuse an already-open tab for a web app, else open `url`.

    Opens a fresh tab only when no window with `title_substr` in its title
    is already showing (webbrowser.open always makes a NEW tab, so a second
    call would stack duplicate WhatsApp tabs). Returns True when the app
    window is focused on Windows; callers that drive by coordinates (the
    vision path) can ignore the return value.
    """
    if _activate_window_by_title(title_substr):
        time.sleep(1.0)  # let the already-warm SPA settle
        return True
    webbrowser.open(url)
    time.sleep(load_wait)
    return _activate_window_by_title(title_substr)


def _screen_find_element(description: str) -> tuple[int, int] | None:
    """Vision-based element locator: screenshot + LLM → center (x, y).

    Reuses the screen_find helper from computer_control, so the browser
    fallback finds the right box on any layout instead of guessing.
    """
    try:
        from actions.computer_control import _screen_find
        return _screen_find(description)
    except Exception as e:
        print(f"[SendMessage] ⚠️ Vision locator unavailable: {e}")
        return None


def _send_whatsapp_web(receiver: str, message: str) -> str:
    """Sends a WhatsApp message through web.whatsapp.com in the browser.

    Fallback when the desktop app isn't installed. Opens the user's default
    browser (their logged-in session), locates the search + message boxes
    with the vision locator, and drives them by keyboard.
    """
    try:
        print("[SendMessage] 🌐 WhatsApp desktop app not found — using web.whatsapp.com")
        # Reuse an open tab when present; vision clicks by coordinates, so
        # the focus result is intentionally ignored (non-Windows included).
        _open_or_focus_webapp("https://web.whatsapp.com/", "WhatsApp")

        search = _screen_find_element(
            "the search box at the top of WhatsApp Web, "
            "placeholder 'Search or start a new chat'"
        )
        if search is None:
            return (
                "Could not find the WhatsApp search box — make sure you are "
                "logged in to web.whatsapp.com in your browser (scan the QR "
                "code if asked) and try again."
            )
        _get_pyautogui().click(search[0], search[1])
        time.sleep(0.5)
        _get_pyautogui().write(receiver, interval=0.06)
        time.sleep(2.5)  # let contact search results render before Enter
        _get_pyautogui().press("enter")
        time.sleep(2.0)  # let the chat open before typing the message

        inbox = _screen_find_element(
            "the message input box at the bottom of the WhatsApp chat, "
            "placeholder 'Type a message'"
        )
        if inbox is None:
            return f"Opened chat with {receiver} but could not find the message box."
        _get_pyautogui().click(inbox[0], inbox[1])
        time.sleep(0.5)
        _get_pyautogui().write(message, interval=0.03)
        time.sleep(0.2)
        _get_pyautogui().press("enter")

        return f"Message sent to {receiver} via WhatsApp Web."
    except Exception as e:
        return f"WhatsApp Web error: {e}"


def _send_whatsapp_web_shortcut(receiver: str, message: str) -> str:
    """Sends a WhatsApp message via web.whatsapp.com using keyboard only.

    The fast, free, private path (no screenshots, no LLM):
      Ctrl+Alt+N (new chat, search focused) → type contact → Enter
      → type message → Enter.

    The browser tab is confirmed + focused by window title first, so we
    never type into an unknown window; if the tab isn't there we stop and
    ask the user to open/log into web.whatsapp.com.
    """
    try:
        print("[SendMessage] ⌨️ WhatsApp Web — keyboard path")
        if not _open_or_focus_webapp("https://web.whatsapp.com/", "WhatsApp"):
            return (
                "Could not find the WhatsApp Web tab — open web.whatsapp.com "
                "in your browser (scan the QR code if asked) and try again."
            )

        _get_pyautogui().hotkey("ctrl", "alt", "n")  # WhatsApp Web: new chat (search focused)
        time.sleep(0.8)
        _get_pyautogui().write(receiver, interval=0.06)
        # Let the contact search results render before Enter — pressing too
        # early picks whatever chat is highlighted instead of the recipient.
        time.sleep(2.5)
        _get_pyautogui().press("enter")              # open the first search match
        time.sleep(2.0)                               # let the chat open before typing

        _get_pyautogui().write(message, interval=0.03)
        time.sleep(0.2)
        _get_pyautogui().press("enter")              # send

        return f"Message sent to {receiver} via WhatsApp Web (keyboard)."
    except Exception as e:
        return f"WhatsApp Web error: {e}"


def _send_whatsapp(receiver: str, message: str) -> str:
    """
    Sends a WhatsApp message via the Windows desktop app.
    Steps: Open WhatsApp → Search contact → Click → Type → Send
    """
    try:
        if not _open_app("WhatsApp"):
            return "Could not open WhatsApp."

        time.sleep(1.5)

        _get_pyautogui().hotkey("ctrl", "f")
        time.sleep(0.4)
        _get_pyautogui().hotkey("ctrl", "a")
        _get_pyautogui().write(receiver, interval=0.04)
        time.sleep(1.0)

        _get_pyautogui().press("enter")
        time.sleep(0.8)

        _get_pyautogui().write(message, interval=0.03)
        time.sleep(0.2)
        _get_pyautogui().press("enter")

        return f"Message sent to {receiver} via WhatsApp."

    except Exception as e:
        return f"WhatsApp error: {e}"


def _send_instagram(receiver: str, message: str) -> str:
    """
    Sends an Instagram DM via browser (instagram.com).
    Steps: Open Chrome → Go to instagram.com/direct → Search contact → Send
    """
    try:
        import webbrowser

        webbrowser.open("https://www.instagram.com/direct/new/")
        time.sleep(3.5)

        _get_pyautogui().write(receiver, interval=0.05)
        time.sleep(1.5)

        _get_pyautogui().press("down")
        time.sleep(0.3)
        _get_pyautogui().press("enter")
        time.sleep(0.5)

        for _ in range(3):
            _get_pyautogui().press("tab")
            time.sleep(0.1)
        _get_pyautogui().press("enter")
        time.sleep(1.5)

        _get_pyautogui().write(message, interval=0.04)
        time.sleep(0.2)
        _get_pyautogui().press("enter")

        return f"Message sent to {receiver} via Instagram."

    except Exception as e:
        return f"Instagram error: {e}"

def _send_telegram_web(receiver: str, message: str) -> str:
    """Sends a Telegram message through web.telegram.org in the browser.

    Fallback when the Telegram desktop app isn't installed (same pattern as
    the WhatsApp Web fallback).
    """
    try:
        print("[SendMessage] 🌐 Telegram desktop app not found — using web.telegram.org")
        # Reuse an open tab when present (vision path — focus not required).
        _open_or_focus_webapp("https://web.telegram.org/", "Telegram")

        search = _screen_find_element(
            "the search field at the top of Telegram Web, placeholder 'Search'"
        )
        if search is None:
            return (
                "Could not find the Telegram search field — make sure you are "
                "logged in to web.telegram.org in your browser and try again."
            )
        _get_pyautogui().click(search[0], search[1])
        time.sleep(0.5)
        _get_pyautogui().write(receiver, interval=0.06)
        time.sleep(2.5)  # let contact search results render before Enter
        _get_pyautogui().press("enter")
        time.sleep(2.0)  # let the chat open before typing the message

        inbox = _screen_find_element(
            "the message input field at the bottom of the Telegram chat, "
            "placeholder 'Message'"
        )
        if inbox is None:
            return f"Opened chat with {receiver} but could not find the message field."
        _get_pyautogui().click(inbox[0], inbox[1])
        time.sleep(0.5)
        _get_pyautogui().write(message, interval=0.03)
        time.sleep(0.2)
        _get_pyautogui().press("enter")

        return f"Message sent to {receiver} via Telegram Web."
    except Exception as e:
        return f"Telegram Web error: {e}"


def _send_telegram(receiver: str, message: str) -> str:
    """Sends a Telegram message via Windows desktop app."""
    try:
        if not _open_app("Telegram"):
            return "Could not open Telegram."

        time.sleep(1.5)

        _get_pyautogui().hotkey("ctrl", "f")
        time.sleep(0.4)
        _get_pyautogui().write(receiver, interval=0.04)
        time.sleep(1.0)
        _get_pyautogui().press("enter")
        time.sleep(0.8)

        _get_pyautogui().write(message, interval=0.03)
        time.sleep(0.2)
        _get_pyautogui().press("enter")

        return f"Message sent to {receiver} via Telegram."

    except Exception as e:
        return f"Telegram error: {e}"



def _send_generic(platform: str, receiver: str, message: str) -> str:
    """
    For any other platform not explicitly supported.
    Opens the app, searches for contact, types and sends.
    Works for: Messenger, Discord, Signal, etc.
    """
    try:
        if not _open_app(platform):
            return f"Could not open {platform}."

        time.sleep(1.5)
        _get_pyautogui().hotkey("ctrl", "f")
        time.sleep(0.4)
        _get_pyautogui().write(receiver, interval=0.04)
        time.sleep(1.0)
        _get_pyautogui().press("enter")
        time.sleep(0.8)
        _get_pyautogui().write(message, interval=0.03)
        time.sleep(0.2)
        _get_pyautogui().press("enter")

        return f"Message sent to {receiver} via {platform}."

    except Exception as e:
        return f"{platform} error: {e}"

# ── Memory aliases: "msg wife" → the real contact name ────────────────────
# People can be linked to relationships in long-term memory, e.g.
# relationships.wife = "😻もま かて". When the boss types a familiar name that
# isn't a WhatsApp contact, resolve it to the stored contact before sending.


def _resolve_receiver(name: str) -> str:
    """Resolve a familiar name ('wife', 'mom') to the real WhatsApp contact
    via long-term memory: memory['relationships'] first, then memory['identity']
    (skipping self-descriptors), matched case- and underscore-insensitively.
    Names with no mapping pass through unchanged."""
    name = (name or "").strip()
    if not name:
        return name
    target = name.lower().replace("_", " ").strip()
    try:
        from memory.memory_manager import load_memory
        memory = load_memory() or {}
    except Exception:
        return name
    blocks = [memory.get("relationships") or {}]
    ident = memory.get("identity") or {}
    blocks.append({k: v for k, v in ident.items()
                   if str(k).lower() not in ("name", "user_name")})
    for block in blocks:
        for key, entry in block.items():
            if str(key).lower().replace("_", " ").strip() != target:
                continue
            val = entry.get("value") if isinstance(entry, dict) else entry
            val = str(val or "").strip()
            if val and val.lower() != "not specified":
                return val
    return name


# ── Background (bridge) sending — PRIMARY WhatsApp path ────────────────────
# The WhatsApp Web bridge (actions/whatsapp_bridge.py) drives web.whatsapp.com
# in its own background browser, so sending never needs the app on screen,
# focused, or unlocked. It shares ONE browser with the secretary monitor, so
# there is never a second browser fighting over the profile. The classic
# desktop-app / web-keyboard flows below stay as the last alternative when
# the bridge is unavailable, not linked, or fails.


def _bridge_config() -> tuple[bool, str | None]:
    """(headless, cdp_url) for the background browser from api_keys.json
    (same keys the secretary monitor uses)."""
    headless, cdp_url = True, None
    try:
        import json
        cfg = json.loads((Path(__file__).resolve().parent.parent
                          / "config" / "api_keys.json")
                         .read_text(encoding="utf-8"))
        # None/null in config means "unset" → default headless. Only an
        # explicit false should show a visible window (bool(None) would be
        # False and wrongly pop a window every time).
        h = cfg.get("secretary_headless", True)
        headless = True if h is None else bool(h)
        cdp = str(cfg.get("secretary_cdp_url", "") or "").strip()
        cdp_url = cdp or None
    except Exception:
        pass
    return headless, cdp_url


def _send_whatsapp_bridge(receiver: str, message: str) -> str:
    """Send via the background WhatsApp Web bridge (Playwright, headless by
    default). Reuses the secretary monitor's browser when it is running — one
    profile, one browser, no collisions. Raises on any failure so callers can
    fall back to the foreground flows."""
    from actions.whatsapp_bridge import (
        acquire_shared_bridge, release_shared_bridge, is_profile_linked)
    headless, cdp_url = _bridge_config()
    bridge, created = acquire_shared_bridge(headless=headless, cdp_url=cdp_url)
    if created and not cdp_url and not is_profile_linked():
        # Never linked (no CDP attach either): launching Chromium would burn
        # ~15s + ~200MB just to discover the QR is needed. Fail fast so the
        # caller falls straight back to the foreground flow (YinYang: don't
        # do work you already know will fail).
        release_shared_bridge(bridge)
        raise RuntimeError(
            "WhatsApp Web is not linked yet — say 'link whatsapp' (or "
            "'secretary link') once to open the window and scan the QR; "
            "sends use the foreground flow until then")
    try:
        bridge.start()          # idempotent — reuses a live session
        # A cold browser takes ~10s to load WhatsApp Web and restore the
        # saved session; a single is_logged_in() check would wrongly report
        # "not linked" and fall back to the keyboard path. Wait for the
        # session to come back (a QR appearing means it's really not linked).
        if not bridge.wait_logged_in(timeout=45):
            if bridge.needs_qr():
                raise RuntimeError(
                    "WhatsApp Web is not linked in the background browser — "
                    "say 'link whatsapp' (or 'secretary link') once to open "
                    "the window and scan the QR with your phone; the session "
                    "is then saved and reused forever")
            raise RuntimeError(
                "WhatsApp Web took too long to restore the saved session "
                "(cold start). Try the message again in a moment — the "
                "background browser stays connected once it finishes "
                "loading.")
        result = bridge.send_message(receiver, message)
        return f"Message sent to {receiver} via WhatsApp Web (background). {result}"
    finally:
        release_shared_bridge(bridge)


def _send_whatsapp_auto(receiver: str, message: str) -> str:
    """Bridge-first WhatsApp send: the background browser is the primary path;
    the classic desktop-app / web-keyboard flows are the last alternative when
    the bridge is unavailable, not linked, or fails mid-send."""
    try:
        return _send_whatsapp_bridge(receiver, message)
    except Exception as e:
        print(f"[SendMessage] ⚠️ Background WhatsApp send unavailable "
              f"({e}) — using the foreground flow instead.")
    if _is_app_installed("WhatsApp"):
        return _send_whatsapp(receiver, message)
    return _send_whatsapp_web_shortcut(receiver, message)


def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None
) -> str:
    """
    Called from main.py.

    parameters:
        receiver     : Contact name to send to
        message_text : The message content
        platform     : whatsapp | instagram | telegram | <any app name>
                       Default: whatsapp
        method       : whatsapp only — 'auto' (default: background bridge
                       first, foreground flow as fallback) | 'bridge' |
                       'desktop' | 'shortcut' | 'vision'
    """
    params       = parameters or {}
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip().lower()

    if not receiver:
        return "Please specify who to send the message to, sir."
    if not message_text:
        return "Please specify what message to send, sir."

    # Resolve memory aliases ("wife" → the real contact) before anything
    # else so every path — shortcuts, secretary replies, direct tool calls —
    # sends to the right person.
    receiver = _resolve_receiver(receiver)

    print(f"[SendMessage] 📨 {platform} → {receiver}: {message_text[:40]}")
    if player:
        player.write_log(f"[msg] Sending to {receiver} via {platform}...")

    if "whatsapp" in platform or "wp" in platform or "wapp" in platform:
        # Primary path: the background WhatsApp Web bridge (headless — no
        # screen needed, reuses the secretary monitor's browser). When it is
        # unavailable or not linked, the classic flows take over as the last
        # alternative: desktop app first, then the browser keyboard path
        # (vision locator stays as the explicit method='vision' fallback).
        method = str(params.get("method", "auto")).strip().lower()
        if method in ("bridge", "background"):
            try:
                result = _send_whatsapp_bridge(receiver, message_text)
            except Exception as e:
                result = f"Background WhatsApp send failed: {e}"
        elif method in ("desktop", "app"):
            result = _send_whatsapp(receiver, message_text)
        elif method == "vision":
            result = _send_whatsapp_web(receiver, message_text)
        elif method == "shortcut":
            result = _send_whatsapp_web_shortcut(receiver, message_text)
        else:  # auto (default) — bridge primary, existing flow as fallback
            result = _send_whatsapp_auto(receiver, message_text)

    elif "instagram" in platform or "ig" in platform or "insta" in platform:
        result = _send_instagram(receiver, message_text)

    elif "telegram" in platform or "tg" in platform:
        if _is_app_installed("Telegram"):
            result = _send_telegram(receiver, message_text)
        else:
            result = _send_telegram_web(receiver, message_text)

    else:
        result = _send_generic(platform, receiver, message_text)

    print(f"[SendMessage] ✅ {result}")
    if player:
        player.write_log(f"[msg] {result}")

    return result
