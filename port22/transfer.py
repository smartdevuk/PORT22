"""Stream a disk-backed directory walk into a bounded pool of SFTP copies."""

from collections import OrderedDict
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
from conflicts import Paused, check, should_replace


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


def copy_file(src, dst, source, destination, item, job, stop, progress, decide):
    before = src.stat(source)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Source changed type while queued.")
    temp = posixpath.join(
        posixpath.dirname(destination), f".violet-{job['id']}-{item['id']}.part"
    )
    if not dst.sftp and os.path.isfile(temp) and has_reservation(destination, temp):
        os.unlink(destination)

    def approved(existing):
        if not decide(item["rel"], before, existing):
            return False
        if not stat.S_ISREG(existing.st_mode):
            raise ValueError("Only regular files can be overwritten.")
        return True

    existing = dst.existing(destination)
    if existing and not approved(existing):
        return "skipped"
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
        check(stop)
        # A file may appear or change while the staged copy is in progress.
        target = dst.existing(destination)

        def identity(info):
            return (info.st_mode, info.st_size, info.st_mtime) if info else None

        if target and identity(target) != identity(existing) and not approved(target):
            return "skipped"

        def apply_metadata(target):
            mode = (
                stat.S_IMODE(target.st_mode) & 0o777
                if target
                else stat.S_IMODE(before.st_mode) & 0o755
            )
            times = (
                (int(before.st_mtime), int(before.st_mtime))
                if dst.sftp
                else (before.st_mtime, before.st_mtime)
            )
            if dst.sftp:
                dst.sftp.chmod(temp, mode)
                dst.sftp.utime(temp, times)
            else:
                for operation, args in (
                    (os.chmod, (temp, mode)),
                    (os.utime, (temp, times)),
                ):
                    try:
                        operation(*args)
                    except OSError as error:
                        if error.errno not in (
                            errno.EOPNOTSUPP,
                            errno.EPERM,
                            errno.EACCES,
                        ):
                            raise

        apply_metadata(target)
        check(stop)
        try:
            dst.commit(temp, destination, bool(target))
        except OSError:
            if target:
                raise
            target = dst.existing(destination)
            if not target:
                raise
            if not approved(target):
                return "skipped"
            apply_metadata(target)
            check(stop)
            dst.commit(temp, destination, True)
        committed = True
        return "done"
    finally:
        if not committed:
            with contextlib.suppress(Exception):
                dst.remove(temp)


class NoSpareChannel(Exception):
    """The server declined an extra worker. Its item goes back to the shared queue."""


