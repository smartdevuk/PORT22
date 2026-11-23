"""Conflict scope, queue edits racing discovery, totals and the four-row viewport."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PyQt5.QtWidgets import QApplication, QPushButton

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "port22"))
from app import Window
from conflicts import ConflictRules, Paused, should_replace
from dialogs import QueueDialog
from store import Store
from telemetry import FileLanes
from transfer import Batch, Endpoint, copy_file


class ControlsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["port22"])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)

    def job(self, entries, rule="ask"):
        self.store.add("demo", "Upload", "/src", "/dst", entries, rule)
        return self.store.jobs()[0]

    def test_conditions_and_sftp_timestamp_precision(self):
        for size, mtime, expected in [
            (12, 50, (False, False, False)),
            (12, 49, (False, True, True)),
            (12, 51, (False, False, True)),
            (10, 50, (True, False, True)),
        ]:
            src = SimpleNamespace(st_size=12, st_mtime=50.95)
            dst = SimpleNamespace(st_size=size, st_mtime=mtime)
            self.assertEqual(
                tuple(
                    should_replace(rule, src, dst)
                    for rule in ("size", "newer", "different")
                ),
                expected,
            )

    def test_serialized_decisions_and_lifetimes(self):
        st = self.root.stat()
        for scope in ("once", "transfer", "session"):
            calls = []

            def ask(*args):
                calls.append(args[1])
                time.sleep(0.005)
                return "skip", scope

            rules = ConflictRules(ask)
            batch = Batch(self.store, None, self.job([("a", "file", 0)]), 8, rules)
            with ThreadPoolExecutor(8) as pool:
                self.assertFalse(
                    any(pool.map(lambda i: batch.decide(str(i), st, st), range(8)))
                )
            self.assertEqual(len(calls), 8 if scope == "once" else 1)
            stored = self.store.jobs()[0]
            self.assertEqual(stored["policy"], "skip" if scope == "transfer" else "ask")
            second = Batch(self.store, None, self.job([("b", "file", 0)]), 4, rules)
            second.decide("b", st, st)
            self.assertEqual(
                len(calls), {"once": 9, "transfer": 2, "session": 1}[scope]
            )
            # Reconnecting gets a fresh rules object, even for the same profile.
            second.conflicts = ConflictRules(lambda *_: ("replace", "once"))
            self.assertEqual(second.decide("c", st, st), scope != "transfer")

    def test_pause_on_dismiss(self):
        batch = Batch(
            self.store,
            None,
            self.job([("a", "file", 0)]),
            4,
            ConflictRules(lambda *_: None),
        )
        with self.assertRaises(Paused):
            batch.decide("a", self.root.stat(), self.root.stat())
        self.assertTrue(batch.stop.is_set())

    def test_late_conflict_never_silently_replaces(self):
        src = self.root / "source"
        src.write_bytes(b"new")
        dest = self.root / "dest"
        calls = []

        def ask(*args):
            calls.append(True)
            return False

        def race(temp, path, replace):
            self.assertFalse(replace)
            dest.write_bytes(b"appeared")
            raise FileExistsError(path)

        end = Endpoint()
        with patch.object(end, "commit", side_effect=race):
            state = copy_file(
                end,
                end,
                str(src),
                str(dest),
                dict(id=3, rel="test"),
                dict(id="batch"),
                threading.Event(),
                lambda *_: None,
                ask,
                src.stat(),
                None,
            )
        self.assertEqual(
            (state, dest.read_bytes(), calls), ("skipped", b"appeared", [True])
        )
        self.assertFalse(list(self.root.glob("*.part")))

    def test_remove_scanning_tree_and_active_progress(self):
        job = self.job([("a%_", "folder", 0), ("a%_2", "folder", 0)])
        key = job["id"]
        folder = self.store.take(key)
        self.store.take(key)
        self.store.discovered(
            key, "a%_", [("a%_/one", "file", 7), ("a%_/two", "file", 9)]
        )
        self.store.discovered(key, "a%_2", [("a%_2/keep", "file", 4)])
        active = self.store.take(key)
        sample = {active["id"]: ("one", 3, 7)}
        self.assertEqual(self.store.progress(key, sample)["transferred"], 3)
        self.assertEqual(self.store.remove_entries(key, [active["id"]]), (0, 0))
        self.assertEqual(self.store.remove_entries(key, [folder["id"]]), (1, 1))
        self.assertFalse(
            self.store.discovered(key, "a%_/sub", [("a%_/sub/late", "file", 10)])
        )
        self.store.finish(active["id"], "done")
        counts = self.store.progress(key, sample)
        self.assertEqual(
            (counts["files"], counts["size"], counts["transferred"], counts["copied"]),
            (2, 11, 7, 1),
        )
        self.assertEqual(self.store.take(key)["rel"], "a%_2/keep")
        # A working directory returned on pause must not re-enter the queue.
        self.store.finish(folder["id"], "ready")
        self.assertIsNone(self.store.take(key))

    def test_dismiss_one_completed_transfer(self):
        completed = []
        for name in ("archive", "website"):
            (self.root / name).write_text(name)
            job = self.store.add(
                "demo", "Download", "/src", str(self.root), [(name, "file", 7)], "skip"
            )
            self.store.status(job, "Complete")
            completed.append(job)
        paused = self.job([("later", "file", 1)])["id"]
        self.store.status(paused, "Paused")
        window = Window(self.store)
        self.addCleanup(window.close)
        window.show_page(2)
        window.monitor_job = completed[1]
        window.queue_monitor.show()
        window.lanes.show()
        card = window.queue_widgets[completed[0]].parentWidget()
        dismiss = next(b for b in card.findChildren(QPushButton) if b.text() == "×")
        self.assertIn("archive", dismiss.accessibleName())
        dismiss.click()
        self.assertEqual({j["id"] for j in self.store.jobs()}, {paused, completed[1]})
        self.assertFalse(window.queue_monitor.isHidden())
        window.clear_history(paused)
        window.clear_done.click()
        self.assertEqual([j["id"] for j in self.store.jobs()], [paused])
        self.assertTrue(window.queue_monitor.isHidden())
        self.assertTrue(window.lanes.isHidden())
        for name in ("archive", "website"):
            self.assertEqual((self.root / name).read_text(), name)

    def test_queue_editor_and_history_scroll(self):
        job = self.job([(f"part{i}", "file", i) for i in range(180)])
        dialog = QueueDialog(None, self.store, job)
        dialog.next.click()
        self.assertEqual(dialog.table.rowCount(), 30)
        dialog.search.setText("part179")
        dialog.table.selectRow(0)
        dialog.remove.click()
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertEqual(self.store.progress(job["id"], {})["files"], 179)
        dialog.reject()

        lanes = FileLanes()
        history = {}
        for i in range(8):
            history[i] = (str(i), i, 10, "working")
            lanes.set_files(history, 10, False)
            self.assertEqual(lanes.height(), 138)
        self.assertEqual(lanes.scroll.value(), 4)
        lanes.scroll.setValue(1)
        history[8] = ("later", 1, 4, "working")
        lanes.set_files(history, 11, False)
        self.assertEqual(lanes.scroll.value(), 1)
        self.assertEqual(lanes.keys[-1], 8)
        lanes.set_files({}, 12, False)
        self.assertEqual(lanes.height(), 138)
        lanes.close()


if __name__ == "__main__":
    unittest.main()
