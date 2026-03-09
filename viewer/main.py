#!/usr/bin/env python3
"""
Ableton Live — Per-Track CPU Monitor  v2

Bells & whistles:
  • Custom SparklineBar: 18-second rolling history, smooth 60-fps animation,
    5-second peak-hold tick, gradient fill, green→yellow→orange→red ramp
  • Per-track all-time session maximum label
  • Ableton track-colour indicator dot
  • Track-type badge  (A = audio  M = midi  G = group  R = return)
  • Per-device breakdown with class name (click ▶ to expand)
  • Live BPM, play-state, time-signature header from Ableton
  • Total-CPU estimate bar + Ableton process CPU (requires psutil)
  • Session uptime clock
  • Sort controls: CPU↓  CPU↑  Name  Ableton-Order
  • Filter: All  |  Active only  |  High-CPU only
  • Alert threshold slider → macOS notifications (throttled 60 s / track)
  • 2-second sustained threshold → flashing row + "!" indicator
  • Freeze / unfreeze (buffer while frozen, applies on resume)
  • Always-on-top toggle
  • CSV export to Desktop
  • Keyboard shortcuts: Space=freeze  T=pin  E=export  Esc=clear alerts
  • Return-track section (collapsible strip)
  • Stale-connection watchdog (5 s)
  • Custom dark scrollbar
"""

import sys, os, csv, socket, json, threading, time, subprocess
from collections import deque
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QScrollArea, QFrame, QPushButton, QSlider, QSizePolicy,
    QProgressBar,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QPoint
from PyQt6.QtGui import (
    QFont, QPainter, QPainterPath, QColor, QLinearGradient,
    QPen, QBrush, QKeySequence, QShortcut,
)

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ── config ────────────────────────────────────────────────────────────────────
UDP_PORT       = 7400
HISTORY_LEN    = 90    # samples  →  90 × 200 ms = 18 s
PEAK_HOLD_SAMP = 25    # 25 × 200 ms = 5 s peak-hold
NOTIF_COOLDOWN = 60    # seconds between macOS alerts per track

# ── palette ───────────────────────────────────────────────────────────────────
BG0, BG1, BG2  = "#16161e", "#1f1f2c", "#282838"   # dark navy-slate
ACCENT         = "#ff7b45"                           # warm orange
ACCENT2        = "#2dd4bf"                           # teal — playing / connected
TXT_H          = "#f0f0f8"                           # near-white, easy on eyes
TXT_M          = "#a8a8c8"                           # mid — peak / session labels (READABLE)
TXT_L          = "#585870"                           # dim — muted / inactive only
BORDER         = "#2e2e42"                           # subtle row borders
ALERT_BG       = "#3d0b0b"                           # deep red flash


# ── colour ramp ───────────────────────────────────────────────────────────────
def cpu_qc(pct: float, alpha: int = 255) -> QColor:
    """Green(120°) → yellow → orange → red(0°) via HSL."""
    hue = max(0, int(120 - pct * 1.2))
    c = QColor.fromHsl(hue, 210, 128)
    c.setAlpha(alpha)
    return c


def mono(sz: int = 11, bold: bool = False) -> QFont:
    f = QFont("Menlo", sz)
    f.setBold(bold)
    return f


