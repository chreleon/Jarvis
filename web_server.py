"""
web_server.py -- Lightweight web/mobile front door for Jeeves.

Lets you talk to Jeeves from any browser (phone included), on your local
network or, combined with a Cloudflare Tunnel, from anywhere -- without
touching the existing desktop voice loop in main.py (that keeps working
exactly as before; this is an additional, separate way in).

Flow:
    Browser records your voice -> POSTs WAV audio to /api/message
    -> stt_engine transcribes it -> or_client (Groq) replies
    -> tts_engine speaks the reply -> browser plays it back

Run:
    py web_server.py
Then either open http://localhost:5000 yourself, or point a Cloudflare
Tunnel at port 5000 to get a public HTTPS URL reachable from your phone.

PIN protection: set "web_pin" in config/api_keys.json, e.g.:
    { "web_pin": "1234", ... }
"""

import io
import json
import secrets
import wave
import base64
from pathlib import Path

from flask import Flask, request, session, redirect, url_for, jsonify, render_template_string

import stt_engine
import tts_engine
from or_client import client as brain_client

BASE_DIR = Path(__file__).resolve().parent
API_KEY_PATH = BASE_DIR / "config" / "api_keys.json"


def _load_web_pin() -> str:
    try:
        with open(API_KEY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        pin = str(data.get("web_pin", "")).strip()
        if not pin:
            raise ValueError("web_pin is empty or missing in config/api_keys.json")
        return pin
    except FileNotFoundError:
        raise RuntimeError(f"config/api_keys.json not found at: {API_KEY_PATH}")


app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # regenerates each restart -- logs everyone out on reboot, by design

WEB_PIN = _load_web_pin()

LOGIN_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jeeves</title>
<style>
body{font-family:sans-serif;background:#111;color:#eee;display:flex;height:100vh;
     align-items:center;justify-content:center;margin:0}
form{background:#1c1c1c;padding:2rem;border-radius:12px;text-align:center}
input{font-size:1.5rem;padding:.5rem;border-radius:8px;border:none;width:8rem;text-align:center}
button{font-size:1.2rem;padding:.5rem 1.5rem;margin-top:1rem;border-radius:8px;border:none;
       background:#4a7;color:white}
.err{color:#f66}
</style></head><body>
<form method="POST">
  <h2>Jeeves</h2>
  <input type="password" name="pin" placeholder="PIN" autofocus>
  <br><button type="submit">Enter</button>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
</form></body></html>
"""

APP_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jeeves</title>
<style>
body{font-family:sans-serif;background:#111;color:#eee;display:flex;flex-direction:column;
     height:100vh;margin:0;padding:1rem;box-sizing:border-box}
#log{flex:1;overflow-y:auto;margin-bottom:1rem}
.msg{margin:.5rem 0;padding:.6rem 1rem;border-radius:10px;max-width:80%}
.user{background:#2a5;margin-left:auto;text-align:right}
.jeeves{background:#333}
#micBtn{font-size:1.1rem;padding:1rem;border-radius:50%;width:5rem;height:5rem;border:none;
        background:#c33;color:white;align-self:center}
#micBtn.recording{background:#f66;animation:pulse 1s infinite}
@keyframes pulse{0%{opacity:1}50%{opacity:.5}100%{opacity:1}}
#status{text-align:center;color:#999;margin-bottom:.5rem}
</style></head><body>
<div id="log"></div>
<div id="status">Tap the mic and speak</div>
<button id="micBtn">&#127908;</button>
<audio id="player" style="display:none"></audio>
<script>
const log = document.getElementById('log');
const micBtn = document.getElementById('micBtn');
const status = document.getElementById('status');
const player = document.getElementById('player');

function addMsg(text, who) {
  const d = document.createElement('div');
  d.className = 'msg ' + who;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

let audioCtx, stream, processor, source, chunks = [], recording = false;

async function startRecording() {
  stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
  source = audioCtx.createMediaStreamSource(stream);
  processor = audioCtx.createScriptProcessor(4096, 1, 1);
  chunks = [];
  processor.onaudioprocess = e => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  source.connect(processor);
  processor.connect(audioCtx.destination);
  recording = true;
  micBtn.classList.add('recording');
  status.textContent = 'Listening...';
}

function floatTo16BitPCM(float32Arr) {
  const buf = new ArrayBuffer(float32Arr.length * 2);
  const view = new DataView(buf);
  let offset = 0;
  for (let i = 0; i < float32Arr.length; i++, offset += 2) {
    let s = Math.max(-1, Math.min(1, float32Arr[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return buf;
}

function encodeWAV(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.byteLength);
  const view = new DataView(buffer);
  const writeStr = (o, s) => { for (let i=0;i<s.length;i++) view.setUint8(o+i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF'); view.setUint32(4, 36 + samples.byteLength, true); writeStr(8, 'WAVE');
  writeStr(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  writeStr(36, 'data'); view.setUint32(40, samples.byteLength, true);
  new Uint8Array(buffer, 44).set(new Uint8Array(samples));
  return buffer;
}

async function stopRecording() {
  recording = false;
  micBtn.classList.remove('recording');
  processor.disconnect(); source.disconnect();
  stream.getTracks().forEach(t => t.stop());

  const totalLen = chunks.reduce((a, c) => a + c.length, 0);
  const merged = new Float32Array(totalLen);
  let off = 0;
  for (const c of chunks) { merged.set(c, off); off += c.length; }
  const pcm16 = floatTo16BitPCM(merged);
  const wavBuf = encodeWAV(pcm16, 16000);

  status.textContent = 'Thinking...';
  const blob = new Blob([wavBuf], { type: 'audio/wav' });
  const form = new FormData();
  form.append('audio', blob, 'speech.wav');

  const res = await fetch('/api/message', { method: 'POST', body: form });
  const data = await res.json();
  if (data.error) { status.textContent = 'Error: ' + data.error; return; }

  addMsg(data.heard_text, 'user');
  addMsg(data.reply_text, 'jeeves');
  status.textContent = 'Tap the mic and speak';

  if (data.audio_base64) {
    player.src = 'data:audio/wav;base64,' + data.audio_base64;
    player.play();
  }
}

micBtn.addEventListener('click', () => { recording ? stopRecording() : startRecording(); });
</script>
</body></html>
"""


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("pin", "") == WEB_PIN:
            session["authed"] = True
            return redirect(url_for("app_page"))
        return render_template_string(LOGIN_PAGE, error="Wrong PIN")
    if session.get("authed"):
        return redirect(url_for("app_page"))
    return render_template_string(LOGIN_PAGE, error=None)


@app.route("/app")
def app_page():
    if not session.get("authed"):
        return redirect(url_for("login"))
    return render_template_string(APP_PAGE)


@app.route("/api/message", methods=["POST"])
def api_message():
    if not session.get("authed"):
        return jsonify({"error": "Not authenticated"}), 401

    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "No audio received"}), 400

    try:
        with wave.open(io.BytesIO(audio_file.read()), "rb") as wf:
            pcm_bytes = wf.readframes(wf.getnframes())
            sample_rate = wf.getframerate()

        heard_text = stt_engine.transcribe_pcm16(pcm_bytes, sample_rate)
        if not heard_text:
            return jsonify({"error": "Didn't catch that -- try again"}), 200

        reply_text = brain_client.chat(heard_text)

        wav_bytes = tts_engine.synthesize_to_wav_bytes(reply_text)
        audio_b64 = base64.b64encode(wav_bytes).decode("utf-8")

        return jsonify({
            "heard_text": heard_text,
            "reply_text": reply_text,
            "audio_base64": audio_b64,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("[Jeeves Web] Starting on http://0.0.0.0:5000 ...")
    print("[Jeeves Web] Point a Cloudflare Tunnel at port 5000 for remote access.")
    app.run(host="0.0.0.0", port=5000, debug=False)
