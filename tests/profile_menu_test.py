"""Exercise actions on a saved connection through the card's Qt popup menu."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "port22"))
from app import Window
from store import Store
from widgets import ProfileDialog


class ProfileMenuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["port22"])

    def test_edit_and_remove_the_selected_profile(self):
        with TemporaryDirectory() as tmp:
            key = Path(tmp) / "example_key"
            key.touch()
            store = Store(Path(tmp) / "state")
            for name in ("Archive", "Staging"):
                profile = dict(
                    id=name,
                    name=name,
                    host="example.test",
                    user="root",
                    port=22,
                    key=str(key),
                    remote="/srv",
                )
                store.save("profiles", name, profile)
            window = Window(store)
            window.show()
            self.addCleanup(window.close)
            self.addCleanup(store.db.close)

            def choose(profile_name, action_name, respond):
                self.app.processEvents()
                card = next(
                    window.cards.itemAt(i).widget()
                    for i in range(window.cards.count())
                    if any(
                        label.text() == profile_name
                        for label in window.cards.itemAt(i)
                        .widget()
                        .findChildren(QLabel)
                    )
                )
                button = next(b for b in card.findChildren(QPushButton) if b.menu())
                menu = button.menu()
                action = next(a for a in menu.actions() if a.text() == action_name)
                menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
                self.app.processEvents()
                QTimer.singleShot(0, respond)
                QTest.mouseClick(
                    menu, Qt.LeftButton, pos=menu.actionGeometry(action).center()
                )

            def save_edits():
                dialog = self.app.activeModalWidget()
                self.assertIsInstance(dialog, ProfileDialog)
                self.assertEqual(dialog.fields["name"].text(), "Staging")
                dialog.fields["name"].setText("Build server")
                dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Save).click()

            choose("Staging", "Edit connection", save_edits)
            profiles = store.objects("profiles")
            self.assertEqual(profiles["Archive"]["name"], "Archive")
            self.assertEqual(profiles["Staging"]["name"], "Build server")
            self.assertEqual(profiles["Staging"]["key"], str(key))

            def answer(button):
                dialog = self.app.activeModalWidget()
                self.assertIsInstance(dialog, QMessageBox)
                self.assertIn("Build server", dialog.text())
                dialog.button(button).click()

            choose("Build server", "Remove connection", lambda: answer(QMessageBox.No))
            self.assertIn("Staging", store.objects("profiles"))
            choose("Build server", "Remove connection", lambda: answer(QMessageBox.Yes))
            self.assertEqual(list(store.objects("profiles")), ["Archive"])


if __name__ == "__main__":
    unittest.main()
