#!/usr/bin/env python3
"""orb.py — floating desktop orb launcher for Jeeves.

A small always-on-top orb sits in the corner of your screen (drag it
anywhere). Left-click (without dragging) opens a mini chat dialog that
talks to the warm daemon — which auto-starts on first use, so the orb
stays light and the conversation persists between messages. Smart
shortcuts ("open notepad", "what's on my screen") work here too.

Run:
    python orb.py

The orb only needs PyQt6 + the daemon client — no heavy app imports.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt, QSettings, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import cli  # noqa: E402  (daemon client helpers; cheap import)

# The holo renderer is a small pure-painter module — no heavy deps.
from holo_orb import draw_holo_orb  # noqa: E402

_STYLE_KEY = "style"   # QSettings: 'face' (default) | 'holo'

ORB_SIZE = 96
CHAT_W, CHAT_H = 400, 480

_APP_ICON: QIcon | None = None


def _app_icon() -> QIcon:
    """The Jeeves icon (jeeves.ico), cached; empty QIcon if missing."""
    global _APP_ICON
    if _APP_ICON is None:
        icon_path = BASE_DIR / "jeeves.ico"
        _APP_ICON = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
    return _APP_ICON


class ChatWindow(QWidget):
    """Mini chat dialog that talks to the warm daemon."""

    _line_sig = pyqtSignal(str)
    _done_sig = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("JEEVES")
        self.setWindowIcon(_app_icon())
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setStyleSheet(
            "QWidget { background: #0a0f1e; color: #d7e6ff; font-family: 'Courier New'; }"
            "QTextEdit { background: #060a14; border: 1px solid #1c3a5e; border-radius: 6px; padding: 6px; }"
            "QLineEdit { background: #0d1428; border: 1px solid #1c3a5e; border-radius: 6px; padding: 7px; }"
            "QPushButton { background: #0e2a44; border: 1px solid #2f7bd9; border-radius: 6px; padding: 7px 14px; }"
            "QPushButton:hover { background: #143a5e; }"
        )
        self.resize(CHAT_W, CHAT_H)
        self._line_sig.connect(self._append_line)
        self._done_sig.connect(lambda: self._send_btn.setEnabled(True))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText(
            "Jeeves is ready. Try: 'open notepad', \"what's on my screen\",\n"
            "'search python', 'play despacito', 'system status'..."
        )
        lay.addWidget(self._log, stretch=1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Ask Jeeves…  (Enter to send, Esc to close)")
        self._input.returnPressed.connect(self._send)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._send)
        row.addWidget(self._input, stretch=1)
        row.addWidget(self._send_btn)
        lay.addLayout(row)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def _append_line(self, text: str):
        self._log.append(text)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _send(self):
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self._append_line(f"You: {text}")
        self._send_btn.setEnabled(False)
        threading.Thread(target=self._worker, args=(text,), daemon=True).start()

    def _worker(self, text: str):
        try:
            resp = cli._daemon_send_or_spawn(
                {"type": "chat", "text": text}, cli.DAEMON_DEFAULT_PORT
            )
            if resp.get("ok"):
                reply = resp.get("reply") or "(no reply)"
            else:
                reply = f"⚠️ {resp.get('error', 'unknown error')}"
        except Exception as e:
            reply = f"⚠️ {type(e).__name__}: {e}"
        self._line_sig.emit(f"Jeeves: {reply}")
        self._line_sig.emit("")  # blank line between exchanges
        self._done_sig.emit()


class OrbWindow(QWidget):
    """Always-on-top draggable orb; left-click opens the chat dialog."""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowIcon(_app_icon())
        self.resize(ORB_SIZE, ORB_SIZE)

        self._face = QPixmap(str(BASE_DIR / "face.png"))
        if self._face.isNull():
            self._face = QPixmap(ORB_SIZE, ORB_SIZE)
            self._face.fill(Qt.GlobalColor.transparent)

        self._drag_offset: object | None = None
        self._press_pos = None
        self._pulse = 0.0
        self.chat: ChatWindow | None = None

        # Orb visual: 'face' (default) or 'holo' (JARVIS wireframe orb).
        # Persisted per-machine via QSettings; the face remains the default.
        self._settings = QSettings("Jeeves", "orb")
        self._style = str(self._settings.value(_STYLE_KEY, "face")).lower()
        if self._style not in ("face", "holo"):
            self._style = "face"

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._tick)
        self._tmr.start(33)  # ~30fps gentle breathing

        self._place_bottom_right()

    def _place_bottom_right(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - ORB_SIZE - 24, screen.bottom() - ORB_SIZE - 24)

    def _tick(self):
        self._pulse = (self._pulse + 0.05) % 6.2832
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        cx = cy = ORB_SIZE / 2
        rad = ORB_SIZE / 2 - 7
        breath = 1.0 + 0.045 * self._pulse

        # soft glow behind the orb
        p.setBrush(QColor(0, 190, 255, 38))
        p.setPen(Qt.PenStyle.NoPen)
        glow_r = rad * breath + 9
        p.drawEllipse(int(cx - glow_r), int(cy - glow_r),
                      int(glow_r * 2), int(glow_r * 2))

        if self._style == "holo":
            # JARVIS wireframe holo-orb (animated) — added alongside the
            # face orb, not replacing it; toggle via the right-click menu.
            draw_holo_orb(
                p, cx, cy, rad * (1.0 + 0.02 * self._pulse),
                t=self._pulse * 3.0, muted=False,
            )
            return

        # circular-clipped face (default style)
        r = rad * breath
        path = QPainterPath()
        path.addEllipse(cx - r, cy - r, r * 2, r * 2)
        p.save()
        p.setClipPath(path)
        p.drawPixmap(int(cx - r), int(cy - r), int(r * 2), int(r * 2), self._face)
        p.restore()

        # rim
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(90, 225, 255, 210), 2))
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.frameGeometry().topLeft()
        elif e.button() == Qt.MouseButton.RightButton:
            self._show_menu(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            if (e.globalPosition().toPoint() - self._press_pos).manhattanLength() < 6:
                self.toggle_chat()
            self._drag_offset = None

    def _show_menu(self, pos):
        m = QMenu(self)
        m.addAction("💬 Open chat", self.open_chat)
        m.addAction("🛑 Stop daemon", self._stop_daemon)
        m.addSeparator()
        style_menu = m.addMenu("🎨 Orb style")
        face_act = style_menu.addAction("😊 Face")
        holo_act = style_menu.addAction("✨ Holo orb")
        face_act.setCheckable(True)
        holo_act.setCheckable(True)
        face_act.setChecked(self._style == "face")
        holo_act.setChecked(self._style == "holo")
        face_act.triggered.connect(lambda: self._set_style("face"))
        holo_act.triggered.connect(lambda: self._set_style("holo"))
        m.addSeparator()
        m.addAction("Quit", QApplication.quit)
        m.exec(pos)

    def _set_style(self, style: str):
        self._style = style
        self._settings.setValue(_STYLE_KEY, style)
        self.update()

    def _stop_daemon(self):
        cli._daemon_request({"type": "shutdown"}, cli.DAEMON_DEFAULT_PORT, timeout=5.0)

    def toggle_chat(self):
        if self.chat is not None and self.chat.isVisible():
            self.chat.hide()
        else:
            self.open_chat()

    def open_chat(self):
        if self.chat is None:
            self.chat = ChatWindow(self)
            # open next to the orb
            orb_pos = self.pos()
            screen = QApplication.primaryScreen().availableGeometry()
            x = min(orb_pos.x() + ORB_SIZE + 8, screen.right() - CHAT_W)
            y = max(0, orb_pos.y() - CHAT_H + ORB_SIZE)
            self.chat.move(x, y)
        self.chat.show()
        self.chat.raise_()
        self.chat.activateWindow()
        self.chat._input.setFocus()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(_app_icon())
    # The orb is the persistent window; closing chat must not quit the app.
    app.setQuitOnLastWindowClosed(False)
    orb = OrbWindow()
    orb.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
