#!/usr/bin/python3
from contextlib import nullcontext
import os
from pathlib import Path
import posixpath
import stat
import sys
import threading
import time
import uuid

from PyQt5.QtCore import QObject, Qt, QLockFile, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence, QPalette, QColor
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QScrollArea,
    QShortcut,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from conflicts import ConflictRules, POLICIES
from dialogs import ConflictDialog, PermissionDialog, QueueDialog
from connection import Session, describe, load_key
from store import Store, data_dir
from transfer import Batch, Endpoint
from telemetry import Throughput, Monitor, FileLanes
from visual import BG, LILAC, STYLES, SignalArt, button, frame, icon, label
from widgets import Pane, ProfileDialog, download_dir, fmt


def page():
    w = QWidget()
    w.setObjectName("page")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(24, 22, 24, 16)
    lay.setSpacing(13)
    return w, lay


def clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()


def scroll_list(layout):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    w = QWidget()
    w.setObjectName("page")
    v = QVBoxLayout(w)
    v.setAlignment(Qt.AlignTop)
    v.setContentsMargins(0, 0, 5, 0)
    v.setSpacing(8)
    scroll.setWidget(w)
    layout.addWidget(scroll, 1)
    return v


def title_row(layout, name, action=None):
    row = QHBoxLayout()
    row.addWidget(label(name, "h1"))
    row.addStretch()
    if action:
        row.addWidget(action)
    layout.addLayout(row)


def count_text(n, singular, plural=None):
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


class Dispatch(QObject):
    invoke = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.invoke.connect(lambda fn: fn(), Qt.QueuedConnection)

    def request(self, fn):
        ready = threading.Event()
        out = []

        def handle():
            try:
                out.append((True, fn()))
            except Exception as e:
                out.append((False, e))
            finally:
                ready.set()

        self.invoke.emit(handle)
        ready.wait()
        ok, val = out[0]
        if not ok:
            raise val
        return val


