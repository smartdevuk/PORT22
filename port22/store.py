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
            db.execute(
                "UPDATE entries SET state=?,error=?,tries=tries+? WHERE id=?",
                (state, message, bump, entry),
            )

    def states(self, job):
        return self.rows(
            "SELECT state,kind,count(*) AS n FROM entries WHERE job=? GROUP BY state,kind",
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
