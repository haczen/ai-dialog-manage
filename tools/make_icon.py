# -*- coding: utf-8 -*-
"""Draw the app icon: a cute fluffy golden retriever waving a wrench,
wearing a hard hat. Flat cartoon style, transparent background.

Renders adm/assets/app_icon.png at 1024x1024 with Qt's QPainter, then a
follow-up Pillow step packs it into a multi-size .ico.
Run: uv run python tools/make_icon.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QImage, QPainter, QPainterPath,
                         QPen)

ASSET_DIR = Path(__file__).resolve().parent.parent / "adm" / "assets"

# palette — warm golden retriever tones on transparency
GOLD = "#eaa74e"          # base coat
GOLD_DARK = "#cd8636"     # ears, outer fluff shading
GOLD_LIGHT = "#f8d493"    # muzzle, chest, paws, inner ear
INK = "#2b2419"           # eyes / nose (warm black)
TONGUE = "#e8838f"
COLLAR = "#e0685f"
TAG = "#f2cc8f"
HELMET = "#e0483c"        # red safety helmet
HELMET_DARK = "#b93a30"
HELMET_LIGHT = "#f7837a"
WRENCH = "#bcc6d4"
WRENCH_DARK = "#8f9aad"
CHEEK = "#f0a58e"
SIZE = 1024


def _fluffy(cx: float, cy: float, rx: float, ry: float, *,
            bumps: int = 16, bump_r: float = 34.0,
            spread: float = 8.0) -> QPainterPath:
    """An ellipse whose rim is decorated with alternating fur bumps."""
    path = QPainterPath()
    path.addEllipse(QRectF(cx - rx, cy - ry, rx * 2, ry * 2))
    for i in range(bumps):
        a = 2.0 * math.pi * i / bumps
        x = cx + (rx + spread) * math.cos(a)
        y = cy + (ry + spread) * math.sin(a)
        r = bump_r * (1.28 if i % 2 == 0 else 0.86)
        bump = QPainterPath()
        bump.addEllipse(QRectF(x - r, y - r, r * 2, r * 2))
        path = path.united(bump)
    return path


def _ellipse(cx, cy, rx, ry) -> QPainterPath:
    p = QPainterPath()
    p.addEllipse(QRectF(cx - rx, cy - ry, rx * 2, ry * 2))
    return p


def _draw_wrench(p: QPainter) -> None:
    """Silver wrench: open jaw + long handle + tail ring, drawn at origin
    pointing up; caller translates/rotates/scales."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(WRENCH_DARK))
    p.drawRoundedRect(QRectF(-30, -70, 60, 330), 30, 30)      # handle
    head = _ellipse(0, -105, 95, 95)                           # jaw ring
    notch = QPainterPath()
    notch.moveTo(-36, -52)
    notch.lineTo(0, 42)
    notch.lineTo(36, -52)
    notch.lineTo(36, -235)
    notch.lineTo(-36, -235)
    notch.closeSubpath()
    p.setBrush(QColor(WRENCH))
    p.drawPath(head.subtracted(notch))
    ring = _ellipse(0, 262, 62, 62).subtracted(_ellipse(0, 262, 28, 28))
    p.drawPath(ring)
    # specular streak on the handle
    p.setBrush(QColor(255, 255, 255, 70))
    p.drawRoundedRect(QRectF(-22, -40, 12, 260), 6, 6)


