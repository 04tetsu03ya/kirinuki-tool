"""
タイムライン：元動画の時間軸に、使っているカット・音の大きさ・再生位置を表示する。

  ・クリック／ドラッグ：その位置へ移動
  ・カットの端をドラッグ：開始・終了を調整
  ・カットをクリック：そのカットを選ぶ
  ・拡大率は set_zoom()（Ctrl＋ホイールでも）、ホイールで左右に移動
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

import engine as E

TICKS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
CLIP_COLOR = QColor(235, 70, 120)
SEL_COLOR = QColor(255, 190, 40)


class Timeline(QWidget):
    seekRequested = Signal(float)      # 秒
    clipSelected = Signal(int)         # カット番号
    clipEdited = Signal(int)           # カットの開始・終了をドラッグで変えた
    viewChanged = Signal()             # 拡大・スクロールが変わった（スクロールバー更新用）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(96)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.clips: list = []
        self.loud = None
        self.step = 0.5
        self.pos_sec = 0.0
        self.selected = -1
        self.zoom = 1.0      # 1 = 全体が幅に収まる
        self.offset = 0.0    # 左端の秒
        self.drag = None

    # ── データ ──
    def set_data(self, duration: float, clips: list, loud=None, step: float = 0.5):
        self.duration = max(0.0, duration)
        self.clips = clips
        self.loud = np.asarray(loud, np.float32) if loud is not None else None
        self.step = step
        self.clamp(); self.update()

    def set_position(self, sec: float, follow: bool = False):
        self.pos_sec = sec
        if follow and self.zoom > 1.01:
            left, right = self.offset, self.offset + self.visible_span()
            if not (left <= sec <= right):
                self.offset = sec - self.visible_span() * 0.1
                self.clamp(); self.viewChanged.emit()
        self.update()

    def set_selected(self, i: int):
        self.selected = i; self.update()

    # ── 表示範囲 ──
    def visible_span(self) -> float:
        return self.duration / self.zoom if self.duration else 1.0

    def pps(self) -> float:
        return max(1e-6, self.width() / self.visible_span())

    def x_of(self, t: float) -> float:
        return (t - self.offset) * self.pps()

    def t_of(self, x: float) -> float:
        return min(self.duration, max(0.0, self.offset + x / self.pps()))

    def clamp(self):
        span = self.visible_span()
        self.offset = min(max(0.0, self.offset), max(0.0, self.duration - span))

    def set_zoom(self, z: float, anchor_t: float | None = None):
        """拡大率を変える。anchor_t（秒）が画面上で同じ位置に残るようにする"""
        z = float(min(max(1.0, z), 2000.0))
        if anchor_t is None:
            anchor_t = self.pos_sec
        ax = self.x_of(anchor_t)
        self.zoom = z
        self.offset = anchor_t - ax / self.pps()
        self.clamp(); self.update(); self.viewChanged.emit()

    def set_offset(self, sec: float):
        self.offset = sec; self.clamp(); self.update()

    # ── 描画 ──
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#1e1e22"))
        ruler_h = 20
        track_top, track_h = ruler_h + 4, h - ruler_h - 8
        if not self.duration:
            p.setPen(QColor("#888")); p.drawText(self.rect(), Qt.AlignCenter, "動画を開くとタイムラインが表示されます")
            return
        # 目盛り
        tick = next((t for t in TICKS if t * self.pps() >= 70), TICKS[-1])
        f = QFont(); f.setPixelSize(11); p.setFont(f)
        t = (self.offset // tick) * tick
        while t <= self.offset + self.visible_span() + tick:
            x = self.x_of(t)
            p.setPen(QColor("#666")); p.drawLine(QPointF(x, ruler_h - 6), QPointF(x, h))
            p.setPen(QColor("#bbb")); p.drawText(QPointF(x + 3, 13), E.fmt_time(t) if tick < 1 else E.fmt_time(t)[:-2])
            t += tick
        # 音の大きさ（波形のような山）
        if self.loud is not None and len(self.loud):
            lo, hi = np.percentile(self.loud, 5), np.percentile(self.loud, 99.5)
            path = QPainterPath(QPointF(0, track_top + track_h))
            n = max(2, w // 2)
            for k in range(n + 1):
                x = k * w / n
                i0 = int(self.t_of(x) / self.step); i1 = max(i0 + 1, int(self.t_of(x + w / n) / self.step))
                v = float(self.loud[min(i0, len(self.loud) - 1): min(i1, len(self.loud))].max(initial=lo))
                r = min(1.0, max(0.0, (v - lo) / max(1e-6, hi - lo)))
                path.lineTo(QPointF(x, track_top + track_h - r * track_h * 0.9))
            path.lineTo(QPointF(w, track_top + track_h)); path.closeSubpath()
            p.fillPath(path, QColor(120, 140, 170, 120))
        # カット
        for i, c in enumerate(self.clips):
            x0, x1 = self.x_of(c.start), self.x_of(c.end)
            if x1 < 0 or x0 > w:
                continue
            col = SEL_COLOR if i == self.selected else CLIP_COLOR
            fill = QColor(col); fill.setAlpha(110)
            r = QRectF(x0, track_top, max(2.0, x1 - x0), track_h)
            p.fillRect(r, fill)
            p.setPen(QPen(col, 2)); p.drawRect(r)
            # 端のつまみ
            p.fillRect(QRectF(x0 - 1, track_top, 4, track_h), col)
            p.fillRect(QRectF(x1 - 3, track_top, 4, track_h), col)
            if x1 - x0 > 14:
                p.setPen(QColor("white")); f2 = QFont(); f2.setPixelSize(12); f2.setBold(True); p.setFont(f2)
                p.drawText(QRectF(x0 + 4, track_top + 2, x1 - x0 - 6, 16), Qt.AlignLeft, str(i + 1))
                p.setFont(f)
        # 再生位置
        x = self.x_of(self.pos_sec)
        p.setPen(QPen(QColor("#ff4040"), 2)); p.drawLine(QPointF(x, 0), QPointF(x, h))

    # ── マウス ──
    def hit(self, x: float, y: float):
        """(種類, カット番号)  種類: 'start' / 'end' / 'body' / None"""
        if y < 20:
            return None, -1
        tol = 6
        for i, c in enumerate(self.clips):
            x0, x1 = self.x_of(c.start), self.x_of(c.end)
            if abs(x - x0) <= tol:
                return "start", i
            if abs(x - x1) <= tol:
                return "end", i
        for i, c in enumerate(self.clips):
            if self.x_of(c.start) < x < self.x_of(c.end):
                return "body", i
        return None, -1

    def mousePressEvent(self, e):
        if not self.duration:
            return
        x, y = e.position().x(), e.position().y()
        kind, i = self.hit(x, y)
        if kind in ("start", "end"):
            self.drag = (kind, i)
            self.clipSelected.emit(i)
        else:
            if kind == "body":
                self.clipSelected.emit(i)
            self.drag = ("seek", -1)
            self.seekRequested.emit(self.t_of(x))

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        if self.drag is None:
            kind, _ = self.hit(x, y)
            self.setCursor(Qt.SizeHorCursor if kind in ("start", "end") else Qt.PointingHandCursor)
            return
        kind, i = self.drag
        t = self.t_of(x)
        if kind == "seek":
            self.seekRequested.emit(t)
        elif 0 <= i < len(self.clips):
            c = self.clips[i]
            if kind == "start":
                c.start = round(min(t, c.end - 0.3), 2)
            else:
                c.end = round(max(t, c.start + 0.3), 2)
            self.seekRequested.emit(t)
            self.update()

    def mouseReleaseEvent(self, e):
        if self.drag and self.drag[0] in ("start", "end"):
            self.clipEdited.emit(self.drag[1])
        self.drag = None

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if e.modifiers() & Qt.ControlModifier:
            self.set_zoom(self.zoom * (1.25 if dy > 0 else 0.8), self.t_of(e.position().x()))
        else:
            self.offset -= dy / 120 * self.visible_span() * 0.1
            self.clamp(); self.update(); self.viewChanged.emit()

    def resizeEvent(self, e):
        self.clamp(); self.viewChanged.emit()