# ── SparklineBar ──────────────────────────────────────────────────────────────
class SparklineBar(QWidget):
    """
    Custom bar widget that shows:
      - faint area sparkline (18-second history)
      - gradient current-CPU bar
      - white peak-hold tick mark (5-second window)
    Animated at 60 fps via an external tick() call.
    """
    H = 20

    def __init__(self):
        super().__init__()
        self.setFixedHeight(self.H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._hist: deque[float] = deque(maxlen=HISTORY_LEN)
        self._win:  deque[float] = deque(maxlen=PEAK_HOLD_SAMP)
        self._disp    = 0.0   # smoothed display value
        self._target  = 0.0   # latest pushed value
        self._peak    = 0.0   # 5-s rolling peak
        self._max     = 0.0   # all-time session max

    def push(self, v: float):
        self._target = v
        self._hist.append(v)
        self._win.append(v)
        self._peak = max(self._win)
        if v > self._max:
            self._max = v

    def tick(self):
        """Lerp toward target; repaint only when changed."""
        d = self._target - self._disp
        if abs(d) > 0.05:
            self._disp = max(0.0, min(100.0, self._disp + d * 0.28))
            self.update()

    @property
    def current(self) -> float: return self._disp
    @property
    def peak(self) -> float:    return self._peak
    @property
    def session_max(self) -> float: return self._max

    def paintEvent(self, _):
        p    = QPainter(self)
        w, h = self.width(), self.height()
        hist = list(self._hist)
        N    = HISTORY_LEN

        # 1. background
        p.fillRect(0, 0, w, h, QColor(BG0))

        # 2. sparkline area
        if len(hist) > 1:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            path = QPainterPath()
            path.moveTo(0.0, float(h))
            for i, v in enumerate(hist):
                path.lineTo(i / N * w, h - v / 100.0 * h)
            path.lineTo((len(hist) - 1) / N * w, float(h))
            path.closeSubpath()
            p.fillPath(path, QBrush(cpu_qc(self._disp, 28)))

            p.setPen(QPen(cpu_qc(self._disp, 70), 1.0))
            pts = [QPoint(int(i / N * w), int(h - v / 100.0 * h))
                   for i, v in enumerate(hist)]
            for i in range(1, len(pts)):
                p.drawLine(pts[i - 1], pts[i])
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # 3. current bar (gradient)
        bw = int(self._disp / 100.0 * w)
        if bw > 0:
            col = cpu_qc(self._disp)
            g   = QLinearGradient(0.0, 0.0, float(bw), 0.0)
            g.setColorAt(0.0, col.darker(160))
            g.setColorAt(1.0, col)
            p.fillRect(0, 0, bw, h, QBrush(g))

        # 4. peak-hold tick
        pk_x = int(self._peak / 100.0 * w) - 1
        if pk_x > bw + 4 and 0 < pk_x < w:
            p.setPen(QPen(QColor(255, 255, 180, 220), 2))
            p.drawLine(pk_x, 1, pk_x, h - 1)

        # 5. border
        p.setPen(QColor("#1e1e1e"))
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


# ── DeviceRow ─────────────────────────────────────────────────────────────────
class DeviceRow(QWidget):
    def __init__(self, name: str, cls: str, cpu: float, active: bool):
        super().__init__()
        self.setStyleSheet(f"background:{BG2};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(28, 1, 8, 1)
        lay.setSpacing(6)

        self._cpu_val = cpu

        self._dot = QLabel("●")
        self._dot.setFixedWidth(10)
        self._dot.setFont(mono(8))

        label = name if name == cls else f"{name}  [{cls}]"
        self._name = QLabel(label[:34])
        self._name.setFixedWidth(190)
        self._name.setFont(mono(10))

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)

        self._pct = QLabel()
        self._pct.setFixedWidth(44)
        self._pct.setFont(mono(10))
        self._pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        lay.addWidget(self._dot)
        lay.addWidget(self._name)
        lay.addWidget(self._bar)
        lay.addWidget(self._pct)

        self.update_data(cpu, active)

    @property
    def cpu(self) -> float:
        return self._cpu_val

    def update_data(self, cpu: float, active: bool):
        self._cpu_val = cpu
        self._bar.setValue(int(cpu * 10))
        self._pct.setText(f"{cpu:.1f}%")
        col = cpu_qc(cpu)
        col_name = col.name()
        dim = not active
        self._bar.setStyleSheet(
            f"QProgressBar {{ background:{BG0}; border:none; border-radius:2px; }}"
            f"QProgressBar::chunk {{ background:{col_name}{'66' if dim else ''}; border-radius:2px; }}"
        )
        txt_col = TXT_L if dim else TXT_M
        self._name.setStyleSheet(f"color:{txt_col};")
        self._pct.setStyleSheet(f"color:{txt_col};")
        self._dot.setStyleSheet(f"color:{'#2a2a2a' if dim else col_name};")


# ── TrackRow ──────────────────────────────────────────────────────────────────
_TYPE_BADGE = {"audio": "A", "midi": "M", "group": "G", "return": "R"}
_TYPE_COLOR = {"audio": "#4a9eff", "midi": "#b07fff", "group": "#ff9f40", "return": "#40d4a0"}


class TrackRow(QWidget):
    def __init__(self, name: str, ttype: str, color_rgb: list):
        super().__init__()
        self._expanded   = False
        self._alerted    = False
        self._flash_on   = False
        self._dev_widgets: dict[str, DeviceRow] = {}

        self._flash_timer = QTimer()
        self._flash_timer.timeout.connect(self._do_flash)

        # store track colour hex for the left border stripe
        r, g, b = (color_rgb + [80, 80, 80])[:3]
        self._track_color_hex = f"#{r:02x}{g:02x}{b:02x}"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 1)
        root.setSpacing(0)

        # ── main bar row ──────────────────────────────────────────────────────
        self._bar_bg = QWidget()
        self._bar_bg.setStyleSheet(
            f"background:{BG1}; border-radius:3px;"
            f" border:1px solid {BORDER}; border-left:3px solid {self._track_color_hex};"
        )
        bar_lay = QHBoxLayout(self._bar_bg)
        bar_lay.setContentsMargins(6, 4, 6, 4)
        bar_lay.setSpacing(5)

        # Ableton track colour dot
        self._col_dot = QLabel("■")
        self._col_dot.setFixedWidth(10)
        self._col_dot.setFont(mono(9))
        self._col_dot.setStyleSheet(f"color:#{r:02x}{g:02x}{b:02x};")

        # track-type badge
        badge = _TYPE_BADGE.get(ttype, "?")
        bcol  = _TYPE_COLOR.get(ttype, "#888")
        self._type_lbl = QLabel(badge)
        self._type_lbl.setFixedWidth(14)
        self._type_lbl.setFont(mono(9, bold=True))
        self._type_lbl.setStyleSheet(f"color:{bcol};")

        # expand toggle
        self._tog = QLabel("▶")
        self._tog.setFixedWidth(12)
        self._tog.setFont(mono(9))
        self._tog.setStyleSheet(f"color:{TXT_M};")
        self._tog.mousePressEvent = lambda _: self._toggle()

        # name
        self._name_lbl = QLabel(name[:22])
        self._name_lbl.setFixedWidth(138)
        self._name_lbl.setFont(mono(11, bold=True))
        self._name_lbl.setStyleSheet(f"color:{TXT_H};")

        # sparkline bar
        self.spark = SparklineBar()

        # current % label
        self._cpu_lbl = QLabel("0.0%")
        self._cpu_lbl.setFixedWidth(42)
        self._cpu_lbl.setFont(mono(11))
        self._cpu_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._cpu_lbl.setStyleSheet(f"color:{TXT_H};")

        # peak-hold label (5-second window)
        self._peak_lbl = QLabel("pk 0%")
        self._peak_lbl.setFixedWidth(48)
        self._peak_lbl.setFont(mono(9))
        self._peak_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._peak_lbl.setStyleSheet(f"color:{TXT_M};")

        # session-max label (crown = all-time high)
        self._max_lbl = QLabel("↑ 0%")
        self._max_lbl.setFixedWidth(46)
        self._max_lbl.setFont(mono(9))
        self._max_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._max_lbl.setStyleSheet(f"color:{TXT_M};")

        # alert indicator
        self._alert_lbl = QLabel("!")
        self._alert_lbl.setFixedWidth(12)
        self._alert_lbl.setFont(mono(11, bold=True))
        self._alert_lbl.setStyleSheet("color:transparent;")

        bar_lay.addWidget(self._col_dot)
        bar_lay.addWidget(self._type_lbl)
        bar_lay.addWidget(self._tog)
        bar_lay.addWidget(self._name_lbl)
        bar_lay.addWidget(self.spark)
        bar_lay.addWidget(self._cpu_lbl)
        bar_lay.addWidget(self._peak_lbl)
        bar_lay.addWidget(self._max_lbl)
        bar_lay.addWidget(self._alert_lbl)

        # ── device sub-panel ──────────────────────────────────────────────────
        self._dev_panel = QWidget()
        self._dev_panel.setStyleSheet(f"background:{BG2};")
        self._dev_layout = QVBoxLayout(self._dev_panel)
        self._dev_layout.setContentsMargins(0, 2, 0, 2)
        self._dev_layout.setSpacing(1)
        self._dev_panel.setVisible(False)

        root.addWidget(self._bar_bg)
        root.addWidget(self._dev_panel)

    # ── expand/collapse ───────────────────────────────────────────────────────
    def _toggle(self):
        self._expanded = not self._expanded
        self._dev_panel.setVisible(self._expanded)
        self._tog.setText("▼" if self._expanded else "▶")

    # ── data update ───────────────────────────────────────────────────────────
    def set_data(self, cpu: float, muted: bool, devices: list, alert: bool):
        self.spark.push(cpu)
        col      = cpu_qc(self.spark.current)
        dim_col  = TXT_L if muted else TXT_H
        cpu_col  = TXT_L if muted else col.name()

        self._cpu_lbl.setText(f"{cpu:.1f}%")
        self._cpu_lbl.setStyleSheet(f"color:{cpu_col};")
        self._peak_lbl.setText(f"pk {self.spark.peak:.0f}%")
        self._max_lbl.setText(f"↑ {self.spark.session_max:.0f}%")
        self._name_lbl.setStyleSheet(f"color:{dim_col};")

        # alert flash
        if alert and not self._alerted:
            self._alerted  = True
            self._flash_on = False
            self._flash_timer.start(380)
        elif not alert and self._alerted:
            self._alerted = False
            self._flash_timer.stop()
            self._set_normal_bg()
            self._alert_lbl.setStyleSheet("color:transparent;")

        self._update_devices(devices)

    def _set_normal_bg(self):
        self._bar_bg.setStyleSheet(
            f"background:{BG1}; border-radius:3px;"
            f" border:1px solid {BORDER}; border-left:3px solid {self._track_color_hex};"
        )

    def _do_flash(self):
        self._flash_on = not self._flash_on
        if self._flash_on:
            self._bar_bg.setStyleSheet(
                f"background:{ALERT_BG}; border-radius:3px;"
                f" border:1px solid #662222; border-left:3px solid {self._track_color_hex};"
            )
            self._alert_lbl.setStyleSheet(f"color:{cpu_qc(100).name()}; font-weight:bold;")
        else:
            self._set_normal_bg()

    def clear_alert(self):
        self._alerted = False
        self._flash_timer.stop()
        self._set_normal_bg()
        self._alert_lbl.setStyleSheet("color:transparent;")

    def _update_devices(self, devices: list):
        incoming = {d["name"] for d in devices}
        for n in list(self._dev_widgets):
            if n not in incoming:
                self._dev_widgets[n].setParent(None)   # type: ignore
                del self._dev_widgets[n]
        for dev in devices:
            n = dev["name"]
            if n in self._dev_widgets:
                self._dev_widgets[n].update_data(dev["cpu"], dev.get("active", True))
            else:
                w = DeviceRow(n, dev.get("class", n), dev["cpu"], dev.get("active", True))
                self._dev_widgets[n] = w
                self._dev_layout.addWidget(w)
        self._tog.setVisible(bool(devices))

    # ── export helpers ────────────────────────────────────────────────────────
    def export_rows(self, track_name: str) -> list[list]:
        rows = []
        for dname, dw in self._dev_widgets.items():
            rows.append([
                track_name,
                f"{self.spark.current:.2f}",
                f"{self.spark.peak:.2f}",
                f"{self.spark.session_max:.2f}",
                dname,
                f"{dw.cpu:.2f}",
            ])
        if not rows:
            rows.append([track_name,
                         f"{self.spark.current:.2f}",
                         f"{self.spark.peak:.2f}",
                         f"{self.spark.session_max:.2f}",
                         "", ""])
        return rows


