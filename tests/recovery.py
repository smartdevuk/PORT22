"""Regression checks for a hard process stop and ChromeOS shared-mount fallbacks."""

import ctypes
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from integration import Batch, Server, Session, Store, digest, load_key, wait
from transfer import install_exclusive

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    srv = Server(root)
    srv.start()
    try:
        local = root / "local"
        remote = root / "remote"
        local.mkdir()
        remote.mkdir()
        big = remote / "large.bin"
        big.write_bytes(os.urandom(32 * 1024 * 1024))
        (local / big.name).write_text("keep original")
        store = Store(root / "app")
        j = store.add(
            "test",
            "Download",
            str(remote),
            str(local),
            [(big.name, "file", big.stat().st_size)],
            "replace",
        )
        store.db.close()
        childcode = """
import sys
from pathlib import Path
from store import Store
from connection import Session,load_key
from transfer import Batch
root=Path(sys.argv[1]);store=Store(root/'app')
s=Session(dict(host='127.0.0.1',port=int(sys.argv[2]),user=sys.argv[3]),load_key(root/'client',lambda:'test password'),store.path)
s.connect(lambda *_:True)
job=store.jobs()[0];store.retry(job['id']);b=Batch(store,s,job,4);b.thread.start();b.thread.join();s.close()
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        child = subprocess.Popen(
            [sys.executable, "-c", childcode, str(root), str(srv.port), srv.user],
            env=env,
        )
        wait(lambda: any(p.stat().st_size >= 32768 for p in local.glob("*.part")))
        child.kill()
        child.wait()
        assert (local / big.name).read_text() == "keep original"
        store = Store(root / "app")
        job = store.jobs()[0]
        assert job["status"] == "Paused"
        s = Session(
            dict(host="127.0.0.1", port=srv.port, user=srv.user),
            load_key(srv.client, lambda: "test password"),
            store.path,
        )
        s.connect()
        store.retry(j)
        b = Batch(store, s, job, 4)
        b.thread.start()
        b.thread.join(60)
        assert store.jobs()[0]["status"] == "Complete" and digest(big) == digest(
            local / big.name
        )
        assert not list(local.glob("*.part"))
        print(
            "Abrupt process kill during replacement: previous file intact; persisted queue restored; 32 MiB SHA-256 matches"
        )
        s.close()
        temp = root / ".violet-test.part"
        temp.write_bytes(b"hello")
        target = root / "committed"
        with (
            patch("transfer.ctypes.CDLL") as libc,
            patch(
                "transfer.os.link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")
            ),
        ):
            libc.return_value.renameat2.return_value = -1
            ctypes.set_errno(errno.EINVAL)
            install_exclusive(str(temp), str(target))
            assert target.read_bytes() == b"hello"
            temp.write_bytes(b"new")
            try:
                install_exclusive(str(temp), str(target))
            except FileExistsError:
                pass
            else:
                raise AssertionError("clobbered")
        print(
            "Shared-mount fallback: exclusive commit & existing-file protection passed"
        )
    finally:
        srv.stop()
