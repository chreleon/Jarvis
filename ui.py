from __future__ import annotations

import json
import math
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from PyQt6.QtCore import (
    QEasingCurve, QMimeData, QObject, QPointF, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QFontDatabase,
    QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton, QProgressBar,
    QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from core.utils import get_base_dir, BASE_DIR, CONFIG_PATH as API_FILE, subprocess_no_window_kwargs
from config.tool_tips import tool_tutorial, random_tip_entry

# JARVIS-style holo orb renderer (pure QPainter, no heavy deps).
from holo_orb import draw_holo_orb

_DEFAULT_W, _DEFAULT_H = 1100, 750
_MIN_W,     _MIN_H     = 900, 620
_LEFT_W  = 160
_RIGHT_W = 420  # Expanded from 300 for better info density

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


# ── Color themes ─────────────────────────────────────────────────────────
THEMES = {
    "jarvis": {
        "BG": "#040d1a", "PANEL": "#051222", "PANEL2": "#061527",
        "BORDER": "#0a3852", "BORDER_B": "#0c7495", "BORDER_A": "#082f49",
        "PRI": "#0c7495", "PRI_DIM": "#085a73", "PRI_GHO": "#061e34",
        "ACC": "#47b5d9", "ACC2": "#0f7e9d",
        "GREEN": "#0c9e7a", "GREEN_D": "#087a5c",
        "RED": "#e05555", "MUTED_C": "#cc4466",
        "TEXT": "#8ec8d8", "TEXT_DIM": "#3a6e80", "TEXT_MED": "#5a9ab0",
        "WHITE": "#c8e8f0", "DARK": "#030e18", "BAR_BG": "#071c30",
        "GLOW_BRIGHT": "#6dd5ed", "GLOW_DIM": "#2193b0",
    },
    "ironman": {
        "BG": "#0d0204", "PANEL": "#1a0808", "PANEL2": "#200c0c",
        "BORDER": "#3d1a1a", "BORDER_B": "#cc3333", "BORDER_A": "#4a1a1a",
        "PRI": "#cc3333", "PRI_DIM": "#992222", "PRI_GHO": "#330808",
        "ACC": "#ffcc00", "ACC2": "#ff9900",
        "GREEN": "#33cc33", "GREEN_D": "#229922",
        "RED": "#ff4444", "MUTED_C": "#cc4466",
        "TEXT": "#ddaa88", "TEXT_DIM": "#664422", "TEXT_MED": "#aa7744",
        "WHITE": "#ffddcc", "DARK": "#080000", "BAR_BG": "#1a0808",
        "GLOW_BRIGHT": "#ff6644", "GLOW_DIM": "#cc3322",
    },
    "winter_soldier": {
        "BG": "#040810", "PANEL": "#081020", "PANEL2": "#0a1428",
        "BORDER": "#1a2a44", "BORDER_B": "#4488cc", "BORDER_A": "#1a2840",
        "PRI": "#4488cc", "PRI_DIM": "#336699", "PRI_GHO": "#0a1830",
        "ACC": "#66aaff", "ACC2": "#3377cc",
        "GREEN": "#44aa88", "GREEN_D": "#338866",
        "RED": "#cc4444", "MUTED_C": "#884466",
        "TEXT": "#aaccdd", "TEXT_DIM": "#335566", "TEXT_MED": "#5588aa",
        "WHITE": "#ddeeff", "DARK": "#020610", "BAR_BG": "#081420",
        "GLOW_BRIGHT": "#66bbff", "GLOW_DIM": "#3388cc",
    },
    "matrix": {
        "BG": "#000a00", "PANEL": "#001400", "PANEL2": "#001a00",
        "BORDER": "#003300", "BORDER_B": "#00cc00", "BORDER_A": "#002200",
        "PRI": "#00cc00", "PRI_DIM": "#009900", "PRI_GHO": "#002200",
        "ACC": "#00ff44", "ACC2": "#00cc22",
        "GREEN": "#00ff00", "GREEN_D": "#00cc00",
        "RED": "#ff0000", "MUTED_C": "#cc4444",
        "TEXT": "#00ff44", "TEXT_DIM": "#006600", "TEXT_MED": "#00aa22",
        "WHITE": "#44ff44", "DARK": "#000400", "BAR_BG": "#001400",
        "GLOW_BRIGHT": "#44ff44", "GLOW_DIM": "#00cc00",
    },
}

_active_theme = "jarvis"


def set_theme(name: str):
    """Switch the active color theme."""
    global _active_theme
    if name in THEMES:
        _active_theme = name
        for k, v in THEMES[name].items():
            setattr(C, k, v)


def get_themes() -> list[str]:
    """Return list of available theme names."""
    return list(THEMES.keys())


def get_current_theme() -> str:
    return _active_theme


class C:
    # Default: jarvis.institute palette — deep navy-teal backgrounds,
    # muted teal/cyan accents, teal-green highlights.
    BG        = "#040d1a"
    PANEL     = "#051222"
    PANEL2    = "#061527"
    BORDER    = "#0a3852"
    BORDER_B  = "#0c7495"
    BORDER_A  = "#082f49"
    PRI       = "#0c7495"
    PRI_DIM   = "#085a73"
    PRI_GHO   = "#061e34"
    ACC       = "#47b5d9"
    ACC2      = "#0f7e9d"
    GREEN     = "#0c9e7a"
    GREEN_D   = "#087a5c"
    RED       = "#e05555"
    MUTED_C   = "#cc4466"
    TEXT      = "#8ec8d8"
    TEXT_DIM  = "#3a6e80"
    TEXT_MED  = "#5a9ab0"
    WHITE     = "#c8e8f0"
    DARK      = "#030e18"
    BAR_BG    = "#071c30"

    # Tool category colors (JARVIS-style)
    TOOL_FILE   = "#0c9e7a"  # green-teal
    TOOL_WEB    = "#47b5d9"  # bright cyan
    TOOL_PHONE  = "#9d7aff"  # purple-blue
    TOOL_CODE   = "#ffaa44"  # amber
    TOOL_SYSTEM = "#0c7495"  # primary teal
    TOOL_COMM   = "#66d9ef"  # cyan-white

    # Data visualization
    DATA_POS    = "#22c55e"  # success green
    DATA_NEG    = "#ef4444"  # alert red
    DATA_NEUTRAL= "#64748b"  # slate

    # Glow effects
    GLOW_BRIGHT = "#6dd5ed"  # bright glow
    GLOW_DIM    = "#2193b0"  # dim glow


# Tool categories for radial HUD ring (12 segments, like a clock)
# Defined here (not at module top) because it references C.TOOL_* colors.
_TOOL_CATEGORIES = [
    {"name": "FILES",    "tools": ["file_processor", "file_controller"], "color": C.TOOL_FILE,   "angle": 0},
    {"name": "WEB",      "tools": ["web_search", "browser_control"],     "color": C.TOOL_WEB,    "angle": 30},
    {"name": "COMMS",    "tools": ["send_message", "secretary"],          "color": C.TOOL_COMM,   "angle": 60},
    {"name": "PHONE",    "tools": ["phone_control"],                      "color": C.TOOL_PHONE,  "angle": 90},
    {"name": "VISION",   "tools": ["screen_process"],                     "color": C.TOOL_WEB,    "angle": 120},
    {"name": "MEDIA",    "tools": ["youtube_video", "anime_watch"],       "color": C.TOOL_CODE,   "angle": 150},
    {"name": "CODE",     "tools": ["code_helper", "dev_agent"],           "color": C.TOOL_CODE,   "angle": 180},
    {"name": "SYSTEM",   "tools": ["system_status", "computer_control"],  "color": C.TOOL_SYSTEM, "angle": 210},
    {"name": "DESKTOP",  "tools": ["desktop_control", "open_app"],        "color": C.TOOL_SYSTEM, "angle": 240},
    {"name": "SETTINGS", "tools": ["computer_settings"],                  "color": C.TOOL_SYSTEM, "angle": 270},
    {"name": "FINANCE",  "tools": ["business_tracker", "daily_briefing"], "color": C.GREEN,       "angle": 300},
    {"name": "MONITOR",  "tools": ["manage_monitor", "system_monitor"],   "color": C.ACC2,        "angle": 330},
]


_APP_ICON: "QIcon | None" = None


def _app_icon() -> QIcon:
    """The Jeeves app icon (jeeves.ico), cached; empty QIcon if missing."""
    global _APP_ICON
    if _APP_ICON is None:
        icon_path = BASE_DIR / "jeeves.ico"
        _APP_ICON = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
    return _APP_ICON


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── YinYang Opt: GPU/temp detection cached (re-probe every 15s, not 1.5s) ──
class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0
        self.gpu  = -1.0
        self.tmp  = -1.0
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._last_gpu_t = 0.0           # YinYang: cache GPU probes
        self._last_tmp_t = 0.0            # YinYang: cache temp probes
        self._cached_gpu = -1.0           # YinYang
        self._cached_tmp = -1.0           # YinYang
        self._gpu_probe_interval = 15.0   # YinYang: every 15s instead of 1.5s
        self._tmp_probe_interval = 15.0   # YinYang
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        # YinYang: only re-probe GPU/temp every 15s, not every 1.5s
        if now - self._last_gpu_t >= self._gpu_probe_interval:
            self._cached_gpu = self._get_gpu()
            self._last_gpu_t = now
        gpu = self._cached_gpu

        if now - self._last_tmp_t >= self._tmp_probe_interval:
            self._cached_tmp = self._get_temp()
            self._last_tmp_t = now
        tmp = self._cached_tmp

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        """GPU probe — only called every 15s now instead of every 1.5s."""
        # NVIDIA — the no_window kwargs stop a console window from flashing
        # every probe (this runs every 15s while the HUD is up).
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
                **subprocess_no_window_kwargs(),
            )
            if r.returncode == 0:
                vals = [float(v.strip()) for v in r.stdout.strip().split("\n") if v.strip()]
                if vals:
                    return sum(vals) / len(vals)
        except Exception:
            pass

        if _OS == "Linux":
            # AMD (Linux)
            try:
                r = subprocess.run(
                    ["rocm-smi", "--showuse", "--csv"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    for line in r.stdout.strip().split("\n"):
                        parts = line.split(",")
                        if len(parts) >= 2:
                            try:
                                return float(parts[1].strip().replace("%", ""))
                            except ValueError:
                                pass
            except Exception:
                pass

            # Intel GPU (Linux)
            try:
                r = subprocess.run(
                    ["intel_gpu_top", "-J", "-s", "500"],
                    capture_output=True, text=True, timeout=1
                )
                if r.returncode == 0 and "Render/3D" in r.stdout:
                    import re
                    m = re.search(r'"busy":\s*([\d.]+)', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        # macOS
        if _OS == "Darwin":
            try:
                r = subprocess.run(
                    ["sudo", "-n", "powermetrics", "-n", "1", "-i", "500",
                     "--samplers", "gpu_power"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0 and "GPU" in r.stdout:
                    import re
                    m = re.search(r'GPU\s+Active:\s+([\d.]+)%', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        return -1.0

    def _get_temp(self) -> float:
        """Temperature probe — only called every 15s now."""
        try:
            temps = psutil.sensors_temperatures()
            candidates = ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                          "cpu-thermal", "zenpower", "it8688"]
            for name in candidates:
                if name in temps:
                    entries = temps[name]
                    if entries:
                        return entries[0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        if _OS == "Darwin":
            try:
                r = subprocess.run(
                    ["osx-cpu-temp"], capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    import re
                    m = re.search(r"([\d.]+)", r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        if _OS == "Windows":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi).CurrentTemperature"],
                    capture_output=True, text=True, timeout=3,
                    **subprocess_no_window_kwargs(),
                )
                if r.returncode == 0 and r.stdout.strip():
                    raw = float(r.stdout.strip().split("\n")[0])
                    return (raw / 10.0) - 273.15
            except Exception:
                pass

        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()


# ── HudCanvas with YinYang optimizations ────────────────────────────────
class HudCanvas(QWidget):
    """Arc-reactor-style HUD canvas.

    YinYang optimizations applied:
      1. Static geometry (grid, ticks, crosshair, brackets) cached to offscreen QPixmap
      2. Face pixmap scaling cached — only re-scale when widget size changes
      3. Adaptive frame rate — idle runs at ~20fps, speaking at 60fps
      4. Pre-computed waveform heights — random() moved out of paintEvent
    """

    # Emits the masked face PNG bytes when the background loader finishes.
    _face_ready_sig = pyqtSignal(bytes)

    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"

        # HUD center visual: 'face' (default) or 'holo' (JARVIS wireframe
        # orb). Opt-in via config/api_keys.json: {"hud_style": "holo"}.
        # The face orb is untouched when unset — nothing is removed.
        self.holo_mode = False
        try:
            cfg = json.loads(API_FILE.read_text(encoding="utf-8"))
            self.holo_mode = str(cfg.get("hud_style", "face")).lower() == "holo"
        except Exception:
            self.holo_mode = False

        # ── YinYang: per-frame cached values ──
        self._tick            = 0
        self._scale           = 1.0
        self._tgt_scale       = 1.0
        self._halo            = 55.0
        self._tgt_halo        = 55.0
        self._last_t          = time.time()
        self._scan            = 0.0
        self._scan2           = 180.0
        self._rings           = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink           = True
        self._blink_tick      = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None

        # ── JARVIS enhancements: tool ring, data pulses ──
        self._active_tools: set[str] = set()  # Currently active tool names
        self._last_tool_time: dict[str, float] = {}  # tool -> last activation time
        self._data_pulses: list[dict] = []  # Animated pulses along circuit traces
        self._tool_ring_glow: dict[int, float] = {}  # segment_idx -> glow_intensity

        # ── Ripple effects (tool activation) ──
        self._ripples: list[dict] = []  # {radius, alpha, max_radius}

        # ── Enhanced particle density ──
        self._ambient_particles: list[list[float]] = []  # persistent ambient dots

        # ── YinYang: cached face scaling ──
        self._cached_fsz    = 0
        self._cached_face   = QPixmap()

        # ── YinYang: cached static background pixmap ──
        self._static_cache: QPixmap | None = None
        self._static_cache_size = (0, 0)

        # ── YinYang: pre-computed waveform heights ──
        self._waveform_heights: list[int] = []
        self._waveform_valid = False

        # ── YinYang: adaptive frame rate ──
        self._speaking_frame_interval = 16    # ~60 fps when speaking
        self._idle_frame_interval     = 50    # ~20 fps when idle
        self._last_frame_w = 0
        self._last_frame_h = 0

        self._face_ready_sig.connect(self._apply_face)
        self._load_face(face_path)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(self._speaking_frame_interval if self.speaking else self._idle_frame_interval)

    def _load_face(self, path: str):
        """Load + circular-mask the HUD face off the UI thread.

        PIL and the image ops cost ~0.3s; doing them in a thread lets the
        window appear immediately (the orb fallback paints until the face
        arrives, then _apply_face swaps it in).
        """
        def _work():
            try:
                from PIL import Image, ImageDraw
                import io
                img = Image.open(path).convert("RGBA")
                sz  = min(img.size)
                img = img.resize((sz, sz), Image.LANCZOS)
                mk  = Image.new("L", (sz, sz), 0)
                ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
                img.putalpha(mk)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                self._face_ready_sig.emit(buf.getvalue())
            except Exception:
                self._face_ready_sig.emit(b"")
        threading.Thread(target=_work, daemon=True).start()

    def _apply_face(self, data: bytes):
        """Set the loaded face pixmap on the UI thread and repaint."""
        if data:
            px = QPixmap()
            if px.loadFromData(data) and not px.isNull():
                self._face_px = px
        self._cached_fsz    = 0          # force re-scale on next paint
        self._cached_face   = QPixmap()
        self.update()

    # ── YinYang: cache invalidation on resize ──
    def set_active_tool(self, tool_name: str):
        """Mark a tool as active (for radial ring animation + ripple)."""
        self._active_tools.add(tool_name)
        self._last_tool_time[tool_name] = time.time()
        # Find which segment this tool belongs to
        for idx, cat in enumerate(_TOOL_CATEGORIES):
            if tool_name in cat["tools"]:
                self._tool_ring_glow[idx] = 1.0  # Full glow
                break
        # Spawn ripple effect
        fw = min(self.width(), self.height())
        self._ripples.append({"radius": fw * 0.30, "alpha": 1.0})

    def clear_active_tool(self, tool_name: str):
        """Remove a tool from active set."""
        self._active_tools.discard(tool_name)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._static_cache = None
        self._cached_fsz = 0

    def _update_frame_rate(self):
        """YinYang: adjust timer interval based on speaking state."""
        target = self._speaking_frame_interval if self.speaking else self._idle_frame_interval
        if self._tmr.interval() != target:
            self._tmr.setInterval(target)

    def _step(self):
        self._tick += 1
        now = time.time()
        changed = False
        fw = min(self.width(), self.height())

        # ── YinYang: track widget size for cache invalidation ──
        w, h = self.width(), self.height()
        size_changed = (w != self._last_frame_w or h != self._last_frame_h)
        self._last_frame_w, self._last_frame_h = w, h

        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            else:
                self._tgt_scale = random.uniform(1.001, 1.008)
                self._tgt_halo  = random.uniform(48, 68)
            self._last_t = now
            changed = True

        old_scale = self._scale
        old_halo  = self._halo
        sp = 0.38 if self.speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp
        if abs(self._scale - old_scale) > 0.0001 or abs(self._halo - old_halo) > 0.01:
            changed = True

        speeds = [1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9]
        for i, spd in enumerate(speeds):
            old = self._rings[i]
            self._rings[i] = (self._rings[i] + spd) % 360
            if abs(self._rings[i] - old) > 0.01:
                changed = True

        self._scan  = (self._scan  + (3.0 if self.speaking else 1.3)) % 360
        self._scan2 = (self._scan2 + (-2.0 if self.speaking else -0.75)) % 360

        lim = fw * 0.74
        spd = 4.2 if self.speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if self.speaking else 0.025):
            self._pulses.append(0.0)

        if self.speaking and random.random() < 0.28:
            cx, cy = w / 2, h / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.28
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.028]
            for p in self._particles if p[4] > 0
        ]
        if self._particles:
            changed = True

        # ── YinYang: pre-compute waveform heights outside paintEvent ──
        if self.muted:
            self._waveform_heights = [2] * 36
        elif self.speaking:
            self._waveform_heights = [random.randint(3, 20) for _ in range(36)]
        else:
            self._waveform_heights = [
                int(3 + 2 * math.sin(self._tick * 0.09 + i * 0.6)) for i in range(36)
            ]
        self._waveform_valid = True

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
            changed = True

        # ── JARVIS: tool ring glow decay ──
        for idx in list(self._tool_ring_glow.keys()):
            old_glow = self._tool_ring_glow[idx]
            self._tool_ring_glow[idx] = max(0, old_glow - 0.02)
            if self._tool_ring_glow[idx] == 0:
                del self._tool_ring_glow[idx]
            elif abs(old_glow - self._tool_ring_glow[idx]) > 0.001:
                changed = True

        # ── JARVIS: data pulse animation ──
        if self.speaking or len(self._active_tools) > 0:
            if random.random() < 0.15:  # Spawn new pulse
                angle = random.uniform(0, 2 * math.pi)
                self._data_pulses.append({
                    "angle": angle,
                    "radius": fw * 0.32,
                    "alpha": 1.0,
                    "speed": random.uniform(0.8, 1.5),
                })
        # Update existing pulses
        self._data_pulses = [
            {**p, "radius": p["radius"] + p["speed"], "alpha": p["alpha"] - 0.015}
            for p in self._data_pulses
            if p["alpha"] > 0 and p["radius"] < fw * 0.60
        ]
        if self._data_pulses:
            changed = True

        # ── Ripple effects (expand outward on tool activation) ──
        self._ripples = [
            {**r, "radius": r["radius"] + 3.0, "alpha": r["alpha"] - 0.02}
            for r in self._ripples if r["alpha"] > 0
        ]
        if self._ripples:
            changed = True

        # ── Ambient particles (persistent floating dots) ──
        if len(self._ambient_particles) < 12 and random.random() < 0.03:
            _cx = w / 2  # use widget center (w, h already computed above)
            _cy = h / 2
            ang = random.uniform(0, 2 * math.pi)
            rad = fw * random.uniform(0.35, 0.55)
            self._ambient_particles.append([
                _cx + math.cos(ang) * rad, _cy - math.sin(ang) * rad,
                random.uniform(-0.3, 0.3), random.uniform(-0.3, 0.3), 0.6,
            ])
        self._ambient_particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.99, p[3]*0.99, p[4]-0.003]
            for p in self._ambient_particles if p[4] > 0
        ]
        if self._ambient_particles:
            changed = True

        # ── YinYang: only update() if something actually changed ──
        if changed or size_changed:
            self.update()

        # ── YinYang: adapt frame rate ──
        self._update_frame_rate()

    # ── YinYang: cache static background to offscreen QPixmap ──
    def _draw_radial_tool_ring(self, p: QPainter, cx: float, cy: float, fw: int):
        """Draw the JARVIS-style radial tool status ring (12 segments)."""
        ring_outer = fw * 0.62
        ring_inner = fw * 0.58
        segment_arc = 28  # degrees per segment (30° with 2° gap)

        for idx, cat in enumerate(_TOOL_CATEGORIES):
            angle = cat["angle"]
            base_alpha = 60

            # Check if any tool in this category is active
            glow = self._tool_ring_glow.get(idx, 0.0)
            if glow > 0:
                base_alpha = int(60 + glow * 160)

            col = qcol(cat["color"], base_alpha)
            p.setPen(QPen(col, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)

            rect = QRectF(cx - ring_outer, cy - ring_outer, ring_outer * 2, ring_outer * 2)
            p.drawArc(rect, int((angle - 90) * 16), int(segment_arc * 16))

            # Draw segment label (small text)
            if glow > 0.3 or not self.speaking:  # Show labels when active or idle
                label_angle = math.radians(angle + segment_arc / 2)
                label_r = (ring_outer + ring_inner) / 2
                label_x = cx + label_r * math.cos(label_angle)
                label_y = cy - label_r * math.sin(label_angle)

                p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
                text_col = qcol(cat["color"], min(255, base_alpha + 40))
                p.setPen(QPen(text_col, 1))

                # Draw text rotated
                p.save()
                p.translate(label_x, label_y)
                if 90 < angle < 270:  # Flip text for bottom half
                    p.rotate(angle - 180)
                else:
                    p.rotate(angle)
                p.drawText(QRectF(-20, -8, 40, 16), Qt.AlignmentFlag.AlignCenter, cat["name"])
                p.restore()

    def _draw_corner_data_panels(self, p: QPainter, W: int, H: int, fw: int):
        """Draw JARVIS-style dense data panels around the orb."""
        panel_bg = qcol(C.PANEL2, 180)
        panel_border = qcol(C.BORDER_B, 120)

        # ── Top-left: CPU cores (mini bar graph) ──
        px, py, pw, ph = 12, 12, 110, 70
        p.setBrush(QBrush(panel_bg)); p.setPen(QPen(panel_border, 1))
        p.drawRoundedRect(QRectF(px, py, pw, ph), 4, 4)
        p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(px+4, py+3, pw-8, 10), Qt.AlignmentFlag.AlignLeft, "CPU CORES")
        try:
            per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            per_cpu = []
        bar_w = max(3, (pw - 12) // max(1, len(per_cpu)))
        for i, val in enumerate(per_cpu[:8]):
            bx = px + 4 + i * (bar_w + 1)
            bh = int((ph - 22) * val / 100)
            by = py + ph - 6 - bh
            col = qcol(C.RED if val > 85 else (C.ACC if val > 65 else C.GREEN), 200)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(col))
            p.drawRect(QRectF(bx, by, bar_w - 1, bh))
        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol(C.PRI), 1))
        avg = sum(per_cpu) / max(1, len(per_cpu)) if per_cpu else 0
        p.drawText(QRectF(px+4, py+3, pw-8, 10), Qt.AlignmentFlag.AlignRight, f"{avg:.0f}%")

        # ── Top-right: Disk usage ──
        dx, dy, dw, dh = W - 122, 12, 110, 50
        p.setBrush(QBrush(panel_bg)); p.setPen(QPen(panel_border, 1))
        p.drawRoundedRect(QRectF(dx, dy, dw, dh), 4, 4)
        p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(dx+4, dy+3, dw-8, 10), Qt.AlignmentFlag.AlignLeft, "DISK")
        try:
            disk = psutil.disk_usage("/" if _OS != "Windows" else "C:\\")
            disk_pct = disk.percent
            disk_free = disk.free / (1024**3)
            p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            col = qcol(C.RED if disk_pct > 90 else (C.ACC if disk_pct > 75 else C.GREEN))
            p.setPen(QPen(col, 1))
            p.drawText(QRectF(dx+4, dy+16, dw-8, 16), Qt.AlignmentFlag.AlignCenter, f"{disk_pct:.0f}%")
            p.setFont(QFont("Courier New", 6))
            p.setPen(QPen(qcol(C.TEXT_DIM), 1))
            p.drawText(QRectF(dx+4, dy+33, dw-8, 12), Qt.AlignmentFlag.AlignCenter, f"{disk_free:.1f}GB free")
        except Exception:
            p.setPen(QPen(qcol(C.TEXT_DIM), 1))
            p.drawText(QRectF(dx+4, dy+20, dw-8, 16), Qt.AlignmentFlag.AlignCenter, "N/A")

        # ── Middle-left: Battery (if present) ──
        bat = psutil.sensors_battery()
        if bat:
            bx2, by2, bw2, bh2 = 12, 88, 110, 42
            p.setBrush(QBrush(panel_bg)); p.setPen(QPen(panel_border, 1))
            p.drawRoundedRect(QRectF(bx2, by2, bw2, bh2), 4, 4)
            p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
            p.setPen(QPen(qcol(C.TEXT_DIM), 1))
            p.drawText(QRectF(bx2+4, by2+3, bw2-8, 10), Qt.AlignmentFlag.AlignLeft, "BATTERY")
            bat_pct = bat.percent
            bat_state = "⚡" if bat.power_plugged else "🔋"
            p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            bcol = qcol(C.GREEN if bat_pct > 50 else (C.ACC if bat_pct > 20 else C.RED))
            p.setPen(QPen(bcol, 1))
            p.drawText(QRectF(bx2+4, by2+15, bw2-8, 16), Qt.AlignmentFlag.AlignCenter, f"{bat_state} {bat_pct:.0f}%")

        # ── Middle-right: GPU (if available) ──
        gx, gy, gw, gh = W - 122, 68, 110, 42
        p.setBrush(QBrush(panel_bg)); p.setPen(QPen(panel_border, 1))
        p.drawRoundedRect(QRectF(gx, gy, gw, gh), 4, 4)
        p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(gx+4, gy+3, gw-8, 10), Qt.AlignmentFlag.AlignLeft, "GPU")
        gpu_val = _metrics.snapshot().get("gpu", -1)
        if gpu_val >= 0:
            p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            gcol = qcol(C.RED if gpu_val > 85 else (C.ACC if gpu_val > 65 else C.GREEN))
            p.setPen(QPen(gcol, 1))
            p.drawText(QRectF(gx+4, gy+15, gw-8, 16), Qt.AlignmentFlag.AlignCenter, f"{gpu_val:.0f}%")
        else:
            p.setPen(QPen(qcol(C.TEXT_DIM), 1))
            p.drawText(QRectF(gx+4, gy+15, gw-8, 16), Qt.AlignmentFlag.AlignCenter, "N/A")

        # ── Bottom-left: Process count + threads ──
        bx3, by3, bw3, bh3 = 12, H - 56, 110, 44
        p.setBrush(QBrush(panel_bg)); p.setPen(QPen(panel_border, 1))
        p.drawRoundedRect(QRectF(bx3, by3, bw3, bh3), 4, 4)
        p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(bx3+4, by3+3, bw3-8, 10), Qt.AlignmentFlag.AlignLeft, "PROCESSES")
        try:
            proc_count = len(psutil.pids())
            p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            p.setPen(QPen(qcol(C.PRI), 1))
            p.drawText(QRectF(bx3+4, by3+15, bw3-8, 16), Qt.AlignmentFlag.AlignCenter, str(proc_count))
            uptime_s = time.time() - psutil.boot_time()
            p.setFont(QFont("Courier New", 6))
            p.setPen(qcol(C.TEXT_DIM))
            p.drawText(QRectF(bx3+4, by3+31, bw3-8, 10), Qt.AlignmentFlag.AlignCenter,
                       f"UP {int(uptime_s//3600)}h {int((uptime_s%3600)//60)}m")
        except Exception:
            pass

        # ── Bottom-right: Network detail ──
        nx, ny, nw, nh = W - 122, H - 56, 110, 44
        p.setBrush(QBrush(panel_bg)); p.setPen(QPen(panel_border, 1))
        p.drawRoundedRect(QRectF(nx, ny, nw, nh), 4, 4)
        p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(nx+4, ny+3, nw-8, 10), Qt.AlignmentFlag.AlignLeft, "NETWORK")
        try:
            nc = psutil.net_io_counters()
            sent_mb = nc.bytes_sent / (1024**2)
            recv_mb = nc.bytes_recv / (1024**2)
            p.setFont(QFont("Courier New", 7))
            p.setPen(QPen(qcol(C.GREEN), 1))
            p.drawText(QRectF(nx+4, ny+15, nw-8, 10), Qt.AlignmentFlag.AlignCenter, f"↑{sent_mb:.0f}MB")
            p.setPen(QPen(qcol(C.ACC), 1))
            p.drawText(QRectF(nx+4, ny+27, nw-8, 10), Qt.AlignmentFlag.AlignCenter, f"↓{recv_mb:.0f}MB")
        except Exception:
            pass

        # ── Hexagon tech ring around center (bottom of orb) ──
        hx, hy = W // 2, H // 2
        p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI, 60), 1))
        # Small hex at bottom
        hex_r = 16
        hex_pts = []
        for i in range(6):
            a = math.radians(i * 60 - 30)
            hex_pts.append(QPointF(hx + hex_r * math.cos(a), H - 100 + hex_r * math.sin(a)))
        for i in range(6):
            p.drawLine(hex_pts[i], hex_pts[(i + 1) % 6])
        p.drawText(QRectF(hx - 20, H - 105, 40, 12), Qt.AlignmentFlag.AlignCenter, "35")

    def _draw_data_pulses(self, p: QPainter, cx: float, cy: float):
        """Draw animated data pulses flowing along circuit traces."""
        for pulse in self._data_pulses:
            r = pulse["radius"]
            angle = pulse["angle"]
            alpha = int(pulse["alpha"] * 200)

            x = cx + r * math.cos(angle)
            y = cy - r * math.sin(angle)

            col = qcol(C.GLOW_BRIGHT if self.speaking else C.PRI, alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col))

            # Draw pulse as small circle with glow
            pulse_size = 3 if self.speaking else 2
            p.drawEllipse(QPointF(x, y), pulse_size, pulse_size)

            # Outer glow
            outer_col = qcol(C.GLOW_BRIGHT if self.speaking else C.PRI, alpha // 3)
            p.setBrush(QBrush(outer_col))
            p.drawEllipse(QPointF(x, y), pulse_size + 2, pulse_size + 2)

    def _draw_ripples(self, p: QPainter, cx: float, cy: float):
        """Draw expanding ripple rings on tool activation."""
        for r in self._ripples:
            rad = r["radius"]
            alpha = int(r["alpha"] * 180)
            if alpha < 2:
                continue
            col = qcol(C.ACC, alpha)
            p.setPen(QPen(col, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rad, rad)

    def _draw_ambient_particles(self, p: QPainter):
        """Draw persistent ambient floating dots for atmosphere."""
        for pt in self._ambient_particles:
            a = max(0, min(255, int(pt[4] * 120)))
            if a < 3:
                continue
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI_DIM, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 1.5, 1.5)

    # ── YinYang: cache static background to offscreen QPixmap ──
    def _ensure_static_cache(self, p: QPainter, W: int, H: int, fw: int):
        """Render static geometry (grid, ticks, crosshair, brackets, circuit traces) to a cached pixmap."""
        if (self._static_cache is not None
                and self._static_cache_size == (W, H)
                and not self.speaking
                and self._halo < 100):
            p.drawPixmap(0, 0, self._static_cache)
            return

        cx, cy = W / 2, H / 2

        # ── YinYang: only rebuild cache when geometry changes ──
        if (self._static_cache is None
                or self._static_cache_size != (W, H)
                or self.speaking):
            cache = QPixmap(W, H)
            cache.fill(Qt.GlobalColor.transparent)
            cp = QPainter(cache)
            cp.setRenderHint(QPainter.RenderHint.Antialiasing)

            # grid dots
            cp.setPen(QPen(qcol(C.PRI_GHO), 1))
            for x in range(0, W, 48):
                for y in range(0, H, 48):
                    cp.drawPoint(x, y)

            # ── Circuit traces: radiating lines from center with right-angle bends (Jarvis-style) ──
            trace_col = qcol(C.PRI, 35)
            cp.setPen(QPen(trace_col, 1))
            import random as _rnd
            _rnd.seed(42)  # deterministic pattern
            for i in range(24):  # Increased from 16 to 24 for more density
                angle = _rnd.uniform(0, 2 * math.pi)
                r1 = fw * _rnd.uniform(0.35, 0.40)
                r2 = fw * _rnd.uniform(0.44, 0.56)
                x1 = cx + r1 * math.cos(angle)
                y1 = cy - r1 * math.sin(angle)
                # Right-angle bend: go horizontal then vertical (or vice versa)
                bend_x = cx + r2 * math.cos(angle)
                bend_y = cy - r2 * math.sin(angle)
                if i % 2 == 0:
                    cp.drawLine(QPointF(x1, y1), QPointF(bend_x, y1))
                    cp.drawLine(QPointF(bend_x, y1), QPointF(bend_x, bend_y))
                else:
                    cp.drawLine(QPointF(x1, y1), QPointF(x1, bend_y))
                    cp.drawLine(QPointF(x1, bend_y), QPointF(bend_x, bend_y))

                # Small node dot at the end
                cp.setBrush(QBrush(qcol(C.PRI, 50)))
                cp.setPen(Qt.PenStyle.NoPen)
                cp.drawEllipse(QPointF(bend_x, bend_y), 2, 2)
                cp.setPen(QPen(trace_col, 1))
                cp.setBrush(Qt.BrushStyle.NoBrush)

                # Additional connecting traces (more complex network)
                if i % 3 == 0:
                    r3 = fw * _rnd.uniform(0.48, 0.54)
                    next_angle = angle + _rnd.uniform(0.3, 0.8)
                    x3 = cx + r3 * math.cos(next_angle)
                    y3 = cy - r3 * math.sin(next_angle)
                    cp.setPen(QPen(qcol(C.PRI, 20), 1))
                    cp.drawLine(QPointF(bend_x, bend_y), QPointF(x3, y3))
                    cp.setPen(QPen(trace_col, 1))

            # ── Hexagonal tech ring around orb ──
            hex_r = fw * 0.33
            cp.setPen(QPen(qcol(C.PRI, 50), 1))
            hex_pts = []
            for i in range(6):
                a = math.radians(i * 60 - 30)
                hex_pts.append(QPointF(cx + hex_r * math.cos(a), cy - hex_r * math.sin(a)))
            for i in range(6):
                cp.drawLine(hex_pts[i], hex_pts[(i + 1) % 6])

            # tick marks
            t_out, t_in = fw * 0.497, fw * 0.474
            cp.setPen(QPen(qcol(C.PRI, 140), 1))
            for deg in range(0, 360, 10):
                rad = math.radians(deg)
                inn = t_in if deg % 30 == 0 else t_in + 6
                cp.drawLine(
                    QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                    QPointF(cx + inn  * math.cos(rad), cy - inn  * math.sin(rad)),
                )

            # crosshair
            ch_r, gap_h = fw * 0.51, fw * 0.16
            cp.setPen(QPen(qcol(C.PRI, 140), 1))
            cp.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
            cp.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
            cp.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
            cp.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

            # ── Corner brackets (larger, more HUD-like) ──
            bl = 32
            bc = qcol(C.PRI, 210)
            hl, hr = cx - fw // 2, cx + fw // 2
            ht, hb = cy - fw // 2, cy + fw // 2
            cp.setPen(QPen(bc, 2))
            for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
                cp.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
                cp.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))
                # Inner accent line
                cp.setPen(QPen(qcol(C.PRI, 80), 1))
                cp.drawLine(QPointF(bx + dx * 4, by + dy * 4),
                           QPointF(bx + dx * (bl - 8), by + dy * 4))
                cp.drawLine(QPointF(bx + dx * 4, by + dy * 4),
                           QPointF(bx + dx * 4, by + dy * (bl - 8)))
                cp.setPen(QPen(bc, 2))

            # ── Data panel corners (HUD text boxes like Jarvis) ──
            cp.setPen(QPen(qcol(C.PRI, 60), 1))
            # Top-left data box
            bx, by = 16, 16
            bw, bh = 90, 40
            cp.drawRect(QRectF(bx, by, bw, bh))
            cp.setPen(QPen(qcol(C.PRI, 40), 1))
            cp.drawLine(QPointF(bx, by + 14), QPointF(bx + bw, by + 14))
            # Bottom-right data box
            bx2, by2 = W - 106, H - 56
            cp.setPen(QPen(qcol(C.PRI, 60), 1))
            cp.drawRect(QRectF(bx2, by2, bw, bh))
            cp.setPen(QPen(qcol(C.PRI, 40), 1))
            cp.drawLine(QPointF(bx2, by2 + 14), QPointF(bx2 + bw, by2 + 14))

            cp.end()
            self._static_cache = cache
            self._static_cache_size = (W, H)

        p.drawPixmap(0, 0, self._static_cache)

        # ── YinYang: crosshair halo (alpha-only, needs per-frame update) ──
        h_alpha = int(self._halo * 0.5)
        if h_alpha > 5:
            ch_r, gap_h = fw * 0.51, fw * 0.16
            p.setPen(QPen(qcol(C.PRI, h_alpha), 1))
            p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
            p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
            p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
            p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # ── YinYang: draw cached static layer ──
        self._ensure_static_cache(p, W, H, int(fw))

        # ── JARVIS: Corner data panels ──
        self._draw_corner_data_panels(p, W, H, int(fw))

        # ── JARVIS: Data pulses (before orb, behind everything) ──
        self._draw_data_pulses(p, cx, cy)

        r_face = fw * 0.31

        # halo glow (alpha varies per frame — skip rings too faint to see;
        # at idle halo~50 the outer 7 rings have alpha < 3, i.e. invisible)
        for i in range(10):
            frc = 1.0 - i / 10
            a   = max(0, min(255, int(self._halo * 0.085 * frc)))
            if a < 3:
                continue
            r   = r_face * (1.8 - i * 0.08)
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # pulse rings
        for pr in self._pulses:
            a   = max(0, int(230 * (1.0 - pr / (fw * 0.74))))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # spinning arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = fw * r_frac
            base   = self._rings[idx]
            a_val  = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            col    = qcol(C.MUTED_C if self.muted else C.PRI, a_val)
            p.setPen(QPen(col, w_r)); p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect  = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        # scanners
        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.5))
        ex = 75 if self.speaking else 44
        p.setPen(QPen(qcol(C.MUTED_C if self.muted else C.PRI, sa), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 2), 1.5))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # ── YinYang: cached face scaling ──
        if self.holo_mode:
            # JARVIS wireframe holo-orb in the center (arc-reactor rings,
            # halo and scanners all stay — only the center visual swaps).
            draw_holo_orb(
                p, cx, cy, fw * 0.30 * self._scale,
                t=self._tick * 0.033, speaking=self.speaking,
                muted=self.muted,
            )
        elif self._face_px:
            fsz = int(fw * 0.62 * self._scale)
            if fsz != self._cached_fsz or self._cached_face.isNull():
                self._cached_fsz = fsz
                self._cached_face = self._face_px.scaled(
                    fsz, fsz,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            p.drawPixmap(int(cx - fsz / 2), int(cy - fsz / 2), self._cached_face)
        else:
            orb_r = int(fw * 0.27 * self._scale)
            oc    = (200, 0, 50) if self.muted else (0, 60, 110)
            for i in range(8, 0, -1):
                r2  = int(orb_r * i / 8)
                frc = i / 8
                a   = max(0, min(255, int(self._halo * 1.1 * frc)))
                p.setBrush(QBrush(QColor(int(oc[0]*frc), int(oc[1]*frc), int(oc[2]*frc), a)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
            p.setPen(QPen(qcol(C.PRI, min(255, int(self._halo * 2))), 1))
            p.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
            p.drawText(QRectF(cx - 80, cy - 14, 160, 28),
                       Qt.AlignmentFlag.AlignCenter, "NEURAL")

        # particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.5, 2.5)

        # ambient particles
        self._draw_ambient_particles(p)

        # ripple effects
        self._draw_ripples(p, cx, cy)

        # ── JARVIS: Radial tool status ring ──
        self._draw_radial_tool_ring(p, cx, cy, int(fw))

        # ── Status display with glow backdrop ──
        sy = cy + fw * 0.42
        if self.muted:
            txt, col = "⊘  MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  PROCESSING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  ANALYSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        # Glow backdrop behind status text
        glow_a = max(0, min(60, int(self._halo * 0.3)))
        if glow_a > 5:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI, glow_a // 3)))
            p.drawRoundedRect(QRectF(W / 2 - 80, sy - 2, 160, 24), 4, 4)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        # ── Enhanced waveform with mirror reflection ──
        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        heights = self._waveform_heights if self._waveform_valid and len(self._waveform_heights) == N else [2] * N
        for i in range(N):
            hgt = heights[i]
            if self.muted:
                cl = qcol(C.MUTED_C)
            elif self.speaking:
                cl = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
            else:
                cl = qcol(C.BORDER_B)
            # Main bar
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)
            # Mirror reflection (faint, below the baseline)
            if hgt > 4:
                ref_col = qcol(C.PRI if not self.muted else C.MUTED_C, 25)
                p.fillRect(QRectF(wx0 + i * bw, wy + 20, bw - 1, min(hgt // 3, 6)), ref_col)
        # Baseline glow line
        p.setPen(QPen(qcol(C.PRI, 40), 1))
        p.drawLine(QPointF(wx0, wy + 20), QPointF(wx0 + N * bw, wy + 20))


# ── MetricBar (no changes needed — already efficient) ──
class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)


# ── RadialGauge: JARVIS-style circular metric gauge ──
class RadialGauge(QWidget):
    """Circular gauge widget for displaying metrics (JARVIS speedometer style)."""

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0
        self._text = "--"
        self._target_value = 0.0
        self.setFixedSize(70, 70)

    def set_value(self, pct: float, text: str):
        """Update gauge value with smooth animation."""
        self._target_value = max(0.0, min(100.0, pct))
        self._text = text
        # Smooth interpolation happens in paintEvent
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        radius = min(W, H) / 2 - 4

        # Smooth value interpolation for animation
        if abs(self._value - self._target_value) > 0.5:
            self._value += (self._target_value - self._value) * 0.15
            QTimer.singleShot(16, self.update)  # Continue animating

        # Background circle
        p.setPen(QPen(qcol(C.BORDER_A), 2))
        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))

        # Value arc (speedometer style: -140° to +140°, 280° range)
        start_angle = 220  # Bottom-left
        span_angle = int((self._value / 100) * 280)

        # Determine color based on value
        if self._value > 85:
            arc_color = qcol(C.RED)
        elif self._value > 65:
            arc_color = qcol(C.ACC)
        else:
            arc_color = qcol(self._color)

        p.setPen(QPen(arc_color, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        arc_rect = QRectF(cx - radius + 3, cy - radius + 3, (radius - 3) * 2, (radius - 3) * 2)
        p.drawArc(arc_rect, start_angle * 16, -span_angle * 16)

        # Glow effect for high values
        if self._value > 80:
            glow_alpha = int((self._value - 80) * 6)
            p.setPen(QPen(qcol(arc_color, glow_alpha), 5))
            p.drawArc(arc_rect, start_angle * 16, -span_angle * 16)

        # Center value text
        p.setPen(QPen(arc_color if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, cy - 8, W, 16), Qt.AlignmentFlag.AlignCenter, self._text)

        # Label at bottom
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        p.drawText(QRectF(0, cy + 12, W, 12), Qt.AlignmentFlag.AlignCenter, self._label)


# ── ToolStatusWidget: Shows active/recent tools ──
class ToolStatusWidget(QWidget):
    """Displays recently used tools with status indicators."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(100)
        self._tools: dict[str, dict] = {}  # tool_name -> {status, time, color}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(2)

        # Header
        header = QLabel("◈ ACTIVE TOOLS")
        header.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        header.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._layout.addWidget(header)

        # Scroll area for tool status
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{ background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px; }}
            QScrollBar:vertical {{ background: {C.BG}; width: 6px; }}
            QScrollBar::handle:vertical {{ background: {C.BORDER_B}; border-radius: 3px; }}
        """)

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(3)
        self._content_layout.addStretch()

        self._scroll.setWidget(self._content)
        self._layout.addWidget(self._scroll)

    def add_tool(self, tool_name: str, status: str = "running"):
        """Add or update a tool status."""
        colors = {
            "running": C.GREEN,
            "completed": C.ACC2,
            "cooldown": C.ACC,
            "error": C.RED,
        }

        self._tools[tool_name] = {
            "status": status,
            "time": time.time(),
            "color": colors.get(status, C.TEXT_MED)
        }
        self._rebuild()

    def remove_tool(self, tool_name: str):
        """Remove a tool from the status list."""
        if tool_name in self._tools:
            del self._tools[tool_name]
            self._rebuild()

    def _rebuild(self):
        """Rebuild the tool status list."""
        # Clear existing
        while self._content_layout.count() > 1:  # Keep the stretch
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Add tools sorted by most recent
        sorted_tools = sorted(self._tools.items(),
                            key=lambda x: x[1]["time"],
                            reverse=True)[:5]  # Show max 5

        for tool_name, info in sorted_tools:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(2, 2, 2, 2)
            row_layout.setSpacing(4)

            # Status dot
            dot = QLabel("●")
            dot.setFont(QFont("Courier New", 8))
            dot.setStyleSheet(f"color: {info['color']}; background: transparent;")
            row_layout.addWidget(dot)

            # Tool name
            name_label = QLabel(tool_name[:15])  # Truncate long names
            name_label.setFont(QFont("Courier New", 7))
            name_label.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            row_layout.addWidget(name_label, stretch=1)

            # Time ago
            elapsed = int(time.time() - info["time"])
            if elapsed < 60:
                time_str = f"{elapsed}s"
            else:
                time_str = f"{elapsed // 60}m"

            time_label = QLabel(time_str)
            time_label.setFont(QFont("Courier New", 6))
            time_label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            row_layout.addWidget(time_label)

            self._content_layout.insertWidget(self._content_layout.count() - 1, row)


# ── MetricBar (no changes needed — already efficient) ──
class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)


# ── YinYang: batched LogWidget typewriter ──
class LogWidget(QTextEdit):
    """Typewriter-style log widget.

    YinYang optimization: batch characters (write 8 per tick instead of 1).
    This reduces QTextCursor operations by ~8x and eliminates per-character signal overhead.

    Tool tips ("TIP: ..." lines, appended via append_tip) are rendered as
    clickable links carrying jeeves://tool/<name>; clicking one emits
    tipClicked(tool_name) so the HUD can open the full tutorial in the
    content panel. Clicks are detected with QTextEdit.anchorAt (the
    anchorClicked signal was removed from QTextEdit in Qt 6.11).
    """
    _sig = pyqtSignal(str)
    tipClicked = pyqtSignal(str)   # tool name of the clicked tip link

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self._tip_tool: str = ""   # tool name for the next queued TIP line
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._batch_size = 8  # YinYang: batch size for typewriter
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def append_tip(self, text: str, tool: str):
        """Log a tool tip as a clickable link to the tool's tutorial."""
        self._tip_tool = tool or ""
        self.append_log(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        if   tl.startswith("you:"):    self._tag = "you"
        elif tl.startswith("jeeves:"): self._tag = "ai"
        elif tl.startswith("file:"):   self._tag = "file"
        elif tl.startswith("tip:"):    self._tag = "tip"
        elif "err" in tl:              self._tag = "err"
        else:                          self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        """YinYang: write up to batch_size characters per tick instead of 1."""
        if self._pos >= len(self._text):
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)
            return

        # ── YinYang: batch characters ──
        cur = self.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        fmt = cur.charFormat()
        col = {
            "you":  qcol(C.WHITE),
            "ai":   qcol(C.PRI),
            "err":  qcol(C.RED),
            "file": qcol(C.GREEN),
            "tip":  qcol("#ffcc66"),
            "sys":  qcol(C.ACC2),
        }.get(self._tag, qcol(C.TEXT))
        fmt.setForeground(QBrush(col))
        if self._tag == "tip" and self._tip_tool:
            # Whole line becomes a link: clicking opens the tool tutorial.
            fmt.setAnchor(True)
            fmt.setAnchorHref(f"jeeves://tool/{self._tip_tool}")
            fmt.setFontUnderline(True)

        end = min(self._pos + self._batch_size, len(self._text))
        chunk = self._text[self._pos:end]
        cur.insertText(chunk, fmt)
        self.setTextCursor(cur)
        self.ensureCursorVisible()
        self._pos = end
        if self._pos >= len(self._text):
            self._tip_tool = ""   # link consumed — reset for the next line

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if e.button() != Qt.MouseButton.LeftButton:
            return
        href = self.anchorAt(e.position().toPoint())
        if href.startswith("jeeves://tool/"):
            self.tipClicked.emit(href[len("jeeves://tool/"):])


_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


# ── YinYang: FileDropZone with frozen animation when idle ──
class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        # ── YinYang: only run animation when needed ──
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        # Don't start timer until hover/drag happens
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _ensure_animation(self, running: bool):
        """YinYang: start/stop animation timer based on hover/drag state."""
        if running and not self._anim_tmr.isActive():
            self._anim_tmr.start(40)
        elif not running and self._anim_tmr.isActive():
            self._anim_tmr.stop()

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True
            self._ensure_animation(True)
            self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False
        self._ensure_animation(self._hovering)
        self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        self._ensure_animation(False)
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True
        self._ensure_animation(True)
        self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False
        self._ensure_animation(self._drag_over)
        self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None
        self._ensure_animation(False)
        self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JEEVES", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._ensure_animation(False)
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#082e44" if z._drag_over else ("#071e30" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#1a5565"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#1a5565"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class SetupOverlay(QWidget):
    """Setup overlay — no performance changes needed (shown once at startup)."""
    done = pyqtSignal(str, str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  NEURAL COMMAND SETUP", 13, True))
        layout.addWidget(_lbl("Configure J.E.E.V.E.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("BRAIN PROVIDER", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        provider_row = QHBoxLayout(); provider_row.setSpacing(6)
        self._provider_btns: dict[str, QPushButton] = {}
        for key, label in [("groq", "Groq"), ("github_models", "GitHub Models")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel_provider(k))
            provider_row.addWidget(btn)
            self._provider_btns[key] = btn
        layout.addLayout(provider_row)
        self._sel_provider("groq")
        layout.addSpacing(8)

        self._provider_hint = _lbl(
            "Groq uses a free Groq key. GitHub Models uses a GitHub token with Models access.",
            7, color=C.PRI_DIM, align=Qt.AlignmentFlag.AlignLeft
        )
        layout.addWidget(self._provider_hint)

        self._groq_key_label = _lbl("GROQ API KEYS (ONE OR MORE)", 8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._groq_key_label)
        self._groq_key_input = QLineEdit()
        self._groq_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._groq_key_input.setPlaceholderText("gsk_…  (separate multiple with commas)")
        self._groq_key_input.setFont(QFont("Courier New", 10))
        self._groq_key_input.setFixedHeight(32)
        self._groq_key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #061828; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._groq_key_input)
        layout.addSpacing(8)

        self._groq_help = _lbl("Get free keys at console.groq.com/keys — add as many as you like; Jeeves rotates between them on rate limits", 7, color=C.PRI_DIM, align=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._groq_help)

        self._github_key_label = _lbl("GITHUB TOKEN", 8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._github_key_label)
        self._github_key_input = QLineEdit()
        self._github_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._github_key_input.setPlaceholderText("ghp_… or github_pat_…")
        self._github_key_input.setFont(QFont("Courier New", 10))
        self._github_key_input.setFixedHeight(32)
        self._github_key_input.setStyleSheet(self._groq_key_input.styleSheet())
        layout.addWidget(self._github_key_input)

        self._github_help = _lbl("Use a GitHub token with Models access. Copilot access alone is not a runtime API.", 7, color=C.PRI_DIM, align=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._github_help)
        self._refresh_provider_ui()
        layout.addSpacing(8)

        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep3)
        layout.addSpacing(4)

        layout.addWidget(_lbl("VOICE MODEL", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        # Lazy import: voice_downloader pulls urllib/ssl (slow cold-start);
        # it's only needed inside this optional setup overlay.
        import voice_downloader
        self._voice_status = _lbl(
            "Present" if voice_downloader.voice_model_present() else "Not downloaded yet",
            7, color=(C.GREEN if voice_downloader.voice_model_present() else C.ACC2),
            align=Qt.AlignmentFlag.AlignLeft
        )
        layout.addWidget(self._voice_status)
        self._voice_btn = QPushButton("⬇  DOWNLOAD VOICE FILES")
        self._voice_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._voice_btn.setFixedHeight(28)
        self._voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._voice_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; }}
        """)
        self._voice_btn.clicked.connect(self._download_voice)
        layout.addWidget(self._voice_btn)
        layout.addSpacing(8)

        layout.addWidget(_lbl("CONNECT ACCOUNTS (OPTIONAL)", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        connect_row = QHBoxLayout(); connect_row.setSpacing(4)
        for app_key, label in [("github", "GitHub"), ("gmail", "Gmail"), ("googlecalendar", "Calendar")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            btn.setFixedHeight(26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #061828; color: {C.TEXT_MED};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.PRI}; border: 1px solid {C.BORDER_B}; }}
            """)
            btn.clicked.connect(lambda _, k=app_key: self._connect_app(k))
            connect_row.addWidget(btn)
        layout.addLayout(connect_row)
        self._connect_status = _lbl("", 7, color=C.PRI_DIM, align=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._connect_status)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#082e44"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#082e1a")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #061828; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _sel_provider(self, key: str):
        self._sel_provider_key = key
        pal = {"groq": (C.PRI, "#082e44"), "github_models": (C.GREEN, "#082e1a")}
        for k, btn in self._provider_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #061828; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)
        self._refresh_provider_ui()

    def _refresh_provider_ui(self):
        is_groq = getattr(self, "_sel_provider_key", "groq") == "groq"
        # Widgets may not exist yet: _sel_provider() is invoked from __init__
        # before the key inputs are created, so skip any that aren't built yet.
        # _refresh_provider_ui() is called again once all widgets exist.
        for attr, visible in (
            ("_groq_key_label", is_groq),
            ("_groq_key_input", is_groq),
            ("_groq_help", is_groq),
            ("_github_key_label", not is_groq),
            ("_github_key_input", not is_groq),
            ("_github_help", not is_groq),
        ):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setVisible(visible)

    def _download_voice(self):
        self._voice_btn.setEnabled(False)
        self._voice_status.setText("Downloading...")
        self._voice_status.setStyleSheet(f"color: {C.ACC2}; background: transparent;")

        def _status(msg):
            self._voice_status.setText(msg)

        def _done(ok):
            self._voice_btn.setEnabled(True)
            color = C.GREEN if ok else C.RED
            self._voice_status.setStyleSheet(f"color: {color}; background: transparent;")
            self._voice_status.setText("Voice files ready." if ok else "Download failed -- check connection.")

        # Lazy import: same reason as above.
        import voice_downloader
        voice_downloader.download_voice_model_async(_status, _done)

    def _connect_app(self, app_key: str):
        self._connect_status.setText(f"Connecting {app_key}...")

        def _status(msg):
            self._connect_status.setText(msg)

        # Lazy import: the Composio SDK chain (composio_connect ->
        # composio_shim -> composio_openai -> openai SDK) costs ~13s of
        # cold import time and hundreds of MB of loaded modules. It's only
        # needed when the user actually clicks a Connect button here, so
        # defer it to that moment instead of paying it on every launch.
        import composio_connect
        composio_connect.connect_app_async(app_key, _status)

    def _submit(self):
        provider = getattr(self, "_sel_provider_key", "groq")
        groq_keys = [
            k.strip() for k in re.split(r"[,;\n]+", self._groq_key_input.text()) if k.strip()
        ]
        github_key = self._github_key_input.text().strip()

        if provider == "groq" and not groq_keys:
            self._groq_key_input.setStyleSheet(
                self._groq_key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return

        if provider == "github_models" and not github_key:
            self._github_key_input.setStyleSheet(
                self._github_key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return

        # Pass all Groq keys (comma-joined) so _on_setup_done can merge them
        # into any keys already stored in config.
        self.done.emit(provider, ",".join(groq_keys), github_key, self._sel_os)


class SettingsOverlay(QWidget):
    """Settings panel for HUD customization (theme, animation speed, etc.)."""

    _OW, _OH = 440, 520
    theme_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SettingsOverlay {{
                background: rgba(0, 6, 14, 0.96);
                border: 1px solid {C.BORDER_B};
                border-radius: 12px;
            }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 16, 22, 16)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        def _btn(txt, color=C.PRI, bg=C.PANEL):
            b = QPushButton(txt)
            b.setFixedHeight(30)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {bg}; color: {color};
                    border: 1px solid {C.PRI_DIM}; border-radius: 5px;
                }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            return b

        lay.addWidget(_lbl("◈  SETTINGS", 12, True))
        lay.addWidget(_lbl("Customize the HUD appearance", 8, color=C.TEXT_DIM))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── Theme Selection ──
        lay.addWidget(_lbl("COLOR THEME", 7, True, C.ACC2, Qt.AlignmentFlag.AlignLeft))
        theme_row = QHBoxLayout(); theme_row.setSpacing(4)
        self._theme_btns: dict[str, QPushButton] = {}
        current = get_current_theme()
        for name in get_themes():
            btn = _btn(name.upper())
            btn.clicked.connect(lambda _, n=name: self._set_theme(n))
            theme_row.addWidget(btn)
            self._theme_btns[name] = btn
        lay.addLayout(theme_row)
        self._set_theme(current)

        # ── HUD Style ──
        lay.addSpacing(6)
        lay.addWidget(_lbl("HUD STYLE", 7, True, C.ACC2, Qt.AlignmentFlag.AlignLeft))
        style_row = QHBoxLayout(); style_row.setSpacing(4)
        self._style_btns: dict[str, QPushButton] = {}
        for style in ["minimal", "standard", "dense"]:
            btn = _btn(style.upper())
            btn.clicked.connect(lambda _, s=style: self._set_style(s))
            style_row.addWidget(btn)
            self._style_btns[style] = btn
        lay.addLayout(style_row)
        self._set_style("standard")

        # ── Animation Speed ──
        lay.addSpacing(6)
        lay.addWidget(_lbl("ANIMATION SPEED", 7, True, C.ACC2, Qt.AlignmentFlag.AlignLeft))
        speed_row = QHBoxLayout(); speed_row.setSpacing(4)
        self._speed_btns: dict[str, QPushButton] = {}
        for speed, label in [("off", "OFF"), ("slow", "SLOW"), ("normal", "NORMAL"), ("fast", "FAST")]:
            btn = _btn(label)
            btn.clicked.connect(lambda _, s=speed: self._set_speed(s))
            speed_row.addWidget(btn)
            self._speed_btns[speed] = btn
        lay.addLayout(speed_row)
        self._set_speed("normal")

        # ── Panel Opacity ──
        lay.addSpacing(6)
        lay.addWidget(_lbl("PANEL OPACITY", 7, True, C.ACC2, Qt.AlignmentFlag.AlignLeft))
        self._opacity_slider = QProgressBar()
        self._opacity_slider.setFixedHeight(20)
        self._opacity_slider.setRange(30, 100)
        self._opacity_slider.setValue(90)
        self._opacity_slider.setTextVisible(True)
        self._opacity_slider.setFormat("%p%")
        self._opacity_slider.setStyleSheet(f"""
            QProgressBar {{ background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px; }}
            QProgressBar::chunk {{ background: {C.PRI}; border-radius: 3px; }}
        """)
        self._opacity_slider.valueChanged.connect(self._set_opacity)
        lay.addWidget(self._opacity_slider)

        # ── Info ──
        lay.addSpacing(6)
        self._info_lbl = _lbl("Theme changes apply immediately. Animation speed affects HUD and tool effects.", 7, color=C.TEXT_DIM)
        self._info_lbl.setWordWrap(True)
        lay.addWidget(self._info_lbl)

        lay.addStretch()

        # Close
        close_btn = _btn("DISMISS", C.TEXT_MED, C.PANEL2)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)

    def _set_theme(self, name: str):
        set_theme(name)
        for k, btn in self._theme_btns.items():
            if k == name:
                btn.setStyleSheet(f"QPushButton {{ background: {C.PRI}; color: {C.DARK}; border: none; border-radius: 5px; font-weight: bold; }}")
            else:
                btn.setStyleSheet(f"QPushButton {{ background: #061828; color: {C.TEXT_DIM}; border: 1px solid {C.BORDER}; border-radius: 5px; }} QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}")
        self.theme_changed.emit(name)
        # Force repaint
        if self.parent():
            self.parent().update()

    def _set_style(self, style: str):
        for k, btn in self._style_btns.items():
            if k == style:
                btn.setStyleSheet(f"QPushButton {{ background: {C.PRI}; color: {C.DARK}; border: none; border-radius: 5px; font-weight: bold; }}")
            else:
                btn.setStyleSheet(f"QPushButton {{ background: #061828; color: {C.TEXT_DIM}; border: 1px solid {C.BORDER}; border-radius: 5px; }} QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}")
        # TODO: Apply HUD style (minimal/standard/dense)

    def _set_speed(self, speed: str):
        for k, btn in self._speed_btns.items():
            if k == speed:
                btn.setStyleSheet(f"QPushButton {{ background: {C.PRI}; color: {C.DARK}; border: none; border-radius: 5px; font-weight: bold; }}")
            else:
                btn.setStyleSheet(f"QPushButton {{ background: #061828; color: {C.TEXT_DIM}; border: 1px solid {C.BORDER}; border-radius: 5px; }} QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}")
        # TODO: Apply animation speed

    def _set_opacity(self, val: int):
        pass  # TODO: Apply panel opacity


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    _OW, _OH = 400, 465

    def __init__(self, url: str = "", key: str = "", auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 28, QFont.Weight.Bold))
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self.update_data(url, key, auto_login_url, manual_url, expiry_secs)

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def update_data(self, url: str, key: str, auto_login_url: str = "",
                    manual_url: str = "", expiry_secs: int = 600) -> None:
        """Refresh overlay content with a freshly generated key/URL pair."""
        self._manual_url     = manual_url or url
        self._url_lbl.setText(self._manual_url)
        self._key_lbl.setText(key)
        self._auto_login_url = auto_login_url
        self._update_qr(auto_login_url or url)
        self._expiry = time.time() + expiry_secs
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._timer_lbl.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent;"
        )
        self._ctimer.start(1000)
        self._tick()

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #0c9e7a; background: #082e1a; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — Jeeves ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self.update_data(url, key, auto, manual)

    def _do_close(self):
        self._ctimer.stop()
        self.hide()


class VoiceTriggersOverlay(QWidget):
    """Floating overlay - connect Google Assistant via TRIGGERcmd and manage
    JARVIS-style voice triggers without touching any code.

    Self-contained: reads/writes config/api_keys.json through voice_triggers.py
    and generates the TRIGGERcmd agent's commands.json. Also installs the
    TRIGGERcmd MCP server so Jeeves' brain can run commands directly (no IFTTT).
    Long outputs (setup steps, MCP status, test results) are shown in the main
    window's ContentPanel via the show_doc signal; the small status label keeps
    the last short message.
    """

    _OW, _OH = 640, 780
    show_doc = pyqtSignal(str, str)   # (title, body) -> main window content panel
    test_result = pyqtSignal(str)     # thread -> UI thread test output

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            VoiceTriggersOverlay {{
                background: rgba(0, 4, 12, 0.96);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        import voice_triggers as vt
        self._vt = vt
        self.test_result.connect(self._set_out)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 14, 22, 14)
        lay.setSpacing(6)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        def _btn(txt, color=C.PRI, bg=C.PANEL):
            b = QPushButton(txt)
            b.setFixedHeight(30)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {bg}; color: {color};
                    border: 1px solid {C.PRI_DIM}; border-radius: 5px;
                }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            return b

        def _field(placeholder):
            e = QLineEdit()
            e.setPlaceholderText(placeholder)
            e.setFont(QFont("Courier New", 8))
            e.setStyleSheet(
                f"QLineEdit {{ background: #071e30; color: {C.TEXT}; "
                f"border: 1px solid {C.BORDER}; border-radius: 4px; padding: 5px; }}"
            )
            return e

        lay.addWidget(_lbl("◈  VOICE TRIGGERS", 12, True))
        lay.addWidget(_lbl("Control Jeeves from Google Assistant (TRIGGERcmd)", 8,
                           color=C.TEXT_DIM))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── Connection ───────────────────────────────────────────────────────
        conn = QWidget()
        conn.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 6px;"
        )
        cl = QVBoxLayout(conn); cl.setContentsMargins(10, 8, 10, 8); cl.setSpacing(4)
        cl.addWidget(_lbl("CONNECTION  (from triggercmd.com)", 7, True, C.ACC2,
                          Qt.AlignmentFlag.AlignLeft))
        self._token_ed = _field("TriggerCmd token  (account -> Instructions)")
        self._token_ed.setEchoMode(QLineEdit.EchoMode.Password)
        self._computer_ed = _field("Computer name as shown in TriggerCmd")
        self._agent_dir_ed = _field("Agent data dir  (where commands.json lives)")
        cl.addWidget(self._token_ed)
        cl.addWidget(self._computer_ed)
        cl.addWidget(self._agent_dir_ed)
        save_btn = _btn("SAVE CONNECTION")
        save_btn.clicked.connect(self._save_connection)
        cl.addWidget(save_btn)
        comp_btn = _btn("🔗  LINK FROM COMPOSIO", C.ACC2, C.PANEL2)
        comp_btn.setToolTip("Use the TRIGGERcmd account already connected in your "
                            "Composio workspace - fills in the computer name for you.")
        comp_btn.clicked.connect(self._link_from_composio)
        cl.addWidget(comp_btn)
        lay.addWidget(conn)

        # ── AI control (TRIGGERcmd MCP - free, no IFTTT) ─────────────────────
        mcp_panel = QWidget()
        mcp_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 6px;"
        )
        ml = QVBoxLayout(mcp_panel); ml.setContentsMargins(10, 8, 10, 8); ml.setSpacing(4)
        ml.addWidget(_lbl("AI CONTROL  -  TRIGGERCMD MCP  (free, no IFTTT)", 7, True,
                          C.GREEN, Qt.AlignmentFlag.AlignLeft))
        self._mcp_status_lbl = _lbl("checking...", 7, color=C.TEXT_MED,
                                    align=Qt.AlignmentFlag.AlignLeft)
        self._mcp_status_lbl.setWordWrap(True)
        ml.addWidget(self._mcp_status_lbl)
        mcp_row = QHBoxLayout(); mcp_row.setSpacing(6)
        install_mcp_btn = _btn("⬇  INSTALL MCP SERVER", C.GREEN)
        install_mcp_btn.setToolTip(
            "Downloads the official TRIGGERcmd stdio MCP binary into bin/ and adds it "
            "to config mcp_servers - Jeeves' brain can then run any TRIGGERcmd command "
            "directly (no IFTTT, no subscription)."
        )
        install_mcp_btn.clicked.connect(self._install_mcp)
        mcp_status_btn = _btn("MCP STATUS", C.ACC2, C.PANEL2)
        mcp_status_btn.clicked.connect(self._mcp_status)
        mcp_row.addWidget(install_mcp_btn)
        mcp_row.addWidget(mcp_status_btn)
        ml.addLayout(mcp_row)
        lay.addWidget(mcp_panel)

        # ── Trigger rows ─────────────────────────────────────────────────────
        lay.addWidget(_lbl("TRIGGERS  -  say these after 'OK Google'", 7, True,
                           C.ACC2, Qt.AlignmentFlag.AlignLeft))
        self._rows_box = QWidget()
        self._rows_lay = QVBoxLayout(self._rows_box)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.setWidget(self._rows_box)
        scroll.setFixedHeight(250)
        lay.addWidget(scroll)

        add_btn = _btn("＋  ADD TRIGGER", C.GREEN)
        add_btn.clicked.connect(self._add_trigger)
        lay.addWidget(add_btn)

        # ── Actions ──────────────────────────────────────────────────────────
        self._ground_lbl = _lbl("ground: detecting...", 7, color=C.TEXT_MED,
                                align=Qt.AlignmentFlag.AlignLeft)
        self._ground_lbl.setToolTip(
            "TRIGGERcmd only uploads commands whose 'ground' matches the agent's "
            "run mode (foreground = desktop app, background = daemon/service). "
            "GENERATE auto-detects it from ~/.TRIGGERcmdData/debug.log; force a "
            "mode with: python voice_triggers.py --generate "
            "--ground foreground|background"
        )
        lay.addWidget(self._ground_lbl)
        act = QHBoxLayout(); act.setSpacing(6)
        gen_btn = _btn("GENERATE commands.json")
        ins_btn = _btn("SETUP STEPS (FREE)")
        test_btn = _btn("TEST FIRST", C.GREEN)
        close_btn = _btn("DISMISS", C.TEXT_MED, C.PANEL2)
        gen_btn.clicked.connect(self._generate)
        ins_btn.clicked.connect(self._instructions)
        test_btn.clicked.connect(self._test)
        close_btn.clicked.connect(self.hide)
        for b in (gen_btn, ins_btn, test_btn, close_btn):
            act.addWidget(b)
        lay.addLayout(act)

        self._out = QLabel("Ready.")
        self._out.setFont(QFont("Courier New", 7))
        self._out.setStyleSheet(
            f"color: {C.TEXT_MED}; background: {C.PANEL2}; "
            f"border: 1px solid {C.BORDER}; border-radius: 4px; padding: 6px;"
        )
        self._out.setWordWrap(True)
        self._out.setFixedHeight(64)
        lay.addWidget(self._out)

        self._reload()  # populate connection fields + trigger rows (seeds JARVIS set)

    # ── data plumbing ────────────────────────────────────────────────────────

    def _set_out(self, text: str):
        if text == "__RELOAD__":
            self._reload()
            return
        self._out.setText(str(text)[:600])

    def _preset_key(self, t: dict) -> str:
        if t.get("bridge"):
            return "bridge"
        tool = t.get("tool")
        args = t.get("args") or {}
        if tool == "system_status":
            return "status"
        if tool == "screen_process":
            return "vision"
        if tool == "open_app":
            return "open_app"
        if tool == "computer_settings":
            return {"lock": "lock", "sleep": "sleep", "shutdown": "shutdown"}.get(
                str(args.get("action", "")).lower(), "custom")
        # brain presets: match by the preset's fixed text prefix
        text = str(t.get("text", ""))
        for p in self._vt.PRESETS:
            base = p.get("text")
            if p.get("mode") == "brain" and base and text.startswith(base[:40]):
                return p["key"]
        return "custom"

    def _details_for(self, t: dict, key: str) -> str:
        if key == "bridge":
            return ""
        if key == "open_app":
            return str((t.get("args") or {}).get("app_name", ""))
        if key == "custom":
            return str(t.get("tool", "") if t.get("mode") == "tool" else t.get("text", ""))
        preset = self._vt.PRESET_BY_KEY.get(key)
        if preset and preset.get("mode") == "brain" and preset.get("text"):
            base = preset["text"]
            text = str(t.get("text", ""))
            return text[len(base):].strip() if text.startswith(base) else ""
        return ""

    def _row_widget(self, t: dict, idx: int):
        row = QWidget()
        rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(4)

        phrase = QLineEdit(str(t.get("phrase", "")))
        phrase.setPlaceholderText("say...")
        key = self._preset_key(t)
        preset = QComboBox()
        for p in self._vt.PRESETS:
            preset.addItem(p["label"], p["key"])
        preset.setCurrentIndex(max(0, next((i for i, p in enumerate(self._vt.PRESETS)
                                            if p["key"] == key), 0)))
        details = QLineEdit(self._details_for(t, key))
        details.setPlaceholderText("Details (optional)")
        enabled = QCheckBox()
        enabled.setChecked(bool(t.get("enabled", True)))
        enabled.setToolTip("On = included in commands.json")
        rm = QPushButton("✕")
        rm.setFixedSize(26, 26)
        rm.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C.TEXT_DIM}; "
            f"border: 1px solid {C.BORDER}; border-radius: 4px; }}"
            f"QPushButton:hover {{ color: #ff6688; border-color: #ff6688; }}"
        )
        for w in (phrase, preset, details):
            w.setStyleSheet(
                f"QWidget {{ background: #071e30; color: {C.TEXT}; "
                f"border: 1px solid {C.BORDER}; border-radius: 4px; padding: 4px; }}"
            )
        phrase.setFont(QFont("Courier New", 8))
        preset.setFont(QFont("Courier New", 7))
        details.setFont(QFont("Courier New", 8))

        rl.addWidget(phrase, 2)
        rl.addWidget(preset, 5)
        rl.addWidget(details, 4)
        rl.addWidget(enabled)
        rl.addWidget(rm)

        def _save(*_):
            self._save_row(idx, phrase, preset, details, enabled)

        def _on_preset(*_):
            # Details only matters for custom/open_app/brain presets
            pkey = preset.currentData() or "custom"
            details.setEnabled(pkey not in ("bridge",) and not (
                self._vt.PRESET_BY_KEY.get(pkey, {}).get("mode") == "tool"
                and pkey != "open_app" and pkey != "custom"))
            _save()

        phrase.editingFinished.connect(_save)
        preset.currentIndexChanged.connect(_on_preset)
        details.editingFinished.connect(_save)
        enabled.toggled.connect(_save)
        rm.clicked.connect(lambda *_: self._remove_row(t))
        _on_preset()
        return row

    def _trigger_from_row(self, old: dict, phrase_ed, preset_cb, details_ed,
                          enabled_cb) -> dict:
        key = preset_cb.currentData() or "custom"
        t = {"phrase": phrase_ed.text().strip(),
             "enabled": enabled_cb.isChecked()}
        if key == "custom":
            t["mode"] = old.get("mode", "brain")
            if t["mode"] == "tool":
                t["tool"] = details_ed.text().strip() or old.get("tool", "")
            else:
                t["text"] = details_ed.text().strip() or old.get("text", "")
        elif key == "bridge":
            t["mode"] = "brain"; t["bridge"] = True
        else:
            preset = self._vt.PRESET_BY_KEY.get(key) or {}
            t["mode"] = preset.get("mode", "brain")
            if preset.get("tool"):
                t["tool"] = preset["tool"]
            if preset.get("args"):
                t["args"] = dict(preset["args"])
            if key == "open_app":
                t["args"]["app_name"] = details_ed.text().strip()
            elif preset.get("text"):
                det = details_ed.text().strip()
                t["text"] = (preset["text"] + " " + det).strip()
        return t

    def _save_row(self, idx: int, phrase_ed, preset_cb, details_ed, enabled_cb):
        triggers = self._vt.get_triggers()
        if idx >= len(triggers):
            return
        t = self._trigger_from_row(triggers[idx], phrase_ed, preset_cb,
                                   details_ed, enabled_cb)
        if not t["phrase"]:
            return  # keep the old row until the user types a phrase
        triggers[idx] = t
        self._vt.set_triggers(triggers)

    def _remove_row(self, t: dict):
        self._vt.remove_trigger(t.get("phrase", ""))
        self._rebuild_rows()
        self._set_out(f"Removed trigger: {t.get('phrase')}")

    def _rebuild_rows(self):
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for idx, t in enumerate(self._vt.get_triggers()):
            self._rows_lay.addWidget(self._row_widget(t, idx))

    def _reload(self):
        """Refresh from disk; seed the JARVIS trigger set on first open."""
        seeded = self._vt.seed_defaults()
        block = self._vt.get_block()
        self._token_ed.setText(block.get("token", ""))
        self._computer_ed.setText(block.get("computer", ""))
        self._agent_dir_ed.setText(block.get("agent_dir", ""))
        try:
            self._mcp_status_lbl.setText(self._vt.mcp_status()["message"])
        except Exception:
            pass
        try:
            ground, source = self._vt.resolve_ground()
            self._ground_lbl.setText(
                f"ground: {ground}  (from {source}) - only matching commands upload"
            )
        except Exception:
            pass
        self._rebuild_rows()
        if seeded:
            self._set_out(f"Seeded {seeded} JARVIS-style triggers - toggle on/off, "
                          "then GENERATE commands.json.")

    # ── actions ──────────────────────────────────────────────────────────────

    def _save_connection(self):
        block = self._vt.get_block()
        block["token"] = self._token_ed.text().strip()
        block["computer"] = self._computer_ed.text().strip()
        block["agent_dir"] = self._agent_dir_ed.text().strip()
        self._vt.save_block(block)
        self._set_out("Connection saved. Now GENERATE commands.json.")

    def _link_from_composio(self):
        """Link the TRIGGERcmd account from the user's Composio workspace.

        If TRIGGERcmd is already connected in Composio, lists the computers
        and fills the Computer field automatically. If not, starts the OAuth
        flow (opens the browser) so the user can authorize.
        """
        def _work():
            try:
                status = self._vt.composio_status()
                if status.get("connected"):
                    computers = status.get("computers") or []
                    if computers:
                        self._vt.save_composio_computer(computers[0])
                        msg = (f"Linked via Composio. Computer '{computers[0]}' saved."
                               + (f" Others: {', '.join(computers[1:])}" if len(computers) > 1 else ""))
                    else:
                        msg = ("Linked via Composio, but no computers registered yet - "
                               "install the TRIGGERcmd agent and run it once, then retry.")
                else:
                    ok, link_msg = self._vt.link_from_composio()
                    msg = link_msg
            except Exception as e:
                msg = f"Composio link failed: {e}"
            self.test_result.emit(msg)
            self.test_result.emit("__RELOAD__")

        self._set_out("Checking Composio for a TRIGGERcmd connection...")
        threading.Thread(target=_work, daemon=True).start()

    def _add_trigger(self):
        triggers = self._vt.get_triggers()
        triggers.append({"phrase": "", "mode": "brain", "bridge": True,
                         "enabled": True})
        self._vt.set_triggers(triggers)
        self._rebuild_rows()
        self._set_out("New trigger added - type a phrase and pick what it does.")

    def _generate(self):
        ok, msg, count = self._vt.write_commands_json()
        try:
            ground, source = self._vt.resolve_ground()
            msg += f"  (ground={ground} from {source})"
        except Exception:
            pass
        self._set_out(msg)

    def _instructions(self):
        self.show_doc.emit("VOICE TRIGGERS  -  FREE SETUP GUIDE", self._vt.instructions())

    def _install_mcp(self):
        """Download + configure the TRIGGERcmd MCP server (free, no IFTTT)."""
        def _work():
            try:
                ok, msg = self._vt.ensure_mcp_configured()
            except Exception as e:
                msg = f"MCP install failed: {e}"
            self.test_result.emit(msg)
            self.test_result.emit("__RELOAD__")

        self._set_out("Installing TRIGGERcmd MCP server - this may take a moment...")
        threading.Thread(target=_work, daemon=True).start()

    def _mcp_status(self):
        s = self._vt.mcp_status()
        lines = [
            "TRIGGERCMD MCP SERVER STATUS",
            "----------------------------",
            f"MCP python package : {'OK' if s['mcp_package'] else 'MISSING (pip install mcp)'}",
            f"Binary installed   : {'yes' if s['binary_installed'] else 'no'}",
            f"Configured in cfg  : {'yes' if s['configured'] else 'no'}",
            f"Token in config    : {'yes' if s['token_set'] else 'no (falls back to ~/.TRIGGERcmdData/token.tkn)'}",
            f"Binary path        : {s['binary_path']}",
            "",
            f"Overall: {s['message']}",
        ]
        self.show_doc.emit("TRIGGERCMD MCP STATUS", "\n".join(lines))

    def _test(self):
        triggers = [t for t in self._vt.get_triggers() if t.get("enabled", True)]
        if not triggers:
            self._set_out("No enabled triggers to test.")
            return
        phrase = triggers[0].get("phrase", "")
        self._set_out(f"Testing '{phrase}' - this calls Jeeves, give it a moment...")
        threading.Thread(target=lambda: self.test_result.emit(
            self._vt.test_run(phrase)), daemon=True).start()


