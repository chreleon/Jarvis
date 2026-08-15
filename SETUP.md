# Setting Up Jeeves

Jeeves is a local, free voice assistant: **Whisper** (listens) -> **Groq or
GitHub Models** (thinks) -> **Piper** (speaks), with optional **Composio** tool
access to GitHub, Gmail, and Google Calendar. No paid APIs, no billing walls,
no "policy violation" gates.

Brains / Providers
-------------------

Jeeves supports multiple LLM "brains" (providers). Out of the box it supports:

- **Groq** (default, free Groq API / llama models)
- **GitHub Models** (GitHub-hosted models such as `gpt-4.1`/`gpt-4.1-mini`)

You can choose the active provider in the setup screen or by editing
`config/api_keys.json` and setting the `brain_provider` field. Example:

```json
{
  "brain_provider": "github_models",
  "github_models_api_key": "YOUR_GITHUB_MODELS_KEY",
  "os_system": "windows"
}
```

Auto-fallback behavior:

- If a request fails due to rate limits, quota, or an expired/invalid token,
  Jeeves will automatically try the alternate provider and will adopt it if
  the fallback request succeeds.
- This means an expired Groq token will no longer necessarily stop Jeeves —
  it will try GitHub Models (if configured) and continue working when possible.

Security note: provider API keys remain in `config/api_keys.json`. Protect this
file and avoid committing it to source control.

This guide covers setup on every device Jeeves' *desktop voice mode* (`main.py`)
can run on. It does not cover the web/remote-access setup -- that part is kept
separate and private.

---

## 1. Windows (primary supported platform)

**Requirements:** Python 3.11 or 3.12, Git, a working microphone.

```powershell
# Install Git if you don't have it: https://git-scm.com/download/win
# Then, in PowerShell:

cd C:\Users\<you>\Downloads
git clone https://github.com/chreleon/Jarvis.git Jeeves
cd Jeeves

pip install -r requirements.txt
```

**Run it:**
```powershell
py main.py
    "enabled": true,
    "provider": "codespace",            
    "codespace": "your-codespace-name", 
    "codespace_workdir": "/workspaces/Jeeves",
    "host": "your-server.example.com",
    "user": "ubuntu",
    "port": 22,
    "identity_file": "C:/Users/you/.ssh/id_ed25519",
    "remote_root": "/tmp/jeeves"
  (`en_GB-jenny_dioco-medium`) into `voices/` for you, no manual download needed
- **Connect accounts (optional)** -- GitHub / Gmail / Calendar buttons open the
  Composio authorization page in your browser directly from the setup screen

That's it -- no more manually creating `config/api_keys.json` or hunting down
voice files by hand, though you're welcome to do so if you prefer (see the
"Manual alternative" note below).

---

## 2. Linux (Ubuntu/Debian/Fedora/etc.)

**Requirements:** Python 3.11+, Git, a microphone, `portaudio` (for `pyaudio`),
and Qt6 system libraries (for the desktop UI).

```bash
sudo apt update
sudo apt install python3 python3-pip git portaudio19-dev libxcb-cursor0 -y   # Debian/Ubuntu
# or: sudo dnf install python3 python3-pip git portaudio-devel -y   # Fedora

git clone https://github.com/chreleon/Jarvis.git Jeeves
cd Jeeves

# Skip Windows-only packages (pywin32, win10toast, pycaw, comtypes, pygetwindow) --
# install the rest by hand instead of the full requirements.txt:
pip3 install sounddevice pillow requests beautifulsoup4 duckduckgo-search \
             playwright pyautogui pyperclip opencv-python numpy psutil \
             youtube-transcript-api pyaudio groq faster-whisper piper-tts \
             composio-core composio-openai flask PyQt6
```

**Run it:**
```bash
python3 main.py
```
The same in-app setup screen (provider choice, key/token entry, voice download,
Composio connect buttons) appears on first launch -- see the Windows section
above for details.

---

## 3. macOS

**Requirements:** Python 3.11+ (via Homebrew), Git, `portaudio`.

```bash
brew install python git portaudio
git clone https://github.com/chreleon/Jarvis.git Jeeves
cd Jeeves

