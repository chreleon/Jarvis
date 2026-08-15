from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

import voice_downloader


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
API_FILE = CONFIG_DIR / "api_keys.json"


def _run(command: list[str], title: str, timeout: int = 600) -> None:
	print(f"\n== {title} ==")
	try:
		subprocess.run(command, cwd=BASE_DIR, check=True, timeout=timeout)
	except subprocess.TimeoutExpired:
		print(f"\n⚠️ {title} timed out after {timeout}s — rerun setup.py to retry.")


def _run_doctor() -> None:
	print("\n== Running Jeeves diagnostics ==")
	result = subprocess.run(
		[sys.executable, "doctor.py"],
		cwd=BASE_DIR,
		text=True,
		capture_output=True,
		timeout=60,
	)
	if result.stdout:
		print(result.stdout)
	if result.stderr:
		print(result.stderr, file=sys.stderr)

	if result.returncode != 0:
		print("Doctor diagnostics reported an error, but setup will continue so missing pieces can be installed.")


def _ensure_config_stub() -> None:
	CONFIG_DIR.mkdir(parents=True, exist_ok=True)
	try:
		existing = json.loads(API_FILE.read_text(encoding="utf-8")) if API_FILE.exists() else {}
	except Exception:
		existing = {}

	existing.setdefault("groq_api_key", "")
	existing.setdefault("brain_provider", "groq")
	existing.setdefault("github_models_api_key", "")
	existing.setdefault("github_token", "")
	existing.setdefault("os_system", platform.system().lower())
	existing.setdefault("enable_clap_wake", False)
	API_FILE.write_text(json.dumps(existing, indent=4), encoding="utf-8")


def main() -> None:
	_run_doctor()

	_run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], "Installing requirements")
	_run([sys.executable, "-m", "playwright", "install"], "Installing Playwright browsers", timeout=900)
	_ensure_config_stub()

	print("\n== Downloading voice model ==")
	ok = voice_downloader.download_voice_model(print)
	if not ok:
		print("Voice model download did not complete cleanly. The setup screen can retry it later.")

	print("\n== Launching Jeeves setup screen ==")
	subprocess.run([sys.executable, "main.py"], cwd=BASE_DIR, check=False)

	print("\n✅ Setup finished. Jeeves should now be open on the setup screen if anything still needs configuration.")


if __name__ == "__main__":
	main()