#!/usr/bin/env python3
"""
切り抜き動画ツール（Windows）

動画を入れる → 長さなどを決めて「自動で作る」→ 一覧とプレビューで直す → 書き出す
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QGraphicsItem, QGraphicsRectItem, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFrame, QGraphicsScene, QGraphicsSimpleTextItem,
                               QGraphicsView, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton, QScrollArea, QScrollBar,
                               QSlider, QSpinBox, QSplitter, QStyle, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

FROZEN = getattr(sys, "frozen", False)
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE))

import engine as E  # noqa: E402
import transcribe as T  # noqa: E402
import updater  # noqa: E402
import fonts as F  # noqa: E402
from timeline import Timeline  # noqa: E402

APP_TITLE = "切り抜き動画ツール"
APP_VERSION = "0.3.0"
FONT_DIR = str(HERE / "assets" / "fonts")
SFX_DIR = str(HERE / "assets" / "sfx")
VIDEO_EXT = "*.mp4 *.mov *.m4v *.mkv *.avi *.wmv *.webm *.ts *.flv"
CLIP_LENS = ["自動", "2秒", "3秒", "4秒", "5秒", "6秒", "8秒", "10秒", "15秒", "20秒"]


class Bus(QObject):
    """別スレッドから画面へ知らせるための信号"""
    progress = Signal(float, str)
    done = Signal(object)
    failed = Signal(str)


def run_bg(fn, on_done, on_fail, on_progress):
    bus = Bus()
    bus.done.connect(on_done)
    bus.failed.connect(on_fail)
    bus.progress.connect(on_progress)

    def work():
        try:
            bus.done.emit(fn(lambda r, m="": bus.progress.emit(float(r), m)))
        except Exception as e:
            bus.failed.emit(f"{e}\n\n{traceback.format_exc()}")
    t = threading.Thread(target=work, daemon=True)
    t._bus = bus  # 信号オブジェクトを生かしておく
    t.start()
    return t


def tsec(s: str) -> float:
    return E.parse_time(s)


class ColorButton(QPushButton):
    """押すと色を選べるボタン（色見本つき）"""
    changed = Signal()

    def __init__(self, color: str):
        super().__init__()
        self.color = color
        self.setMinimumWidth(110)
        self.clicked.connect(self.pick)
        self.set_color(color)

    def set_color(self, c: str):
        self.color = c.upper()
        light = QColor(c).lightness() > 140
        self.setText(self.color)
        self.setStyleSheet(f"background:{c}; color:{'#000' if light else '#fff'}; border:1px solid #888; padding:4px;")

    def pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, "色を選ぶ")
        if c.isValid():
            self.set_color(c.name()); self.changed.emit()


class FontCombo(QComboBox):
    """日本語フォントの選択（同梱フォント＋パソコンのフォント）。各項目はそのフォントで表示"""
    _fonts = None

    def __init__(self, allow_default: bool = False):
        super().__init__()
        self.setMaxVisibleItems(20)
        if FontCombo._fonts is None:
            FontCombo._fonts = F.all_fonts(T.data_dir())
            FontCombo._qt = {}
            for fi in FontCombo._fonts:
                if fi.bundled:
                    fid = QFontDatabase.addApplicationFont(fi.path)
                    fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
                    FontCombo._qt[fi.family] = fams[0] if fams else fi.family
                else:
                    FontCombo._qt[fi.family] = fi.family
        self.fonts = FontCombo._fonts
        if allow_default:
            self.addItem("全体と同じ", "")
        sep_done = False
        for fi in self.fonts:
            if not fi.bundled and not sep_done:
                self.insertSeparator(self.count()); sep_done = True
            self.addItem(fi.label, fi.family)
            self.setItemData(self.count() - 1, QFont(self.qt_family(fi.family), 12), Qt.FontRole)
        self.select_family("" if allow_default else F.DEFAULT_FAMILY)

    def qt_family(self, family: str) -> str:
        return FontCombo._qt.get(family, family)

    def family(self) -> str:
        return self.currentData() or ""

    def label_of(self, family: str) -> str:
        return next((f.label for f in self.fonts if f.family == family), family)

    def select_family(self, family: str):
        i = self.findData(family)
        if i >= 0:
            self.setCurrentIndex(i)


class LookDialog(QDialog):
    """選んだテロップだけの見た目（フォント・色・大きさ）"""

    def __init__(self, parent, cap, project, main_font_box):
        super().__init__(parent)
        self.setWindowTitle("選んだテロップの見た目")
        g = QGridLayout(self)
        g.addWidget(QLabel("フォント"), 0, 0)
        self.font = FontCombo(allow_default=True); self.font.select_family(cap.font); g.addWidget(self.font, 0, 1, 1, 2)
        self.use_col = QCheckBox("文字の色を変える"); self.use_col.setChecked(bool(cap.color)); g.addWidget(self.use_col, 1, 0)
        self.col = ColorButton(cap.color or project.caption_color); g.addWidget(self.col, 1, 1)
        self.use_out = QCheckBox("ふちの色を変える"); self.use_out.setChecked(bool(cap.outline)); g.addWidget(self.use_out, 2, 0)
        self.out = ColorButton(cap.outline or project.caption_outline); g.addWidget(self.out, 2, 1)
        self.col.changed.connect(lambda: self.use_col.setChecked(True))
        self.out.changed.connect(lambda: self.use_out.setChecked(True))
        self.use_size = QCheckBox("大きさを変える"); self.use_size.setChecked(bool(cap.scale)); g.addWidget(self.use_size, 3, 0)
        self.size = QSlider(Qt.Horizontal); self.size.setRange(40, 300); self.size.setValue(cap.scale or project.caption_scale)
        self.size_lbl = QLabel(f"{self.size.value()}%")
        self.size.valueChanged.connect(lambda v: (self.size_lbl.setText(f"{v}%"), self.use_size.setChecked(True)))
        g.addWidget(self.size, 3, 1); g.addWidget(self.size_lbl, 3, 2)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        g.addWidget(bb, 4, 0, 1, 3)

    def result_values(self):
        return (self.font.family(), self.col.color if self.use_col.isChecked() else "",
                self.out.color if self.use_out.isChecked() else "", self.size.value() if self.use_size.isChecked() else 0)


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE}  v{APP_VERSION}")
        self.resize(1360, 860)
        self.setAcceptDrops(True)
        self.project = E.Project()
        self.an = None          # 解析結果
        self.an_source = None
        self.busy = False
        self.jobs = []
        self.seq_play = False   # 全体を通して再生中
        self.seq_index = 0
        self.fired = set()      # 再生中に鳴らした効果音
        self.sounds = {}

        split = QSplitter(Qt.Horizontal)
        self.setCentralWidget(QWidget())
        root = QVBoxLayout(self.centralWidget())
        root.addWidget(split, 1)

        # ── 左：設定 ──
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(6, 6, 6, 6)
        g = QGroupBox("① 素材"); gl = QVBoxLayout(g)
        b = QPushButton("動画を開く…"); b.clicked.connect(self.open_video); gl.addWidget(b)
        self.src_lbl = QLabel("ここに動画をドラッグ＆ドロップしてもOK"); self.src_lbl.setWordWrap(True)
        gl.addWidget(self.src_lbl)
        lv.addWidget(g)

        g = QGroupBox("② 作り方"); gl = QGridLayout(g)
        self.mode_auto = QRadioButton("おまかせ（全体からまんべんなく）"); self.mode_auto.setChecked(True)
        self.mode_range = QRadioButton("範囲を指定")
        grp = QButtonGroup(self); grp.addButton(self.mode_auto); grp.addButton(self.mode_range)
        gl.addWidget(self.mode_auto, 0, 0, 1, 2); gl.addWidget(self.mode_range, 1, 0, 1, 2)
        gl.addWidget(QLabel("出来上がりの長さ"), 2, 0)
        self.len_spin = QSpinBox(); self.len_spin.setRange(5, 1800); self.len_spin.setValue(60); self.len_spin.setSuffix(" 秒")
        gl.addWidget(self.len_spin, 2, 1)
        gl.addWidget(QLabel("1カットの長さ"), 3, 0)
        self.cut_len = QComboBox(); self.cut_len.addItems(CLIP_LENS); gl.addWidget(self.cut_len, 3, 1)
        self.skip_edges = QCheckBox("最初と最後（オープニング等）を少し避ける"); self.skip_edges.setChecked(True)
        gl.addWidget(self.skip_edges, 4, 0, 1, 2)
        self.range_box = QGroupBox("範囲"); rl = QVBoxLayout(self.range_box)
        self.range_tbl = QTableWidget(0, 2); self.range_tbl.setHorizontalHeaderLabels(["開始", "終了"])
        self.range_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.range_tbl.setMinimumHeight(110); rl.addWidget(self.range_tbl)
        rb = QHBoxLayout()
        for text, fn in (("追加", self.add_range), ("今の位置を開始に", lambda: self.set_range_now(0)),
                         ("今の位置を終了に", lambda: self.set_range_now(1)), ("削除", self.del_range)):
            x = QPushButton(text); x.clicked.connect(fn); rb.addWidget(x)
        rl.addLayout(rb)
        self.range_use = QComboBox(); self.range_use.addItems(["範囲の中からおまかせで選ぶ", "範囲をそのまま使う"])
        rl.addWidget(self.range_use)
        gl.addWidget(self.range_box, 5, 0, 1, 2)
        self.mode_range.toggled.connect(lambda on: self.range_box.setVisible(on))
        self.range_box.setVisible(False)
        lv.addWidget(g)

        g = QGroupBox("③ 出力"); gl = QGridLayout(g)
        self.asp_h = QRadioButton("横 16:9（YouTube）"); self.asp_v = QRadioButton("縦 9:16（ショート等）")
        self.asp_h.setChecked(True)
        ag = QButtonGroup(self); ag.addButton(self.asp_h); ag.addButton(self.asp_v)
        gl.addWidget(self.asp_h, 0, 0, 1, 2); gl.addWidget(self.asp_v, 1, 0, 1, 2)
        gl.addWidget(QLabel("形が違うときの埋め方"), 2, 0)
        self.fill = QComboBox(); self.fill.addItems(["ぼかした映像で埋める", "中央を切り抜く"]); gl.addWidget(self.fill, 2, 1)
        gl.addWidget(QLabel("カットのつなぎ"), 3, 0)
        self.trans = QComboBox(); self.trans.addItems(["パッと切り替え", "フェード"]); gl.addWidget(self.trans, 3, 1)
        self.norm = QCheckBox("音量を自動でそろえる"); self.norm.setChecked(True); gl.addWidget(self.norm, 4, 0, 1, 2)
        gl.addWidget(QLabel("元の音の音量"), 5, 0)
        self.src_vol = QSlider(Qt.Horizontal); self.src_vol.setRange(0, 200); self.src_vol.setValue(100)
        gl.addWidget(self.src_vol, 5, 1)
        self.asp_v.toggled.connect(lambda _: (self.sync_settings(), self.fit_video()))
        self.fill.currentIndexChanged.connect(lambda _: self.fit_video())
        lv.addWidget(g)

        g = QGroupBox("④ テロップ"); gl = QGridLayout(g)
        self.auto_cap = QCheckBox("話している言葉を自動でテロップにする"); self.auto_cap.setChecked(True)
        gl.addWidget(self.auto_cap, 0, 0, 1, 2)
        gl.addWidget(QLabel("文字起こしの精度"), 1, 0)
        self.cap_model = QComboBox(); self.cap_model.addItems([v[0] for v in T.MODELS.values()]); gl.addWidget(self.cap_model, 1, 1)
        lv.addWidget(g)

        self.make_btn = QPushButton("▶ 自動で作る"); self.make_btn.setMinimumHeight(44)
        f = self.make_btn.font(); f.setPointSize(f.pointSize() + 3); f.setBold(True); self.make_btn.setFont(f)
        self.make_btn.clicked.connect(self.make_auto)
        lv.addWidget(self.make_btn)

        g = QGroupBox("⑤ テロップの見た目（全体・あとからいつでも変更OK）"); gl = QGridLayout(g)
        gl.addWidget(QLabel("フォント"), 0, 0)
        self.font_box = FontCombo(); self.font_box.currentIndexChanged.connect(self.on_look_changed)
        gl.addWidget(self.font_box, 0, 1, 1, 2)
        gl.addWidget(QLabel("文字の色"), 1, 0)
        self.col_btn = ColorButton("#FFFFFF"); self.col_btn.changed.connect(self.on_look_changed); gl.addWidget(self.col_btn, 1, 1)
        gl.addWidget(QLabel("ふちの色"), 2, 0)
        self.out_btn = ColorButton("#000000"); self.out_btn.changed.connect(self.on_look_changed); gl.addWidget(self.out_btn, 2, 1)
        pr = QHBoxLayout()
        for name, fg, bd in (("白", "#FFFFFF", "#000000"), ("黄", "#FFF000", "#000000"), ("黒", "#000000", "#FFFFFF"),
                             ("ピンク", "#FF69B4", "#FFFFFF"), ("水色", "#40D0FF", "#000000")):
            b = QPushButton(name); b.setMaximumWidth(56)
            b.clicked.connect(lambda _, a=fg, c=bd: (self.col_btn.set_color(a), self.out_btn.set_color(c), self.on_look_changed()))
            pr.addWidget(b)
        gl.addWidget(QLabel("早く選ぶ"), 3, 0); gl.addLayout(pr, 3, 1, 1, 2)
        gl.addWidget(QLabel("大きさ"), 4, 0)
        self.size_sl = QSlider(Qt.Horizontal); self.size_sl.setRange(40, 250); self.size_sl.setValue(100)
        self.size_lbl = QLabel("100%")
        self.size_sl.valueChanged.connect(lambda v: (self.size_lbl.setText(f"{v}%"), self.on_look_changed()))
        gl.addWidget(self.size_sl, 4, 1); gl.addWidget(self.size_lbl, 4, 2)
        lv.addWidget(g)

        lv.addStretch(1)
        sc = QScrollArea(); sc.setWidget(left); sc.setWidgetResizable(True); sc.setMinimumWidth(360)
        split.addWidget(sc)

        # ── 右：プレビューと編集 ──
        right = QWidget(); rv = QVBoxLayout(right); rv.setContentsMargins(6, 6, 6, 6)
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene); self.view.setBackgroundBrush(QColor("#111"))
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFrameShape(QFrame.NoFrame); self.view.setMinimumHeight(300)
        # 出来上がりの画面（横 16:9／縦 9:16）の枠。映像はこの枠の中に、書き出しと同じ形で映す
        self.frame = QGraphicsRectItem(); self.frame.setBrush(QColor("#2a2a2e")); self.frame.setPen(QPen(QColor("#555"), 1))
        self.frame.setFlag(QGraphicsItem.ItemClipsChildrenToShape, True)
        self.scene.addItem(self.frame)
        self.vitem = QGraphicsVideoItem(self.frame)
        self.cap_item = QGraphicsSimpleTextItem(""); self.scene.addItem(self.cap_item)
        self.cap_item.setBrush(QColor("white")); self.cap_item.setPen(QColor("black"))
        self.player = QMediaPlayer(self); self.player.setVideoOutput(self.vitem)
        if not os.environ.get("KIRINUKI_NO_AUDIO"):  # テスト環境（音が出ない機械）用のスイッチ
            self.audio = QAudioOutput(self); self.player.setAudioOutput(self.audio); self.audio.setVolume(0.8)
        self.player.positionChanged.connect(self.on_pos)
        self.player.durationChanged.connect(lambda d: self.seek.setRange(0, d))
        rv.addWidget(self.view, 3)
        pc = QHBoxLayout()
        self.play_btn = QPushButton(); self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.clicked.connect(self.toggle_play); pc.addWidget(self.play_btn)
        self.seek = QSlider(Qt.Horizontal); self.seek.sliderMoved.connect(lambda v: self.player.setPosition(v))
        pc.addWidget(self.seek, 1)
        self.time_lbl = QLabel("0:00.0"); pc.addWidget(self.time_lbl)
        pc.addWidget(QLabel("速度"))
        self.speed = QComboBox(); self.speed.addItems(["1倍", "1.5倍", "2倍"])
        self.speed.currentIndexChanged.connect(lambda i: self.player.setPlaybackRate([1.0, 1.5, 2.0][i]))
        pc.addWidget(self.speed)
        pc.addWidget(QLabel("音量"))
        self.pv_vol = QSlider(Qt.Horizontal); self.pv_vol.setRange(0, 100); self.pv_vol.setValue(80); self.pv_vol.setMaximumWidth(110)
        self.pv_vol.valueChanged.connect(self.on_preview_volume)
        pc.addWidget(self.pv_vol)
        x = QPushButton("選んだカットを再生"); x.clicked.connect(self.play_clip); pc.addWidget(x)
        x = QPushButton("全体を通して再生"); x.clicked.connect(self.play_all); pc.addWidget(x)
        rv.addLayout(pc)
        # タイムライン（拡大・縮小できる）
        self.tl = Timeline()
        self.tl.seekRequested.connect(lambda t: self.player.setPosition(int(t * 1000)))
        self.tl.clipSelected.connect(lambda i: self.clip_tbl.selectRow(i))
        self.tl.clipEdited.connect(lambda i: (self.fill_clip_table(), self.fill_cap_table(), self.update_total(),
                                              self.clip_tbl.selectRow(i)))
        self.tl.viewChanged.connect(self.sync_tl_scroll)
        rv.addWidget(self.tl)
        self.tl_scroll = QScrollBar(Qt.Horizontal)
        self.tl_scroll.valueChanged.connect(lambda v: self.tl.set_offset(v / 10))
        rv.addWidget(self.tl_scroll)
        zr = QHBoxLayout()
        zr.addWidget(QLabel("タイムラインの拡大"))
        self.zoom = QSlider(Qt.Horizontal); self.zoom.setRange(0, 100); self.zoom.setValue(0)
        self.zoom.valueChanged.connect(self.on_zoom); zr.addWidget(self.zoom, 1)
        self.zoom_lbl = QLabel("全体"); self.zoom_lbl.setMinimumWidth(110); zr.addWidget(self.zoom_lbl)
        x = QPushButton("全体を表示"); x.clicked.connect(lambda: self.zoom.setValue(0)); zr.addWidget(x)
        zr.addWidget(QLabel("（Ctrl＋ホイールでも拡大・縮小／カットの端をドラッグで長さ調整）"))
        rv.addLayout(zr)

        self.tabs = QTabWidget()
        # カット
        w = QWidget(); v = QVBoxLayout(w)
        self.clip_tbl = QTableWidget(0, 6)
        self.clip_tbl.setHorizontalHeaderLabels(["場面", "開始", "終了", "長さ", "音量%", "テロップ"])
        self.clip_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.clip_tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.clip_tbl.verticalHeader().setDefaultSectionSize(62)
        self.clip_tbl.setColumnWidth(0, 116)
        self.clip_tbl.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.clip_tbl.itemChanged.connect(self.on_clip_edit)
        self.clip_tbl.itemSelectionChanged.connect(self.on_clip_select)
        self.clip_tbl.cellDoubleClicked.connect(lambda r, c: self.play_clip() if c == 0 else None)
        v.addWidget(self.clip_tbl)
        hb = QHBoxLayout()
        for text, fn in (("↑ 上へ", lambda: self.move_clip(-1)), ("↓ 下へ", lambda: self.move_clip(1)),
                         ("別の場面に替える", self.replace_clip), ("今の位置にカットを追加", self.add_clip_here),
                         ("削除", self.del_clip)):
            x = QPushButton(text); x.clicked.connect(fn); hb.addWidget(x)
        v.addLayout(hb)
        self.tabs.addTab(w, "カット")
        # テロップ
        w = QWidget(); v = QVBoxLayout(w)
        self.cap_tbl = QTableWidget(0, 5)
        self.cap_tbl.setHorizontalHeaderLabels(["カット", "開始（秒）", "終了（秒）", "文字（クリックで直せます）", "見た目"])
        self.cap_tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.cap_tbl.setColumnWidth(4, 230)
        self.cap_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.cap_tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.cap_tbl.itemChanged.connect(self.on_cap_edit)
        self.cap_tbl.cellDoubleClicked.connect(self.jump_to_caption)
        v.addWidget(self.cap_tbl)
        hb = QHBoxLayout()
        for text, fn in (("今の位置にテロップを追加", self.add_caption), ("削除", self.del_caption),
                         ("選んだテロップの見た目を変える…", self.edit_caption_look),
                         ("全体の見た目に戻す", self.reset_caption_look),
                         ("文字起こしをやり直す", self.redo_transcribe)):
            x = QPushButton(text); x.clicked.connect(fn); hb.addWidget(x)
        v.addLayout(hb)
        self.tabs.addTab(w, "テロップ")
        # 効果音
        w = QWidget(); v = QVBoxLayout(w)
        self.sfx_tbl = QTableWidget(0, 4)
        self.sfx_tbl.setHorizontalHeaderLabels(["時間（出来上がりの頭から・秒）", "音", "音量%", "試聴"])
        self.sfx_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.sfx_tbl.setColumnWidth(0, 230)
        self.sfx_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sfx_tbl.itemChanged.connect(self.on_sfx_edit)
        v.addWidget(self.sfx_tbl)
        hb = QHBoxLayout()
        self.sfx_pick = QComboBox(); self.sfx_pick.addItems(list(E.BUILTIN_SFX)); hb.addWidget(self.sfx_pick)
        for text, fn in (("今の位置に追加", self.add_sfx), ("自分の音を追加…", self.add_own_sfx), ("削除", self.del_sfx)):
            x = QPushButton(text); x.clicked.connect(fn); hb.addWidget(x)
        v.addLayout(hb)
        self.tabs.addTab(w, "効果音")
        rv.addWidget(self.tabs, 2)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

        # ── 下：状態と書き出し ──
        bar = QHBoxLayout()
        self.total_lbl = QLabel("合計 0秒"); bar.addWidget(self.total_lbl)
        self.status = QLabel("動画を開いてください"); bar.addWidget(self.status, 1)
        self.prog = QProgressBar(); self.prog.setMaximumWidth(260); self.prog.setRange(0, 1000); bar.addWidget(self.prog)
        for text, fn in (("プロジェクトを開く", self.load_project), ("保存", self.save_project),
                         ("更新を確認", lambda: self.check_update(True))):
            x = QPushButton(text); x.clicked.connect(fn); bar.addWidget(x)
        self.export_btn = QPushButton("書き出す…"); self.export_btn.setMinimumHeight(36)
        f = self.export_btn.font(); f.setBold(True); self.export_btn.setFont(f)
        self.export_btn.clicked.connect(self.export); bar.addWidget(self.export_btn)
        root.addLayout(bar)
        QTimer.singleShot(3000, lambda: self.check_update(False))
        self._loading_tables = False

    # ── 共通 ──
    def msg(self, text: str):
        self.status.setText(text)

    def set_busy(self, on: bool):
        self.busy = on
        for b in (self.make_btn, self.export_btn):
            b.setEnabled(not on)
        if not on:
            self.prog.setValue(0)

    def on_progress(self, r: float, m: str):
        self.prog.setValue(int(r * 1000))
        if m:
            self.msg(m)

    def fail(self, err: str):
        self.set_busy(False)
        self.msg("エラーが発生しました")
        QMessageBox.warning(self, APP_TITLE, err.split("\n\n")[0])

    def sync_settings(self):
        p = self.project
        p.aspect = "9:16" if self.asp_v.isChecked() else "16:9"
        p.vertical_fill = "blur" if self.fill.currentIndex() == 0 else "crop"
        p.fade = self.trans.currentIndex() == 1
        p.normalize = self.norm.isChecked()
        p.source_volume = self.src_vol.value() / 100
        p.caption_font = self.font_box.family() or F.DEFAULT_FAMILY
        p.caption_color = self.col_btn.color
        p.caption_outline = self.out_btn.color
        p.caption_scale = self.size_sl.value()

    def on_look_changed(self, *_):
        """全体の見た目を変えたら、すぐプレビューに反映"""
        self.sync_settings()
        self._cap_key = "x"; self.on_pos(self.player.position())
        self.fill_cap_table()

    def load_look(self, p: E.Project):
        for w in (self.font_box, self.col_btn, self.out_btn, self.size_sl):
            w.blockSignals(True)
        self.font_box.select_family(p.caption_font)
        self.col_btn.set_color(p.caption_color); self.out_btn.set_color(p.caption_outline)
        self.size_sl.setValue(p.caption_scale); self.size_lbl.setText(f"{p.caption_scale}%")
        for w in (self.font_box, self.col_btn, self.out_btn, self.size_sl):
            w.blockSignals(False)

    # ── 素材 ──
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls()]
        if urls:
            self.load_video(urls[0])

    def open_video(self):
        f, _ = QFileDialog.getOpenFileName(self, "動画を選んでください", "", f"動画 ({VIDEO_EXT});;すべて (*.*)")
        if f:
            self.load_video(f)

    def load_video(self, path: str):
        if self.busy:
            return
        try:
            info = E.probe(path)
        except Exception as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return
        self.project = E.Project(source=path)
        self.sync_settings() if hasattr(self, "font_box") else None
        self.an = None
        self.src_duration = info["duration"]
        self.src_lbl.setText(f"{Path(path).name}\n長さ {E.fmt_time(info['duration'])}・{info['width']}×{info['height']}")
        if info["height"] > info["width"]:
            self.asp_v.setChecked(True)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.pause()
        self.refresh_all()
        self.msg("長さなどを決めて「自動で作る」を押してください")

    # ── 範囲 ──
    def add_range(self):
        r = self.range_tbl.rowCount(); self.range_tbl.insertRow(r)
        now = self.player.position() / 1000
        self.range_tbl.setItem(r, 0, QTableWidgetItem(E.fmt_time(now)))
        self.range_tbl.setItem(r, 1, QTableWidgetItem(E.fmt_time(now + 60)))

    def set_range_now(self, col: int):
        r = self.range_tbl.currentRow()
        if r < 0:
            self.add_range(); r = self.range_tbl.rowCount() - 1
        self.range_tbl.setItem(r, col, QTableWidgetItem(E.fmt_time(self.player.position() / 1000)))

    def del_range(self):
        r = self.range_tbl.currentRow()
        if r >= 0:
            self.range_tbl.removeRow(r)

    def ranges(self) -> list:
        out = []
        for r in range(self.range_tbl.rowCount()):
            try:
                a, b = tsec(self.range_tbl.item(r, 0).text()), tsec(self.range_tbl.item(r, 1).text())
                if b > a:
                    out.append((a, b))
            except Exception:
                raise RuntimeError(f"範囲の {r + 1} 行目の時間が読めません（例：1:23 や 1:02:03）")
        return out

    # ── 自動で作る ──
    def make_auto(self):
        if not self.project.source:
            QMessageBox.information(self, APP_TITLE, "先に動画を開いてください"); return
        try:
            rng = self.ranges() if self.mode_range.isChecked() else None
        except RuntimeError as e:
            QMessageBox.warning(self, APP_TITLE, str(e)); return
        if self.mode_range.isChecked() and not rng:
            QMessageBox.information(self, APP_TITLE, "範囲を追加してください"); return
        target = float(self.len_spin.value())
        cl = None if self.cut_len.currentIndex() == 0 else float(CLIP_LENS[self.cut_len.currentIndex()][:-1])
        use_as_is = self.mode_range.isChecked() and self.range_use.currentIndex() == 1
        skip = self.skip_edges.isChecked()
        do_cap = self.auto_cap.isChecked()
        model = list(T.MODELS)[self.cap_model.currentIndex()]
        src = self.project.source
        need_an = self.an is None or self.an_source != src

        def work(progress):
            an = self.an
            if need_an:
                an = E.analyze(src, lambda r, m: progress(r * (0.5 if do_cap else 1.0), m))
            if use_as_is:
                clips = [E.Clip(a, b) for a, b in rng]
            else:
                clips = E.auto_digest(an, target, cl, rng, skip_edges=skip)
            if do_cap and an["info"]["has_audio"]:
                T.transcribe_clips(src, clips, model,
                                   progress=lambda r, m: progress(0.5 + r * 0.5 if need_an else r, m))
            return an, clips

        def done(res):
            self.an, clips = res
            self.an_source = src
            self.project.clips = clips
            self.project.sfx = []
            self.set_busy(False)
            self.refresh_all()
            self.msg(f"{len(clips)}カット・合計{self.project.total():.0f}秒を作りました。プレビューで確認して、必要なら直してください")
            if clips:
                self.clip_tbl.selectRow(0)
        self.set_busy(True)
        self.jobs.append(run_bg(work, done, self.fail, self.on_progress))

    # ── 表の更新 ──
    def refresh_all(self):
        self.fill_clip_table(); self.fill_cap_table(); self.fill_sfx_table(); self.update_total()
        self.refresh_timeline()

    def refresh_timeline(self):
        dur = self.an["duration"] if self.an else (self.player.duration() / 1000 or getattr(self, "src_duration", 0))
        loud = self.an["loud"] if self.an and self.an_source == self.project.source else None
        self.tl.set_data(dur, self.project.clips, loud, self.an["step"] if self.an else 0.5)
        self.sync_tl_scroll()

    def update_total(self):
        t = self.project.total()
        self.total_lbl.setText(f"合計 {t:.1f}秒（{len(self.project.clips)}カット）")

    def fill_clip_table(self):
        self._loading_tables = True
        tb = self.clip_tbl
        tb.setRowCount(len(self.project.clips))
        for i, c in enumerate(self.project.clips):
            it = QTableWidgetItem(f" {i + 1}"); it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            tb.setItem(i, 0, it)
            self.set_thumb(i, c)
            tb.setItem(i, 1, QTableWidgetItem(E.fmt_time(c.start)))
            tb.setItem(i, 2, QTableWidgetItem(E.fmt_time(c.end)))
            it = QTableWidgetItem(f"{c.length:.1f}秒"); it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            tb.setItem(i, 3, it)
            tb.setItem(i, 4, QTableWidgetItem(str(int(round(c.volume * 100)))))
            caps = " / ".join(x.text for x in c.captions)
            it = QTableWidgetItem(caps); it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            tb.setItem(i, 5, it)
        self._loading_tables = False

    def set_thumb(self, row: int, c: E.Clip):
        key = (self.project.source, round(c.start, 1))
        pm = getattr(self, "_thumbs", {}).get(key)
        if pm is None:
            try:
                data = E.thumbnail(self.project.source, c.start + min(0.5, c.length / 2), 160)
                pm = QPixmap(); pm.loadFromData(data)
            except Exception:
                pm = QPixmap()
            self._thumbs = getattr(self, "_thumbs", {}); self._thumbs[key] = pm
        lbl = QLabel(); lbl.setAlignment(Qt.AlignCenter)
        if not pm.isNull():
            lbl.setPixmap(pm.scaled(110, 58, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.clip_tbl.setCellWidget(row, 0, lbl)

    def on_clip_edit(self, item: QTableWidgetItem):
        if self._loading_tables:
            return
        r, col = item.row(), item.column()
        if r >= len(self.project.clips):
            return
        c = self.project.clips[r]
        try:
            if col == 1:
                c.start = min(tsec(item.text()), c.end - 0.3)
            elif col == 2:
                c.end = max(tsec(item.text()), c.start + 0.3)
            elif col == 4:
                c.volume = max(0, min(300, float(item.text()))) / 100
        except Exception:
            self.msg("時間は 1:23.5 のように入力してください")
        dur = self.an["duration"] if self.an else None
        if dur:
            c.end = min(c.end, dur)
        self.fill_clip_table(); self.fill_cap_table(); self.update_total(); self.tl.update()

    def selected_clip(self) -> int:
        r = self.clip_tbl.currentRow()
        return r if 0 <= r < len(self.project.clips) else -1

    def on_clip_select(self):
        r = self.selected_clip()
        self.tl.set_selected(r)
        if r >= 0 and not self.seq_play:
            self.player.pause()
            self.player.setPosition(int(self.project.clips[r].start * 1000))

    def move_clip(self, d: int):
        r = self.selected_clip()
        if r < 0 or not (0 <= r + d < len(self.project.clips)):
            return
        cl = self.project.clips
        cl[r], cl[r + d] = cl[r + d], cl[r]
        self.refresh_all(); self.clip_tbl.selectRow(r + d)

    def del_clip(self):
        r = self.selected_clip()
        if r >= 0:
            self.project.clips.pop(r); self.refresh_all()

    def add_clip_here(self):
        if not self.project.source:
            return
        now = self.player.position() / 1000
        L = 3.0 if self.cut_len.currentIndex() == 0 else float(CLIP_LENS[self.cut_len.currentIndex()][:-1])
        r = self.selected_clip()
        self.project.clips.insert(r + 1 if r >= 0 else len(self.project.clips), E.Clip(round(now, 2), round(now + L, 2)))
        self.refresh_all(); self.clip_tbl.selectRow(r + 1 if r >= 0 else len(self.project.clips) - 1)

    def replace_clip(self):
        r = self.selected_clip()
        if r < 0 or self.an is None:
            return
        c = self.project.clips[r]
        used = getattr(c, "_tried", [c.start])
        alts = E.alternatives(self.an, c, count=8)
        nxt = next((s for s in alts if all(abs(s - u) > c.length * 0.8 for u in used)), None)
        if nxt is None:
            self.msg("このあたりに他の候補が見つかりませんでした"); return
        L = c.length
        c.start, c.end = round(nxt, 2), round(nxt + L, 2)
        c.captions = []
        c._tried = used + [nxt]
        self.refresh_all(); self.clip_tbl.selectRow(r)
        self.msg("別の場面に替えました（テロップはこのカットだけ「文字起こしをやり直す」で作り直せます）")

    # ── テロップ ──
    def fill_cap_table(self):
        self._loading_tables = True
        rows = [(ci, k, cap) for ci, c in enumerate(self.project.clips) for k, cap in enumerate(c.captions)]
        self._cap_rows = rows
        tb = self.cap_tbl
        tb.setRowCount(len(rows))
        for i, (ci, k, cap) in enumerate(rows):
            it = QTableWidgetItem(str(ci + 1)); it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            tb.setItem(i, 0, it)
            tb.setItem(i, 1, QTableWidgetItem(f"{cap.start:.1f}"))
            tb.setItem(i, 2, QTableWidgetItem(f"{cap.end:.1f}"))
            tb.setItem(i, 3, QTableWidgetItem(cap.text))
            it = QTableWidgetItem(self.look_text(cap)); it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            if cap.color:
                it.setForeground(QColor(cap.color)); it.setBackground(QColor(cap.outline or "#000000"))
            tb.setItem(i, 4, it)
        self._loading_tables = False

    def look_text(self, cap) -> str:
        if not (cap.font or cap.color or cap.outline or cap.scale):
            return "全体と同じ"
        parts = []
        if cap.font:
            parts.append(self.font_box.label_of(cap.font))
        if cap.color:
            parts.append(f"色 {cap.color}")
        if cap.scale:
            parts.append(f"{cap.scale}%")
        return "・".join(parts)

    def selected_caps(self) -> list:
        rows = sorted({i.row() for i in self.cap_tbl.selectedItems()})
        return [self._cap_rows[r][2] for r in rows if r < len(self._cap_rows)]

    def edit_caption_look(self):
        caps = self.selected_caps()
        if not caps:
            QMessageBox.information(self, APP_TITLE, "見た目を変えたいテロップを一覧で選んでください（Ctrl/Shiftで複数選べます）"); return
        self.sync_settings()
        d = LookDialog(self, caps[0], self.project, self.font_box)
        if d.exec() == QDialog.Accepted:
            font, color, outline, scale = d.result_values()
            for c in caps:
                c.font, c.color, c.outline, c.scale = font, color, outline, scale
            self.fill_cap_table(); self._cap_key = "x"; self.on_pos(self.player.position())

    def reset_caption_look(self):
        for c in self.selected_caps():
            c.font, c.color, c.outline, c.scale = "", "", "", 0
        self.fill_cap_table(); self._cap_key = "x"; self.on_pos(self.player.position())

    def on_cap_edit(self, item):
        if self._loading_tables or item.row() >= len(self._cap_rows):
            return
        ci, k, cap = self._cap_rows[item.row()]
        try:
            if item.column() == 1:
                cap.start = max(0.0, float(item.text()))
            elif item.column() == 2:
                cap.end = max(cap.start + 0.2, float(item.text()))
            elif item.column() == 3:
                cap.text = item.text()
        except ValueError:
            self.msg("秒は数字で入力してください")
        self.fill_clip_table()

    def jump_to_caption(self, row, col):
        if col != 3 and row < len(self._cap_rows):
            ci, k, cap = self._cap_rows[row]
            self.player.setPosition(int((self.project.clips[ci].start + cap.start) * 1000))

    def clip_at_source(self, t: float) -> int:
        for i, c in enumerate(self.project.clips):
            if c.start - 0.05 <= t <= c.end + 0.05:
                return i
        return self.selected_clip()

    def add_caption(self):
        now = self.player.position() / 1000
        ci = self.clip_at_source(now)
        if ci < 0:
            QMessageBox.information(self, APP_TITLE, "テロップを入れたいカットを選ぶか、カットの中で一時停止してください"); return
        c = self.project.clips[ci]
        st = max(0.0, min(c.length - 0.5, now - c.start))
        c.captions.append(E.Caption(round(st, 2), round(min(c.length, st + 2.0), 2), "ここに文字を入力"))
        c.captions.sort(key=lambda x: x.start)
        self.fill_cap_table(); self.fill_clip_table(); self.tabs.setCurrentIndex(1)

    def del_caption(self):
        r = self.cap_tbl.currentRow()
        if 0 <= r < len(self._cap_rows):
            ci, k, cap = self._cap_rows[r]
            self.project.clips[ci].captions.remove(cap)
            self.fill_cap_table(); self.fill_clip_table()

    def redo_transcribe(self):
        if not self.project.clips:
            return
        r = self.selected_clip()
        target = [self.project.clips[r]] if r >= 0 and self.tabs.currentIndex() == 0 else self.project.clips
        model = list(T.MODELS)[self.cap_model.currentIndex()]
        src = self.project.source

        def done(_):
            self.set_busy(False); self.fill_cap_table(); self.fill_clip_table()
        self.set_busy(True)
        self.jobs.append(run_bg(lambda p: T.transcribe_clips(src, target, model, progress=p), done, self.fail, self.on_progress))

    # ── 効果音 ──
    def fill_sfx_table(self):
        self._loading_tables = True
        tb = self.sfx_tbl
        tb.setRowCount(len(self.project.sfx))
        for i, s in enumerate(self.project.sfx):
            tb.setItem(i, 0, QTableWidgetItem(f"{s.time:.1f}"))
            name = s.sound if s.sound in E.BUILTIN_SFX else Path(s.sound).name
            it = QTableWidgetItem(name); it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            tb.setItem(i, 1, it)
            tb.setItem(i, 2, QTableWidgetItem(str(int(round(s.volume * 100)))))
            b = QPushButton("▶"); b.clicked.connect(lambda _, x=s: self.play_sfx(x)); tb.setCellWidget(i, 3, b)
        self._loading_tables = False

    def on_sfx_edit(self, item):
        if self._loading_tables or item.row() >= len(self.project.sfx):
            return
        s = self.project.sfx[item.row()]
        try:
            if item.column() == 0:
                s.time = max(0.0, float(item.text()))
            elif item.column() == 2:
                s.volume = max(0, min(300, float(item.text()))) / 100
        except ValueError:
            self.msg("数字で入力してください")
        self.project.sfx.sort(key=lambda x: x.time)
        self.fill_sfx_table()

    def output_time_now(self) -> float:
        """今の再生位置が、出来上がり動画の何秒目か"""
        now = self.player.position() / 1000
        t0 = 0.0
        for c in self.project.clips:
            if c.start - 0.05 <= now <= c.end + 0.05:
                return t0 + max(0.0, now - c.start)
            t0 += c.length
        return 0.0

    def add_sfx(self, path: str | None = None):
        sound = path or self.sfx_pick.currentText()
        self.project.sfx.append(E.Sfx(round(self.output_time_now(), 2), sound, 1.0))
        self.project.sfx.sort(key=lambda x: x.time)
        self.fill_sfx_table(); self.tabs.setCurrentIndex(2)

    def add_own_sfx(self):
        f, _ = QFileDialog.getOpenFileName(self, "効果音のファイル", "", "音声 (*.wav *.mp3 *.m4a *.aac *.ogg);;すべて (*.*)")
        if f:
            self.add_sfx(f)

    def del_sfx(self):
        r = self.sfx_tbl.currentRow()
        if 0 <= r < len(self.project.sfx):
            self.project.sfx.pop(r); self.fill_sfx_table()

    def play_sfx(self, s: E.Sfx):
        p = E.resolve_sfx(s.sound, SFX_DIR)
        if not p or os.environ.get("KIRINUKI_NO_AUDIO"):
            return
        if p.lower().endswith(".wav"):
            se = self.sounds.get(p)
            if se is None:
                se = QSoundEffect(self); se.setSource(QUrl.fromLocalFile(p)); self.sounds[p] = se
            se.setVolume(min(1.0, s.volume) * self.pv_vol.value() / 100); se.play()
        else:
            mp = QMediaPlayer(self); ao = QAudioOutput(self); mp.setAudioOutput(ao); ao.setVolume(min(1.0, s.volume) * self.pv_vol.value() / 100)
            mp.setSource(QUrl.fromLocalFile(p)); mp.play(); self.sounds[("tmp", p)] = (mp, ao)

    # ── プレビュー ──
    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self.fit_video)

    def frame_rect(self) -> QRectF:
        """プレビューの中の「出来上がりの画面」の位置と大きさ"""
        vw, vh = self.view.viewport().width(), self.view.viewport().height()
        vertical = self.asp_v.isChecked() if hasattr(self, "asp_v") else False
        ar = 9 / 16 if vertical else 16 / 9
        w = min(vw, vh * ar); h = w / ar
        return QRectF((vw - w) / 2, (vh - h) / 2, w, h)

    def fit_video(self):
        vw, vh = self.view.viewport().width(), self.view.viewport().height()
        self.scene.setSceneRect(QRectF(0, 0, vw, vh))
        fr = self.frame_rect()
        self.frame.setRect(QRectF(0, 0, fr.width(), fr.height())); self.frame.setPos(fr.topLeft())
        crop = hasattr(self, "fill") and self.fill.currentIndex() == 1
        self.vitem.setAspectRatioMode(Qt.KeepAspectRatioByExpanding if crop else Qt.KeepAspectRatio)
        self.vitem.setSize(QSizeF(fr.width(), fr.height())); self.vitem.setPos(0, 0)
        self._cap_key = "x"
        if hasattr(self, "tl"):
            self.on_pos(self.player.position())

    def place_caption(self):
        fr = self.frame_rect()
        vertical = fr.height() > fr.width()
        br = self.cap_item.boundingRect()
        margin = fr.height() * (0.22 if vertical else 0.07)  # 書き出しと同じ下からの余白
        self.cap_item.setPos(fr.x() + (fr.width() - br.width()) / 2, fr.bottom() - margin - br.height())

    def on_zoom(self, v: int):
        z = 2000 ** (v / 100)
        if abs(self.tl.zoom - z) > 1e-3:
            self.tl.set_zoom(z)
        span = self.tl.visible_span()
        self.zoom_lbl.setText("全体" if v == 0 else f"×{z:.0f}（約{E.fmt_time(span)[:-2]}幅）")

    def sync_tl_scroll(self):
        span = self.tl.visible_span()
        self.tl_scroll.blockSignals(True)
        self.tl_scroll.setRange(0, int(max(0.0, self.tl.duration - span) * 10))
        self.tl_scroll.setPageStep(max(1, int(span * 10)))
        self.tl_scroll.setValue(int(self.tl.offset * 10))
        self.tl_scroll.blockSignals(False)
        # Ctrl＋ホイールで変わった拡大率をスライダーにも反映
        import math
        v = int(round(100 * math.log(max(1.0, self.tl.zoom)) / math.log(2000)))
        if v != self.zoom.value():
            self.zoom.blockSignals(True); self.zoom.setValue(v); self.zoom.blockSignals(False)
            self.on_zoom(v)

    def on_preview_volume(self, v: int):
        """プレビューの音量（書き出す動画の音量は変わらない）"""
        if hasattr(self, "audio"):
            self.audio.setVolume(v / 100)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause(); self.seq_play = False
        else:
            self.player.play()
        self.update_play_icon()

    def update_play_icon(self):
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay))

    def play_clip(self):
        r = self.selected_clip()
        if r < 0:
            return
        self.seq_play = True; self.seq_index = r; self.seq_single = True
        self.player.setPosition(int(self.project.clips[r].start * 1000)); self.player.play(); self.update_play_icon()

    def play_all(self):
        if not self.project.clips:
            return
        self.seq_play = True; self.seq_index = 0; self.seq_single = False; self.fired = set()
        self.player.setPosition(int(self.project.clips[0].start * 1000)); self.player.play(); self.update_play_icon()

    def on_pos(self, ms: int):
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        now = ms / 1000
        self.time_lbl.setText(E.fmt_time(now))
        self.tl.set_position(now, follow=self.player.playbackState() == QMediaPlayer.PlayingState)
        # カットの終わりで次のカットへ（全体を通して再生）
        if self.seq_play and 0 <= self.seq_index < len(self.project.clips):
            c = self.project.clips[self.seq_index]
            if now >= c.end:
                if getattr(self, "seq_single", False) or self.seq_index + 1 >= len(self.project.clips):
                    self.player.pause(); self.seq_play = False; self.update_play_icon()
                else:
                    self.seq_index += 1
                    self.player.setPosition(int(self.project.clips[self.seq_index].start * 1000))
                    return
            # 効果音
            out_t = sum(x.length for x in self.project.clips[: self.seq_index]) + max(0, now - c.start)
            for k, s in enumerate(self.project.sfx):
                if k not in self.fired and 0 <= out_t - s.time < 0.3:
                    self.fired.add(k); self.play_sfx(s)
        # テロップの重ね表示（実際のフォント・色・大きさで）
        found = None
        for c in self.project.clips:
            if c.start <= now <= c.end:
                for cap in c.captions:
                    if cap.start <= now - c.start <= cap.end:
                        found = cap; break
                break
        key = None if found is None else (found.text, found.font, found.color, found.outline, found.scale,
                                          self.project.caption_font, self.project.caption_color,
                                          self.project.caption_outline, self.project.caption_scale, self.project.aspect)
        if key != getattr(self, "_cap_key", "x"):
            self._cap_key = key
            self.show_caption(found)

    def show_caption(self, cap):
        if cap is None or not cap.text.strip():
            self.cap_item.setText(""); return
        p = self.project
        W, H = E.output_size(p)
        k = self.frame_rect().height() / H  # 出来上がりの画面 → プレビューの大きさ
        base = E.base_caption_size(W, H)
        size = base * ((cap.scale or p.caption_scale) / 100) * k
        f = QFont(self.font_box.qt_family(cap.font or p.caption_font)); f.setPixelSize(max(10, int(size)))
        per = E.chars_per_line(W, H, cap.scale or p.caption_scale)
        self.cap_item.setFont(f)
        self.cap_item.setText(E.wrap_text(cap.text, per, 3 if H > W else 2).replace("\\N", "\n"))
        self.cap_item.setBrush(QColor(cap.color or p.caption_color))
        self.cap_item.setPen(QPen(QColor(cap.outline or p.caption_outline), max(1.5, size / 14)))
        self.place_caption()

    # ── 書き出し・保存 ──
    def export(self):
        if not self.project.clips:
            QMessageBox.information(self, APP_TITLE, "先に「自動で作る」か、カットを追加してください"); return
        self.sync_settings()
        src = Path(self.project.source)
        tag = "縦" if self.project.aspect == "9:16" else "横"
        default = str(src.with_name(f"{src.stem}_切り抜き_{int(self.project.total())}秒_{tag}.mp4"))
        f, _ = QFileDialog.getSaveFileName(self, "書き出す場所", default, "MP4 (*.mp4)")
        if not f:
            return
        proj = E.Project.from_json(self.project.to_json())

        def done(out):
            self.set_busy(False)
            self.msg(f"書き出しました：{Path(out).name}")
            if sys.platform.startswith("win"):
                os.startfile(str(Path(out).parent))  # type: ignore[attr-defined]
        self.set_busy(True)
        self.msg("書き出し中…")
        fams = {proj.caption_font} | {cap.font for c in proj.clips for cap in c.captions if cap.font}
        files = F.files_for(fams, self.font_box.fonts)
        self.jobs.append(run_bg(lambda p: E.render(proj, f, lambda r: p(r, f"書き出し中… {r*100:.0f}%"),
                                                   sfx_dir=SFX_DIR, font_files=files),
                                done, self.fail, self.on_progress))

    def save_project(self):
        if not self.project.source:
            return
        self.sync_settings()
        src = Path(self.project.source)
        f, _ = QFileDialog.getSaveFileName(self, "プロジェクトを保存", str(src.with_suffix(".kirinuki")),
                                           "切り抜きプロジェクト (*.kirinuki)")
        if f:
            Path(f).write_text(self.project.to_json(), encoding="utf-8")
            self.msg("保存しました")

    def load_project(self):
        f, _ = QFileDialog.getOpenFileName(self, "プロジェクトを開く", "", "切り抜きプロジェクト (*.kirinuki)")
        if not f:
            return
        try:
            p = E.Project.from_json(Path(f).read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.warning(self, APP_TITLE, f"開けませんでした：{e}"); return
        if not Path(p.source).exists():
            QMessageBox.warning(self, APP_TITLE, f"元の動画が見つかりません：\n{p.source}"); return
        self.load_video(p.source)
        self.project = p
        (self.asp_v if p.aspect == "9:16" else self.asp_h).setChecked(True)
        self.load_look(p)
        self.refresh_all()
        self.msg("プロジェクトを開きました")

    # ── 自動更新 ──
    def check_update(self, manual: bool):
        def work(_):
            return updater.latest_version()

        def done(latest):
            if latest is None:
                if manual:
                    QMessageBox.warning(self, APP_TITLE, "更新の確認ができませんでした。インターネット接続を確認してください。")
                return
            if not updater.is_newer(latest, APP_VERSION):
                if manual:
                    QMessageBox.information(self, APP_TITLE, f"最新版です（v{APP_VERSION}）")
                return
            if not updater.can_self_update():
                QMessageBox.information(self, APP_TITLE, f"新しいバージョン v{latest} が出ています（今は v{APP_VERSION}）。")
                return
            if self.busy:
                if manual:
                    QMessageBox.information(self, APP_TITLE, "作業中は更新できません。終わってからもう一度押してください。")
                return
            ans = QMessageBox.question(self, APP_TITLE, f"新しいバージョン v{latest} が出ています（今は v{APP_VERSION}）。\n\n"
                                       "今すぐ更新しますか？\n（ダウンロードが終わるとアプリが自動で開き直します。"
                                       "作業中の内容は先に「保存」してください）")
            if ans != QMessageBox.Yes:
                return
            self.set_busy(True)

            def dl_done(path):
                try:
                    updater.install_and_restart(path)
                except Exception as e:
                    self.fail(f"更新できませんでした：{e}"); return
                QApplication.quit()
                os._exit(0)
            self.jobs.append(run_bg(lambda p: updater.download(lambda r: p(r, f"新しいバージョンをダウンロード中… {r*100:.0f}%")),
                                    dl_done, self.fail, self.on_progress))
        self.jobs.append(run_bg(work, done, lambda e: None, lambda r, m: None))

    def closeEvent(self, e):
        self.player.stop()
        super().closeEvent(e)


def selftest(src: str, out_dir: str, with_captions: bool = True) -> int:
    """ビルド確認用：画面を作り、解析→おまかせ→（文字起こし）→縦型で書き出し まで行う"""
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    log = out / "selftest.log"
    try:
        lines = []
        app = QApplication.instance() or QApplication(sys.argv)
        w = Main(); w.show(); app.processEvents()
        an = E.analyze(src)
        clips = E.auto_digest(an, 12)
        lines.append(f"clips={[(c.start, c.end) for c in clips]}")
        if with_captions and an["info"]["has_audio"]:
            n = T.transcribe_clips(src, clips)
            lines.append(f"captions={n}: " + " / ".join(x.text for c in clips for x in c.captions)[:200])
        p = E.Project(source=src, clips=clips, aspect="9:16", sfx=[E.Sfx(0.5, "ピコン"), E.Sfx(2.0, "ドン")])
        if not any(c.captions for c in clips):
            clips[0].captions = [E.Caption(0, 2, "テロップのテスト")]
        E.render(p, out / "selftest_v.mp4", font_dir=FONT_DIR, sfx_dir=SFX_DIR)
        info = E.probe(out / "selftest_v.mp4")
        lines.append(f"render={info}")
        assert info["width"] == 1080 and info["height"] == 1920
        w.close()
        log.write_text("OK\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return 0
    except Exception:
        log.write_text("NG\n" + traceback.format_exc(), encoding="utf-8")
        return 1


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3], "--no-captions" not in sys.argv))
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icon = HERE / "assets" / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    w = Main(); w.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        w.load_video(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