# ── HeaderPanel ───────────────────────────────────────────────────────────────
class HeaderPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(58)
        self.setStyleSheet(f"background:{BG1};")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.setSpacing(3)

        # top row
        top = QHBoxLayout(); top.setSpacing(10)

        self._conn_dot = QLabel("●")
        self._conn_dot.setFont(mono(10))
        self._conn_dot.setStyleSheet("color:#cc2222;")

        self._play_lbl = QLabel("■ STOPPED")
        self._play_lbl.setFont(mono(10, bold=True))
        self._play_lbl.setStyleSheet(f"color:{TXT_M};")

        self._bpm_lbl = QLabel("-- BPM")
        self._bpm_lbl.setFont(mono(10))
        self._bpm_lbl.setStyleSheet(f"color:{TXT_H};")

        self._sig_lbl = QLabel("--/--")
        self._sig_lbl.setFont(mono(10))
        self._sig_lbl.setStyleSheet(f"color:{TXT_M};")

        self._uptime_lbl = QLabel("00:00:00")
        self._uptime_lbl.setFont(mono(10))
        self._uptime_lbl.setStyleSheet(f"color:{TXT_M};")

        top.addWidget(self._conn_dot)
        top.addWidget(self._play_lbl)
        top.addSpacing(6)
        top.addWidget(self._bpm_lbl)
        top.addWidget(self._sig_lbl)
        top.addStretch()
        top.addWidget(self._uptime_lbl)

        # bottom row: total CPU bar
        bot = QHBoxLayout(); bot.setSpacing(6)

        lbl = QLabel("TOTAL CPU")
        lbl.setFont(mono(8))
        lbl.setStyleSheet(f"color:{TXT_M}; letter-spacing:1px;")
        lbl.setFixedWidth(64)

        self._total_bar = QProgressBar()
        self._total_bar.setRange(0, 1000)
        self._total_bar.setTextVisible(False)
        self._total_bar.setFixedHeight(10)

        self._total_pct = QLabel("0.0%")
        self._total_pct.setFixedWidth(44)
        self._total_pct.setFont(mono(10))
        self._total_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._total_pct.setStyleSheet(f"color:{ACCENT};")

        self._proc_lbl = QLabel("")
        self._proc_lbl.setFont(mono(9))
        self._proc_lbl.setStyleSheet(f"color:{TXT_M};")

        bot.addWidget(lbl)
        bot.addWidget(self._total_bar)
        bot.addWidget(self._total_pct)
        bot.addSpacing(16)
        bot.addWidget(self._proc_lbl)
        bot.addStretch()

        lay.addLayout(top)
        lay.addLayout(bot)

        # session clock
        self._start = time.monotonic()
        t = QTimer(self); t.timeout.connect(self._tick_uptime); t.start(1000)

        # Ableton process CPU monitor
        self._ableton_proc = None
        if HAS_PSUTIL:
            self._find_ableton()
            pt = QTimer(self); pt.timeout.connect(self._update_proc); pt.start(2000)

    def _find_ableton(self):
        try:
            for proc in psutil.process_iter(["name"]):
                if "Live" in proc.info["name"]:
                    self._ableton_proc = psutil.Process(proc.pid)
                    self._ableton_proc.cpu_percent()   # prime the counter
                    break
        except Exception:
            pass

    def _update_proc(self):
        if not self._ableton_proc:
            self._find_ableton(); return
        try:
            pct = self._ableton_proc.cpu_percent()
            self._proc_lbl.setText(f"Ableton process: {pct:.1f}%")
        except Exception:
            self._ableton_proc = None

    def _tick_uptime(self):
        e = int(time.monotonic() - self._start)
        h, r = divmod(e, 3600); m, s = divmod(r, 60)
        self._uptime_lbl.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def set_connected(self, ok: bool):
        self._conn_dot.setStyleSheet(f"color:{ACCENT2 if ok else '#cc2222'};")

    def update_meta(self, meta: dict, total_cpu: float):
        bpm     = meta.get("bpm", 0.0)
        playing = meta.get("playing", False)
        sig_n   = meta.get("sig_num", 4)
        sig_d   = meta.get("sig_den", 4)

        self._bpm_lbl.setText(f"{bpm:.2f} BPM")
        self._sig_lbl.setText(f"{sig_n}/{sig_d}")
        if playing:
            self._play_lbl.setText("► PLAYING")
            self._play_lbl.setStyleSheet(f"color:{ACCENT2}; font-weight:bold;")
        else:
            self._play_lbl.setText("■ STOPPED")
            self._play_lbl.setStyleSheet(f"color:{TXT_L}; font-weight:bold;")

        capped = min(total_cpu, 100.0)
        self._total_bar.setValue(int(capped * 10))
        self._total_pct.setText(f"{total_cpu:.1f}%")
        col = cpu_qc(capped).name()
        self._total_bar.setStyleSheet(
            f"QProgressBar {{ background:{BG0}; border:none; border-radius:3px; }}"
            f"QProgressBar::chunk {{ background:{col}; border-radius:3px; }}"
        )
        self._total_pct.setStyleSheet(f"color:{col};")


