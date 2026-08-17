"""holo_orb.py — reusable JARVIS-style holographic wireframe orb renderer.

Pure QPainter drawing (no widget class), so both the desktop orb (orb.py)
and the HUD canvas (ui.py) can render the same structure. Inspired by the
classic sci-fi holo-sphere (Tron / Iron Man JARVIS):

  • a glowing purple wireframe sphere — great circles + latitude rings
  • a faint, larger sphere behind it for depth
  • two tilted rings orbiting around the sphere
  • a pulsing energized core + specular highlight
  • an optional status label under the orb (e.g. "SPEAKING…")

Everything is vector math on QPainter primitives — cheap enough for the
HUD's frame loop, and it animates from a single `t` clock.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QRadialGradient,
)

_PURPLE = (168, 85, 247)       # neon purple (core)
_PURPLE_D = (120, 60, 220)     # deep purple
_WIRE = (185, 120, 255)        # wireframe line
_CYAN = (90, 225, 255)         # status label / accents


def _q(rgb: tuple, a: int = 255) -> QColor:
    return QColor(rgb[0], rgb[1], rgb[2], max(0, min(255, a)))


def draw_holo_orb(
    p: QPainter,
    cx: float,
    cy: float,
    radius: float,
    t: float,
    speaking: bool = False,
    muted: bool = False,
    label: str = "",
) -> None:
    """Draw the holo orb centered at (cx, cy).

    t        — animation clock in seconds (drives rotation + rings)
    speaking — boosts the core glow and ring motion
    muted    — dims everything (low-power idle state)
    label    — optional status text drawn beneath the orb
    """
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    dim = 0.32 if muted else 1.0
    rot = t * 0.35                                   # slow sphere rotation
    core_pulse = 1.0 + 0.08 * math.sin(t * 2.2)
    ring_motion = 2.0 if speaking else 0.6

    # ── soft outer glow ──
    glow_r = radius * 1.55
    grad = QRadialGradient(cx, cy, glow_r)
    grad.setColorAt(0.0, _q(_PURPLE, int(75 * dim)))
    grad.setColorAt(1.0, _q(_PURPLE, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawEllipse(QRectF(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2))

    # ── faint larger sphere behind (depth) ──
    back_r = radius * 1.22
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(_q(_PURPLE, int(58 * dim)), 1.0))
    p.drawEllipse(QRectF(cx - back_r, cy - back_r, back_r * 2, back_r * 2))
    p.drawEllipse(QRectF(cx - back_r, cy - back_r * 0.52, back_r * 2, back_r))
    p.drawEllipse(QRectF(cx - back_r, cy + back_r * 0.52, back_r * 2, back_r))

    # ── wireframe sphere ──
    R = radius * 0.78
    p.save()
    p.translate(cx, cy)

    # great circles (meridians) rotated in-plane → gyroscope wireframe
    for k in range(3):
        p.save()
        p.rotate(rot + k * 60.0)
        p.setPen(QPen(_q(_WIRE, int(170 * dim)), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(-R, -R, R * 2, R * 2))
        p.restore()

    # latitude rings (offset up/down, squashed → sphere volume)
    for off, sy in ((0.5, 0.87), (-0.5, 0.87), (0.82, 0.5), (-0.82, 0.5)):
        p.save()
        p.rotate(rot * 0.6)
        p.setPen(QPen(_q(_WIRE, int(120 * dim * sy)), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(-R, -R * sy - R * off, R * 2, R * 2 * sy))
        p.restore()
    p.restore()

    # ── energized core ──
    core_r = radius * 0.30 * core_pulse
    cg = QRadialGradient(cx, cy, core_r)
    cg.setColorAt(0.0, _q((235, 205, 255), int(210 * dim)))
    cg.setColorAt(0.55, _q(_PURPLE, int(150 * dim)))
    cg.setColorAt(1.0, _q(_PURPLE_D, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(cg)
    p.drawEllipse(QRectF(cx - core_r, cy - core_r, core_r * 2, core_r * 2))

    # ── specular highlight (top-left) ──
    p.setBrush(_q((255, 255, 255), int(70 * dim)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(cx - R * 0.60, cy - R * 0.78, R * 0.44, R * 0.20))

    # ── orbiting rings (like the video's horizontal rings) ──
    for i, (scale, tilt, phase) in enumerate(
        ((1.62, 14.0, 0.0), (1.34, -11.0, 2.1))
    ):
        w = R * scale
        h = w * 0.30
        wob = 18.0 * math.sin(rot * 1.8 + phase)
        p.save()
        p.translate(cx, cy)
        p.rotate(tilt + wob)
        p.setPen(QPen(_q(_PURPLE, int((105 - i * 28) * dim)), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(-w / 2, -h / 2, w, h))
        p.restore()

    # ── status label ──
    if label:
        font = QFont("Consolas", max(7, int(radius * 0.16)))
        font.setBold(True)
        p.setFont(font)
        p.setPen(_q(_CYAN, int(235 * dim)))
        p.drawText(
            QRectF(cx - radius, cy + radius * 0.98, radius * 2, radius * 0.45),
            Qt.AlignmentFlag.AlignCenter,
            label,
        )

    p.restore()
