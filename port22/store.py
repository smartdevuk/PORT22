"""Persist profiles, transfer batches and discovered entries in SQLite."""

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


class Store:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            directory / "relay.db", check_same_thread=False, timeout=10
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, profile TEXT NOT NULL, direction TEXT NOT NULL, src TEXT NOT NULL,
          dst TEXT NOT NULL, title TEXT NOT NULL, policy TEXT NOT NULL, status TEXT NOT NULL,
          stamp REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS entries (
          id INTEGER PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs ON DELETE CASCADE, rel TEXT NOT NULL,
          kind TEXT NOT NULL, size INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
          tries INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', UNIQUE(job,rel));
        CREATE INDEX IF NOT EXISTS schedule ON entries(job,state,id);
        CREATE TABLE IF NOT EXISTS excluded_folders (
          job TEXT NOT NULL REFERENCES jobs ON DELETE CASCADE, rel TEXT NOT NULL, PRIMARY KEY(job,rel));
        CREATE INDEX IF NOT EXISTS batch_totals ON entries(job,state,kind,size);
        UPDATE entries SET state='removed' WHERE state='working' AND EXISTS (
          SELECT 1 FROM excluded_folders x WHERE x.job=entries.job AND
          (entries.rel=x.rel OR (entries.rel>=x.rel||'/' AND entries.rel<x.rel||'0')));
        UPDATE entries SET state='ready' WHERE state='working';
        UPDATE jobs SET status='Paused' WHERE status IN ('Queued','Running');
        """)
        self.db.commit()

    @contextlib.contextmanager
    def write(self):
        with self.lock, self.db:
            yield self.db

    def rows(self, query, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(query, args)]

    def objects(self, table):
        assert table in ("profiles", "settings")
        return {
            r["id"]: json.loads(r["body"]) for r in self.rows("SELECT * FROM " + table)
        }

    def save(self, table, key, value):
        assert table in ("profiles", "settings")
        with self.write() as db:
            db.execute(
                "INSERT OR REPLACE INTO " + table + " VALUES (?,?)",
                (key, json.dumps(value)),
            )

    def jobs(self):
        return self.rows("SELECT * FROM jobs ORDER BY stamp DESC")

    def add(self, profile, direction, src, dst, entries, policy):
        job = uuid.uuid4().hex
        title = (
            entries[0][0]
            if len(entries) == 1
            else f"{entries[0][0]} + {len(entries) - 1} more"
        )
        with self.write() as db:
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job,
                    profile,
                    direction,
                    src,
                    dst,
                    title,
                    policy,
                    "Queued",
                    time.time(),
                ),
            )
            self.children(job, entries)
        return job

    def children(self, job, entries):
        # Caller owns the write transaction.
        self.db.executemany(
            "INSERT OR IGNORE INTO entries(job,rel,kind,size) VALUES (?,?,?,?)",
            ((job, rel, kind, size) for rel, kind, size in entries),
        )

    def excluded(self, db, job, rel):
        parts = rel.split("/")
        ancestors = ("/".join(parts[:n]) for n in range(1, len(parts) + 1))
        return any(
            db.execute(
                "SELECT 1 FROM excluded_folders WHERE job=? AND rel=?", (job, path)
            ).fetchone()
            for path in ancestors
        )

    def discovered(self, job, folder, entries):
        # Discovery and folder removal share a transaction boundary, including on resume.
        with self.write() as db:
            if self.excluded(db, job, folder):
                return False
            self.children(job, entries)
            return True

    def queued_entries(self, job, term, limit, offset):
        return self.rows(
            """
            SELECT id,rel,kind,state FROM entries e WHERE job=?
              AND instr(lower(rel),lower(?))>0
              AND (state IN ('ready','failed') OR (kind='folder'
                   AND NOT EXISTS (SELECT 1 FROM excluded_folders x WHERE x.job=e.job AND x.rel=e.rel)
                   AND (state='working' OR (state='done' AND EXISTS (
                        SELECT 1 FROM entries d WHERE d.job=e.job AND d.rel>=e.rel||'/' AND d.rel<e.rel||'0'
                        AND d.state IN ('ready','failed','working'))))))
            ORDER BY id LIMIT ? OFFSET ?
        """,
            (job, term, limit, offset),
        )

    def remove_entries(self, job, ids):
        removed = 0
        folders = 0
        with self.write() as db:
            for entry in ids:
                row = db.execute(
                    "SELECT kind,rel,state FROM entries WHERE job=? AND id=?",
                    (job, entry),
                ).fetchone()
                if not row:
                    continue
                if row["kind"] == "folder":
                    rel = row["rel"]
                    folders += db.execute(
                        "INSERT OR IGNORE INTO excluded_folders VALUES (?,?)",
                        (job, rel),
                    ).rowcount
                    removed += db.execute(
                        """
                        UPDATE entries SET state='removed',error='' WHERE job=? AND rel>=? AND rel<?
                        AND state IN ('ready','failed')
                    """,
                        (job, rel + "/", rel + "0"),
                    ).rowcount
                removed += db.execute(
                    "UPDATE entries SET state='removed',error='' WHERE id=? AND state IN ('ready','failed')",
                    (entry,),
                ).rowcount
            if (
                removed
                and not db.execute(
                    "SELECT 1 FROM entries WHERE job=? AND state IN ('working','ready','failed')",
                    (job,),
                ).fetchone()
            ):
                db.execute(
                    "UPDATE jobs SET status='Complete' WHERE id=? AND status!='Running'",
                    (job,),
                )
        return removed, folders

    def progress(self, job, active):
        with self.lock:
            states = self.states(job)
            partial = 0
            for entry, (_, offset, _) in active.items():
                row = self.db.execute(
                    "SELECT size FROM entries WHERE job=? AND id=? AND state='working'",
                    (job, entry),
                ).fetchone()
                if row:
                    partial += min(offset, row["size"])
        files = [
            r
            for r in states
            if r["kind"] == "file" and r["state"] not in ("removed", "skipped")
        ]
        copied = [r for r in files if r["state"] == "done"]
        return dict(
            files=sum(r["n"] for r in files),
            copied=sum(r["n"] for r in copied),
            size=sum(r["bytes"] for r in files),
            transferred=sum(r["bytes"] for r in copied) + partial,
            skipped=sum(r["n"] for r in states if r["state"] == "skipped"),
            failed=sum(r["n"] for r in states if r["state"] == "failed"),
            discovering=any(
                r["kind"] == "folder" and r["state"] in ("working", "ready")
                for r in states
            ),
        )

    def take(self, job):
        with self.write() as db:
            row = db.execute(
                "SELECT * FROM entries WHERE job=? AND state='ready' ORDER BY id LIMIT 1",
                (job,),
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE entries SET state='working' WHERE id=?", (row["id"],)
                )
                return dict(row)

    def finish(self, entry, state, message="", bump=0):
        with self.write() as db:
            if state in ("ready", "failed"):
                row = db.execute(
                    "SELECT job,rel FROM entries WHERE id=?", (entry,)
                ).fetchone()
                if row and self.excluded(db, row["job"], row["rel"]):
                    state, message = "removed", ""
            db.execute(
                "UPDATE entries SET state=?,error=?,tries=tries+? WHERE id=?",
                (state, message, bump, entry),
            )

    def states(self, job):
        return self.rows(
            "SELECT state,kind,count(*) AS n,sum(size) AS bytes FROM entries WHERE job=? GROUP BY state,kind",
            (job,),
        )

    def status(self, job, state):
        with self.write() as db:
            db.execute("UPDATE jobs SET status=? WHERE id=?", (state, job))

    def retry(self, job):
        with self.write() as db:
            db.execute(
                "UPDATE entries SET state='ready',tries=0,error='' WHERE job=? AND state='failed'",
                (job,),
            )
            db.execute("UPDATE jobs SET status='Queued' WHERE id=?", (job,))

    def errors(self, job):
        return self.rows(
            "SELECT rel,error FROM entries WHERE job=? AND state='failed' ORDER BY id LIMIT 50",
            (job,),
        )


def data_dir():
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    # Keep existing installations on their original lock, trust store and queue.
    legacy = base / "violet-relay"
    return legacy if (legacy / "relay.db").is_file() else base / "port22"
