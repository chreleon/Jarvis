# Setting Up Jeeves

Jeeves is a local, free voice assistant: **Whisper** (listens) -> **Groq** (thinks)
-> **Piper** (speaks), with optional **Composio** tool access to GitHub, Gmail,
and Google Calendar. No paid APIs, no billing walls, no "policy violation" gates.

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
```

On first launch, a setup screen walks you through everything else:
- **Groq API key** -- paste one, or get a free one (no card) at https://console.groq.com/keys
- **Operating system** -- auto-detected, just confirm
- **Voice model** -- click "DOWNLOAD VOICE FILES" and it fetches the Piper voice
  (`en_GB-alan-medium`) into `voices/` for you, no manual download needed
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
The same in-app setup screen (Groq key, voice download, Composio connect
buttons) appears on first launch -- see the Windows section above for details.

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

Voice files: download `en_GB-alan-medium.onnx` and `.onnx.json` from
https://github.com/rhasspy/piper/releases into a `voices/` folder (no
auto-download button in this mode). Add your Groq key to
`config/api_keys.json`:
```json
{ "groq_api_key": "YOUR_FREE_GROQ_KEY" }
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
and download `en_GB-alan-medium.onnx` + `en_GB-alan-medium.onnx.json` from
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
