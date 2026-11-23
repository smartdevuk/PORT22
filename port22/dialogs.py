"""Transfer decisions and queue controls for the two-pane workspace."""

import stat
import time

from PyQt5.QtCore import Qt, QTimer, QRegularExpression
from PyQt5.QtGui import QRegularExpressionValidator
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from conflicts import POLICIES
from visual import button, label
from widgets import fmt


class ConflictDialog(QDialog):
    def __init__(self, parent, direction, path, source, target):
        super().__init__(parent)
        self.setWindowTitle(f"{direction} · File already exists")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        title = label(path, "h2")
        title.setWordWrap(True)
        layout.addWidget(title)
        form = QFormLayout()
        for caption, info in zip(("Source", "Destination"), (source, target)):
            modified = (
                time.strftime("%d %b %Y  %H:%M:%S", time.localtime(info.st_mtime))
                if info.st_mtime is not None
                else "Unknown time"
            )
            name = "Folder" if stat.S_ISDIR(info.st_mode) else fmt(info.st_size or 0)
            details = label(f"{name}   ·   {modified}")
            if stat.S_ISREG(info.st_mode):
                details.setToolTip(f"{info.st_size:,} bytes")
            form.addRow(caption, details)
        self.rule = QComboBox()
        for key, text in POLICIES.items():
            self.rule.addItem(text, key)
        form.addRow("When names match", self.rule)
        self.scope = QComboBox()
        for text, key in (
            ("Just this file", "once"),
            ("This transfer", "transfer"),
            ("This connection session", "session"),
        ):
            self.scope.addItem(text, key)
        self.scope.setCurrentIndex(2)
        form.addRow("Apply to", self.scope)
        layout.addLayout(form)
        box = QDialogButtonBox()
        box.addButton("Pause transfer", QDialogButtonBox.RejectRole)
        box.addButton("Apply", QDialogButtonBox.AcceptRole).setDefault(True)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def choice(self):
        if self.exec_():
            return self.rule.currentData(), self.scope.currentData()


class PermissionDialog(QDialog):
    def __init__(self, parent, title, modes):
        super().__init__(parent)
        self.setWindowTitle("Permissions")
        self.setMinimumWidth(365)
        layout = QVBoxLayout(self)
        heading = label(title, "h2")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        form = QFormLayout()
        self.mode = QLineEdit()
        self.mode.setPlaceholderText("Mixed permissions")
        self.mode.setValidator(
            QRegularExpressionValidator(QRegularExpression("[0-7]{3,4}"), self)
        )
        form.addRow("Octal permissions", self.mode)
        layout.addLayout(form)
        preview = label("", "micro")
        layout.addWidget(preview)
        if len(modes) > 1 or any(stat.S_ISDIR(m) for m in modes):
            layout.addWidget(label("Applies to selected items only.", "muted"))
        box = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        apply = box.button(QDialogButtonBox.Apply)
        apply.clicked.connect(self.accept)
        box.rejected.connect(self.reject)

        def changed(value):
            valid = self.mode.hasAcceptableInput()
            apply.setEnabled(valid)
            preview.setText(
                stat.filemode(int(value, 8))[1:]
                if valid
                else "Enter a mode such as 644 or 755."
            )

        self.mode.textChanged.connect(changed)
        unique = {stat.S_IMODE(m) for m in modes}
        self.mode.setText(format(unique.pop(), "03o") if len(unique) == 1 else "")
        changed(self.mode.text())
        layout.addWidget(box)
        self.mode.selectAll()


class QueueDialog(QDialog):
    PAGE = 150

    def __init__(self, parent, store, job):
        super().__init__(parent)
        self.store, self.job = store, job
        self.offset = 0
        self.signature = None
        self.setWindowTitle("Transfer queue")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(620, 430)
        layout = QVBoxLayout(self)
        layout.addWidget(label(job["title"], "h2"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a queued path…")
        self.search.textChanged.connect(self.search_changed)
        layout.addWidget(self.search)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Queued path", "Type"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.note = label("", "micro")
        layout.addWidget(self.note)
        self.feedback = label("", "muted")
        self.feedback.hide()
        layout.addWidget(self.feedback)
        row = QHBoxLayout()
        self.prev = button("Previous", lambda: self.page(-1))
        self.next = button("Next", lambda: self.page(1))
        self.remove = button("Remove selected", self.remove_selected)
        for b in (self.prev, self.next):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self.remove)
        row.addWidget(button("Close", self.accept))
        layout.addLayout(row)
        self.table.itemSelectionChanged.connect(
            lambda: self.remove.setEnabled(bool(self.selected()))
        )
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.finished.connect(self.timer.stop)
        self.timer.start(700)
        self.refresh()

    def selected(self):
        return [
            self.table.item(i.row(), 0).data(Qt.UserRole)
            for i in self.table.selectionModel().selectedRows()
        ]

    def search_changed(self):
        self.offset = 0
        self.refresh()

    def page(self, step):
        self.offset = max(0, self.offset + step * self.PAGE)
        self.refresh()

    def refresh(self):
        rows = self.store.queued_entries(
            self.job["id"], self.search.text(), self.PAGE + 1, self.offset
        )
        if not rows and self.offset:
            self.offset = 0
            return self.refresh()
        self.prev.setEnabled(self.offset > 0)
        self.next.setEnabled(len(rows) > self.PAGE)
        rows = rows[: self.PAGE]
        signature = [r["id"] for r in rows]
        if signature != self.signature:
            selected = set(self.selected())
            scroll = self.table.verticalScrollBar().value()
            self.table.clearSelection()
            self.table.setRowCount(0)
            self.table.setRowCount(len(rows))
            for i, entry in enumerate(rows):
                name = QTableWidgetItem(entry["rel"])
                name.setData(Qt.UserRole, entry["id"])
                name.setToolTip(entry["rel"])
                self.table.setItem(i, 0, name)
                self.table.setItem(i, 1, QTableWidgetItem(entry["kind"].capitalize()))
                if entry["id"] in selected:
                    name.setSelected(True)
                    self.table.item(i, 1).setSelected(True)
            self.table.verticalScrollBar().setValue(scroll)
            self.signature = signature
        self.remove.setEnabled(bool(self.selected()))
        self.note.setText(
            f"Items {self.offset + 1}–{self.offset + len(rows)} · Active files finish normally."
            if rows
            else ("No matching paths." if self.search.text() else "No items waiting.")
        )

    def remove_selected(self):
        removed, folders = self.store.remove_entries(self.job["id"], self.selected())
        message = f"Removed {removed:,} queued items."
        if folders:
            message += " Remaining contents of selected folders are excluded."
        elif not removed:
            message = "The selected files have already started."
        self.feedback.setText(message)
        self.feedback.show()
        self.refresh()
