"""Rate accounting at UI timer boundaries and compatibility with existing profiles."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "port22"))
from telemetry import Throughput
from store import Store, data_dir


class ThroughputTests(unittest.TestCase):
    def test_bytes_and_elapsed_time(self):
        series = Throughput()
        series.reset(100)
        series.sample(100.1, 100, {1: ("file", 100, 1000)})
        series.sample(100.5, 512, {})
        self.assertEqual(series.rate, 1024)
        series.sample(101, 512, {})
        self.assertEqual(series.rate, 0)
        series.sample(101.25, 640, {}, True)
        self.assertFalse(series.running)
        self.assertEqual(series.total, 640)
        self.assertEqual(series.peak, 1024)
        self.assertEqual(series.average, 512)
        series.reset(102)
        self.assertEqual((series.total, series.peak, len(series.samples)), (0, 0, 0))

    def test_upgrade_uses_original_queue_and_identity_store(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.dict(os.environ, {"XDG_DATA_HOME": d}),
        ):
            self.assertEqual(data_dir(), Path(d) / "port22")
            old = Store(Path(d) / "violet-relay")
            old.save("profiles", "web", {"name": "Web"})
            old.add("web", "Download", "/srv", "/tmp", [("site", "folder", 0)], "skip")
            old.db.close()
            upgraded = Store(data_dir())
            self.assertEqual(upgraded.objects("profiles")["web"]["name"], "Web")
            self.assertEqual(upgraded.jobs()[0]["status"], "Paused")
            upgraded.db.close()


if __name__ == "__main__":
    unittest.main()