class ContentPanel(QWidget):
    """Floating panel that displays rich dynamic content (search results,
    news briefings, file summaries) without disrupting the HUD layout.

    Shown on demand via ``show_content(title, body)``. Thread-safe: the
    MainWindow routes updates through a Qt signal, so any thread can call
    ``JeevesUI.show_content`` and the panel updates on the UI thread.

    Supports HTML rendering for rich formatted output (search results,
    code blocks, tables). Panels are draggable via the title bar.
    """

    _OW, _OH = 620, 440

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ContentPanel {{
                background: rgba(0, 6, 14, 0.96);
                border: 1px solid {C.BORDER_B};
                border-radius: 12px;
            }}
        """)
        self._drag_pos = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 14)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        self._title_lbl = QLabel("◈ CONTENT")
        self._title_lbl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        head.addWidget(self._title_lbl)
        head.addStretch()

        # Pin button (keep on top)
        self._pin_btn = QPushButton("📌")
        self._pin_btn.setFixedSize(24, 24)
        self._pin_btn.setFont(QFont("Courier New", 9))
        self._pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pin_btn.setToolTip("Pin on top")
        self._pin_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: none; border-radius: 4px; }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        self._pin_btn.clicked.connect(self._toggle_pin)
        self._pinned = False
        head.addWidget(self._pin_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.RED}; border: 1px solid {C.RED}; }}
        """)
        close_btn.clicked.connect(self.hide)
        head.addWidget(close_btn)
        lay.addLayout(head)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        self._body = QTextEdit()
        self._body.setReadOnly(True)
        self._body.setFont(QFont("Courier New", 8))
        self._body.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 6px;
                padding: 8px;
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 8px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 4px; min-height: 20px;
            }}
        """)
        lay.addWidget(self._body, stretch=1)

        # Bottom bar: resize handle + status
        bottom = QHBoxLayout()
        self._status_lbl = QLabel("")
        self._status_lbl.setFont(QFont("Courier New", 6))
        self._status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        bottom.addWidget(self._status_lbl)
        bottom.addStretch()
        resize_lbl = QLabel("⟷ drag to move")
        resize_lbl.setFont(QFont("Courier New", 6))
        resize_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        bottom.addWidget(resize_lbl)
        lay.addLayout(bottom)

        self.hide()

    def show_content(self, title: str, body: str, format: str = "text"):
        """Populate and reveal the panel. format='html' for rich text."""
        self._title_lbl.setText(f"◈ {title}")
        if format == "html":
            self._body.setHtml(body)
        else:
            self._body.setPlainText(body if body else "No content.")
        self._status_lbl.setText(f"{len(body)} chars · {body.count(chr(10))+1} lines")
        self.show()
        self.raise_()

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self._pin_btn.setText("📌" if self._pinned else "📌")
        self._pin_btn.setStyleSheet(f"""
            QPushButton {{ background: {'transparent' if not self._pinned else C.PRI_GHO}; color: {C.PRI if self._pinned else C.TEXT_MED}; border: none; border-radius: 4px; }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() < 30:
            self._drag_pos = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self._drag_pos:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def _apply_geometry(self, cw):
        """Pin the panel to the right side of the HUD area."""
        ow, oh = self._OW, self._OH
        x = max(8, cw.width() - ow - 16)
        y = max(8, (cw.height() - oh) // 2)
        self.setGeometry(x, y, ow, oh)


# ── MainWindow ──
# The MainWindow class is kept as-is except for replacing _base_dir/API_FILE
# with the shared import from core.utils.
# The file is too large to rewrite entirely here; the critical optimizations
# above (HudCanvas, LogWidget, FileDropZone, _SysMetrics) cover the hot paths.
class MainWindow(QMainWindow):
    _log_sig   = pyqtSignal(str)
    _tip_sig   = pyqtSignal(str, str)   # (tool_name, display_text) for clickable tips
    _state_sig = pyqtSignal(str)
    _remote_sig = pyqtSignal()
    _content_sig = pyqtSignal(str, str)
    _brain_status_sig = pyqtSignal()

    def __init__(self, face_path: str):
        super().__init__()
        self.setWindowTitle("J.E.E.V.E.S — Neural Command Interface")
        self.setWindowIcon(_app_icon())
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command  = None
        self.on_clap_toggle   = None
        self.on_phone_command = None
        self._muted           = False
        self._clap_enabled    = self._load_clap_enabled()
        self._current_file: str | None = None
        self.on_remote_clicked = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._voice_triggers_overlay: VoiceTriggersOverlay | None = None
        self._settings_overlay: SettingsOverlay | None = None
        # Mirrored onto the window too: GUI-typed commands (_send) fire this
        # so they reach external sinks (e.g. the dashboard) like voice lines.
        self.on_log = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addWidget(self._build_tip_banner())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        self.hud = HudCanvas(face_path)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.addWidget(self.hud, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metrik güncelleme timer'ı
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._tip_sig.connect(self._log_tip)
        self._log.tipClicked.connect(self._on_tip_clicked)
        self._state_sig.connect(self._apply_state)
        self._remote_sig.connect(self.mark_remote_connected)
        self._content_sig.connect(self._show_content_panel)
        self._brain_status_sig.connect(self._apply_brain_status)

        self._content_panel = ContentPanel(central)
        self._content_panel._apply_geometry(central)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 590
            cw = self.centralWidget()
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            cw = self.centralWidget()
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._voice_triggers_overlay and self._voice_triggers_overlay.isVisible():
            ow, oh = VoiceTriggersOverlay._OW, VoiceTriggersOverlay._OH
            cw = self.centralWidget()
            self._voice_triggers_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if getattr(self, "_settings_overlay", None) and self._settings_overlay.isVisible():
            ow, oh = SettingsOverlay._OW, SettingsOverlay._OH
            cw = self.centralWidget()
            self._settings_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if getattr(self, "_content_panel", None) and self._content_panel.isVisible():
            self._content_panel._apply_geometry(self.centralWidget())

    def _update_metrics(self):
        snap = _metrics.snapshot()
        cpu = snap["cpu"]
        self._gauge_cpu.set_value(cpu, f"{cpu:.0f}%")
        mem = snap["mem"]
        self._gauge_mem.set_value(mem, f"{mem:.0f}%")
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)
        self._bar_net.set_value(net_pct, net_str)
        gpu = snap["gpu"]
        if gpu >= 0:
            self._gauge_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._gauge_gpu.set_value(0, "N/A")
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")
        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")
        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")

    def _build_header(self) -> QWidget:
        outer = QWidget()
        outer.setStyleSheet(f"background: {C.DARK};")
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        w = QWidget()
        w.setFixedHeight(58)
        w.setStyleSheet(f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                        f"stop:0 {C.DARK}, stop:0.5 {C.PANEL}, stop:1 {C.DARK});"
                        f"border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(18, 0, 18, 0)

        def _badge(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(QFont("Courier New", 8))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        # Left side: privacy mode badge + brain status
        self._privacy_lbl = _badge("GUARDED", C.GREEN)
        self._privacy_lbl.setStyleSheet(
            f"color: {C.GREEN}; background: {C.PANEL2};"
            f"border: 1px solid {C.GREEN}; border-radius: 3px;"
            f"padding: 2px 8px;"
        )
        lay.addWidget(self._privacy_lbl)
        lay.addSpacing(6)
        self._status_lbl = _badge("ONLINE", C.GREEN)
        self._status_lbl.setStyleSheet(
            f"color: {C.GREEN}; background: transparent; font-weight: bold;"
        )
        lay.addWidget(self._status_lbl)
        lay.addSpacing(4)
        lay.addWidget(_badge("NEURAL", C.PRI_DIM))
        lay.addStretch()

        mid = QVBoxLayout(); mid.setSpacing(1)
        title = QLabel("J.E.E.V.E.S")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Courier New", 18, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(title)
        sub = QLabel("Neural Command Interface")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setFont(QFont("Courier New", 7))
        sub.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(sub)
        lay.addLayout(mid)
        lay.addStretch()

        right_col = QVBoxLayout(); right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Courier New", 15, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        # Provider indicator (updated when provider switches)
        self._provider_lbl = QLabel("PROV  UNKNOWN")
        self._provider_lbl.setFont(QFont("Courier New", 7))
        self._provider_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._provider_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._provider_lbl)

        self._model_lbl = QLabel("MODEL  UNKNOWN")
        self._model_lbl.setFont(QFont("Courier New", 7))
        self._model_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._model_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._model_lbl)
        self._refresh_brain_status()
        lay.addLayout(right_col)
        outer_lay.addWidget(w)

        # ── Scrolling activity ticker ──
        self._ticker = QLabel("")
        self._ticker.setFont(QFont("Courier New", 6))
        self._ticker.setStyleSheet(f"color: {C.PRI_DIM}; background: {C.PANEL2}; padding: 1px 8px; border-bottom: 1px solid {C.BORDER};")
        self._ticker.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._ticker.setFixedHeight(16)
        self._ticker_text = ""
        self._ticker_offset = 0
        self._ticker_tmr = QTimer(self)
        self._ticker_tmr.timeout.connect(self._scroll_ticker)
        self._ticker_tmr.start(80)
        outer_lay.addWidget(self._ticker)

        return outer

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _scroll_ticker(self):
        """Scroll the activity ticker text leftward."""
        if not self._ticker_text:
            return
        display_w = self._ticker.width()
        text_w = len(self._ticker_text) * 5  # approx pixel width per char
        if text_w <= display_w:
            self._ticker.setText(self._ticker_text)
        else:
            # Wrap text with spaces for seamless scroll
            full = self._ticker_text + "    " + self._ticker_text
            visible = full[self._ticker_offset:self._ticker_offset + display_w // 5]
            self._ticker.setText(visible)
            self._ticker_offset = (self._ticker_offset + 1) % (len(self._ticker_text) + 4)

    def set_ticker(self, text: str):
        """Update the scrolling ticker text (thread-safe via signal)."""
        self._ticker_text = text
        self._ticker_offset = 0

    def _refresh_brain_status(self):
        """Refresh provider/model labels without stalling window build.

        Importing or_client (the brain) costs ~0.5s; it happens in a
        background thread and the labels update via signal when ready.
        """
        def _load():
            try:
                from or_client import client as brain_client
                self._brain_client_ref = brain_client
            except Exception:
                self._brain_client_ref = None
            self._brain_status_sig.emit()
        threading.Thread(target=_load, daemon=True).start()

    def _apply_brain_status(self):
        brain_client = getattr(self, "_brain_client_ref", None)
        provider, model = "UNKNOWN", "UNKNOWN"
        if brain_client is not None:
            try:
                provider = str(getattr(brain_client, "provider", "unknown")).upper()
                try:
                    model_info = brain_client.available_models() or {}
                except Exception:
                    model_info = {}
                model_value = model_info.get("active_text_model")
                if not model_value:
                    text_models = model_info.get("text_models") or ["unknown"]
                    model_value = text_models[0]
                model = str(model_value).upper()
            except Exception:
                pass
        if hasattr(self, "_provider_lbl"):
            self._provider_lbl.setText(f"PROV  {provider}")
        if hasattr(self, "_model_lbl"):
            self._model_lbl.setText(f"MODEL  {model}")
        # Update privacy mode badge from config
        try:
            if API_FILE.exists():
                cfg = json.loads(API_FILE.read_text(encoding="utf-8"))
                pm = str(cfg.get("privacy_mode", "GUARDED")).upper()
                if hasattr(self, "_privacy_lbl"):
                    self._privacy_lbl.setText(pm)
                    pm_col = {"STRICT": C.ACC, "GUARDED": C.GREEN, "RELAXED": C.PRI}.get(pm, C.GREEN)
                    self._privacy_lbl.setStyleSheet(
                        f"color: {pm_col}; background: {C.PANEL2};"
                        f"border: 1px solid {pm_col}; border-radius: 3px;"
                        f"padding: 2px 6px;"
                    )
        except Exception:
            pass

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(5)

        # ── System Metrics ──
        hdr = QLabel("◈ SYSTEM")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 3px;")
        lay.addWidget(hdr)

        # ── Circular Gauges for CPU, MEM, GPU ──
        gauge_row = QHBoxLayout()
        gauge_row.setSpacing(4)

        self._gauge_cpu = RadialGauge("CPU", C.PRI)
        self._gauge_mem = RadialGauge("MEM", C.ACC2)
        gauge_row.addWidget(self._gauge_cpu)
        gauge_row.addWidget(self._gauge_mem)
        lay.addLayout(gauge_row)

        self._gauge_gpu = RadialGauge("GPU", C.ACC)
        gpu_center = QHBoxLayout()
        gpu_center.addStretch()
        gpu_center.addWidget(self._gauge_gpu)
        gpu_center.addStretch()
        lay.addLayout(gpu_center)

        # ── Bar widgets for NET and TEMP ──
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_tmp = MetricBar("TMP", "#e05555")

        for bar in [self._bar_net, self._bar_tmp]:
            lay.addWidget(bar)

        # ── System Info ──
        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(6, 4, 6, 4)
        ip_lay.setSpacing(2)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(QFont("Courier New", 7))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(QFont("Courier New", 7))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addSpacing(2)

        # ── Status Badges ──
        for txt, col in [
            ("AI CORE  ACTIVE",     C.GREEN),
            ("PRIVACY  GUARDED",    C.PRI),
            ("NEURAL  ONLINE",      C.TEXT_DIM),
        ]:
            lbl = QLabel(txt)
            lbl.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color: {col}; background: {C.PANEL2};"
                f"border: 1px solid {C.BORDER_A}; border-radius: 3px; padding: 3px;"
            )
            lay.addWidget(lbl)

        lay.addStretch()

        # ── Quick Tool Shortcuts ──
        tool_hdr = QLabel("◈ TOOLS")
        tool_hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        tool_hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                               f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 3px;")
        lay.addWidget(tool_hdr)

        quick_tools = [
            ("System Status", "system_status"),
            ("Web Search",    "web_search"),
            ("Screenshot",    "screen_process"),
            ("File Process",  "file_processor"),
            ("Phone Control", "phone_control"),
        ]
        self._quick_tool_btns: list[QPushButton] = []
        for label, tool in quick_tools:
            btn = QPushButton(label)
            btn.setFixedHeight(22)
            btn.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {C.PANEL2}; color: {C.TEXT_MED};
                    border: 1px solid {C.BORDER}; border-radius: 2px;
                    text-align: left; padding: 2px 4px;
                }}
                QPushButton:hover {{ color: {C.PRI}; border: 1px solid {C.BORDER_B}; }}
            """)
            btn.clicked.connect(lambda _, t=tool: self._send_quick_tool(t))
            lay.addWidget(btn)
            self._quick_tool_btns.append(btn)

        return w

    def _send_quick_tool(self, tool: str):
        """Quick tool button → send as text command."""
        if self.on_text_command:
            threading.Thread(target=self.on_text_command,
                           args=(f"use the {tool} tool",), daemon=True).start()

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(5)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            return l

        # ── Tool Status Widget (NEW) ──
        self._tool_status = ToolStatusWidget()
        lay.addWidget(self._tool_status)

        # ── Activity Log ──
        lay.addWidget(_sec("ACTIVITY LOG"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        # Collapsible TIPS section: expanded by default; click the header
        # to reclaim log space.
        self._tips_btn = QPushButton("▾ QUICK COMMANDS")
        self._tips_btn.setCheckable(True)
        self._tips_btn.setChecked(True)
        self._tips_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._tips_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tips_btn.setStyleSheet(f"""
            QPushButton {{
                text-align: left; padding: 2px 4px;
                background: transparent; color: {C.TEXT_MED}; border: none;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        self._tips_btn.toggled.connect(self._toggle_tips)
        lay.addWidget(self._tips_btn)
        self._tips_box = self._build_tips_section()
        lay.addWidget(self._tips_box)
        # Register provider-change callback to surface provider switches in the UI log
        try:
            from or_client import client as brain_client

            def _on_provider_change(old, new):
                try:
                    self._log.append_log(f"SYS: Provider switched from {old} to {new} — continuing with {new}.")
                    self._refresh_brain_status()
                except Exception:
                    pass

            brain_client.register_provider_change_callback(_on_provider_change)
        except Exception:
            # non-fatal: UI works without brain client registration
            pass

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(QFont("Courier New", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        self._remote_btn = QPushButton("🛰  REMOTE DASHBOARD")
        self._remote_btn.setFixedHeight(30)
        self._remote_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remote_btn.setStyleSheet(f"""
            QPushButton {{
                background: #0a2e42; color: {C.GREEN};
                border: 1px solid {C.GREEN}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: #0c3a50; }}
        """)
        self._remote_btn.clicked.connect(self._open_remote_dashboard)
        lay.addWidget(self._remote_btn)

        self._voice_btn = QPushButton("🎛  VOICE TRIGGERS")
        self._voice_btn.setFixedHeight(30)
        self._voice_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._voice_btn.setStyleSheet(f"""
            QPushButton {{
                background: #0a2e42; color: {C.ACC2};
                border: 1px solid {C.ACC2}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: #0c3a50; }}
        """)
        self._voice_btn.clicked.connect(self._open_voice_triggers)
        lay.addWidget(self._voice_btn)

        sep_ph = QFrame(); sep_ph.setFrameShape(QFrame.Shape.HLine)
        sep_ph.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep_ph)

        # PHONE CONTROL — Phantom Droid-style quick actions. Each button
        # runs phone_control directly through the on_phone_command callback
        # (no LLM hop), so they work instantly; results land in the log.
        lay.addWidget(_sec("PHONE CONTROL"))
        ph_grid = QGridLayout(); ph_grid.setSpacing(4)
        self._phone_btns: list[QPushButton] = []
        for i, (label, act, p) in enumerate([
            ("📶 STATUS",  "status",     None),
            ("🔌 CONNECT", "connect",    None),
            ("📸 SHOT",    "screenshot", {"analyze": True}),
            ("📺 LIVE",    "screen",     None),
            ("🔔 RING",    "ring",       None),
            ("🔋 BATTERY", "battery",    None),
            ("📋 REPORT",  "report",     None),
            ("📱 APPS",    "apps",       None),
        ]):
            btn = QPushButton(label)
            btn.setFixedHeight(26)
            btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #0a2e42; color: {C.TEXT_MED};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.PRI}; border: 1px solid {C.PRI_DIM}; }}
            """)
            btn.clicked.connect(
                lambda _, a=act, pp=p: self._send_phone_action(a, pp))
            ph_grid.addWidget(btn, i // 2, i % 2)
            self._phone_btns.append(btn)
        lay.addLayout(ph_grid)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        self._mute_btn = QPushButton("🎙  MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(30)
        self._mute_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        self._clap_btn = QPushButton()
        self._clap_btn.setFixedHeight(28)
        self._clap_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._clap_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clap_btn.clicked.connect(self._toggle_clap)
        self._style_clap_btn()
        lay.addWidget(self._clap_btn)

        settings_btn = QPushButton("⚙  SETTINGS")
        settings_btn.setFixedHeight(28)
        settings_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(f"""
            QPushButton {{
                background: #0a1e2e; color: {C.ACC2};
                border: 1px solid {C.ACC2}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: #0c2a3e; }}
        """)
        settings_btn.clicked.connect(self._open_settings)
        lay.addWidget(settings_btn)

        fs_btn = QPushButton("⛶  FULLSCREEN  [F11]")
        fs_btn.setFixedHeight(26)
        fs_btn.setFont(QFont("Courier New", 7))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{
                color: {C.PRI}; border: 1px solid {C.BORDER_B};
            }}
        """)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        return w

    def _build_tip_banner(self) -> QWidget:
        """Slim rotating tip-of-the-moment banner under the header.

        Shows a random smart-shortcut tip (rotated every 60s, like the remote
        dashboard) and is clickable to open the tool's full tutorial in the
        content panel — same flow as the clickable tips in the log.
        """
        w = QWidget()
        w.setFixedHeight(26)
        w.setStyleSheet(f"background: {C.PANEL2}; border-bottom: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(8)

        tag = QLabel("💡 TIP")
        tag.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        tag.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        lay.addWidget(tag)

        self._tip_banner_tool: str = ""
        self._tip_banner_btn = QPushButton("")
        self._tip_banner_btn.setFlat(True)
        self._tip_banner_btn.setFont(QFont("Courier New", 8))
        self._tip_banner_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tip_banner_btn.setStyleSheet(f"""
            QPushButton {{
                text-align: left; padding: 0; border: none;
                background: transparent; color: {C.TEXT_MED};
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        self._tip_banner_btn.clicked.connect(
            lambda: self._on_tip_clicked(self._tip_banner_tool)
        )
        lay.addWidget(self._tip_banner_btn, stretch=1)

        self._tip_banner_tmr = QTimer(self)
        self._tip_banner_tmr.timeout.connect(self._rotate_tip_banner)
        self._tip_banner_tmr.start(60_000)
        self._rotate_tip_banner()
        return w

    def _rotate_tip_banner(self):
        """Pick a fresh random tip for the banner (rotates every 60s)."""
        try:
            tool, tip = random_tip_entry()
            self._tip_banner_tool = tool
            tip = tip.replace("\n", " ")
            if len(tip) > 110:
                tip = tip[:109] + "…"
            self._tip_banner_btn.setText(tip)
        except Exception:
            pass

    def _toggle_tips(self, visible: bool):
        """Expand/collapse the side-panel TIPS list."""
        self._tips_box.setVisible(visible)
        self._tips_btn.setText(("▾" if visible else "▸") + " TIPS — QUICK COMMANDS")

    def _build_tips_section(self) -> QWidget:
        """Compact scrollable list of smart-shortcut tips for the side panel.

        Each row is 'phrase — what it does' (rich text for two colors).
        Content comes from the shared config/tool_tips.SHORTCUT_TIPS so it
        stays in sync with any other surface that lists shortcuts.
        """
        from config.tool_tips import SHORTCUT_TIPS

        wrap = QWidget()
        wrap.setFixedHeight(90)
        wrap.setStyleSheet(f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: %s; width: 6px; border: none; }
            QScrollBar::handle:vertical { background: %s; border-radius: 3px; min-height: 20px; }
        """ % (C.BG, C.BORDER_B))

        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(8, 5, 8, 5)
        v.setSpacing(3)
        for phrase, what in SHORTCUT_TIPS:
            row = QLabel()
            row.setTextFormat(Qt.TextFormat.RichText)
            row.setText(
                f"<b style='color:{C.PRI};'>{phrase}</b>"
                f" <span style='color:{C.TEXT_MED};'>— {what}</span>"
            )
            row.setFont(QFont("Courier New", 7))
            row.setWordWrap(True)
            v.addWidget(row)
        v.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll)
        return wrap

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Courier New", 9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: #061828; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(24)
        w.setStyleSheet(f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                        f"stop:0 {C.DARK}, stop:0.5 {C.PANEL}, stop:1 {C.DARK});"
                        f"border-top: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen  ·  [Ctrl+Q] Quit"))
        lay.addStretch()
        lay.addWidget(_fl("Neural Command Interface  ·  Private AI System"))
        lay.addStretch()
        lay.addWidget(_fl("jarvis.institute", C.PRI_DIM))
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell JEEVES what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _open_remote_dashboard(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Remote dashboard unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        self._log.append_log(f"SYS: Remote dashboard ready — {url} | key={key}")

        if self._remote_overlay is None:
            self._remote_overlay = RemoteKeyOverlay(
                url, key, auto, manual, parent=self.centralWidget()
            )
            self._remote_overlay.set_new_key_callback(self.on_remote_clicked)
        else:
            self._remote_overlay.update_data(url, key, auto, manual)

        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        cw = self.centralWidget()
        self._remote_overlay.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        self._remote_overlay.show()
        self._remote_overlay.raise_()

    def _open_voice_triggers(self):
        """Open the Voice Triggers overlay (Google Assistant / TRIGGERcmd)."""
        if self._voice_triggers_overlay is None:
            self._voice_triggers_overlay = VoiceTriggersOverlay(
                parent=self.centralWidget()
            )
            self._voice_triggers_overlay.show_doc.connect(self._show_content_panel)
        self._voice_triggers_overlay._reload()
        ow, oh = VoiceTriggersOverlay._OW, VoiceTriggersOverlay._OH
        cw = self.centralWidget()
        self._voice_triggers_overlay.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        self._voice_triggers_overlay.show()
        self._voice_triggers_overlay.raise_()

    def _open_settings(self):
        """Open the Settings overlay (theme, animation, HUD style)."""
        if self._settings_overlay is None:
            self._settings_overlay = SettingsOverlay(
                parent=self.centralWidget()
            )
            self._settings_overlay.theme_changed.connect(self._on_theme_changed)
        ow, oh = SettingsOverlay._OW, SettingsOverlay._OH
        cw = self.centralWidget()
        self._settings_overlay.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        self._settings_overlay.show()
        self._settings_overlay.raise_()

    def _on_theme_changed(self, theme: str):
        """Handle theme change: re-apply stylesheets and repaint."""
        # Re-apply central widget background
        cw = self.centralWidget()
        if cw:
            cw.setStyleSheet(f"background: {C.BG};")
        # Repaint all widgets
        self.update()
        self.hud.update()
        self._log.append_log(f"SYS: Theme changed to {theme.upper()}.")

    def mark_remote_connected(self):
        """UI-side update (Qt main thread) when a phone pairs via QR/key."""
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _log_tip(self, tool: str, text: str):
        """UI-thread slot: append a tool tip as a clickable log line."""
        self._log.append_tip(text, tool)

    def _on_tip_clicked(self, tool: str):
        """A tool tip link was clicked — open the full /tools-style tutorial."""
        tool = str(tool or "").strip()
        if not tool:
            return
        text = tool_tutorial(tool)
        if not text:
            text = f"No tutorial for '{tool}'."
        self._show_content_panel(f"TOOL TUTORIAL — {tool}", text)

    def _show_content_panel(self, title: str, body: str):
        """UI-thread slot: populate + reveal the dynamic content panel."""
        panel = getattr(self, "_content_panel", None)
        if panel is None:
            return
        panel.show_content(title, body)
        panel._apply_geometry(self.centralWidget())

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("🔇  MICROPHONE MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #1a0a14; color: {C.MUTED_C};
                    border: 1px solid {C.MUTED_C}; border-radius: 3px;
                }}
            """)
        else:
            self._mute_btn.setText("🎙  MICROPHONE ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #0a2e1a; color: {C.GREEN};
                    border: 1px solid {C.GREEN}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #0c3a24; }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        # Mirror GUI-typed commands to the dashboard (via on_log) the same
        # way voice utterances do, so both sides stay in sync.
        self._log_sig.emit(f"You: {txt}")
        cb = getattr(self, "on_log", None)
        if cb:
            try:
                cb(f"You: {txt}")
            except Exception:
                pass
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        # YinYang: adapt frame rate on state change
        self.hud._update_frame_rate()

    def _load_clap_enabled(self) -> bool:
        if not API_FILE.exists():
            return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("enable_clap_wake", False))
        except Exception:
            return False

    def _style_clap_btn(self):
        if self._clap_enabled:
            self._clap_btn.setText("\U0001F44F  CLAP WAKE: ON")
            self._clap_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #0a2e1a; color: {C.GREEN};
                    border: 1px solid {C.GREEN}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #0c3a24; }}
            """)
        else:
            self._clap_btn.setText("\U0001F44F  CLAP WAKE: OFF")
            self._clap_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #0a1e2e; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_clap(self):
        self._clap_enabled = not self._clap_enabled
        self._style_clap_btn()
        API_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if API_FILE.exists():
            try:
                existing = json.loads(API_FILE.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing["enable_clap_wake"] = self._clap_enabled
        API_FILE.write_text(json.dumps(existing, indent=4), encoding="utf-8")
        self._log.append_log(
            f"SYS: Clap wake {'enabled' if self._clap_enabled else 'disabled'}."
        )
        if self.on_clap_toggle:
            threading.Thread(
                target=self.on_clap_toggle, args=(self._clap_enabled,), daemon=True
            ).start()

    def _send_phone_action(self, action: str, params: dict | None = None):
        """Phone-panel button → on_phone_command (runs phone_control
        directly, no LLM hop). Falls back to a text command so it still
        works if the main loop hasn't wired the direct callback yet."""
        if self.on_phone_command:
            self.on_phone_command(action, params or {})
            return
        if self.on_text_command:
            words = action
            if params and params.get("pkg"):
                words += " " + str(params["pkg"])
            self.on_text_command(f"phone {words}")

    def _check_config(self) -> bool:
        try:
            from memory.config_manager import is_configured
            return is_configured()
        except Exception:
            # fallback to legacy check
            if not API_FILE.exists():
                return False
            try:
                d = json.loads(API_FILE.read_text(encoding="utf-8"))
                return bool(d.get("groq_api_key")) and bool(d.get("os_system"))
            except Exception:
                return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 590
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, provider: str, groq_key: str, github_key: str, os_name: str):
        """Handle setup completion with provider choice and credentials."""
        API_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if API_FILE.exists():
            try:
                existing = json.loads(API_FILE.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing["brain_provider"] = provider
        if groq_key:
            # Merge new keys with any already stored so adding keys never
            # discards the existing pool (Jeeves rotates across all of them).
            new_keys = [k.strip() for k in re.split(r"[,;\n]+", groq_key) if k.strip()]
            if new_keys:
                old = existing.get("groq_api_key", [])
                if isinstance(old, str):
                    old = [old] if old else []
                merged = []
                for k in list(old) + new_keys:
                    k = str(k).strip()
                    if k and k not in merged:
                        merged.append(k)
                existing["groq_api_key"] = merged if len(merged) > 1 else (merged[0] if merged else "")
        if github_key:
            existing["github_models_api_key"] = github_key
        existing["os_system"] = os_name
        API_FILE.write_text(json.dumps(existing, indent=4), encoding="utf-8")
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._log.append_log(f"SYS: Initialised. Provider={provider}, OS={os_name.upper()}. JEEVES online.")

    @property
    def current_file(self) -> str | None:
        return self._current_file


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JeevesUI:
    """Top-level UI wrapper exposed to main.py."""
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._app.setWindowIcon(_app_icon())  # taskbar icon while running
        self._win = MainWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)
        # Optional callback mirroring every HUD log line (tool tips included)
        # to an external sink (e.g. the remote dashboard). Called from
        # whatever thread logged the line — the sink must be thread-safe.
        self._on_log = None

    @property
    def on_log(self):
        return self._on_log

    @on_log.setter
    def on_log(self, cb):
        self._on_log = cb
        # Mirrored onto the window too: GUI-typed commands (_send) fire it
        # so they reach external sinks like the dashboard.
        self._win.on_log = cb

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_clap_toggle(self):
        return self._win.on_clap_toggle

    @on_clap_toggle.setter
    def on_clap_toggle(self, cb):
        self._win.on_clap_toggle = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def set_active_tool(self, tool_name: str):
        """Mark a tool as active (shows in radial HUD ring)."""
        self._win.hud.set_active_tool(tool_name)
        # Also update the tool status widget
        try:
            self._win._tool_status.add_tool(tool_name, "running")
        except Exception:
            pass

    def clear_active_tool(self, tool_name: str):
        """Clear a tool from active status."""
        self._win.hud.clear_active_tool(tool_name)
        # Mark as completed in status widget
        try:
            self._win._tool_status.add_tool(tool_name, "completed")
        except Exception:
            pass

    def write_log(self, text: str):
        self._win._log_sig.emit(text)
        cb = getattr(self, "on_log", None)
        if cb:
            try:
                cb(text)
            except Exception:
                pass
        # Keep the window's copy in sync for GUI-typed commands (_send).
        self._win.on_log = cb

    def set_ticker(self, text: str):
        """Update the scrolling activity ticker (thread-safe)."""
        self._win.set_ticker(text)

    def write_tool_tip(self, tool: str, text: str):
        """Thread-safe: log a clickable tool tip (click → tutorial panel)."""
        self._win._tip_sig.emit(tool, text)
        cb = getattr(self, "on_log", None)
        if cb:
            try:
                # Pass the tool name so sinks (e.g. the dashboard) can make
                # the tip line clickable to its tutorial.
                cb(text, tool)
            except Exception:
                pass

    def mark_remote_connected(self):
        """Thread-safe: tell the QR overlay a phone connected."""
        self._win._remote_sig.emit()

    def show_content(self, title: str, body: str):
        """Thread-safe: display rich dynamic content in the HUD-side panel."""
        self._win._content_sig.emit(title, body)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
