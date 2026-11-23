"""A bounded worker pool and an interruptible, disk-backed directory walk."""

import concurrent.futures as futures
import contextlib
import ctypes
import errno
import os
import posixpath
import socket
import stat
import threading
import time

import paramiko
from connection import describe


class Paused(Exception):
    pass


def check(stop):
    if stop.is_set():
        raise Paused()


def safe_name(name):
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError("A directory contains an invalid filename.")
    return name


def kind(mode):
    if stat.S_ISDIR(mode):
        return "folder"
    if stat.S_ISREG(mode):
        return "file"
    return "link" if stat.S_ISLNK(mode) else "special"


def marker(temp):
    return ("Violet Relay commit " + posixpath.basename(temp) + "\n").encode("ascii")


def has_reservation(dest, temp):
    data = marker(temp)
    try:
        info = os.lstat(dest)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
            return False
        fd = os.open(dest, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            return stream.read(len(data) + 1) == data
    except FileNotFoundError:
        return False


def install_exclusive(temp, dest):
    """Publish a complete file on ext4 and on more limited ChromeOS shared mounts."""
    func = ctypes.CDLL(None, use_errno=True).renameat2
    if func(-100, os.fsencode(temp), -100, os.fsencode(dest), 1) == 0:
        return
    code = ctypes.get_errno()
    if code not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
        raise OSError(code, os.strerror(code), dest)
    try:
        os.link(temp, dest)
    except OSError as error:
        if error.errno not in (
            errno.EPERM,
            errno.EOPNOTSUPP,
            errno.ENOSYS,
            errno.EACCES,
        ):
            raise
    else:
        os.unlink(temp)
        return
    # Network/shared mounts may implement neither flagged renames nor hard links.
    # Reserve the name exclusively before publishing the already complete temporary file.
    with open(dest, "xb", buffering=0) as reservation:
        reservation.write(marker(temp))
    try:
        os.replace(temp, dest)
    except OSError:
        if has_reservation(dest, temp):
            os.unlink(dest)
        raise


class Endpoint:
    def __init__(self, sftp=None):
        self.sftp = sftp

    def stat(self, path):
        return self.sftp.lstat(path) if self.sftp else os.lstat(path)

    def existing(self, path):
        try:
            return self.stat(path)
        except FileNotFoundError:
            return None

    def folder(self, path):
        attr = self.existing(path)
        if attr is None:
            if self.sftp:
                self.sftp.mkdir(path)
            else:
                os.mkdir(path)
        elif not stat.S_ISDIR(attr.st_mode):
            raise ValueError(f"Destination is not a regular directory: {path}")

    def list(self, path):
        if self.sftp:
            for entry in self.sftp.listdir_iter(path, read_aheads=4):
                yield (
                    safe_name(entry.filename),
                    kind(entry.st_mode),
                    entry.st_size or 0,
                    entry.st_mtime or 0,
                )
        else:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    yield (
                        safe_name(entry.name),
                        kind(info.st_mode),
                        info.st_size,
                        info.st_mtime,
                    )

    def parents(self, root, rel):
        # Never traverse a link discovered inside a selected tree, on either side.
        path = root
        if not stat.S_ISDIR(self.stat(root).st_mode):
            raise ValueError(f"Base folder is no longer a directory: {root}")
        for name in rel.split("/")[:-1]:
            path = posixpath.join(path, safe_name(name))
            if not stat.S_ISDIR(self.stat(path).st_mode):
                raise ValueError(f"Directory changed or became a link: {path}")

    def open(self, path, mode):
        return self.sftp.open(path, mode) if self.sftp else open(path, mode)

    def remove(self, path):
        (self.sftp.remove if self.sftp else os.unlink)(path)

    def commit(self, temp, dest, replace):
        if self.sftp:
            (self.sftp.posix_rename if replace else self.sftp.rename)(temp, dest)
        elif replace:
            os.replace(temp, dest)
        else:
            install_exclusive(temp, dest)


def copy_file(src, dst, source, destination, item, job, stop, progress):
    before = src.stat(source)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Source changed type while queued.")
    temp = posixpath.join(
        posixpath.dirname(destination), f".violet-{job['id']}-{item['id']}.part"
    )
    if not dst.sftp and os.path.isfile(temp) and has_reservation(destination, temp):
        os.unlink(destination)
    existing = dst.existing(destination)
    if existing and job["policy"] == "skip":
        return "skipped"
    if existing and not stat.S_ISREG(existing.st_mode):
        raise ValueError(
            "A directory, link, or special file already has the destination name."
        )
    # The random batch ID plus persistent row ID identifies our interrupted staging file.
    if dst.existing(temp):
        dst.remove(temp)
    size = before.st_size
    progress(0, size)
    committed = False
    try:
        with (
            src.open(source, "rb") as reader,
            dst.open(temp, "wx" if dst.sftp else "xb") as writer,
        ):
            if dst.sftp:
                dst.sftp.chmod(temp, 0o600)
                writer.set_pipelined(True)
            offset = 0
            while offset < size:
                check(stop)
                stop_at = min(size, offset + 1024 * 1024)
                if src.sftp and stop_at - offset > 32768:
                    spans = [
                        (n, min(32768, stop_at - n))
                        for n in range(offset, stop_at, 32768)
                    ]
                    blocks = reader.readv(spans)
                else:
                    blocks = (reader.read(stop_at - offset),)
                for block in blocks:
                    if not block:
                        if src.sftp and src.stat(source).st_size == size:
                            # Paramiko can report EOF when an outstanding read loses its channel.
                            raise EOFError(
                                "Remote read interrupted before the expected end of file."
                            )
                        raise IOError("Source ended early.")
                    if dst.sftp:
                        # A synchronous final write drains acknowledgements for the entire window.
                        cut = max(0, len(block) - 32768)
                        if cut:
                            writer.write(block[:cut])
                        writer.set_pipelined(False)
                        writer.write(block[cut:])
                        writer.set_pipelined(True)
                    else:
                        writer.write(block)
                    offset += len(block)
                if offset != stop_at:
                    raise IOError("Source length changed during the transfer.")
                progress(offset, size)
            if not dst.sftp:
                writer.flush()
                try:
                    os.fsync(writer.fileno())
                except OSError as error:
                    if error.errno not in (errno.EINVAL, errno.EOPNOTSUPP):
                        raise
        after = src.stat(source)
        if (after.st_size, after.st_mtime) != (size, before.st_mtime):
            raise IOError(
                "Source changed during the transfer. Retry when it has finished changing."
            )
        if dst.stat(temp).st_size != size:
            raise IOError("Destination size check failed.")
        mode = (
            stat.S_IMODE(existing.st_mode) & 0o777
            if existing
            else (stat.S_IMODE(before.st_mode) & 0o755)
        )
        if dst.sftp:
            dst.sftp.chmod(temp, mode)
            dst.sftp.utime(temp, (int(before.st_mtime), int(before.st_mtime)))
        else:
            for operation, args in (
                (os.chmod, (temp, mode)),
                (os.utime, (temp, (before.st_mtime, before.st_mtime))),
            ):
                try:
                    operation(*args)
                except OSError as error:
                    if error.errno not in (errno.EOPNOTSUPP, errno.EPERM, errno.EACCES):
                        raise
        check(stop)
        try:
            dst.commit(temp, destination, job["policy"] == "replace")
        except OSError:
            if job["policy"] == "skip" and dst.existing(destination):
                return "skipped"
            raise
        committed = True
        return "done"
    finally:
        if not committed:
            with contextlib.suppress(Exception):
                dst.remove(temp)


class Batch:
    def __init__(self, store, session, job, limit):
        self.store, self.session, self.job, self.limit = store, session, job, limit
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.active = {}
        self.transferred = 0
        self.finished_at = None
        self.outcome = None
        self.local = threading.local()
        self.channels = []
        self.thread = threading.Thread(
            target=self.run, daemon=True, name="queue-controller"
        )

    def snapshot(self):
        with self.lock:
            return dict(self.active), self.transferred

    def channel(self):
        channel = getattr(self.local, "sftp", None)
        if channel is None or channel.get_channel().closed:
            channel = self.session.channel()
            self.local.sftp = channel
            with self.lock:
                self.channels.append(channel)
        return channel

    def execute(self, item):
        check(self.stop)
        sftp = self.channel()
        source, dest = Endpoint(), Endpoint(sftp)
        if self.job["direction"] == "Download":
            source, dest = dest, source
        rel = item["rel"]
        for name in rel.split("/"):
            safe_name(name)
        source.parents(self.job["src"], rel)
        dest.parents(self.job["dst"], rel)
        srcpath = posixpath.join(self.job["src"], rel)
        dstpath = posixpath.join(self.job["dst"], rel)
        if item["kind"] in ("link", "special"):
            return "skipped"
        if item["kind"] == "folder":
            if not stat.S_ISDIR(source.stat(srcpath).st_mode):
                raise ValueError("The selected folder changed type.")
            dest.folder(dstpath)
            rows = []
            for name, typ, size, _ in source.list(srcpath):
                check(self.stop)
                rows.append((posixpath.join(rel, name), typ, size))
                if len(rows) == 128:
                    with self.store.write():
                        self.store.children(self.job["id"], rows)
                    rows.clear()
            with self.store.write():
                self.store.children(self.job["id"], rows)
            return "done"
        previous = 0

        def progress(offset, size):
            nonlocal previous
            with self.lock:
                self.active[item["id"]] = (rel, offset, size)
                self.transferred += offset - previous
            previous = offset

        return copy_file(
            source, dest, srcpath, dstpath, item, self.job, self.stop, progress
        )

    def work(self, item):
        try:
            state = self.execute(item)
            self.store.finish(item["id"], state)
        except Paused:
            self.store.finish(item["id"], "ready")
        except Exception as error:
            channel = getattr(self.local, "sftp", None)
            if channel:
                with contextlib.suppress(Exception):
                    channel.close()
                with self.lock:
                    if channel in self.channels:
                        self.channels.remove(channel)
                self.local.sftp = None
            network = (
                isinstance(
                    error,
                    (
                        EOFError,
                        ConnectionError,
                        socket.timeout,
                        socket.gaierror,
                        paramiko.SSHException,
                        paramiko.ssh_exception.NoValidConnectionsError,
                    ),
                )
                or getattr(error, "errno", None)
                in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE)
                or str(error).lower()
                in ("socket is closed", "server connection dropped", "channel closed.")
            )
            retry = network and not isinstance(
                error, (paramiko.BadHostKeyException, paramiko.AuthenticationException)
            )
            if self.stop.is_set():
                self.store.finish(item["id"], "ready")
            elif retry and item["tries"] < 2:
                self.stop.wait(2 * (item["tries"] + 1))
                self.store.finish(item["id"], "ready", describe(error), 1)
            elif network:
                self.stop.set()
                self.store.finish(item["id"], "failed", describe(error))
            else:
                self.store.finish(item["id"], "failed", describe(error))
        finally:
            with self.lock:
                self.active.pop(item["id"], None)

    def run(self):
        job = self.job["id"]
        self.store.status(job, "Running")
        problem = False
        try:
            with futures.ThreadPoolExecutor(
                max_workers=self.limit, thread_name_prefix="sftp"
            ) as pool:
                pending = set()
                while True:
                    while not self.stop.is_set() and len(pending) < self.limit:
                        item = self.store.take(job)
                        if not item:
                            break
                        pending.add(pool.submit(self.work, item))
                    if not pending:
                        break
                    done, pending = futures.wait(
                        pending, timeout=0.12, return_when=futures.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
        except Exception as error:
            problem = True
            with self.store.write() as db:
                db.execute(
                    "UPDATE entries SET state='failed',error=? WHERE job=? AND state IN ('working','ready')",
                    (describe(error), job),
                )
        finally:
            for channel in self.channels:
                with contextlib.suppress(Exception):
                    channel.close()
            states = {r["state"] for r in self.store.states(job)}
            status = (
                "Paused"
                if "ready" in states
                else "Needs attention"
                if problem or "failed" in states
                else "Complete"
            )
            self.store.status(job, status)
            self.outcome = status
            self.finished_at = time.monotonic()

    def pause(self):
        self.stop.set()
