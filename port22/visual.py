"""PORT22's native Qt controls, instrument icons and header artwork."""

import random

from PyQt5.QtCore import Qt, QPointF, QRectF, QSize
from PyQt5.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PyQt5.QtWidgets import QLabel, QPushButton, QFrame, QWidget

BG = "#0c0b13"
SURFACE = "#181422"
LINE = "#40364f"
TEXT = "#f5f1ff"
MUTED = "#c1baca"
LILAC = "#c2a6ff"
MINT = "#8ee0ca"

STYLES = """
* {color:#f5f1ff; font-family:'Inter','Open Sans','DejaVu Sans',sans-serif; font-size:13px;}
QMainWindow,QDialog,QWidget#page {background:#0c0b13;}
QToolTip {color:#f5f1ff; background:#30263e; border:1px solid #635477;}
QFrame#rail {background:#131019; border-right:1px solid #322a40;}
QFrame#card,QFrame#pane {background:#181422; border:1px solid #393044; border-radius:10px;}
QFrame#bar {background:#15111e; border-top:1px solid #322a40;}
QLabel {background:transparent;}
QLabel#brand {font-family:'DejaVu Sans Mono',monospace; font-size:22px; font-weight:700; letter-spacing:1px;}
QLabel#h1 {font-size:25px; font-weight:700;}
QLabel#h2 {font-size:17px; font-weight:600;}
QLabel#strong {font-size:14px; font-weight:600;}
QLabel#muted {color:#b4acbf;}
QLabel#micro {color:#b9a8cb; font-family:'DejaVu Sans Mono',monospace; font-size:11px;}
QLabel#mint {color:#8ee0ca; font-family:'DejaVu Sans Mono',monospace; font-size:11px;}
QLabel#warning {color:#ffb9bb;}
QPushButton {background:#241e2e; border:1px solid #493e57; border-radius:6px; padding:7px 10px; font-weight:500;}
QPushButton:hover {background:#332a40; border-color:#8d75a9;}
QPushButton:pressed {background:#43334f;}
QPushButton:disabled {color:#827b8b; background:#1c1822; border-color:#302935;}
QPushButton#primary {background:#c2a6ff; color:#211332; border:1px solid #d3bfff; font-weight:700;}
QPushButton#primary:hover {background:#d3bdff;}
QPushButton#primary:disabled {background:#403448; color:#aba0b8; border-color:#514657;}
QPushButton#nav {text-align:left; border:1px solid transparent; background:transparent; padding:10px 12px; color:#c1baca;}
QPushButton#nav:checked {background:#30243f; border:1px solid #57436f; color:#e0ccff;}
QPushButton#nav:hover {background:#251e30;}
QPushButton:focus,QToolButton:focus {border:1px solid #c2a6ff;}
QLineEdit,QSpinBox,QComboBox,QTextEdit,QPlainTextEdit {background:#100e17; color:#f5f1ff; border:1px solid #4a4058; border-radius:5px; padding:6px; selection-background-color:#644a86;}
QLineEdit:focus,QSpinBox:focus,QComboBox:focus {border-color:#c2a6ff;}
QTableView,QListWidget,QScrollArea {background:#14111b; border:0; alternate-background-color:#18141f; outline:0; selection-background-color:#463356; selection-color:#f5f1ff;}
QHeaderView::section {background:#201a29; color:#bcafc9; font-size:11px; font-weight:600; padding:5px; border:0; border-bottom:1px solid #372e42;}
QScrollBar:vertical {background:transparent; width:9px; margin:0;}
QScrollBar:horizontal {background:transparent; height:9px; margin:0;}
QScrollBar::handle {background:#51405f; border-radius:4px; min-height:18px; min-width:18px;}
QScrollBar::add-line,QScrollBar::sub-line {width:0; height:0;}
QMenu {background:#211a2a; border:1px solid #51445f;}
QMenu::item {padding:7px 18px;} QMenu::item:selected {background:#453455;}
QMessageBox {background:#191420;} QCheckBox {spacing:6px;}
QProgressBar {background:#32283e; border:0; border-radius:3px;} QProgressBar::chunk {background:#bd9df8; border-radius:3px;}
QSplitter::handle {background:transparent;}
QDialogButtonBox QPushButton {min-width:65px;}
"""

