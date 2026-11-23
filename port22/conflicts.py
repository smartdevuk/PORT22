"""Resolve concurrent name conflicts, with one decision dialog per connection."""

import threading


POLICIES = {
    "skip": "Skip",
    "replace": "Overwrite",
    "size": "Overwrite if sizes differ",
    "newer": "Overwrite if source is newer",
    "different": "Overwrite if size or modification time differs",
}


def should_replace(rule, source, target):
    if rule == "replace":
        return True
    if rule == "skip":
        return False
    if rule == "size":
        return source.st_size != target.st_size
    if rule == "newer":
        return int(source.st_mtime) > int(target.st_mtime)
    if rule == "different":
        return source.st_size != target.st_size or int(source.st_mtime) != int(
            target.st_mtime
        )
    raise ValueError("Unknown conflict rule.")


class Paused(Exception):
    pass


def check(stop):
    if stop.is_set():
        raise Paused()


class ConflictRules:
    def __init__(self, ask):
        self.ask = ask
        self.lock = threading.Lock()
        self.session_rule = None

    def resolve(self, batch, path, source, target):
        # Only workers acquire this lock. A remembered rule is visible to every waiter.
        with self.lock:
            check(batch.stop)
            job = batch.job
            rule = job["policy"]
            if rule == "ask":
                rule = self.session_rule
            if not rule:
                choice = self.ask(batch, path, source, target)
                if choice is None:
                    batch.pause()
                    raise Paused()
                check(batch.stop)
                rule, scope = choice
                if rule not in POLICIES:
                    raise ValueError("Unknown conflict rule.")
                if scope == "transfer":
                    with batch.store.write() as db:
                        db.execute(
                            "UPDATE jobs SET policy=? WHERE id=?", (rule, job["id"])
                        )
                    job["policy"] = rule
                elif scope == "session":
                    self.session_rule = rule
            return should_replace(rule, source, target)