def draw_icon() -> QImage:
    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)

    HEAD_CX, HEAD_CY = 468, 560          # head slightly left; arm waves right
    HEAD_RX, HEAD_RY = 236, 228

    # ---- raised arm holding the wrench (behind the head/body joints)
    arm_pen = QPen(QColor(GOLD), 96, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap)
    p.setPen(arm_pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    arm = QPainterPath(QPointF(590, 780))
    arm.cubicTo(QPointF(700, 720), QPointF(800, 640), QPointF(820, 520))
    p.drawPath(arm)

    # wrench in the raised paw, tilted like a friendly wave
    p.save()
    p.translate(838, 420)
    p.rotate(16)
    p.scale(0.92, 0.92)
    _draw_wrench(p)
    p.restore()

    # raised paw clutching the handle (over the wrench)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(GOLD_LIGHT))
    p.drawEllipse(QPointF(822, 505), 66, 58)
    p.setBrush(QColor("#e0b374"))
    for dx, dy in ((-18, 6), (8, 16), (24, -4)):
        p.drawEllipse(QPointF(822 + dx, 505 + dy), 15, 12)

    # ---- floppy ears (behind the head), golden-retriever style
    for side in (-1, 1):
        p.save()
        p.translate(HEAD_CX + side * 196, HEAD_CY + 30)
        p.rotate(side * 16)
        p.setBrush(QColor(GOLD_DARK))
        p.drawRoundedRect(QRectF(-84, -70, 168, 320), 84, 84)
        p.setBrush(QColor(GOLD))
        p.drawRoundedRect(QRectF(-60, -40, 120, 250), 60, 60)
        p.restore()

    # ---- chest (fluffy mounds below the head)
    p.setBrush(QColor(GOLD))
    p.drawPath(_fluffy(430, 905, 96, 90, bumps=12, bump_r=26))
    p.drawPath(_fluffy(512, 880, 110, 100, bumps=12, bump_r=28))
    p.drawPath(_fluffy(594, 905, 96, 90, bumps=12, bump_r=26))
    p.setBrush(QColor(GOLD_LIGHT))
    p.setOpacity(0.5)
    p.drawPath(_fluffy(512, 918, 66, 54, bumps=10, bump_r=18, spread=4))
    p.setOpacity(1.0)

    # ---- head with a fluffy rim
    p.setBrush(QColor(GOLD_DARK))
    p.drawPath(_fluffy(HEAD_CX, HEAD_CY, HEAD_RX + 10, HEAD_RY + 10,
                       bumps=18, bump_r=30))
    p.setBrush(QColor(GOLD))
    p.drawPath(_fluffy(HEAD_CX, HEAD_CY, HEAD_RX, HEAD_RY,
                       bumps=18, bump_r=26, spread=5))

    # soft top-light on the coat
    p.setBrush(QColor(GOLD_LIGHT))
    p.setOpacity(0.35)
    p.drawPath(_ellipse(HEAD_CX - 40, HEAD_CY - 110, 150, 80))
    p.setOpacity(1.0)

    # ---- safety hard hat (dome + brim + center ridge)
    p.setBrush(QColor(HELMET))
    dome = QPainterPath()  # upper-half arch, base on the brim line
    dome.moveTo(HEAD_CX - 218, HEAD_CY - 152)
    dome.arcTo(QRectF(HEAD_CX - 218, HEAD_CY - 302, 436, 300), 180, -180)
    dome.closeSubpath()
    p.drawPath(dome)
    p.setBrush(QColor(HELMET_DARK))
    p.drawRoundedRect(QRectF(HEAD_CX - 262, HEAD_CY - 152, 524, 46), 23, 23)
    p.setBrush(QColor(HELMET))
    p.drawRoundedRect(QRectF(HEAD_CX - 238, HEAD_CY - 158, 476, 34), 17, 17)
    # ridge
    p.setBrush(QColor(HELMET_LIGHT))
    p.drawRoundedRect(QRectF(HEAD_CX - 26, HEAD_CY - 300, 52, 190), 26, 26)
    # little shine
    p.setBrush(QColor(255, 255, 255, 90))
    p.drawEllipse(QPointF(HEAD_CX - 100, HEAD_CY - 232), 44, 26)

    # ---- face
    # eyes: big round ink + glints
    p.setBrush(QColor(INK))
    for cx in (HEAD_CX - 88, HEAD_CX + 88):
        p.drawEllipse(QPointF(cx, HEAD_CY - 38), 40, 44)
    p.setBrush(QColor("#ffffff"))
    for cx in (HEAD_CX - 98, HEAD_CX + 78):
        p.drawEllipse(QPointF(cx, HEAD_CY - 52), 13, 13)

    # muzzle
    p.setBrush(QColor(GOLD_LIGHT))
    p.drawEllipse(QPointF(HEAD_CX, HEAD_CY + 78), 152, 112)

    # nose
    p.setBrush(QColor(INK))
    p.drawEllipse(QPointF(HEAD_CX, HEAD_CY + 40), 46, 34)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(HEAD_CX - 15, HEAD_CY + 28), 12, 8)

    # smile
    p.setPen(QPen(QColor(INK), 12, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap))
    m1 = QPainterPath(QPointF(HEAD_CX, HEAD_CY + 74))
    m1.quadTo(HEAD_CX, HEAD_CY + 122, HEAD_CX - 46, HEAD_CY + 118)
    p.drawPath(m1)
    m2 = QPainterPath(QPointF(HEAD_CX, HEAD_CY + 74))
    m2.quadTo(HEAD_CX, HEAD_CY + 122, HEAD_CX + 46, HEAD_CY + 118)
    p.drawPath(m2)

    # tongue
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(TONGUE))
    p.drawRoundedRect(QRectF(HEAD_CX - 26, HEAD_CY + 112, 52, 58), 24, 24)

    # cheeks
    p.setBrush(QColor(CHEEK))
    p.setOpacity(0.55)
    p.drawEllipse(QPointF(HEAD_CX - 168, HEAD_CY + 42), 42, 28)
    p.drawEllipse(QPointF(HEAD_CX + 168, HEAD_CY + 42), 42, 28)
    p.setOpacity(1.0)

    # ---- collar with a little tag
    p.setBrush(QColor(COLLAR))
    p.drawRoundedRect(QRectF(HEAD_CX - 168, HEAD_CY + 218, 336, 58), 29, 29)
    p.setBrush(QColor(TAG))
    p.drawEllipse(QPointF(HEAD_CX, HEAD_CY + 288), 42, 42)
    p.setPen(QPen(QColor(INK), 7, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap))
    p.drawRoundedRect(QRectF(HEAD_CX - 5, HEAD_CY + 268, 10, 32), 5, 5)
    p.drawPoint(QPointF(HEAD_CX, HEAD_CY + 262))

    # ---- other paw resting on the chest
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(GOLD_LIGHT))
    p.drawEllipse(QPointF(352, 838), 58, 50)
    p.setBrush(QColor("#e0b374"))
    p.drawEllipse(QPointF(352, 838), 24, 18)

    p.end()
    return img


def main() -> int:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    img = draw_icon()
    out = ASSET_DIR / "app_icon.png"
    ok = img.save(str(out), "PNG")
    print(f"saved {out} ({img.width()}x{img.height()}) ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
