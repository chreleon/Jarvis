#!/usr/bin/env python3
"""Configure a GitHub Codespace for Jeeves (JARVIS).

Usage:
  python scripts/configure_codespace.py --codespace NAME --workdir /workspaces/Jeeves [--install]

This updates `config/api_keys.json` to enable `provider: "codespace"` and
optionally runs `gh codespace exec` to install Python requirements inside the
Codespace. The script does not ask for secrets.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
import sys


BASE_DIR = Path(__file__).resolve().parent.parent
API_FILE = BASE_DIR / "config" / "api_keys.json"


def load_config() -> dict:
    if not API_FILE.exists():
        return {}
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    API_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


def gh_available() -> bool:
    return bool(shutil.which("gh"))


def gh_run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 2, "", str(e)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codespace", required=True, help="Codespace name from `gh codespace list`")
    parser.add_argument("--workdir", required=True, help="Repository path inside codespace, e.g. /workspaces/Jeeves")
    parser.add_argument("--install", action="store_true", help="If set, install requirements inside the Codespace")
    args = parser.parse_args()

    cfg = load_config()
    cfg.setdefault("remote_execution", {})
    cfg["remote_execution"].update({
        "enabled": True,
        "provider": "codespace",
        "codespace": args.codespace,
        "codespace_workdir": args.workdir,
    })

    save_config(cfg)
    print(f"Updated config/api_keys.json with Codespace={args.codespace}, workdir={args.workdir}")

    if not gh_available():
        print("gh CLI not found locally. Install it and authenticate with `gh auth login` to run verification/install steps.")
        return

    # Verify that the Codespace exists
    rc, out, err = gh_run(["gh", "codespace", "list", "--limit", "100"])
    if rc != 0:
        print(f"Could not list Codespaces: {err or out}")
        return

    if args.codespace not in out:
        print("Warning: the provided Codespace name was not found in your `gh codespace list` output. Check the name and try again.")
    else:
        print("Codespace found in your account.")

    # Quick exec smoke test
    print("Running quick smoke test inside Codespace (prints cwd + python version)...")
    test_cmd = ["gh", "codespace", "exec", "--codespace", args.codespace, "--", "bash", "-lc", f"cd {args.workdir} && pwd && python3 --version"]
    rc, out, err = gh_run(test_cmd)
    if rc == 0:
        print("Smoke test output:\n", out)
    else:
        print("Smoke test failed:\n", err or out)

    if args.install:
        print("Installing Python requirements inside Codespace. This may take a while...")
        install_cmd = ["gh", "codespace", "exec", "--codespace", args.codespace, "--", "bash", "-lc", f"cd {args.workdir} && python3 -m pip install -r requirements.txt"]
        rc, out, err = gh_run(install_cmd)
        if rc == 0:
            print("Requirements installed successfully inside Codespace.")
        else:
            print("Install failed:\n", err or out)


if __name__ == "__main__":
    main()