# ── ControlsPanel ─────────────────────────────────────────────────────────────
class ControlsPanel(QWidget):
    sort_changed      = pyqtSignal(str)   # cpu_desc | cpu_asc | name | order
    filter_changed    = pyqtSignal(str)   # all | active | high
    threshold_changed = pyqtSignal(int)
    freeze_toggled    = pyqtSignal(bool)
    pin_toggled       = pyqtSignal(bool)
    export_clicked    = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setStyleSheet(f"background:{BG2};")
        self.setFixedHeight(68)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(4)

        # row 1: sort + filter buttons
        r1 = QHBoxLayout(); r1.setSpacing(5)
        r1.addWidget(self._cap("SORT"))
        self._sort_btns: dict[str, QPushButton] = {}
        for key, label in [("cpu_desc","CPU▼"),("cpu_asc","CPU▲"),("name","Name"),("order","Order")]:
            b = self._toggle_btn(label, key == "cpu_desc")
            b.clicked.connect(lambda _, k=key: self._on_sort(k))
            self._sort_btns[key] = b
            r1.addWidget(b)
        r1.addSpacing(10)
        r1.addWidget(self._cap("SHOW"))
        self._filt_btns: dict[str, QPushButton] = {}
        for key, label in [("all","All"),("active","Active"),("high","High CPU")]:
            b = self._toggle_btn(label, key == "all")
            b.clicked.connect(lambda _, k=key: self._on_filter(k))
            self._filt_btns[key] = b
            r1.addWidget(b)
        r1.addStretch()

        # row 2: threshold + actions
        r2 = QHBoxLayout(); r2.setSpacing(8)
        r2.addWidget(self._cap("ALERT"))

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 100)
        self._slider.setValue(70)
        self._slider.setFixedWidth(110)
        self._slider.setFixedHeight(14)
        self._slider.valueChanged.connect(self._on_slider)
        self._slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ background:{BG0}; height:4px; border-radius:2px; }}
            QSlider::sub-page:horizontal {{ background:{ACCENT}; height:4px; border-radius:2px; }}
            QSlider::handle:horizontal {{
                background:{ACCENT}; width:12px; height:12px;
                margin:-4px 0; border-radius:6px;
            }}
        """)
        self._thresh_val = QLabel("70%")
        self._thresh_val.setFont(mono(10))
        self._thresh_val.setFixedWidth(30)
        self._thresh_val.setStyleSheet(f"color:{ACCENT};")

        r2.addWidget(self._slider)
        r2.addWidget(self._thresh_val)
        r2.addStretch()

        self._freeze_btn = self._action_btn("Freeze [Space]", checkable=True)
        self._freeze_btn.clicked.connect(lambda c: self.freeze_toggled.emit(c))
        self._pin_btn = self._action_btn("Pin [T]", checkable=True)
        self._pin_btn.clicked.connect(lambda c: self.pin_toggled.emit(c))
        self._export_btn = self._action_btn("Export CSV [E]")
        self._export_btn.clicked.connect(self.export_clicked.emit)

        r2.addWidget(self._freeze_btn)
        r2.addWidget(self._pin_btn)
        r2.addWidget(self._export_btn)

        lay.addLayout(r1)
        lay.addLayout(r2)

    def _cap(self, text: str) -> QLabel:
        l = QLabel(text)
        l.setFont(mono(8))
        l.setStyleSheet(f"color:{TXT_M}; letter-spacing:1px;")
        return l

    def _toggle_btn(self, text: str, checked: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setCheckable(True)
        b.setChecked(checked)
        b.setFixedHeight(20)
        b.setFont(mono(9))
        b.setStyleSheet(f"""
            QPushButton {{ background:{BG1}; color:{TXT_M}; border:1px solid {BORDER};
                           border-radius:3px; padding:0 5px; }}
            QPushButton:checked {{ background:#281a10; color:{ACCENT}; border-color:{ACCENT}; }}
            QPushButton:hover   {{ color:{TXT_H}; }}
        """)
        return b

    def _action_btn(self, text: str, checkable: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setCheckable(checkable)
        b.setFixedHeight(20)
        b.setFont(mono(9))
        b.setStyleSheet(f"""
            QPushButton {{ background:{BG1}; color:{TXT_M}; border:1px solid {BORDER};
                           border-radius:3px; padding:0 6px; }}
            QPushButton:checked {{ background:#14142a; color:#8888ff; border-color:#8888ff; }}
            QPushButton:hover   {{ color:{TXT_H}; }}
        """)
        return b

    def _on_sort(self, key: str):
        for k, b in self._sort_btns.items():
            b.setChecked(k == key)
        self.sort_changed.emit(key)

    def _on_filter(self, key: str):
        for k, b in self._filt_btns.items():
            b.setChecked(k == key)
        self.filter_changed.emit(key)

    def _on_slider(self, val: int):
        self._thresh_val.setText(f"{val}%")
        self.threshold_changed.emit(val)

    @property
    def threshold(self) -> int:
        return self._slider.value()


# ── AlertManager ──────────────────────────────────────────────────────────────
class AlertManager:
    def __init__(self):
        self._high_since: dict[str, float] = {}
        self._last_notif: dict[str, float] = {}

    def check(self, name: str, cpu: float, threshold: int) -> bool:
        now  = time.monotonic()
        over = cpu >= threshold
        if over:
            self._high_since.setdefault(name, now)
            sustained = now - self._high_since[name] >= 2.0
            if sustained and now - self._last_notif.get(name, 0.0) >= NOTIF_COOLDOWN:
                self._last_notif[name] = now
                self._notify(name, cpu)
        else:
            self._high_since.pop(name, None)
        return over

    @staticmethod
    def _notify(name: str, cpu: float):
        try:
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "CPU at {cpu:.1f}%" with title "Ableton Alert: {name}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


# ── UDP Receiver ──────────────────────────────────────────────────────────────
class Receiver(QObject):
    got_data = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", UDP_PORT))
        self._sock.settimeout(1.0)
        self._alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self._alive:
            try:
                raw, _ = self._sock.recvfrom(65535)
                self.got_data.emit(json.loads(raw.decode("utf-8")))
            except socket.timeout:
                continue
            except Exception:
                continue

    def stop(self):
        self._alive = False
        try: self._sock.close()
        except Exception: pass


# ── MainWindow ────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ableton Track CPU Monitor")
        self.setMinimumSize(680, 520)
        self.resize(720, 640)
        self.setStyleSheet(f"background:{BG0}; color:{TXT_H};")

        self._rows:      dict[str, TrackRow] = {}
        self._ret_rows:  dict[str, TrackRow] = {}
        self._last_rx    = 0.0
        self._frozen     = False
        self._pending    = None
        self._sort_mode  = "cpu_desc"
        self._filter     = "all"
        self._threshold  = 70
        self._ableton_order: list[str] = []
        self._alerts     = AlertManager()

        # ── layout ────────────────────────────────────────────────────────────
        c = QWidget(); self.setCentralWidget(c)
        root = QVBoxLayout(c)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # title bar
        tb = QWidget(); tb.setStyleSheet(f"background:{BG1};"); tb.setFixedHeight(30)
        tbl = QHBoxLayout(tb); tbl.setContentsMargins(12, 0, 12, 0)
        title = QLabel("ABLETON LIVE  ·  TRACK CPU MONITOR")
        title.setFont(mono(11, bold=True))
        title.setStyleSheet(f"color:{ACCENT}; letter-spacing:2px;")
        self._frozen_lbl = QLabel("")
        self._frozen_lbl.setFont(mono(10))
        self._frozen_lbl.setStyleSheet("color:#8888ff;")
        tbl.addWidget(title); tbl.addStretch(); tbl.addWidget(self._frozen_lbl)
        root.addWidget(tb)

        # header + controls
        self._header = HeaderPanel()
        root.addWidget(self._header)
        self._divider(root)

        self._controls = ControlsPanel()
        self._controls.sort_changed.connect(self._on_sort)
        self._controls.filter_changed.connect(self._on_filter)
        self._controls.threshold_changed.connect(lambda v: setattr(self, "_threshold", v))
        self._controls.freeze_toggled.connect(self._on_freeze)
        self._controls.pin_toggled.connect(self._on_pin)
        self._controls.export_clicked.connect(self._export_csv)
        root.addWidget(self._controls)
        self._divider(root)

        # column headers
        ch = QWidget(); ch.setStyleSheet(f"background:{BG0};"); ch.setFixedHeight(18)
        chl = QHBoxLayout(ch); chl.setContentsMargins(6, 0, 6, 0); chl.setSpacing(5)
        for txt, w, align in [
            ("",            36, Qt.AlignmentFlag.AlignLeft),
            ("TRACK",      138, Qt.AlignmentFlag.AlignLeft),
            ("HISTORY + LOAD", None, Qt.AlignmentFlag.AlignLeft),
            ("NOW",         42, Qt.AlignmentFlag.AlignRight),
            ("5s PK",       48, Qt.AlignmentFlag.AlignRight),
            ("SESSION↑",    46, Qt.AlignmentFlag.AlignRight),
            ("",            12, Qt.AlignmentFlag.AlignLeft),
        ]:
            l = QLabel(txt); l.setFont(mono(8))
            l.setStyleSheet(f"color:{TXT_L}; letter-spacing:1px;")
            if w: l.setFixedWidth(w)
            l.setAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            chl.addWidget(l)
        root.addWidget(ch)

        # main track scroll
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border:none;")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.verticalScrollBar().setStyleSheet("""
            QScrollBar:vertical { background:#111; width:5px; margin:0; }
            QScrollBar::handle:vertical { background:#333; border-radius:2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        """)
        self._track_area = QWidget(); self._track_area.setStyleSheet(f"background:{BG0};")
        self._track_layout = QVBoxLayout(self._track_area)
        self._track_layout.setSpacing(2); self._track_layout.setContentsMargins(6, 4, 6, 4)
        self._track_layout.addStretch()
        scroll.setWidget(self._track_area)
        root.addWidget(scroll)

        # return tracks section
        self._ret_section = QWidget()
        rs_lay = QVBoxLayout(self._ret_section)
        rs_lay.setContentsMargins(0, 0, 0, 0); rs_lay.setSpacing(0)

        rh = QWidget(); rh.setStyleSheet(f"background:{BG2};"); rh.setFixedHeight(20)
        rhl = QHBoxLayout(rh); rhl.setContentsMargins(10, 0, 10, 0)
        rl = QLabel("RETURN TRACKS"); rl.setFont(mono(8, bold=True))
        rl.setStyleSheet(f"color:{TXT_M}; letter-spacing:2px;")
        rhl.addWidget(rl); rhl.addStretch()

        ret_scroll = QScrollArea(); ret_scroll.setWidgetResizable(True)
        ret_scroll.setStyleSheet("border:none;"); ret_scroll.setMaximumHeight(110)
        self._ret_area = QWidget(); self._ret_area.setStyleSheet(f"background:{BG0};")
        self._ret_layout = QVBoxLayout(self._ret_area)
        self._ret_layout.setSpacing(2); self._ret_layout.setContentsMargins(6, 2, 6, 2)
        self._ret_layout.addStretch()
        ret_scroll.setWidget(self._ret_area)

        rs_lay.addWidget(rh); rs_lay.addWidget(ret_scroll)
        self._ret_section.setVisible(False)
        root.addWidget(self._ret_section)

        # status bar
        self._status = QLabel("Waiting — install TrackCpuMonitor remote script in Ableton")
        self._status.setFixedHeight(20)
        self._status.setFont(mono(9))
        self._status.setStyleSheet(f"background:{BG2}; color:{TXT_M}; padding:2px 10px;")
        root.addWidget(self._status)

        # ── timers ────────────────────────────────────────────────────────────
        self._anim = QTimer(); self._anim.timeout.connect(self._anim_tick); self._anim.start(16)
        self._wdog = QTimer(); self._wdog.timeout.connect(self._watchdog);  self._wdog.start(3000)

        # ── keyboard shortcuts ────────────────────────────────────────────────
        QShortcut(QKeySequence("Space"), self).activated.connect(self._toggle_freeze)
        QShortcut(QKeySequence("T"),     self).activated.connect(self._toggle_pin)
        QShortcut(QKeySequence("E"),     self).activated.connect(self._export_csv)
        QShortcut(QKeySequence("Escape"),self).activated.connect(self._clear_alerts)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _divider(self, parent_layout):
        d = QFrame(); d.setFrameShape(QFrame.Shape.HLine)
        d.setStyleSheet("color:#1e1e1e;"); parent_layout.addWidget(d)

    # ── animation tick ────────────────────────────────────────────────────────
    def _anim_tick(self):
        for row in self._rows.values():     row.spark.tick()
        for row in self._ret_rows.values(): row.spark.tick()

    # ── data flow ─────────────────────────────────────────────────────────────
    def on_data(self, payload: dict):
        self._last_rx = time.monotonic()
        self._header.set_connected(True)
        if self._frozen:
            self._pending = payload
            return
        self._apply(payload)

    def _apply(self, payload: dict):
        meta    = payload.get("meta", {})
        tracks  = payload.get("tracks", [])
        returns = payload.get("returns", [])

        total_cpu = sum(t["cpu"] for t in tracks) + meta.get("master_cpu", 0.0)
        self._header.update_meta(meta, total_cpu)

        self._ableton_order = [t["name"] for t in tracks]
        self._sync_rows(tracks,  self._rows,     self._track_layout)
        self._sync_rows(returns, self._ret_rows,  self._ret_layout)
        self._ret_section.setVisible(bool(returns))

        self._reorder()
        self._apply_filter()

        n = len(tracks)
        r = len(returns)
        self._status.setText(
            f"{n} track{'s' if n!=1 else ''}"
            + (f"  +  {r} return{'s' if r!=1 else ''}" if r else "")
            + "   ·   Space=freeze   T=pin   E=export   Esc=clear alerts"
        )

    def _sync_rows(self, tracks: list, rows: dict, layout: QVBoxLayout):
        incoming = {t["name"] for t in tracks}
        for name in list(rows):
            if name not in incoming:
                rows[name].setParent(None)   # type: ignore
                del rows[name]
        for t in tracks:
            name  = t["name"]
            alert = self._alerts.check(name, t["cpu"], self._threshold)
            if name not in rows:
                row = TrackRow(name, t.get("type", "audio"), t.get("color", [80, 80, 80]))
                rows[name] = row
                layout.insertWidget(layout.count() - 1, row)
            rows[name].set_data(t["cpu"], t.get("muted", False), t.get("devices", []), alert)

    # ── sort ──────────────────────────────────────────────────────────────────
    def _on_sort(self, mode: str):
        self._sort_mode = mode; self._reorder()

    def _reorder(self):
        if not self._rows: return
        if self._sort_mode == "cpu_desc":
            order = sorted(self._rows, key=lambda n: self._rows[n].spark.current, reverse=True)
        elif self._sort_mode == "cpu_asc":
            order = sorted(self._rows, key=lambda n: self._rows[n].spark.current)
        elif self._sort_mode == "name":
            order = sorted(self._rows)
        else:  # Ableton order
            known = set(self._rows)
            order = [n for n in self._ableton_order if n in known]
            order += [n for n in self._rows if n not in set(order)]
        for i, name in enumerate(order):
            self._track_layout.insertWidget(i, self._rows[name])

    # ── filter ────────────────────────────────────────────────────────────────
    def _on_filter(self, f: str):
        self._filter = f; self._apply_filter()

    def _apply_filter(self):
        for name, row in self._rows.items():
            if self._filter == "active":
                show = row.spark.current > 0.01
            elif self._filter == "high":
                show = row.spark.current >= self._threshold
            else:
                show = True
            row.setVisible(show)

    # ── freeze ────────────────────────────────────────────────────────────────
    def _on_freeze(self, frozen: bool):
        self._frozen = frozen
        self._frozen_lbl.setText("  ❚❚ FROZEN" if frozen else "")
        if not frozen and self._pending is not None:
            self._apply(self._pending); self._pending = None

    def _toggle_freeze(self):
        b = self._controls._freeze_btn
        b.setChecked(not b.isChecked())
        self._on_freeze(b.isChecked())

    # ── pin ───────────────────────────────────────────────────────────────────
    def _on_pin(self, pinned: bool):
        flags = self.windowFlags()
        if pinned:
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def _toggle_pin(self):
        b = self._controls._pin_btn
        b.setChecked(not b.isChecked())
        self._on_pin(b.isChecked())

    # ── alerts ────────────────────────────────────────────────────────────────
    def _clear_alerts(self):
        for row in self._rows.values():
            row.clear_alert()

    # ── watchdog ──────────────────────────────────────────────────────────────
    def _watchdog(self):
        if self._last_rx and time.monotonic() - self._last_rx > 5:
            self._header.set_connected(False)
            self._status.setText("Connection lost — is Ableton running with TrackCpuMonitor?")

    # ── CSV export ────────────────────────────────────────────────────────────
    def _export_csv(self):
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.expanduser(f"~/Desktop/ableton_cpu_{ts}.csv")
        try:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["track", "type", "cpu_now", "peak_5s", "session_max", "device", "device_cpu"])
                for name, row in self._rows.items():
                    for data_row in row.export_rows(name):
                        w.writerow(data_row)
            self._status.setText(f"Saved → {path}")
        except Exception as e:
            self._status.setText(f"Export failed: {e}")


# ── entry point ───────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    win = MainWindow()
    win.show()

    rx = Receiver()
    rx.got_data.connect(win.on_data)

    print(f"[TrackCpuMonitor] Listening on UDP 127.0.0.1:{UDP_PORT}")
    ret = app.exec()
    rx.stop()
    sys.exit(ret)


if __name__ == "__main__":
    main()
