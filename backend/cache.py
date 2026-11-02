"""Bounded, disposable content cache, separate from authoritative metadata."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing


def digest(*parts):
    h = hashlib.sha256()
    for part in parts:
        value = part if isinstance(part, bytes) else str(part).encode()
        h.update(len(value).to_bytes(8, "big"))
        h.update(value)
    return h.hexdigest()


class ContentCache:
    def __init__(self, root, max_mb=512):
        self.path = root / "cache.sqlite3"
        self.budget = max_mb * 1024 * 1024
        with closing(sqlite3.connect(self.path, timeout=10)) as c, c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS entries(kind TEXT,key TEXT,value BLOB,created REAL,PRIMARY KEY(kind,key))")
            c.execute("CREATE INDEX IF NOT EXISTS cache_age ON entries(created)")

    def get(self, kind, key):
        with closing(sqlite3.connect(self.path, timeout=10)) as c:
            row = c.execute("SELECT value FROM entries WHERE kind=? AND key=?", (kind, key)).fetchone()
            return row[0] if row else None

    def get_many(self, kind, keys):
        result = {}
        with closing(sqlite3.connect(self.path, timeout=10)) as c:
            for start in range(0,len(keys),500):
                batch = keys[start:start+500]
                rows = c.execute("SELECT key,value FROM entries WHERE kind=? AND key IN ("+",".join("?" for _ in batch)+")",(kind,*batch))
                result.update(rows)
        return result

    def put_many(self, kind, pairs):
        with closing(sqlite3.connect(self.path, timeout=10)) as c, c:
            c.executemany("INSERT OR REPLACE INTO entries VALUES(?,?,?,?)", [(kind, k, v, time.time()) for k, v in pairs])

    def prune(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as c, c:
            size = c.execute("SELECT coalesce(sum(length(value)),0) FROM entries").fetchone()[0]
            while size > self.budget:
                rows = c.execute("SELECT kind,key,length(value) FROM entries ORDER BY created LIMIT 512").fetchall()
                if not rows:
                    break
                c.executemany("DELETE FROM entries WHERE kind=? AND key=?", [(r[0], r[1]) for r in rows])
                size -= sum(r[2] for r in rows)
        # Logical payload is bounded; SQLite can reuse freed pages. No blocking VACUUM.

    def get_json(self, kind, key):
        value = self.get(kind, key)
        return json.loads(value) if value else None

    def put_json(self, kind, key, value):
        self.put_many(kind, [(key, json.dumps(value, ensure_ascii=False).encode())])
