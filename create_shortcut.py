#!/usr/bin/env python3
"""create_shortcut.py — create desktop shortcuts for Jeeves.

Creates two shortcuts on the Windows desktop (no console window, using
pythonw.exe) with the Jeeves icon (jeeves.ico):

  * "Jeeves"      → launches the full app (main.py)
  * "Jeeves Orb"  → launches the floating desktop orb + mini chat (orb.py)

Run:  python create_shortcut.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ICON = BASE_DIR / "jeeves.ico"

# Windows consoles default to cp1252 and crash on emoji output.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]


def _pythonw() -> Path:
    """Path to pythonw.exe (no console) next to the running python."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def _desktop_dir() -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, timeout=30,
    )
    return (out.stdout or "").strip()


def _make_shortcut(desktop: str, name: str, script: str, description: str) -> str:
    target = _pythonw()
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut('{desktop}\\{name}.lnk');"
        f"$s.TargetPath = '{target}';"
        f"$s.Arguments = '\"{BASE_DIR / script}\"';"
        f"$s.WorkingDirectory = '{BASE_DIR}';"
        f"$s.IconLocation = '{ICON},0';"
        f"$s.Description = '{description}';"
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=True, capture_output=True, text=True, timeout=30)
    return f"{desktop}\\{name}.lnk"


def main() -> int:
    if not ICON.exists():
        print(f"⚠️  Missing icon: {ICON} — shortcuts will use the default icon.")
    try:
        desktop = _desktop_dir()
    except Exception as e:
        print(f"❌ Could not find the desktop folder: {e}")
        return 1
    if not desktop:
        print("❌ Desktop folder resolved empty.")
        return 1

    try:
        app_lnk = _make_shortcut(desktop, "Jeeves", "main.py",
                                 "JEEVES — Mark XXXIX personal AI assistant")
        orb_lnk = _make_shortcut(desktop, "Jeeves Orb", "orb.py",
                                 "JEEVES — floating desktop orb + mini chat")
    except subprocess.CalledProcessError as e:
        print(f"❌ Shortcut creation failed: {e.stderr or e}")
        return 1

    print(f"✅ Created: {app_lnk}")
    print(f"✅ Created: {orb_lnk}")
    print("Tip: right-click 'Jeeves' → Pin to taskbar for one-click launch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
