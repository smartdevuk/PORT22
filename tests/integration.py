"""Run against an isolated loopback OpenSSH server created with throwaway keys."""

import getpass
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "port22"))
from connection import Session, load_key
from store import Store
from transfer import Batch


def command(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def wait(predicate, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError("Operation timed out")


class Server:
    def __init__(self, root):
        self.root = root
        self.user = getpass.getuser()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.client = root / "client"
        self.host = root / "host"
        command(
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-f",
            str(self.client),
            "-N",
            "test password",
        )
        command("ssh-keygen", "-q", "-t", "ed25519", "-f", str(self.host), "-N", "")
        self.config = root / "sshd.conf"
        self.config.write_text(f"""Port {self.port}
ListenAddress 127.0.0.1
HostKey {self.host}
PidFile {root}/sshd.pid
AuthorizedKeysFile {self.client}.pub
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
Subsystem sftp internal-sftp
""")

    def start(self):
        self.proc = subprocess.Popen(
            ["/usr/sbin/sshd", "-D", "-e", "-f", str(self.config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def available():
            if self.proc.poll() is not None:
                raise RuntimeError("sshd could not start")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return True
            except OSError:
                return False

        wait(available, 3)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(root):
    srv = Server(root)
    srv.start()
    session = None
    try:
        local = root / "local"
        remote = root / "remote"
        restore = root / "restore"
        for d in [local, remote, restore]:
            d.mkdir()
        store = Store(root / "app")
        p = dict(id="test", host="127.0.0.1", port=srv.port, user=srv.user)
        key = load_key(srv.client, lambda: "test password")
        session = Session(p, key, store.path)
        try:
            session.connect(lambda *_: False)
        except ValueError:
            pass
        else:
            raise AssertionError("Unapproved first-use key was accepted")
        session.connect(lambda *_: True)
        server_home = session.channel()
        assert server_home.normalize(".").startswith("/")
        server_home.close()
        tree = local / "Many files"
        tree.mkdir()
        (tree / "empty").mkdir()
        names = [
            "one [2]*?.txt",
            'quotes "and" spaces.txt',
            "café.txt",
            ".htaccess",
            "new\nline.txt",
        ]
        for i in range(int(os.environ.get("VIOLET_TEST_FILES", "300"))):
            d = tree / f"dir {i % 16}"
            d.mkdir(exist_ok=True)
            (d / (names[i % len(names)] + str(i))).write_bytes(os.urandom(200 + i))
        for i in range(8):
            (tree / f"large-{i}").write_bytes(os.urandom(2 * 1024 * 1024 + 777))
        (tree / "emptyfile").touch()
        (tree / "a-link").symlink_to("empty")
        (tree / ".env").write_bytes(b"hidden")

        def job(direction, src, dst, entries, policy="skip"):
            id_ = store.add("test", direction, str(src), str(dst), entries, policy)
            return next(j for j in store.jobs() if j["id"] == id_)

        def start(j):
            b = Batch(store, session, j, 4)
            b.thread.start()
            return b

        def finish(b):
            peak = 0
            while b.thread.is_alive():
                peak = max(peak, len(b.snapshot()[0]))
                b.thread.join(0.015)
            j = next(j for j in store.jobs() if j["id"] == b.job["id"])
            assert j["status"] == "Complete", (j, store.errors(j["id"]))
            return peak

        j = job("Upload", local, remote, [("Many files", "folder", 0)])
        peak = finish(start(j))
        assert peak >= 2, peak
        items = [p for p in tree.rglob("*") if p.is_file()]
        assert all(digest(p) == digest(remote / p.relative_to(local)) for p in items)
        assert (remote / "Many files/empty").is_dir() and not (
            remote / "Many files/a-link"
        ).exists()
        print(
            f"Recursive upload: {len(items)} hashes match; {peak} concurrent files",
            flush=True,
        )
        j = job("Download", remote, restore, [("Many files", "folder", 0)])
        b = start(j)
        wait(lambda: bool(b.snapshot()[0]))
        b.pause()
        b.thread.join(20)
        assert not b.thread.is_alive()
        store.db.close()
        store = Store(root / "app")
        assert next(x for x in store.jobs() if x["id"] == j["id"])["status"] == "Paused"
        store.retry(j["id"])
        peak = finish(start(j))
        assert all(digest(p) == digest(restore / p.relative_to(local)) for p in items)
        assert (restore / "Many files/empty").is_dir()
        print(
            f"Pause, database reopen and resume: {len(items)} download hashes match; {peak} concurrent files",
            flush=True,
        )
        retained = remote / "preserve.txt"
        retained.write_bytes(b"original")
        (local / retained.name).write_bytes(b"replacement")
        finish(start(job("Upload", local, remote, [(retained.name, "file", 11)])))
        assert retained.read_bytes() == b"original"
        finish(
            start(
                job("Upload", local, remote, [(retained.name, "file", 11)], "replace")
            )
        )
        assert retained.read_bytes() == b"replacement"
        (restore / retained.name).write_bytes(b"old")
        finish(
            start(
                job(
                    "Download",
                    remote,
                    restore,
                    [(retained.name, "file", 11)],
                    "replace",
                )
            )
        )
        assert (restore / retained.name).read_bytes() == b"replacement"
        print("Both replacement directions and skip-existing passed", flush=True)
        # Drop the SSH transport during a live batch. The queue must create a fresh authenticated transport.
        recover = root / "recovery"
        recover.mkdir()
        j = job(
            "Download",
            remote,
            recover,
            [(f"large-{i}", "file", 2 * 1024 * 1024 + 777) for i in range(8)],
        )
        with store.write() as db:
            db.execute(
                "UPDATE jobs SET src=? WHERE id=?",
                (str(remote / "Many files"), j["id"]),
            )
        j["src"] = str(remote / "Many files")
        b = start(j)
        wait(lambda: len(b.snapshot()[0]) >= 2)
        session.client.get_transport().close()
        finish(b)
        assert all(
            digest(recover / f"large-{i}") == digest(tree / f"large-{i}")
            for i in range(8)
        )
        assert not list(root.rglob(".violet-*.part"))
        print(
            "Connection interruption: automatically reconnected, all 8 hashes match",
            flush=True,
        )
        session.close()
        srv.stop()
        srv.host.unlink()
        srv.host.with_suffix(".pub").unlink()
        command("ssh-keygen", "-q", "-t", "ed25519", "-f", str(srv.host), "-N", "")
        srv.start()
        import paramiko

        session = Session(p, key, store.path)
        try:
            session.connect(lambda *_: True)
        except paramiko.BadHostKeyException:
            pass
        else:
            raise AssertionError("Changed host key was trusted")
        print("Changed server key blocked", flush=True)
    finally:
        if session:
            session.close()
        srv.stop()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="violet-test-") as directory:
        run(Path(directory))
