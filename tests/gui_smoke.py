"""Native Qt connection and download workflow using a temporary loopback server."""

import sys, time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "port22"))
from integration import Server
from app import Window
from store import Store
from widgets import ProfileDialog
from visual import STYLES
from PyQt5.QtCore import QTimer
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

        def download():
            d = app.activeModalWidget()
            assert d.windowTitle() == "Download"
            next(
                b for b in d.findChildren(QPushButton) if b.text() == "Download"
            ).click()

        QTimer.singleShot(55, download)
        w.remote.transfer.click()
        until(
            lambda: (
                (local / "index.php").exists()
                and store.jobs()[0]["status"] == "Complete"
            )
        )
        assert (local / "index.php").read_text() == "test GUI flow"
        w.show_page(2)
        app.processEvents()
        assert w.queue_key[0][1] == "Complete"
        w.show_page(1)
        w.resize(825, 530)
        app.processEvents()
        assert w.disconnect()
        w.show_page(0)
        print(
            "UI: saved profile, unlocked encrypted key, trusted host, remote navigation, hidden files, real download, queue history and disconnect passed"
        )
        w.close()
    finally:
        server.stop()
