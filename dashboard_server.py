# NOTE: no `from __future__ import annotations` here — FastAPI resolves
# endpoint params like `req: Request` at function-definition time. With
# postponed annotations + the lazy FastAPI import inside _build_app(), the
# `Request` name is NOT in module globals, so FastAPI would misread `req`
# as a query parameter (422) and every dashboard endpoint would break.
import asyncio
import json
import secrets
import socket
import string
import time
from pathlib import Path
from typing import Any

from core.utils import CONFIG_PATH

# NOTE: FastAPI is imported lazily inside _build_app() — it costs ~3.5s of
# cold import time and pulls in starlette/pydantic/uvicorn. The remote
# dashboard is optional (degrades gracefully when FastAPI is missing), so
# the cost is only paid when the server is actually constructed.

PORT = 8000
_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits) if c not in {"O", "I", "L", "0", "1"}]


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_web_pin() -> str:
    cfg = _load_config()
    pin = str(cfg.get("web_pin", "") or "").strip()
    return pin


def _local_ip() -> str:
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.connect((probe, 80))
            ip = sock.getsockname()[0]
            sock.close()
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
        except Exception:
            pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


class DashboardServer:
    def __init__(self):
        self._ip = _local_ip()
        self._tokens: set[str] = set()
        # In-memory only: QR pairing resets when Jeeves restarts (parity with Mark-L).
        self._device_sessions: dict[str, dict] = {}  # device_token → {"key": str}
        self._pending_keys: dict[str, float] = {}
        self._clients: set[Any] = set()
        self._history: list[dict] = []
        self._command_callback = None
        self._wake_callback = None
        self._connect_callback = None
        self._status_provider = None   # optional fn() -> dict merged into /api/status
        self._loop = None              # asyncio loop captured in serve() — for thread-safe pushes
        self._ready = False
        self._web_pin = _load_web_pin()
        self.app = None  # built lazily by _build_app() on first serve()
        self._tip = ""            # cached rotating tip for the dashboard Tip pill
        self._tip_tool = ""       # tool the current tip belongs to (for tutorials)
        self._tip_ts = 0.0
        self._tip_interval = 60.0  # rotate at most once a minute, not per 10s poll

    def _current_tip(self) -> str:
        """Rotating usage tip for the dashboard's Tip pill (cached ~60s).

        Same tip source as the CLI and HUD (config/tool_tips), so all three
        surfaces stay in sync. Lazy import keeps startup light; never raises.
        """
        now = time.time()
        if not self._tip or now - self._tip_ts >= self._tip_interval:
            try:
                from config.tool_tips import random_tip_entry
                self._tip_tool, self._tip = random_tip_entry()
            except Exception:
                self._tip_tool = ""
                self._tip = (
                    "Type a command below — e.g. 'open notepad', "
                    "'search python 3.13', 'play despacito'."
                )
            self._tip_ts = now
        return self._tip

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = "".join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    def get_url(self) -> str:
        return f"http://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        return self.get_url()

    def set_command_callback(self, fn) -> None:
        self._command_callback = fn

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    def set_status_provider(self, fn) -> None:
        """Register a callable returning extra live status for /api/status.

        Called on every dashboard status poll (in the FastAPI thread); must
        be cheap and never raise (exceptions are swallowed).
        """
        self._status_provider = fn

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[Any] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    def push_log(self, speaker: str, text: str, tool: str | None = None) -> None:
        if not text:
            return
        msg: dict = {"type": "log", "speaker": speaker, "text": text}
        if tool:
            msg["tool"] = tool   # lets clients render the line clickable to its tutorial
        asyncio.create_task(self.broadcast(msg))

    def push_log_threadsafe(self, speaker: str, text: str, tool: str | None = None) -> None:
        """push_log from any thread (UI thread, executor threads).

        push_log needs a running asyncio loop in the calling thread; GUI log
        lines arrive from the Qt thread, so schedule the broadcast onto the
        dashboard's own loop instead. No-op until serve() is running.
        """
        if not text:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.push_log, speaker, text, tool)
        except RuntimeError:
            pass  # loop shutting down

    def push_status(self, state: str) -> None:
        asyncio.create_task(self.broadcast({"type": "status", "state": state}))

    def _build_app(self):
        try:
            from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
            from fastapi.responses import HTMLResponse, JSONResponse
        except Exception:
            return None

        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Any) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        LOGIN_HTML = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jeeves Remote</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at top,#08131a,#03070a 62%);color:#e6f7ff;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial}
