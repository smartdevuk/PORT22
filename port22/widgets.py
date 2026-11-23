import os
from pathlib import Path
import posixpath
import time

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, pyqtSignal, QMimeData
from PyQt5.QtGui import QDrag
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from visual import button, frame, icon, label, LILAC


def fmt(size):
    if size < 1024:
        return f"{size} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"


def download_dir():
    paths = [
        Path("/mnt/chromeos/MyFiles/Downloads"),
        Path("/mnt/shared/MyFiles/Downloads"),
        Path.home() / "Downloads",
    ]
    return str(next((p for p in paths if p.is_dir()), Path.home()))


class ProfileDialog(QDialog):
    def __init__(self, parent, profile=None):
        super().__init__(parent)
        self.setWindowTitle("Saved connection")
        self.setMinimumWidth(435)
        self.profile = profile or {}
        top = QVBoxLayout(self)
        top.setContentsMargins(24, 22, 24, 22)
        top.setSpacing(14)
        top.addWidget(label("Edit connection" if profile else "New connection", "h1"))
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.fields = {}
        for k, title, hint in (
            ("name", "Name", "e.g. RawHoney production"),
            ("host", "Host", "Droplet IP address or hostname"),
            ("user", "Username", "e.g. paul or root"),
        ):
            field = QLineEdit(self.profile.get(k, ""))
            field.setPlaceholderText(hint)
            form.addRow(title, field)
            self.fields[k] = field
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self.profile.get("port", 22))
        form.addRow("Port", self.port)
        row = QHBoxLayout()
        self.key = QLineEdit(self.profile.get("key", ""))
        self.key.setPlaceholderText("OpenSSH, PEM or PuTTY key")
        row.addWidget(self.key, 1)
        row.addWidget(button("Browse…", self.pick))
        form.addRow("Private key", row)
        self.remote = QLineEdit(self.profile.get("remote", ""))
        self.remote.setPlaceholderText("Server home directory")
        form.addRow("Start folder", self.remote)
        top.addLayout(form)
        self.error = label("", "warning")
        self.error.setWordWrap(True)
        self.error.hide()
        top.addWidget(self.error)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        top.addWidget(box)

    def pick(self):
        dlg = QFileDialog(
            self,
            "Choose your SSH private key",
            str(Path(self.key.text() or "~/.ssh").expanduser()),
        )
        from PyQt5.QtCore import QDir

        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setFileMode(QFileDialog.ExistingFile)
        dlg.setFilter(QDir.AllDirs | QDir.Files | QDir.Hidden | QDir.NoDotAndDotDot)
        if dlg.exec_():
            self.key.setText(dlg.selectedFiles()[0])

    def save(self):
        p = {k: v.text().strip() for k, v in self.fields.items()}
        p.update(
            key=os.path.abspath(os.path.expanduser(self.key.text().strip())),
            port=self.port.value(),
            remote=self.remote.text().strip(),
        )
        if not all((p["name"], p["host"], p["user"])):
            msg = "Enter a name, host and username."
        elif (
            any(c.isspace() for c in p["host"]) or "/" in p["host"] or "@" in p["host"]
        ):
            msg = "Enter just the hostname or IP address."
        elif not os.path.isfile(p["key"]):
            msg = "Choose a private key file that Linux can read."
        else:
            p["host"] = p["host"].strip("[]")
            self.profile.update(p)
            self.accept()
            return
        self.error.setText(msg)
        self.error.show()


class FilesModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.icons = {
            k: icon(
                k if k != "special" else "file", LILAC if k == "folder" else "#ab9bbd"
            )
            for k in ("folder", "file", "link", "special")
        }

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 3

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return ("Name", "Size", "Modified")[section]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        name, typ, size, mtime = self.rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return name
            if col == 1:
                return fmt(size) if typ == "file" else "—"
            try:
                return (
                    time.strftime("%d %b %Y", time.localtime(mtime)) if mtime else "—"
                )
            except (OSError, ValueError, OverflowError):
                return "—"
        if role == Qt.DecorationRole and col == 0:
            return self.icons[typ]
        if role == Qt.ToolTipRole:
            return name + " · " + typ
        if role == Qt.TextAlignmentRole and col == 1:
            return Qt.AlignRight | Qt.AlignVCenter

    def sort(self, column, order=Qt.AscendingOrder):
        if column not in (0, 1, 2):
            return
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        names = [self.rows[i.row()][0] for i in old]
        key = (lambda r: r[0].casefold()) if column == 0 else (lambda r: r[column + 1])
        self.rows.sort(key=key, reverse=order == Qt.DescendingOrder)
        self.rows.sort(key=lambda r: r[1] != "folder")
        positions = {r[0]: i for i, r in enumerate(self.rows)} if old else {}
        self.changePersistentIndexList(
            old, [self.index(positions[n], i.column()) for n, i in zip(names, old)]
        )
        self.layoutChanged.emit()

    def reset(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class FileTable(QTableView):
    dropped = pyqtSignal(object)

    def startDrag(self, actions):
        if not self.selectionModel().selectedRows():
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData("application/x-port22", b"selected")
        drag.setMimeData(mime)
        drag.exec_(Qt.CopyAction)

    def dragEnterEvent(self, e):
        if isinstance(e.source(), FileTable) and e.source() != self:
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        self.dragEnterEvent(e)

    def dropEvent(self, e):
        if isinstance(e.source(), FileTable) and e.source() != self:
            self.dropped.emit(e.source())
            e.acceptProposedAction()


class Pane(QWidget):
    goto = pyqtSignal(str)
    send = pyqtSignal()
    selection = pyqtSignal()

    def __init__(self, local):
        super().__init__()
        self.local = local
        self.path = ""
        self.all = []
        self.seq = 0
        self.hidden = False
        self.busy = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = frame("pane")
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(label("Chromebook" if local else "Remote server", "h2"))
        heading.addStretch()
        self.transfer = button(
            "Upload" if local else "Download", self.send.emit, "up" if local else "down"
        )
        heading.addWidget(self.transfer)
        layout.addLayout(heading)
        toolbar = QHBoxLayout()
        for symbol, tip, callback in (
            (
                "up",
                "Parent folder",
                lambda: self.goto.emit(posixpath.dirname(self.path) or "/"),
            ),
            (
                "home",
                "Home folder",
                lambda: self.goto.emit(str(Path.home()) if local else "."),
            ),
        ):
            w = button("", callback, symbol)
            w.setToolTip(tip)
            w.setAccessibleName(tip)
            w.setFixedWidth(31)
            toolbar.addWidget(w)
        self.address = QLineEdit()
        self.address.setPlaceholderText("Folder path")
        self.address.setAccessibleName("Folder path")
        self.address.returnPressed.connect(
            lambda: self.goto.emit(self.address.text() or ".")
        )
        toolbar.addWidget(self.address, 1)
        w = button("", lambda: self.goto.emit(self.path), "refresh")
        w.setFixedWidth(31)
        w.setToolTip("Refresh folder")
        w.setAccessibleName("Refresh folder")
        toolbar.addWidget(w)
        layout.addLayout(toolbar)
        self.table = FileTable()
        self.model = FilesModel()
        self.table.setModel(self.model)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDragDropMode(QAbstractItemView.DragDrop)
        self.table.setDefaultDropAction(Qt.CopyAction)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 96)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(27)
        self.table.selectionModel().selectionChanged.connect(
            lambda *args: self.selection.emit()
        )
        self.table.doubleClicked.connect(self.open)
        layout.addWidget(self.table, 1)
        self.note = label("", "micro")
        layout.addWidget(self.note)

    def resizeEvent(self, event):
        self.table.setColumnHidden(2, self.width() < 390)
        super().resizeEvent(event)

    def open(self, index):
        name, typ, *_ = self.model.rows[index.row()]
        if typ == "folder":
            self.goto.emit(posixpath.join(self.path, name))
        elif typ == "file":
            self.send.emit()

    def chosen(self):
        return [
            self.model.rows[i.row()][:3]
            for i in self.table.selectionModel().selectedRows()
        ]

    def render(self):
        visible = [r for r in self.all if self.hidden or not r[0].startswith(".")]
        self.model.reset(visible)
        header = self.table.horizontalHeader()
        self.model.sort(header.sortIndicatorSection(), header.sortIndicatorOrder())
        self.address.setText(self.path)
        self.note.setText(f"{len(visible):,} items")
        self.note.setToolTip(self.path)