pip3 install sounddevice pillow requests beautifulsoup4 duckduckgo-search \
             playwright pyautogui pyperclip opencv-python numpy psutil \
             youtube-transcript-api pyaudio groq faster-whisper piper-tts \
             composio-core composio-openai flask PyQt6
```

Run with:
```bash
python3 main.py
```
Same in-app setup screen appears on first launch.

---

## 4. Android via Termux (plain, no NetHunter)

Best for a lightweight, always-available setup on a spare phone.

**Note:** the desktop GUI (`ui.py`, PyQt6) generally isn't practical on Android.
This path is best suited to the private web-hosted mode instead, so it's kept
brief here -- see Windows/Linux/macOS above for the full desktop experience.

```bash
pkg update && pkg upgrade
pkg install python git portaudio -y

git clone https://github.com/chreleon/Jarvis.git Jeeves
cd Jeeves

pip install flask groq faster-whisper piper-tts composio-core composio-openai
```

Voice files: download `en_GB-jenny_dioco-medium.onnx` and `.onnx.json` from
https://github.com/rhasspy/piper/releases into a `voices/` folder (no
auto-download button in this mode). Choose Groq or GitHub Models in the setup
screen, then add the matching credential to `config/api_keys.json`:
```json
{ "brain_provider": "groq", "groq_api_key": "YOUR_FREE_GROQ_KEY" }
```

**Keep it running in the background:**
```bash
termux-wake-lock
```
Also set Termux to "unrestricted" battery usage in Android's app settings, or
Android will kill it after a while.

---

## 5. Android via Kali NetHunter (Termux chroot)

Same as plain Termux, but inside the Kali chroot environment:

```bash
apt update
apt install python3 python3-pip portaudio19-dev -y

git clone https://github.com/chreleon/Jarvis.git Jeeves
cd Jeeves