.card{width:min(92vw,380px);background:rgba(7,15,22,.92);border:1px solid rgba(0,212,255,.18);border-radius:18px;padding:24px;box-shadow:0 20px 60px rgba(0,0,0,.4)}
h1{margin:0 0 6px;font-size:22px;letter-spacing:2px;color:#00d4ff}.sub{margin:0 0 18px;color:#8cb7c8;font-size:13px}
input,button{width:100%;box-sizing:border-box;border-radius:12px;border:1px solid rgba(0,212,255,.15);font-size:16px}
input{padding:14px 12px;background:#061017;color:#e6f7ff;letter-spacing:4px;text-align:center}
button{margin-top:12px;padding:13px;background:linear-gradient(90deg,#00d4ff,#00ff88);color:#001018;font-weight:700;cursor:pointer}
.hint{margin-top:14px;color:#6f8f9d;font-size:12px;line-height:1.5}
.err{color:#ff7b86;margin-top:12px;font-size:13px;min-height:1.2em}
</style></head><body><div class="card"><h1>JEEVES</h1><div class="sub">Remote Dashboard Login</div>
<input id="pin" placeholder="REMOTE KEY" autocomplete="one-time-code" autofocus><button id="go">Enter</button><div class="err" id="err"></div>
<div class="hint">Enter the one-time key shown in the desktop app. Keep this page open for live commands and logs.</div></div>
<script>const p=document.getElementById('pin'),e=document.getElementById('err');
(async()=>{const dev=localStorage.getItem('jeeves_device_token');
if(dev){try{const r=await fetch('/api/device-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_token:dev})});const j=await r.json();if(j.ok){sessionStorage.setItem('jeeves_token',j.token);location.href='/app';return;}}catch(_){}}})();
document.getElementById('go').onclick=async()=>{const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:p.value})});const j=await r.json().catch(()=>({}));if(j.ok){sessionStorage.setItem('jeeves_token',j.token);location.href='/app';}else e.textContent=j.error||'Invalid or expired key';};p.addEventListener('keydown',x=>{if(x.key==='Enter'){x.preventDefault();document.getElementById('go').click();}});</script></body></html>"""

        APP_HTML = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jeeves Remote</title>
<style>
body{margin:0;height:100vh;display:flex;flex-direction:column;background:#05080c;color:#e6f7ff;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial}
.top{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;border-bottom:1px solid rgba(0,212,255,.14);background:linear-gradient(180deg,rgba(9,18,25,.96),rgba(5,8,12,.92))}
.brand{font-weight:800;letter-spacing:2px;color:#00d4ff}.small{font-size:12px;color:#7da0b1}
.wrap{flex:1;display:grid;grid-template-columns:1.2fr .8fr;gap:14px;padding:14px;min-height:0}
.card{background:rgba(8,14,20,.96);border:1px solid rgba(0,212,255,.12);border-radius:16px;min-height:0;display:flex;flex-direction:column}
.head{padding:12px 14px;border-bottom:1px solid rgba(0,212,255,.08);font-size:12px;letter-spacing:1.6px;color:#8fb6c8}
#log{flex:1;overflow:auto;padding:12px}
.msg{margin:0 0 10px;padding:10px 12px;border-radius:12px;white-space:pre-wrap;word-break:break-word}
.user{background:#0c2b38}.jeeves{background:#13212f}.sys{background:#10161d;color:#9fc4d5}.status{background:#091b1a;color:#94f3c1}
.composer{display:flex;gap:10px;padding:12px;border-top:1px solid rgba(0,212,255,.08)}
textarea,input{background:#071018;color:#e6f7ff;border:1px solid rgba(0,212,255,.14);border-radius:12px}
textarea{flex:1;min-height:60px;resize:vertical;padding:12px}
button{border:0;border-radius:12px;padding:12px 14px;background:linear-gradient(90deg,#00d4ff,#00ff88);color:#001018;font-weight:800;cursor:pointer}
.stack{padding:12px;display:grid;gap:10px}.pill{padding:12px;border-radius:14px;background:#09131a;border:1px solid rgba(0,212,255,.09)}
.alert-body{font-size:12px;color:#9fc4d5;margin-top:4px;line-height:1.5;word-break:break-word}
.kv{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:3px 0;color:#aec9d6}
#clear-log{margin:0;padding:4px 10px;font-size:11px;font-weight:700;width:auto;border:1px solid rgba(0,212,255,.25);background:rgba(0,20,28,.6);color:#9fc4d5;border-radius:6px;cursor:pointer}
#clear-log:hover{color:#ff7b86;border-color:#ff7b86}
#pause-chip{font-size:11px;color:#ffcc00}
#tip{cursor:pointer;color:#00d4ff;text-align:right;text-decoration:underline dotted;overflow-wrap:anywhere}
#tip:hover{color:#6ee7ff}
.tip-line{cursor:pointer;text-decoration:underline dotted}
.tip-line:hover{color:#6ee7ff}
.modal{position:fixed;inset:0;background:rgba(2,6,10,.72);display:none;align-items:center;justify-content:center;z-index:50}
.modal-card{width:min(560px,92vw);max-height:82vh;display:flex;flex-direction:column;background:#081420;border:1px solid rgba(0,212,255,.28);border-radius:16px;overflow:hidden;box-shadow:0 24px 70px rgba(0,0,0,.55)}
.modal-card .head{display:flex;justify-content:space-between;align-items:center}
.modal-body{flex:1;overflow:auto;padding:14px;font-size:13px;line-height:1.65;white-space:pre-wrap;word-break:break-word;color:#cfe9f5}
#tut-close{margin:10px 12px;padding:9px 14px;width:auto;align-self:flex-end}
@media (max-width: 900px){.wrap{grid-template-columns:1fr;}.card:first-child{min-height:52vh}}
</style></head><body>
<div class="top"><div><div class="brand">JEEVES REMOTE</div><div class="small" id="ip">Connecting…</div></div><div class="small" id="state">IDLE</div></div>
<div class="wrap"><div class="card"><div class="head" style="display:flex;align-items:center;gap:10px"><span style="flex:1">LIVE LOG</span><span class="small" id="pause-chip" style="display:none">⏸ paused</span><button id="clear-log">Clear</button></div><div id="log"></div><div class="composer"><textarea id="cmd" placeholder="Type a remote command..."></textarea><button id="send">Send</button></div></div>
<div class="card"><div class="head">STATUS</div><div class="stack"><div class="pill"><div class="kv"><span>Provider</span><span id="provider">unknown</span></div><div class="kv"><span>Model</span><span id="model">unknown</span></div><div class="kv"><span>Dashboard</span><span id="url">unknown</span></div></div><div class="pill"><div class="kv"><span>Monitoring</span><span id="monitors">none</span></div><div class="kv"><span>Last alert</span><span id="alert-time">—</span></div><div id="last-alert" class="alert-body">none</div></div><div class="pill"><div class="kv"><span>Tip</span><span id="tip" title="Click for the full tutorial">Loading…</span></div></div></div></div></div>
<div class="modal" id="tut-modal"><div class="modal-card"><div class="head"><span id="tut-title">TOOL TUTORIAL</span><span class="small" style="cursor:pointer" id="tut-x">✕</span></div><div class="modal-body" id="tut-body"></div><button id="tut-close">Close</button></div></div>
<script>
const token=sessionStorage.getItem('jeeves_token');
if(!token) location.href='/login';
const auth={Authorization:'Bearer '+token,'Content-Type':'application/json'};
const log=document.getElementById('log');
const state=document.getElementById('state');
const provider=document.getElementById('provider');
const model=document.getElementById('model');
const url=document.getElementById('url');
const ip=document.getElementById('ip');
const monitors=document.getElementById('monitors');
const lastAlert=document.getElementById('last-alert');
const alertTime=document.getElementById('alert-time');
const tip=document.getElementById('tip');
const tutModal=document.getElementById('tut-modal');
const tutTitle=document.getElementById('tut-title');
const tutBody=document.getElementById('tut-body');
let tipTool='';
let autoScroll=true;
const pauseChip=document.getElementById('pause-chip');
function esc(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
async function openTut(tool){if(!tool)return;try{const r=await fetch('/api/tutorial?name='+encodeURIComponent(tool),{headers:auth});const j=await r.json();tutTitle.textContent='TOOL TUTORIAL — '+(j.name||tool);tutBody.textContent=(j&&j.tutorial)||("No tutorial for '"+(j.name||tool)+"'.");tutModal.style.display='flex';}catch(_){}}
function append(cls,who,text,tool){const d=document.createElement('div');d.className='msg '+cls;if(tool){d.innerHTML='<strong>'+esc(who)+'</strong><br><span class="tip-line">'+esc(text)+'</span>';d.querySelector('.tip-line').onclick=()=>openTut(tool);}else{d.innerHTML='<strong>'+esc(who)+'</strong><br>'+esc(text);}log.appendChild(d);if(autoScroll)log.scrollTop=log.scrollHeight;}
log.addEventListener('scroll',()=>{const nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;autoScroll=nearBottom;pauseChip.style.display=autoScroll?'none':'inline';});
function fmtAlert(a){if(!a)return 'none';const first=(String(a).split('\\n')[0]||'').replace(/^\\[[^\\]]*\\]\\s*/,'').trim();return first.slice(0,140)||'none';}
async function refresh(){const r=await fetch('/api/status',{headers:auth});const j=await r.json();ip.textContent=j.url||'';provider.textContent=j.provider||'unknown';model.textContent=j.model||'unknown';url.textContent=j.url||'unknown';state.textContent=j.state||'IDLE';monitors.textContent=(j.monitors&&j.monitors.length)?j.monitors.join(', ').slice(0,90):'none';lastAlert.textContent=fmtAlert(j.last_alert);alertTime.textContent=j.last_alert_at?new Date(j.last_alert_at*1000).toLocaleTimeString():'—';tip.textContent=j.tip||'Use the desktop app to generate a fresh key';tipTool=j.tip_tool||'';}
function wsConnect(){const proto=location.protocol==='https:'?'wss':'ws';const ws=new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.type==='clear'){log.innerHTML='';autoScroll=true;pauseChip.style.display='none';}if(m.type==='log') append(m.speaker||'sys',m.speaker||'SYS',m.text||'',m.tool||''); if(m.type==='status') state.textContent=m.state||'IDLE'; if(m.type==='sys') append('sys','SYS',m.text||''); if(m.type==='status') state.textContent=m.state||'IDLE';};ws.onclose=()=>setTimeout(wsConnect,1000);}
document.getElementById('clear-log').onclick=async()=>{try{await fetch('/api/clear-log',{method:'POST',headers:auth});}catch(_){}};
function closeTut(){tutModal.style.display='none';}
tip.onclick=()=>openTut(tipTool);
document.getElementById('tut-close').onclick=closeTut;
document.getElementById('tut-x').onclick=closeTut;
document.getElementById('send').onclick=async()=>{const t=document.getElementById('cmd');const txt=t.value.trim();if(!txt)return;t.value='';await fetch('/api/command',{method:'POST',headers:auth,body:JSON.stringify({text:txt})});};
document.getElementById('cmd').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();document.getElementById('send').click();}});
refresh();wsConnect();setInterval(refresh,10000);
</script></body></html>"""

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(LOGIN_HTML)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return HTMLResponse(APP_HTML)

        @app.get("/app", response_class=HTMLResponse)
        async def app_page():
            """Alias used as the redirect target after login / QR pairing."""
            return HTMLResponse(APP_HTML)

        @app.post("/login")
        async def login(req: Request):
            if not self._web_pin:
                return JSONResponse({"ok": False, "error": "web_pin is missing in config/api_keys.json"}, status_code=400)
            body = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                if self._connect_callback:
                    self._connect_callback()
                await self.broadcast({"type": "sys", "text": "Remote connection established."})
                return JSONResponse({"ok": True, "token": tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"}, status_code=401)

        _EXPIRED_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#05080c;color:#e6f7ff;font-family:system-ui;text-align:center">
<div><h2 style="color:#ff7b86">Link Expired</h2>
<p style="color:#7da0b1">Press <strong style="color:#e6f7ff">REMOTE DASHBOARD</strong> in the Jeeves desktop app to get a new QR code.</p></div></body></html>"""

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates a one-time key, pairs the device, redirects to the app."""
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse(_EXPIRED_HTML)

            del self._pending_keys[key]   # one-time use
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._device_sessions[dev_tok] = {"key": key}   # persistent pairing

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#05080c;color:#e6f7ff;font-family:system-ui;text-align:center">
<p style="color:#7da0b1">Connecting to Jeeves…</p>
<script>
  sessionStorage.setItem('jeeves_token', '{tok}');
  localStorage.setItem('jeeves_device_token', '{dev_tok}');
  setTimeout(function(){{ location.replace('/app'); }}, 300);
</script></body></html>""")

        @app.post("/api/device-login")
        async def device_login(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = str(body.get("device_token", "") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.get("/api/status")
        async def status(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            payload = {
                "ok": True, "url": self.get_url(), "state": "online",
                "provider": "jeeves", "model": "jeeves",
                "tip": self._current_tip(),
                "tip_tool": self._tip_tool,
            }
            if self._status_provider is not None:
                try:
                    extra = self._status_provider() or {}
                    if isinstance(extra, dict):
                        payload.update(extra)
                except Exception:
                    pass   # live status must never break the dashboard
            return JSONResponse(payload)

        @app.get("/api/tutorial")
        async def tutorial(req: Request, name: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from config.tool_tips import tool_tutorial
            return JSONResponse({"ok": True, "name": name, "tutorial": tool_tutorial(name)})

        @app.post("/api/clear-log")
        async def clear_log(req: Request):
            """Clear the dashboard's live log for every connected client."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            # Broadcast first, then wipe: broadcast() appends to _history,
            # and a reconnect should replay a clean slate, not the clear msg.
            await self.broadcast({"type": "clear"})
            self._history.clear()
            return JSONResponse({"ok": True})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            text = str(body.get("text", "")).strip()
            if text and self._command_callback:
                self._command_callback(text)
            if text and self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            await self.broadcast({"type": "sys", "text": "Wake signal received."})
            return JSONResponse({"ok": True})

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "command":
                        text = str(data.get("text", "")).strip()
                        if text and self._command_callback:
                            self._command_callback(text)
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    async def serve(self) -> None:
        self._loop = asyncio.get_running_loop()   # for push_log_threadsafe
        if self.app is None:
            self.app = self._build_app()
        if self.app is None:
            print("[Dashboard] fastapi/uvicorn not installed — remote dashboard disabled.")
            return
        import importlib

        uvicorn = importlib.import_module("uvicorn")
        cfg = uvicorn.Config(self.app, host="0.0.0.0", port=PORT, log_level="warning")
        print(f"[Dashboard] http://{self._ip}:{PORT}")
        await uvicorn.Server(cfg).serve()