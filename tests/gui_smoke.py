"""Native Qt connection and download workflow using a temporary loopback server."""

import sys, time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "port22"))
from integration import Server
from app import Window
from dialogs import ConflictDialog, PermissionDialog
import os
import stat
from store import Store
from widgets import ProfileDialog
from visual import STYLES
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QApplication, QInputDialog, QMessageBox
from PyQt5.QtWidgets import QPushButton, QDialogButtonBox
from PyQt5.QtTest import QTest

app = QApplication(["port22"])
app.setStyle("Fusion")
app.setStyleSheet(STYLES)


def until(fn, timeout=10):
    end = time.monotonic() + timeout
    while not fn():
        if time.monotonic() > end:
            raise Exception("UI wait timed out")
        QTest.qWait(30)


with TemporaryDirectory() as td:
    root = Path(td)
    server = Server(root)
    server.start()
    try:
        store = Store(root / "state")
        w = Window(store)
        w.show()
        QTest.qWait(150)
        local = root / "Downloads"
        local.mkdir()
        remote = root / "web"
        remote.mkdir()
        (remote / "index.php").write_text("test GUI flow")
        (remote / "assets").mkdir()
        (remote / ".hidden").touch()
        (remote / "image.webp").write_bytes(b"1234")
        (remote / "config.php").touch()
        submitted = []

        def fill():
            d = app.activeModalWidget()
            assert isinstance(d, ProfileDialog)
            for k, value in dict(
                name="Web production", host="127.0.0.1", user=server.user
            ).items():
                d.fields[k].setText(value)
            d.port.setValue(server.port)
            d.key.setText(str(server.client))
            d.remote.setText(str(remote))
            d.findChild(QDialogButtonBox).button(QDialogButtonBox.Save).click()
            submitted.append(1)

        QTimer.singleShot(90, fill)
        next(
            b for b in w.findChildren(QPushButton) if b.text() == "New connection"
        ).click()
        assert submitted and len(w.profiles) == 1
        profile = next(iter(w.profiles.values()))
        w.settings["last_local:" + profile["id"]] = str(local)
        pass_count = []
        trust_count = []

        def answer():
            dlg = app.activeModalWidget()
            if (
                isinstance(dlg, QInputDialog)
                and dlg.windowTitle() == "Unlock private key"
            ):
                dlg.setTextValue("test password")
                dlg.accept()
                pass_count.append(1)
            elif (
                isinstance(dlg, QMessageBox)
                and dlg.windowTitle() == "Verify server identity"
            ):
                next(
                    b for b in dlg.buttons() if b.text() == "Trust and connect"
                ).click()
                trust_count.append(1)

        timer = QTimer()
        timer.timeout.connect(answer)
        timer.start(25)
        next(b for b in w.findChildren(QPushButton) if b.text() == "Connect").click()
        until(lambda: w.session and w.remote.path and w.local.path)
        timer.stop()
        assert len(pass_count) == len(trust_count) == 1
        assert w.stack.currentIndex() == 1 and len(w.remote.model.rows) == 4
        w.dotfiles.setChecked(True)
        assert len(w.remote.model.rows) == 5
        idx = next(i for i, r in enumerate(w.remote.model.rows) if r[0] == "index.php")
        w.remote.table.selectRow(idx)

        w.remote.transfer.click()
        until(
            lambda: (
                (local / "index.php").exists()
                and store.jobs()[0]["status"] == "Complete"
            )
        )
        assert (local / "index.php").read_text() == "test GUI flow"

        # Drive the real context menu and dialog for a local and a remote chmod.
        def menu_action(pane, name, text):
            until(lambda: not pane.busy)
            i = next(i for i, r in enumerate(pane.model.rows) if r[0] == name)
            index = pane.model.index(i, 0)
            pane.table.scrollTo(index)

            def choose():
                menu = app.activePopupWidget()
                action = next(a for a in menu.actions() if a.text() == text)
                QTest.mouseClick(
                    menu, Qt.LeftButton, pos=menu.actionGeometry(action).center()
                )

            QTimer.singleShot(10, choose)
            pane.table.customContextMenuRequested.emit(
                pane.table.visualRect(index).center()
            )

        for pane, path in (
            (w.local, local / "index.php"),
            (w.remote, remote / "index.php"),
        ):
            until(
                lambda: "index.php" in [r[0] for r in pane.model.rows] and not pane.busy
            )
            answered = []

            def set_mode():
                dlg = app.activeModalWidget()
                if isinstance(dlg, PermissionDialog):
                    dlg.mode.setText("640")
                    dlg.findChild(QDialogButtonBox).button(
                        QDialogButtonBox.Apply
                    ).click()
                    answered.append(True)

            timer.timeout.disconnect()
            timer.timeout.connect(set_mode)
            timer.start(20)
            menu_action(pane, "index.php", "Permissions…")
            until(
                lambda: (
                    answered
                    and stat.S_IMODE(path.stat().st_mode) == 0o640
                    and "640" in pane.note.text()
                )
            )
            timer.stop()

        # Prompt on a real collision. Equal-size files with different mtimes must be replaced.
        (local / "index.php").write_text("X" * len("test GUI flow"))
        os.utime(local / "index.php", (100, 100))
        seen = []

        def decide():
            dlg = app.activeModalWidget()
            if isinstance(dlg, ConflictDialog):
                seen.append((dlg.rule.currentData(), dlg.scope.currentData()))
                dlg.rule.setCurrentIndex(dlg.rule.findData("different"))
                dlg.accept()

        timer.timeout.disconnect()
        timer.timeout.connect(decide)
        timer.start(20)

        def download_again(number):
            menu_action(w.remote, "index.php", "Download")
            until(
                lambda: (
                    len(store.jobs()) == number
                    and store.jobs()[0]["status"] == "Complete"
                    and w.batch is None
                )
            )

        download_again(2)
        assert (
            seen == [("skip", "session")]
            and (local / "index.php").read_text() == "test GUI flow"
        )
        assert "1/1 files" in w.work_note.text()
        download_again(3)
        assert len(seen) == 1 and "1 skipped" in w.work_note.text()
        timer.stop()
        w.show_page(2)
        app.processEvents()
        assert w.queue_key[0][1] == "Complete"
        w.show_page(1)
        w.resize(825, 530)
        app.processEvents()
        assert w.disconnect()
        w.show_page(0)
        print(
            "UI: saved profile, unlocked encrypted key, trusted host, remote navigation, hidden files, real downloads, context-menu chmod on both endpoints, conditional overwrite, session skip, queue history and disconnect passed"
        )
        w.close()
    finally:
        server.stop()
