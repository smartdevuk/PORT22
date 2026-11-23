"""Real SFTP regressions for streaming discovery and restrictive server limits."""

import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from integration import Batch, Server, Session, Store, load_key, wait
from transfer import Endpoint


class StreamingTest(unittest.TestCase):
    def server(self, limit):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        server = Server(root)
        with server.config.open("a") as config:
            config.write(f"MaxSessions {limit}\n")
        server.start()
        self.addCleanup(server.stop)
        store = Store(root / "state")
        self.addCleanup(store.db.close)
        session = Session(
            dict(id="demo", host="127.0.0.1", port=server.port, user=server.user),
            load_key(server.client, lambda: "test password"),
            store.path,
        )
        session.connect(lambda *_: True)
        self.addCleanup(session.close)
        return root, store, session

    def start(self, store, session, direction, src, dest, limit):
        store.add(
            "demo", direction, str(src), str(dest), [("tree", "folder", 0)], "skip"
        )
        batch = Batch(store, session, store.jobs()[0], limit)
        batch.thread.start()
        self.addCleanup(batch.thread.join, 10)
        self.addCleanup(batch.pause)
        return batch

    def completed(self, store, batch):
        batch.thread.join(20)
        self.assertEqual(batch.outcome, "Complete", store.errors(batch.job["id"]))

    def test_copy_while_first_listing_is_still_open(self):
        root, store, session = self.server(2)
        # The listing cannot deliver its second name until the first file lands.
        # This covers both the scanner's first partial page and eager worker wakeup.
        for direction in ("Upload", "Download"):
            with self.subTest(direction=direction):
                src, dest = root / (direction + "-src"), root / (direction + "-dest")
                (src / "tree").mkdir(parents=True)
                dest.mkdir()
                for name in ("first", "second", "third"):
                    (src / "tree" / name).write_bytes(name.encode())
                listing = Endpoint.list
                observed = threading.Event()

                def gated(endpoint, path):
                    for index, row in enumerate(listing(endpoint, path)):
                        yield row
                        if index == 0:
                            wait(lambda: (dest / "tree" / row[0]).is_file(), 8)
                            observed.set()

                with patch.object(Endpoint, "list", gated):
                    batch = self.start(store, session, direction, src, dest, 1)
                    self.completed(store, batch)
                self.assertTrue(observed.is_set())
                for file in (src / "tree").iterdir():
                    self.assertEqual(
                        file.read_bytes(), (dest / "tree" / file.name).read_bytes()
                    )

    def test_server_with_one_sftp_channel_drains_both_queues(self):
        root, store, session = self.server(1)
        src, dest = root / "source", root / "destination"
        dest.mkdir()
        for i in range(18):
            folder = src / "tree" / str(i) / "leaf"
            folder.mkdir(parents=True)
            (folder / "payload").write_bytes(str(i).encode() * 1024)
        batch = self.start(store, session, "Download", src, dest, 8)
        self.completed(store, batch)
        self.assertEqual(batch.worker_count, 1)

        def hashes(base):
            return {
                p.relative_to(base): hashlib.sha256(p.read_bytes()).digest()
                for p in base.rglob("payload")
            }

        self.assertEqual(hashes(src), hashes(dest))


if __name__ == "__main__":
    unittest.main()