class Window(QMainWindow):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.bridge = Dispatch()
        self.profiles = store.objects("profiles")
        self.settings = store.objects("settings")
        self.session = None
        self.conflicts = None
        self.connecting = False
        self.batch = None
        self.closing = False
        self.queue_widgets = {}
        self.queue_key = None
        self.monitor_job = None
        self.series = Throughput()
        self.setWindowTitle("PORT22")
        self.setWindowIcon(icon("mark", LILAC, 64))
        self.resize(1150, 730)
        self.setMinimumSize(720, 480)
        root = QWidget()
        root.setObjectName("page")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        rail = frame("rail")
        self.rail = rail
        rail.setFixedWidth(192)
        outer.addWidget(rail)
        v = QVBoxLayout(rail)
        v.setContentsMargins(13, 23, 13, 15)
        v.setSpacing(7)
        brand = QHBoxLayout()
        logo = label()
        logo.setPixmap(icon("mark", LILAC, 28).pixmap(28, 28))
        brand.addWidget(logo)
        self.brand_text = label("PORT22", "brand")
        brand.addWidget(self.brand_text)
        brand.addStretch()
        v.addLayout(brand)
        v.addSpacing(25)
        self.nav = []
        for i, (name, symbol) in enumerate(
            (
                ("Connections", "server"),
                ("Workspace", "workspace"),
                ("Transfers", "queue"),
            )
        ):
            b = button(name, lambda i=i: self.show_page(i), symbol, "nav")
            b.setCheckable(True)
            b.setToolTip(name)
            b.setAccessibleName(name)
            v.addWidget(b)
            self.nav.append(b)
        v.addStretch()
        self.pref_button = button("Preferences", self.preferences, "settings", "nav")
        v.addWidget(self.pref_button)
        self.status = label("○  Disconnected", "micro")
        v.addWidget(self.status)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        saved, lay = page()
        self.stack.addWidget(saved)
        self.hero = SignalArt()
        self.hero.setFixedHeight(152)
        h = QVBoxLayout(self.hero)
        h.setContentsMargins(22, 16, 0, 15)
        h.setSpacing(4)
        h.addWidget(label("S F T P   /   C O N N E C T I O N S", "micro"))
        h.addWidget(label("Saved connections", "h1"))
        self.lead = label("", "muted")
        h.addWidget(self.lead)
        b = button("New connection", self.edit, "plus", "primary")
        h.addWidget(b, 0, Qt.AlignLeft)
        lay.addWidget(self.hero)
        self.saved_sub = label("", "micro")
        lay.addWidget(self.saved_sub)
        self.cards = scroll_list(lay)
        self.saved_foot = label("", "micro")
        lay.addWidget(self.saved_foot)
        work, wl = page()
        self.stack.addWidget(work)
        top = QHBoxLayout()
        self.work_title = label("Workspace", "h1")
        top.addWidget(self.work_title, 1)
        self.dotfiles = QCheckBox("Hidden files")
        self.dotfiles.setChecked(self.settings.get("hidden", False))
        self.dotfiles.toggled.connect(self.toggle_hidden)
        top.addWidget(self.dotfiles)
        top.addWidget(button("Disconnect", self.disconnect))
        wl.addLayout(top)
        self.address_hint = label("", "mint")
        wl.addWidget(self.address_hint)
        spl = QSplitter()
        spl.setChildrenCollapsible(False)
        self.local = Pane(True)
        self.remote = Pane(False)
        for p in (self.local, self.remote):
            spl.addWidget(p)
            p.goto.connect(lambda path, p=p: self.browse(p, path))
            p.send.connect(lambda p=p: self.enqueue(p))
            p.selection.connect(self.selection)
            p.permissions.connect(lambda p=p: self.permissions(p))
        self.local.table.dropped.connect(lambda _: self.enqueue(self.remote))
        self.remote.table.dropped.connect(lambda _: self.enqueue(self.local))
        wl.addWidget(spl, 1)
        strip = frame()
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(12, 8, 10, 8)
        self.work_monitor = Monitor(self.series)
        self.work_monitor.hide()
        wl.addWidget(self.work_monitor)
        self.work_note = label("", "muted")
        self.work_note.setWordWrap(True)
        strip_layout.addWidget(self.work_note, 1)
        strip_layout.addWidget(
            button("View transfers", lambda: self.show_page(2), "queue")
        )
        wl.addWidget(strip)
        queue, ql = page()
        self.stack.addWidget(queue)
        self.clear_done = button("Clear completed", self.clear_history)
        title_row(ql, "Transfers", self.clear_done)
        self.queue_top = label("", "micro")
        ql.addWidget(self.queue_top)
        self.queue_monitor = Monitor(self.series)
        self.queue_monitor.hide()
        ql.addWidget(self.queue_monitor)
        self.lanes = FileLanes()
        self.lanes.hide()
        ql.addWidget(self.lanes)
        self.queue_cards = scroll_list(ql)
        self.show_page(0)
        self.draw_profiles()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.edit)
        QShortcut(
            QKeySequence("Ctrl+H"),
            self,
            activated=lambda: (
                self.dotfiles.toggle() if self.stack.currentIndex() == 1 else None
            ),
        )
        QShortcut(QKeySequence("Ctrl+1"), self, activated=lambda: self.show_page(0))
        QShortcut(QKeySequence("Ctrl+2"), self, activated=lambda: self.show_page(1))
        QShortcut(QKeySequence("Ctrl+3"), self, activated=lambda: self.show_page(2))
        self.tick()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "pref_button"):
            return
        compact = self.width() < 940
        self.rail.setFixedWidth(66 if compact else 192)
        self.brand_text.setVisible(not compact)
        self.status.setVisible(not compact)
        for obj, name in zip(
            [*self.nav, self.pref_button],
            ["Connections", "Workspace", "Transfers", "Preferences"],
        ):
            obj.setText("" if compact else name)
            obj.setToolTip(name)
            obj.setAccessibleName(name)
        for key_label in getattr(self, "key_labels", []):
            key_label.setVisible(self.width() >= 1000)
        self.compact = compact

    def background(self, fn, ok, fail=None):
        def run():
            try:
                result = fn()
            except Exception as e:
                self.bridge.invoke.emit(
                    lambda e=e: (fail or self.alert)(e) if not self.closing else None
                )
            else:
                self.bridge.invoke.emit(
                    lambda: ok(result) if not self.closing else None
                )

        threading.Thread(target=run, daemon=True).start()

    def alert(self, e):
        QMessageBox.warning(
            self,
            "PORT22",
            describe(e) if isinstance(e, BaseException) else str(e),
        )

    def show_page(self, i):
        if i == 1 and not self.session:
            i = 0
        self.stack.setCurrentIndex(i)
        for j, b in enumerate(self.nav):
            b.setChecked(i == j)
        self.nav[1].setEnabled(bool(self.session))
        if i == 2:
            self.draw_queue()

    def draw_profiles(self):
        clear(self.cards)
        self.key_labels = []
        profiles = list(self.profiles.values())
        profiles.sort(key=lambda p: p["name"].casefold())
        self.lead.setText(
            "Choose a server to open its file workspace."
            if profiles
            else "Add your first server to get started."
        )
        self.saved_sub.setText(
            count_text(len(profiles), "SAVED SERVER", "SAVED SERVERS").upper()
        )
        for p in profiles:
            w = frame()
            row = QHBoxLayout(w)
            row.setContentsMargins(16, 12, 14, 12)
            row.setSpacing(12)
            logo = label()
            logo.setFixedSize(40, 40)
            logo.setAlignment(Qt.AlignCenter)
            logo.setStyleSheet("background:#2b2138; border-radius:8px;")
            logo.setPixmap(icon("server", LILAC, 24).pixmap(24, 24))
            row.addWidget(logo)
            pair = QVBoxLayout()
            pair.setSpacing(2)
            pair.addWidget(label(p["name"], "strong"))
            address = label(f"{p['user']}@{p['host']}  ·  {p['port']}", "micro")
            address.setToolTip(p["key"])
            pair.addWidget(address)
            row.addLayout(pair, 1)
            identity = label(Path(p["key"]).name, "micro")
            identity.setToolTip(p["key"])
            identity.setMaximumWidth(180)
            identity.setVisible(self.width() >= 1000)
            self.key_labels.append(identity)
            row.addWidget(identity)
            here = self.session and self.session.profile["id"] == p["id"]
            go = button(
                "Open" if here else "Connect", lambda p=p: self.connect_to(p), "right"
            )
            go.setEnabled(not self.connecting)
            row.addWidget(go)
            menu = button("⋯")
            menu.setToolTip("Connection options")
            menu.setAccessibleName("Connection options")
            m = QMenu(menu)
            m.addAction("Edit connection").triggered.connect(
                lambda checked=False, profile=p: self.edit(profile)
            )
            m.addAction("Remove connection").triggered.connect(
                lambda checked=False, profile=p: self.remove(profile)
            )
            menu.setMenu(m)
            row.addWidget(menu)
            self.cards.addWidget(w)
        self.saved_foot.setText(
            "Connecting…"
            if self.connecting
            else f"Parallel transfers   {self.settings.get('parallel', 4)}"
        )

    def unfinished(self, p):
        return any(
            j["profile"] == p["id"] and j["status"] != "Complete"
            for j in self.store.jobs()
        )

    def edit(self, p=None):
        if p and (
            self.connecting or self.session and self.session.profile["id"] == p["id"]
        ):
            self.alert("Disconnect before editing this connection.")
            return
        dlg = ProfileDialog(self, dict(p) if p else None)
        if dlg.exec_() != QDialog.Accepted:
            return
        new = dlg.profile
        if (
            p
            and self.unfinished(p)
            and any(new[k] != p[k] for k in ("host", "user", "port"))
        ):
            self.alert(
                "This connection has unfinished transfers. Save the new server as a separate connection."
            )
            return
        new.setdefault("id", uuid.uuid4().hex)
        self.store.save("profiles", new["id"], new)
        self.profiles[new["id"]] = new
        self.draw_profiles()

    def remove(self, p):
        if (
            self.connecting
            or self.session
            and self.session.profile["id"] == p["id"]
            or self.unfinished(p)
        ):
            self.alert(
                "Disconnect and finish or remove this server’s queued transfers first."
            )
            return
        if (
            QMessageBox.question(
                self,
                "Remove saved connection",
                f"Remove {p['name']} from saved connections?",
            )
            != QMessageBox.Yes
        ):
            return
        with self.store.write() as db:
            db.execute("DELETE FROM profiles WHERE id=?", (p["id"],))
        del self.profiles[p["id"]]
        self.draw_profiles()

    def ask_key(self, name):
        value, ok = QInputDialog.getText(
            self, "Unlock private key", f"Passphrase for {name}", QLineEdit.Password
        )
        if not ok:
            raise ValueError("Key unlock cancelled.")
        return value

    def trust(self, host, algorithm, fp):
        box = QMessageBox(self)
        box.setWindowTitle("Verify server identity")
        box.setTextFormat(Qt.PlainText)
        box.setText(f"Trust the SSH host key for {host}?")
        box.setInformativeText(
            f"{algorithm}\n{fp}\n\nCompare this fingerprint with your Droplet before continuing."
        )
        box.setDetailedText(
            'In the DigitalOcean Droplet Console, run:\nfor key in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$key"; done'
        )
        yes = box.addButton("Trust and connect", QMessageBox.AcceptRole)
        no = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(no)
        box.exec_()
        return box.clickedButton() == yes

    def connect_to(self, p):
        if self.connecting:
            return
        if self.session and self.session.profile["id"] == p["id"]:
            self.show_page(1)
            return
        if self.session and not self.disconnect():
            return
        self.connecting = True
        self.draw_profiles()
        self.status.setText("◌  Connecting")

        def connect():
            key = load_key(
                p["key"],
                lambda: self.bridge.request(lambda: self.ask_key(Path(p["key"]).name)),
            )
            s = Session(dict(p), key, self.store.path)
            try:
                s.connect(lambda *a: self.bridge.request(lambda: self.trust(*a)))
                with s.channel() as ch:
                    home = ch.normalize(".")
                return s, home
            except BaseException:
                s.close()
                raise

        def ok(result):
            self.connecting = False
            self.session, home = result
            self.conflicts = ConflictRules(self.ask_conflict)
            self.status.setText("●  Connected")
            self.status.setObjectName("mint")
            self.draw_profiles()
            self.show_page(1)
            self.work_title.setText(p["name"])
            self.work_monitor.hide()
            self.address_hint.setText(f"{p['user']}@{p['host']}  /  SFTP")
            self.work_note.setText(
                "Select files or folders, then choose Upload or Download."
            )
            for pane in (self.local, self.remote):
                pane.path = ""
                pane.all = []
                pane.render()
            self.remote_home = home
            remembered = self.settings.get("last_local:" + p["id"], download_dir())
            self.browse(
                self.local, remembered if Path(remembered).is_dir() else download_dir()
            )
            self.browse(self.remote, p["remote"] or home)

        def failed(e):
            self.connecting = False
            self.draw_profiles()
            self.status.setText("○  Disconnected")
            self.alert(e)

        self.background(connect, ok, failed)

    def disconnect(self):
        if not self.session:
            return True
        if self.batch and self.batch.thread.is_alive():
            if (
                QMessageBox.question(
                    self, "Pause transfer", "Pause the active transfer and disconnect?"
                )
                != QMessageBox.Yes
            ):
                return False
            self.batch.pause()
        pid = self.session.profile["id"]
        with self.store.write() as db:
            db.execute(
                "UPDATE jobs SET status='Paused' WHERE profile=? AND status='Queued'",
                (pid,),
            )
        self.session.close()
        self.session = None
        self.status.setText("○  Disconnected")
        self.show_page(0)
        self.draw_profiles()
        return True

    def browse(self, p, path):
        if not self.session:
            return
        s = self.session
        p.seq += 1
        seq = p.seq
        p.busy = True
        p.note.setText("Reading folder…")
        self.selection()

        def read():
            if p.local:
                target = os.path.realpath(
                    os.path.join(p.path or download_dir(), os.path.expanduser(path))
                )
                rows = list(Endpoint().list(target))
            else:
                with s.channel() as ch:
                    target = ch.normalize(
                        self.remote_home
                        if path == "."
                        else posixpath.join(p.path or self.remote_home, path)
                    )
                    rows = list(Endpoint(ch).list(target))
            rows.sort(key=lambda r: (r[1] != "folder", r[0].casefold(), r[0]))
            return target, rows

        def current():
            return self.session is s and seq == p.seq

        def ok(result):
            if current():
                p.busy = False
                p.path, p.all = result
                p.hidden = self.dotfiles.isChecked()
                p.render()
                self.selection()
                if p.local:
                    setting = "last_local:" + s.profile["id"]
                    self.settings[setting] = p.path
                    self.store.save("settings", setting, p.path)

        def fail(e):
            if current():
                p.busy = False
                p.address.setText(p.path)
                p.note.setText("Could not open folder")
                self.selection()
                self.alert(e)

        self.background(read, ok, fail)

    def toggle_hidden(self, on):
        self.settings["hidden"] = on
        self.store.save("settings", "hidden", on)
        for p in (self.local, self.remote):
            p.hidden = on
            p.render()

    def selection(self):
        if not hasattr(self, "remote"):
            return
        ready = bool(
            self.session
            and self.local.path
            and self.remote.path
            and not self.local.busy
            and not self.remote.busy
        )
        for p in (self.local, self.remote):
            p.transfer.setEnabled(ready and bool(p.chosen()))

    def enqueue(self, p):
        if not self.session or self.local.busy or self.remote.busy:
            return
        entries = p.chosen()
        if not entries or not self.local.path or not self.remote.path:
            return
        dest = self.remote if p.local else self.local
        direction = "Upload" if p.local else "Download"
        self.store.add(
            self.session.profile["id"],
            direction,
            p.path,
            dest.path,
            entries,
            "ask",
        )
        self.work_note.setText(f"{direction} queued. Open Transfers to see progress.")
        self.tick()

    def ask_conflict(self, batch, path, source, target):
        def prompt():
            if self.closing or self.session is not batch.session or batch.stop.is_set():
                return None
            dlg = ConflictDialog(self, batch.job["direction"], path, source, target)
            guard = QTimer(dlg)
            guard.timeout.connect(
                lambda: dlg.reject() if batch.stop.is_set() or self.closing else None
            )
            guard.start(200)
            choice = dlg.choice()
            guard.stop()
            dlg.deleteLater()
            return choice

        return self.bridge.request(prompt)

    def permissions(self, pane):
        entries = pane.chosen()
        session, folder, sequence = self.session, pane.path, pane.seq
        if not session or not entries or pane.busy:
            return
        paths = [(posixpath.join(folder, name), typ) for name, typ, _ in entries]

        def inspect(path, typ, endpoint):
            mode = endpoint.stat(path).st_mode
            if not (
                (typ == "folder" and stat.S_ISDIR(mode))
                or (typ == "file" and stat.S_ISREG(mode))
            ):
                raise ValueError(f"Item changed type: {path}")
            return mode

        def channel():
            return nullcontext(None) if pane.local else session.channel()

        def read():
            with channel() as ch:
                endpoint = Endpoint(ch)
                return [inspect(path, typ, endpoint) for path, typ in paths]

        def edit(modes):
            if self.session is not session or pane.seq != sequence:
                return
            title = (
                entries[0][0]
                if len(entries) == 1
                else count_text(len(entries), "selected item")
            )
            dlg = PermissionDialog(self, title, modes)
            if not dlg.exec_() or self.session is not session:
                return
            mode = int(dlg.mode.text(), 8)

            def apply():
                with channel() as ch:
                    endpoint = Endpoint(ch)
                    for i, (path, typ) in enumerate(paths):
                        try:
                            inspect(path, typ, endpoint)
                            if ch:
                                ch.chmod(path, mode)
                            else:
                                os.chmod(path, mode, follow_symlinks=False)
                        except Exception as error:
                            raise ValueError(
                                f"Updated {i} of {len(paths)} items. {path}: {describe(error)}"
                            ) from error

            def complete(_):
                if self.session is session and pane.seq == sequence:
                    pane.note.setText(f"Permissions set to {mode:03o} · {title}")

            self.background(apply, complete)

        self.background(read, edit)

    def progress_text(self, job, active):
        counts = self.store.progress(job["id"], active)
        text = (
            f"{counts['copied']:,}/{counts['files']:,} files   ·   "
            f"{fmt(counts['transferred'])} / {fmt(counts['size'])}"
        )
        if counts["discovering"]:
            text += (
                "   ·   Scanning and transferring…"
                if active
                else "   ·   Scanning folders…"
            )
        if counts["skipped"]:
            text += f"   ·   {counts['skipped']:,} skipped"
        if counts["failed"]:
            text += f"   ·   {counts['failed']:,} failed"
        return text

    def preferences(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Preferences")
        l = QVBoxLayout(dlg)
        l.setContentsMargins(22, 20, 22, 20)
        l.setSpacing(12)
        l.addWidget(label("Transfer preferences", "h2"))
        f = QFormLayout()
        n = QSpinBox()
        n.setRange(1, 8)
        n.setValue(self.settings.get("parallel", 4))
        f.addRow("Simultaneous files", n)
        motion = QCheckBox("Animate live graphs and file progress")
        motion.setChecked(self.settings.get("motion", True))
        f.addRow("", motion)
        l.addLayout(f)
        hint = label(
            "Applies to the next batch. Pause and resume an active batch to change its limit.",
            "muted",
        )
        hint.setWordWrap(True)
        l.addWidget(hint)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        l.addWidget(box)
        if dlg.exec_():
            self.settings["parallel"] = n.value()
            self.store.save("settings", "parallel", n.value())
            self.settings["motion"] = motion.isChecked()
            self.store.save("settings", "motion", motion.isChecked())
            self.tick()
            self.draw_profiles()
            self.draw_queue()

    def tick(self):
        if self.closing:
            return
        activation = self.store.path / "activate"
        if activation.exists() and not QApplication.activeModalWidget():
            activation.unlink(missing_ok=True)
            if self.isMinimized():
                self.showNormal()
            self.show_page(0)
            self.raise_()
            self.activateWindow()
        if self.batch and not self.batch.thread.is_alive():
            finished = self.batch
            self.batch = None
            _, total = finished.snapshot()
            self.series.sample(finished.finished_at, total, {}, finished=True)
            self.lanes.set_files(finished.activity(), finished.finished_at, False)
            outcome = finished.outcome or "Stopped"
            self.render_monitor(finished, outcome)
            if self.session and self.session is finished.session:
                pane = (
                    self.local
                    if finished.job["direction"] == "Download"
                    else self.remote
                )
                if pane.path and not pane.busy:
                    self.browse(pane, pane.path)
                self.work_note.setText(
                    outcome + "   ·   " + self.progress_text(finished.job, {})
                )
        jobs = self.store.jobs()
        if self.session and not self.batch:
            nextjob = next(
                (
                    j
                    for j in reversed(jobs)
                    if j["profile"] == self.session.profile["id"]
                    and j["status"] == "Queued"
                ),
                None,
            )
            if nextjob:
                self.batch = Batch(
                    self.store,
                    self.session,
                    nextjob,
                    self.settings.get("parallel", 4),
                    self.conflicts,
                )
                self.series.reset(time.monotonic())
                self.batch.thread.start()
        if self.batch:
            now = time.monotonic()
            active, total = self.batch.snapshot()
            self.series.sample(now, total, active)
            self.render_monitor(self.batch, "")
            self.lanes.set_files(
                self.batch.activity(), now, self.settings.get("motion", True)
            )
            if self.session is self.batch.session and self.stack.currentIndex() == 1:
                self.work_note.setText(self.progress_text(self.batch.job, active))
        n = sum(j["status"] not in ("Complete",) for j in jobs)
        self.nav[2].setText(
            ""
            if getattr(self, "compact", False)
            else "Transfers" + (f"  ·  {n}" if n else "")
        )
        if self.stack.currentIndex() == 2:
            self.draw_queue()

    def render_monitor(self, batch, outcome):
        self.monitor_job = batch.job["id"]
        self.queue_monitor.display(
            batch.job["direction"], outcome, self.settings.get("motion", True)
        )
        if self.session is batch.session:
            self.work_monitor.display(
                batch.job["direction"], outcome, self.settings.get("motion", True)
            )
        else:
            self.work_monitor.hide()

    def clear_history(self, job_id=None):
        with self.store.write() as db:
            if job_id is None:
                db.execute("DELETE FROM jobs WHERE status='Complete'")
            else:
                db.execute(
                    "DELETE FROM jobs WHERE id=? AND status='Complete'", (job_id,)
                )
        if self.monitor_job and not any(
            job["id"] == self.monitor_job for job in self.store.jobs()
        ):
            self.monitor_job = None
            self.queue_monitor.hide()
            self.lanes.hide()
        self.draw_queue()

    def action(self, job):
        state = job["status"]
        id_ = job["id"]
        if self.batch and self.batch.job["id"] == id_ and self.batch.thread.is_alive():
            self.batch.pause()
        elif state == "Queued":
            self.store.status(id_, "Paused")
        elif state in ("Paused", "Needs attention"):
            if not self.session or self.session.profile["id"] != job["profile"]:
                self.alert(
                    "Connect to "
                    + self.profiles.get(job["profile"], {}).get(
                        "name", "the saved server"
                    )
                    + " first, then resume this transfer."
                )
                return
            self.store.retry(id_)
        self.tick()

    def remove_job(self, j):
        if (
            self.batch
            and self.batch.job["id"] == j["id"]
            and self.batch.thread.is_alive()
        ):
            return
        if (
            QMessageBox.question(
                self,
                "Remove from queue",
                "Remove this batch from the queue? Files already copied will stay in place.",
            )
            == QMessageBox.Yes
        ):
            with self.store.write() as db:
                db.execute("DELETE FROM jobs WHERE id=?", (j["id"],))
            self.draw_queue()

    def details(self, j):
        errs = self.store.errors(j["id"])
        text = "\n".join(e["rel"] + " — " + e["error"] for e in errs)
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Transfer details")
        dlg.setTextFormat(Qt.PlainText)
        dlg.setText(j["title"])
        nm = self.profiles.get(j["profile"], {}).get("name", "Removed connection")
        rule = next(
            (row["policy"] for row in self.store.jobs() if row["id"] == j["id"]),
            j["policy"],
        )
        policy = POLICIES.get(rule, "Ask on conflict")
        if (
            rule == "ask"
            and self.session
            and self.session.profile["id"] == j["profile"]
            and self.conflicts
            and self.conflicts.session_rule
        ):
            policy = POLICIES[self.conflicts.session_rule] + " (this session)"
        msg = f"{j['direction']} · {nm}\nFrom {j['src']}\nTo {j['dst']}\nExisting files: {policy}"
        dlg.setInformativeText(msg)
        if text:
            dlg.setDetailedText(text)
        dlg.exec_()

    def draw_queue(self):
        jobs = self.store.jobs()
        self.clear_done.setEnabled(any(j["status"] == "Complete" for j in jobs))
        self.queue_top.setText(
            f"{count_text(len(jobs), 'batch', 'batches')}  /  {self.settings.get('parallel', 4)} parallel files"
        )
        signature = tuple((j["id"], j["status"]) for j in jobs)
        if signature != self.queue_key:
            self.queue_key = signature
            clear(self.queue_cards)
            self.queue_widgets = {}
            if not jobs:
                w = frame()
                l = QVBoxLayout(w)
                l.setContentsMargins(18, 18, 18, 18)
                l.addWidget(label("Your queue is empty", "h2"))
                l.addWidget(
                    label("Upload or download a selection from the workspace.", "muted")
                )
                self.queue_cards.addWidget(w)
            for j in jobs:
                w = frame()
                col = QVBoxLayout(w)
                col.setContentsMargins(14, 10, 14, 10)
                col.setSpacing(5)
                row = QHBoxLayout()
                title = label(j["title"], "strong")
                title.setWordWrap(True)
                row.addWidget(title, 1)
                row.addWidget(
                    label(j["status"], "mint" if j["status"] == "Complete" else "micro")
                )
                row.addWidget(button("Details", lambda j=j: self.details(j)))
                if j["status"] == "Complete":
                    dismiss = button("×", lambda j=j: self.clear_history(j["id"]))
                    dismiss.setAccessibleName(
                        f"Remove {j['title']} from transfer history"
                    )
                    dismiss.setToolTip("Remove from transfer history")
                    dismiss.setFixedWidth(32)
                    row.addWidget(dismiss)
                else:
                    row.addWidget(
                        button(
                            "Queue",
                            lambda j=j: QueueDialog(self, self.store, j).exec_(),
                        )
                    )
                action = {
                    "Running": "Pause",
                    "Queued": "Pause",
                    "Paused": "Resume",
                    "Needs attention": "Retry",
                }.get(j["status"])
                if action:
                    row.addWidget(button(action, lambda j=j: self.action(j)))
                if j["status"] in ("Paused", "Needs attention"):
                    row.addWidget(button("Remove", lambda j=j: self.remove_job(j)))
                col.addLayout(row)
                p = self.profiles.get(j["profile"], {}).get(
                    "name", "Removed connection"
                )
                col.addWidget(label(f"{j['direction']}  ·  {p}", "micro"))
                status = label("", "muted")
                status.setWordWrap(True)
                col.addWidget(status)
                self.queue_widgets[j["id"]] = status
                self.queue_cards.addWidget(w)
                self.update_summary(j)
        if self.batch and self.batch.job["id"] in self.queue_widgets:
            self.update_summary(self.batch.job)

    def update_summary(self, j):
        active = (
            self.series.active if self.batch and self.batch.job["id"] == j["id"] else {}
        )
        self.queue_widgets[j["id"]].setText(self.progress_text(j, active))

    def closeEvent(self, event):
        if self.connecting:
            self.alert("Finish or cancel the connection prompt before closing.")
            event.ignore()
            return
        if self.batch and self.batch.thread.is_alive():
            if (
                not self.closing
                and QMessageBox.question(
                    self,
                    "Pause and quit",
                    "Pause the transfer and close PORT22? You can resume the queue next time.",
                )
                != QMessageBox.Yes
            ):
                event.ignore()
                return
            self.closing = True
            self.batch.pause()
            if self.session:
                self.session.close()
            event.ignore()
            QTimer.singleShot(200, self.close)
            return
        self.closing = True
        if self.session:
            self.session.close()
        event.accept()


def main():
    os.umask(0o077)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    app = QApplication(["port22", *sys.argv[1:]])
    app.setApplicationName("port22")
    app.setApplicationDisplayName("PORT22")
    app.setDesktopFileName("port22")
    app.setStyle("Fusion")
    palette = app.palette()
    for role, col in (
        (QPalette.Window, BG),
        (QPalette.Base, "#14111b"),
        (QPalette.Text, "#f5f1ff"),
        (QPalette.WindowText, "#f5f1ff"),
        (QPalette.ButtonText, "#f5f1ff"),
        (QPalette.Button, "#241e2e"),
        (QPalette.Highlight, "#614a78"),
        (QPalette.HighlightedText, "#ffffff"),
    ):
        palette.setColor(role, QColor(col))
    app.setPalette(palette)
    app.setStyleSheet(STYLES)
    directory = data_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = QLockFile(str(directory / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        (directory / "activate").touch(mode=0o600)
        return 0
    window = Window(Store(directory))
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