pip3 install flask groq faster-whisper piper-tts composio-core composio-openai
```

If `git clone` fails with a DNS error (`Could not resolve host`), fix the
chroot's resolver first:
```bash
echo "nameserver 8.8.8.8" > /etc/resolv.conf
```

Same config/voices setup as the Termux section above.

---

## Optional: Composio tool access (GitHub / Gmail / Calendar)

**Desktop (Windows/macOS/Linux):** just use the "GitHub" / "Gmail" / "Calendar"
buttons on the first-run setup screen -- each opens the authorization page in
your browser directly.

**Manual alternative (any platform, including Termux/NetHunter):**
```bash
composio login
composio add github
composio add gmail
composio add googlecalendar
```
Each `composio add` opens a browser OAuth flow. Once connected, `composio_agent.py`
lets Jeeves actually act on these accounts, not just talk about them.

If you also want to register the local HTTP bridge as a Composio custom tool,
use the helper script with the matching server path:
```bash
python agent/register_composio_tool.py --url http://127.0.0.1:8000 --path /invoke
python agent/register_composio_tool.py --url http://127.0.0.1:5051 --path /call
```

---

## Optional: MCP client (consume external MCP servers)

Jeeves ships with an MCP *client* (`mcp_client.py`) so the agent loop can call
tools exposed by any Model Context Protocol server -- stdio or Streamable
HTTP. MCP tools are merged into the same Groq tool-calling loop as Composio
tools.

**Install:** `pip install mcp` (already in `requirements.txt`).

**Configure servers** in `config/api_keys.json` under `mcp_servers` (a list), or
set the `MCP_SERVERS` JSON env var (which wins):

```json
{
  "mcp_servers": [
    {
      "name": "composio",
      "transport": "streamablehttp",
      "url": "https://connect.composio.dev/mcp",
      "auth": { "type": "bearer", "key_ref": "composio_mcp_token" }
    },
    {
      "name": "my-local-server",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@some/mcp-server"]
    }
  ]
}
```

Notes:

- `connect.composio.dev/mcp` requires a **Bearer AuthKit JWT** -- put it in
  `composio_mcp_token`. The SDK `composio_api_key` is *not* accepted there
  (the server returns 401). Grab a token from the Composio dashboard's
  **AI Clients / Connect** section.
- Auth types: `header` (name + `key_ref`/`value`) or `bearer` (`key_ref`/
  `token`). Any header value may use `${config_key}` templates.
- If a server is unreachable or unauthenticated, Jeeves logs a warning and
  keeps running without its tools -- nothing crashes.

---

## Optional: spawning Jeeves headlessly (fast one-shots, direct tools, daemon)

The CLI (`cli.py`) can be driven as a spawnable agent from scripts, other
agents, or automation. Three tiers, fastest to slowest:

### 1. Direct tool calls -- no LLM, deterministic, instant-ish

Bypasses the brain entirely and calls a tool by name with JSON arguments:

```bash
python cli.py --tool open_app --args '{"app_name": "Notepad"}'
python cli.py --tool system_status --raw          # --raw prints only the result text
python cli.py --tool file_controller --args '{"action": "list", "path": "desktop"}'
```

This is the fastest path (no model call, no follow-up) and never surprises
you with a tool the LLM "decided" to call instead.

### 2. Single-shot chat -- one prompt, LLM picks the tool

```bash
python cli.py -c "open notepad and tell me what you did"
```

Every `-c` is a fresh process, so it pays startup import time (~10-20s) and
has no memory of previous calls.

### 3. Warm daemon -- persistent brain + conversation, ~1s spawns

A background daemon keeps Jeeves warm and stateful on a localhost socket, so
repeated spawns are fast and share conversation memory:

```bash
python cli.py --daemon               # start the daemon explicitly (optional)
python cli.py --send "hello"         # auto-starts the daemon if needed, then chats
python cli.py --send "open notepad"
python cli.py --send-tool system_status --raw
python cli.py --send "who am I?" --reset     # wipe the daemon's conversation
python cli.py --daemon-stop          # shut the daemon down
```

Details:

- Listens on `127.0.0.1:8877` (override with `--port`); auth via the
  `jeeves_api_secret` from `config/api_keys.json` (auto-generated if missing).
- Logs go to `jeeves_daemon.log` in the project root.
- The first `--send` after boot pays the cold start; every later call is warm
  and keeps the conversation from previous calls.
- Direct tool calls (`--send-tool`) work too, with the same raw output option.
- Chat replies depend on the LLM provider: free-tier Groq can rate-limit
  rapid-fire calls (429 retries of 5-30s). Direct `--tool`/`--send-tool`
  calls never touch the LLM, so they are always fast.

---

## Manual alternative: setting keys/voice files by hand

If you'd rather skip the in-app setup screen, create `config/api_keys.json`
yourself:
```json
{
  "groq_api_key": "YOUR_FREE_GROQ_KEY",
  "os_system": "windows"
}
```
and download `en_GB-jenny_dioco-medium.onnx` + `en_GB-jenny_dioco-medium.onnx.json` from
https://github.com/rhasspy/piper/releases into a `voices/` folder in the
project root.

---

## Common troubleshooting

- **`ModuleNotFoundError`** -- you're missing a pip install; re-run the
  install line for your platform above.
- **`pip: command not found`** -- use `pip3` instead, or `python3 -m pip`.
- **No sound / mic not detected** -- check your OS's microphone permissions
  for the terminal app you're running Jeeves from.
- **`piper: command not found`** -- confirm `pip install piper-tts` succeeded
  and that your terminal was restarted after install.
- **UI won't launch / `ModuleNotFoundError: PyQt6`** -- run
  `pip install PyQt6` (this is now included in `requirements.txt`, so a fresh
  `pip install -r requirements.txt` on Windows covers it automatically).

---

## Optional: remote execution for heavy tasks

If you want Jeeves to run generated code, builds, or project execution on an
online terminal instead of your computer, add a `remote_execution` block to
`config/api_keys.json`:

```json
{
  "groq_api_key": "YOUR_FREE_GROQ_KEY",
  "os_system": "windows",
  "remote_execution": {
    "enabled": true,
    "host": "your-server.example.com",
    "user": "ubuntu",
    "port": 22,
    "identity_file": "C:/Users/you/.ssh/id_ed25519",
    "provider": "codespace",   
    "codespace": "your-codespace-name", 
    "codespace_workdir": "/workspaces/your-repo",
    "remote_root": "/tmp/jeeves"
  }
}
```

Requirements for this mode:
- SSH access from this PC to that host
- `ssh` and `scp` available on the local machine
- Python installed on the remote host

When enabled, Jeeves keeps planning and file writing local, but executes heavy
runtime steps on the remote shell.