PATHS = {
    "server": [
        [(3, 4), (21, 4), (21, 10), (3, 10), (3, 4)],
        [(3, 14), (21, 14), (21, 20), (3, 20), (3, 14)],
        [(6, 7), (7, 7)],
        [(6, 17), (7, 17)],
    ],
    "grid": [
        [(3, 7), (9, 7), (9, 3), (3, 3), (3, 7)],
        [(14, 7), (20, 7), (20, 3), (14, 3), (14, 7)],
        [(3, 13), (9, 13), (9, 20), (3, 20), (3, 13)],
        [(14, 13), (20, 13), (20, 20), (14, 20), (14, 13)],
    ],
    "workspace": [
        [(2, 4), (22, 4), (22, 20), (2, 20), (2, 4)],
        [(12, 4), (12, 20)],
        [(5, 8), (8, 8)],
        [(15, 8), (18, 8)],
    ],
    "queue": [
        [(3, 7), (18, 7)],
        [(14, 3), (18, 7), (14, 11)],
        [(21, 17), (6, 17)],
        [(10, 13), (6, 17), (10, 21)],
    ],
    "key": [
        [(9, 14), (4, 19), (4, 21), (7, 21), (8, 19), (10, 19), (12, 16)],
        [
            (9, 14),
            (8, 10),
            (10, 6),
            (14, 4),
            (18, 5),
            (20, 8),
            (20, 12),
            (17, 15),
            (13, 16),
            (9, 14),
        ],
        [(16, 8), (16, 9)],
    ],
    "folder": [[(2, 6), (9, 6), (11, 9), (21, 9), (21, 19), (2, 19), (2, 6)]],
    "file": [
        [(5, 3), (14, 3), (19, 8), (19, 21), (5, 21), (5, 3)],
        [(14, 3), (14, 8), (19, 8)],
    ],
    "link": [[(3, 18), (10, 11), (16, 11)], [(12, 7), (16, 11), (12, 15)]],
    "up": [[(12, 20), (12, 4)], [(5, 11), (12, 4), (19, 11)]],
    "down": [[(12, 4), (12, 20)], [(5, 13), (12, 20), (19, 13)]],
    "right": [[(3, 12), (20, 12)], [(14, 6), (20, 12), (14, 18)]],
    "plus": [[(12, 4), (12, 20)], [(4, 12), (20, 12)]],
    "settings": [
        [(3, 6), (20, 6)],
        [(3, 17), (20, 17)],
        [(8, 3), (8, 9)],
        [(16, 14), (16, 20)],
    ],
    "refresh": [
        [
            (20, 11),
            (18, 7),
            (14, 5),
            (10, 5),
            (6, 7),
            (4, 11),
            (4, 14),
            (6, 17),
            (10, 19),
            (14, 19),
            (17, 17),
        ],
        [(20, 5), (20, 11), (14, 11)],
    ],
    "home": [[(2, 11), (12, 3), (22, 11)], [(5, 10), (5, 21), (19, 21), (19, 10)]],
    "mark": [
        [(2, 7), (5, 4), (10, 4), (12, 7), (11, 10), (4, 16), (3, 19), (11, 19)],
        [(13, 7), (16, 4), (21, 4), (23, 7), (22, 10), (15, 16), (14, 19), (22, 19)],
    ],
}


def icon(name, color=LILAC, size=24):
    canvas = QPixmap(size * 2, size * 2)
    canvas.fill(Qt.transparent)
    canvas.setDevicePixelRatio(2)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 24, size / 24)
    p.setPen(QPen(QColor(color), 1.65, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    for path in PATHS.get(name, PATHS["file"]):
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in path]))
    p.end()
    return QIcon(canvas)


def label(txt="", style=""):
    w = QLabel(txt)
    w.setTextFormat(Qt.PlainText)
    w.setObjectName(style)
    return w


def button(txt, action=None, symbol=None, style=""):
    w = QPushButton(txt)
    w.setObjectName(style)
    w.setCursor(Qt.PointingHandCursor)
    if symbol:
        w.setIcon(icon(symbol, "#342045" if style == "primary" else LILAC))
        w.setIconSize(QSize(16, 16))
    if action:
        w.clicked.connect(lambda _=False: action())
    return w


def frame(name="card"):
    w = QFrame()
    w.setObjectName(name)
    return w


class SignalArt(QWidget):
    """Stacked isometric server plates, drawn at the device's pixel density."""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        grad = QLinearGradient(0, 0, r.width(), r.height())
        grad.setColorAt(0, QColor("#20192c"))
        grad.setColorAt(0.47, QColor("#20182d"))
        grad.setColorAt(1, QColor("#241833"))
        p.setPen(QPen(QColor("#493958")))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(r), 12, 12)
        px, py = r.width() - 119, r.height() / 2 + 2
        p.save()
        p.setClipRect(max(0, r.width() - 265), 4, 263, r.height() - 8)
        p.translate(px, py)
        halo = QRadialGradient(0, 0, 108)
        halo.setColorAt(0, QColor(152, 85, 243, 86))
        halo.setColorAt(1, QColor(96, 44, 145, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QPointF(0, 0), 108, 92)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(138, 94, 182, 45), 0.75))
        for n in range(-9, 10):
            p.drawLine(QPointF(n * 23 - 130, 18 - 52), QPointF(n * 23 + 130, 18 + 52))
            p.drawLine(QPointF(n * 23 - 130, 18 + 52), QPointF(n * 23 + 130, 18 - 52))
        rng = random.Random(44)
        for _ in range(32):
            x, y = rng.uniform(-140, 120), rng.uniform(-85, 82)
            p.setPen(QPen(QColor(198, 156, 253, rng.randint(40, 125)), 1.4))
            p.drawPoint(QPointF(x, y))

        def face(coords, fill, outline, lw=1):
            poly = QPolygonF([QPointF(x, y) for x, y in coords])
            p.setPen(QPen(QColor(outline), lw))
            p.setBrush(QColor(fill))
            p.drawPolygon(poly)

        for y in (24, 5, -14):
            face(
                [(-61, y - 9), (0, y + 15), (0, y + 28), (-61, y + 3)],
                "#332041",
                "#9161bb",
            )
            face(
                [(0, y + 15), (61, y - 9), (61, y + 3), (0, y + 28)],
                "#20172f",
                "#76529a",
            )
            face(
                [(-61, y - 9), (0, y - 33), (61, y - 9), (0, y + 15)],
                "#442c61",
                "#ce9cff",
                1.5,
            )
            p.setPen(QPen(QColor("#dbb2ff"), 2))
            p.drawLine(QPointF(-51, y - 1), QPointF(-38, y + 4))
            for j in range(3):
                p.setPen(QPen(QColor("#8ee0ca" if j == 0 else "#ae88ce"), 2))
                p.drawPoint(QPointF(48 - j * 5, y - 1 + j * 2))
        p.setPen(QPen(QColor("#d7b3ff"), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawPolygon(
            QPolygonF(
                [QPointF(-16, -23), QPointF(0, -30), QPointF(16, -23), QPointF(0, -16)]
            )
        )
        p.restore()
        p.end()
