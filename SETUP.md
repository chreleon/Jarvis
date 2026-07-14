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

**Add your API keys** -- create `config\api_keys.json` (not tracked in git):
```json
{
  "groq_api_key": "YOUR_FREE_GROQ_KEY"
}
```
Get a free key, no card required, at https://console.groq.com/keys

**Add the voice model** -- download `en_GB-alan-medium.onnx` and
`en_GB-alan-medium.onnx.json` from https://github.com/rhasspy/piper/releases
into a `voices\` folder in the project root.

**Run it:**
```powershell
py main.py
```

---

## 2. Linux (Ubuntu/Debian/Fedora/etc.)

**Requirements:** Python 3.11+, Git, a microphone, `portaudio` (for `pyaudio`).

```bash
sudo apt update
sudo apt install python3 python3-pip git portaudio19-dev -y   # Debian/Ubuntu
# or: sudo dnf install python3 python3-pip git portaudio-devel -y   # Fedora

git clone https://github.com/chreleon/Jarvis.git Jeeves
cd Jeeves

# Skip Windows-only packages (pywin32, win10toast, pycaw, comtypes, pygetwindow) --
# install the rest by hand instead of the full requirements.txt:
pip3 install sounddevice pillow requests beautifulsoup4 duckduckgo-search \
             playwright pyautogui pyperclip opencv-python numpy psutil \
             youtube-transcript-api pyaudio groq faster-whisper piper-tts \
             composio-core composio-openai
```

**Config and voice files:** same as Windows -- create `config/api_keys.json`
with your Groq key, and download the Piper voice into `voices/`.

**Run it:**
```bash
python3 main.py
```

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
             composio-core composio-openai
```

Same `config/api_keys.json` and `voices/` setup as above. Run with:
```bash
python3 main.py
```

---

## 4. Android via Termux (plain, no NetHunter)

Best for a lightweight, always-available setup on a spare phone.

```bash
pkg update && pkg upgrade
pkg install python git portaudio -y

git clone https://github.com/chreleon/Jarvis.git Jeeves
cd Jeeves

pip install flask groq faster-whisper piper-tts composio-core composio-openai
```

Note: many of the desktop-automation packages (`pyautogui`, `pywinauto`,
screen/window control, etc.) either don't apply to or won't install on Android
-- that's expected. Voice chat (listen -> think -> speak) still works fine.

Same config and voice-file steps as above (`config/api_keys.json`, `voices/`).

**Keep it running in the background:**
```bash
termux-wake-lock
python main.py
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

Same config/voices setup as above, then:
```bash
python3 main.py
```

---

## Optional: Composio tool access (GitHub / Gmail / Calendar)

On any of the above platforms, once the base setup works:
```bash
composio login
composio add github
composio add gmail
composio add googlecalendar
```
Each `composio add` opens a browser OAuth flow. Once connected, `composio_agent.py`
lets Jeeves actually act on these accounts, not just talk about them.

---

## Common troubleshooting

- **`ModuleNotFoundError`** -- you're missing a pip install; re-run the
  install line for your platform above.
- **`pip: command not found`** -- use `pip3` instead, or `python3 -m pip`.
- **No sound / mic not detected** -- check your OS's microphone permissions
  for the terminal app you're running Jeeves from.
- **`piper: command not found`** -- confirm `pip install piper-tts` succeeded
  and that your terminal was restarted after install.