class Batch:
    scan_limit = 5

    def __init__(self, store, session, job, limit, conflicts=None):
        self.store, self.session, self.job, self.limit = store, session, job, limit
        self.stop = threading.Event()
        self.lock = threading.Lock()
        # Cap channels per batch so browsing has headroom on the usual 10-session server.
        self.worker_count = min(9, self.limit + self.scan_limit)
        self.schedule = threading.Condition()
        self.scanning = self.copying = 0
        self.active = {}
        self.recent = OrderedDict()
        self.conflicts = conflicts
        self.transferred = 0
        self.finished_at = None
        self.outcome = None
        self.local = threading.local()
        self.channel_lock = threading.Lock()
        self.idle_channels = []
        self.channel_count = 0
        self.channel_limit = self.worker_count
        self.thread = threading.Thread(
            target=self.run, daemon=True, name="queue-controller"
        )

    def snapshot(self):
        with self.lock:
            return dict(self.active), self.transferred

    def activity(self):
        with self.lock:
            return self.recent.copy()

    def decide(self, path, source, target):
        if self.conflicts:
            return self.conflicts.resolve(self, path, source, target)
        return should_replace(self.job["policy"], source, target)

    def channel(self):
        with self.channel_lock:
            while self.idle_channels:
                channel = self.idle_channels.pop()
                if not channel.get_channel().closed:
                    break
                self.channel_count -= 1
            else:
                if self.channel_count >= self.channel_limit:
                    raise NoSpareChannel()
                try:
                    channel = self.session.channel()
                except paramiko.ChannelException:
                    if self.channel_count:
                        self.channel_limit = self.channel_count
                        raise NoSpareChannel()
                    raise
                self.channel_count += 1
            self.local.sftp = channel
            return channel

    def release_channel(self, *, discard=False):
        channel = getattr(self.local, "sftp", None)
        if channel is None:
            return
        if discard:
            with contextlib.suppress(Exception):
                channel.close()
        with self.channel_lock:
            if discard or channel.get_channel().closed:
                self.channel_count -= 1
            else:
                self.idle_channels.append(channel)
        self.local.sftp = None

    def discover(self, source, srcpath, rel):
        rows = []
        seen_folder = seen_file = False
        last_flush = time.monotonic()

        def flush():
            accepted = self.store.discovered(self.job["id"], rel, rows)
            if accepted and rows:
                with self.schedule:
                    self.schedule.notify_all()
            rows.clear()
            return accepted

        for name, typ, size, _ in source.list(srcpath):
            check(self.stop)
            rows.append((posixpath.join(rel, name), typ, size))
            first = (typ == "folder" and not seen_folder) or (
                typ == "file" and not seen_file
            )
            seen_folder |= typ == "folder"
            seen_file |= typ == "file"
            now = time.monotonic()
            if first or len(rows) >= 128 or now - last_flush >= 0.1:
                if not flush():
                    # An interrupted remote directory iterator can leave unread replies.
                    if source.sftp:
                        self.release_channel(discard=True)
                    return "removed"
                last_flush = now
        return "done" if flush() else "removed"

    def execute(self, item):
        check(self.stop)
        if item["kind"] in ("link", "special"):
            return "skipped"
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
        if item["kind"] == "folder":
            info = source.stat(srcpath)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("The selected folder changed type.")
            target = dest.existing(dstpath)
            if target is None:
                (dest.sftp.mkdir if dest.sftp else os.mkdir)(dstpath)
            elif not stat.S_ISDIR(target.st_mode):
                if not self.decide(rel, info, target):
                    return "skipped"
                raise ValueError("A folder cannot replace a file or symbolic link.")
            return self.discover(source, srcpath, rel)
        previous = 0

        def progress(offset, size):
            nonlocal previous
            if size != item["size"]:
                with self.store.write() as db:
                    db.execute(
                        "UPDATE entries SET size=? WHERE id=?", (size, item["id"])
                    )
                item["size"] = size
            with self.lock:
                self.recent[item["id"]] = (rel, offset, size, "working")
                self.active[item["id"]] = (rel, offset, size)
                self.transferred += offset - previous
            previous = offset

        return copy_file(
            source,
            dest,
            srcpath,
            dstpath,
            item,
            self.job,
            self.stop,
            progress,
            self.decide,
        )

    def work(self, item):
        state = "stopped"
        if item["kind"] == "file":
            with self.lock:
                self.recent.pop(item["id"], None)
                if len(self.recent) >= 256:
                    oldest = next(
                        (k for k, row in self.recent.items() if row[3] != "working"),
                        None,
                    )
                    if oldest is not None:
                        del self.recent[oldest]
                self.recent[item["id"]] = (item["rel"], 0, item["size"], "working")
        try:
            state = self.execute(item)
            self.store.finish(item["id"], state)
        except Paused:
            state = "paused"
            self.store.finish(item["id"], "ready")
        except NoSpareChannel:
            state = "waiting"
            self.store.finish(item["id"], "ready")
            raise
        except Exception as error:
            state = "failed"
            self.release_channel(discard=True)
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
                state = "paused"
                self.store.finish(item["id"], "ready")
            elif retry and item["tries"] < 2:
                state = "retry"
                self.stop.wait(2 * (item["tries"] + 1))
                self.store.finish(item["id"], "ready", describe(error), 1)
            elif network:
                self.stop.set()
                self.store.finish(item["id"], "failed", describe(error))
            else:
                self.store.finish(item["id"], "failed", describe(error))
        finally:
            self.release_channel()
            with self.lock:
                self.active.pop(item["id"], None)
                if item["id"] in self.recent:
                    name, offset, size, _ = self.recent[item["id"]]
                    self.recent[item["id"]] = (name, offset, size, state)

    def worker(self):
        job = self.job["id"]
        while True:
            with self.schedule:
                while not self.stop.is_set():
                    if self.copying < self.limit:
                        item = self.store.take(job, folders=False)
                        if item:
                            self.copying += 1
                            break
                    scan_slots = min(self.scan_limit, max(1, self.worker_count - 1))
                    if self.scanning < scan_slots:
                        item = self.store.take(job, folders=True)
                        if item:
                            self.scanning += 1
                            break
                    if not (self.copying or self.scanning):
                        return
                    self.schedule.wait()
                else:
                    return
            try:
                self.work(item)
            except NoSpareChannel:
                # Keep a copy slot available when a server accepts fewer channels.
                with self.schedule:
                    self.worker_count -= 1
                return
            finally:
                with self.schedule:
                    if item["kind"] == "folder":
                        self.scanning -= 1
                    else:
                        self.copying -= 1
                    self.schedule.notify_all()

    def run(self):
        job = self.job["id"]
        self.store.status(job, "Running")
        problem = False
        try:
            workers = self.worker_count
            with futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="sftp"
            ) as pool:
                tasks = [pool.submit(self.worker) for _ in range(workers)]
                for task in futures.as_completed(tasks):
                    task.result()
        except Exception as error:
            problem = True
            with self.store.write() as db:
                db.execute(
                    "UPDATE entries SET state='failed',error=? WHERE job=? AND state IN ('working','ready')",
                    (describe(error), job),
                )
        finally:
            for channel in self.idle_channels:
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
        with self.schedule:
            self.schedule.notify_all()
