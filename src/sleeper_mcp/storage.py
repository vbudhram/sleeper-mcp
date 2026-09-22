import hashlib
import json
import sqlite3
import time
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, directory: str):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path / "manager.sqlite", timeout=10, check_same_thread=False)
        (path / "manager.sqlite").chmod(0o600)
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS response_history (
            key TEXT NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(key, hash));
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY, league TEXT NOT NULL, payload TEXT NOT NULL,
            hash TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
            league TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
            collected REAL NOT NULL, PRIMARY KEY(league, id));
        CREATE TABLE IF NOT EXISTS chat_posts (
            request_id TEXT PRIMARY KEY, league TEXT NOT NULL, text TEXT NOT NULL,
            created REAL NOT NULL, status TEXT NOT NULL, message_id TEXT);
        CREATE TABLE IF NOT EXISTS leases (key TEXT PRIMARY KEY, expires REAL NOT NULL);
        """)

    def cached(self, key):
        row = self.db.execute("SELECT payload, fetched FROM cache WHERE key=?", (key,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def cache(self, key, payload, fetched_at=None):
        with self.db:
            self.db.execute(
                "INSERT INTO response_history VALUES(?,?,?,?,?) "
                "ON CONFLICT(key,hash) DO UPDATE SET last_seen=excluded.last_seen",
                (key, digest(payload), json.dumps(payload), time.time(), time.time()),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                (key, json.dumps(payload), time.time() if fetched_at is None else fetched_at),
            )

    def acquire(self, key, seconds=90):
        with self.db:
            self.db.execute("DELETE FROM leases WHERE expires < ?", (time.time(),))
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO leases VALUES(?,?)", (key, time.time() + seconds)
            )
            return cursor.rowcount == 1

    def release(self, key):
        with self.db:
            self.db.execute("DELETE FROM leases WHERE key=?", (key,))

    def snapshot(self, league, payload):
        previous = self.db.execute(
            "SELECT id, payload, hash FROM snapshots WHERE league=? ORDER BY id DESC LIMIT 1",
            (league,),
        ).fetchone()
        content_hash = digest(payload)
        if previous and previous[2] == content_hash:
            return {
                "snapshot_id": previous[0],
                "previous_snapshot_id": previous[0],
                "changed": False,
                "changed_sections": [],
            }
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO snapshots(league,payload,hash,created) VALUES(?,?,?,?)",
                (league, json.dumps(payload), content_hash, time.time()),
            )
        old = json.loads(previous[1]) if previous else {}
        return {
            "snapshot_id": cursor.lastrowid,
            "previous_snapshot_id": previous[0] if previous else None,
            "changed": previous is None or previous[2] != content_hash,
            "changed_sections": [k for k, v in payload.items() if old.get(k) != v],
        }

    def save_messages(self, league, messages):
        with self.db:
            for message in messages:
                mid = str(message.get("message_id") or "local:" + digest(message))
                self.db.execute(
                    "INSERT OR REPLACE INTO messages VALUES(?,?,?,?)",
                    (league, mid, json.dumps(message), time.time()),
                )

    def search_messages(self, league, query, limit):
        rows = self.db.execute(
            "SELECT payload FROM messages WHERE league=? ORDER BY collected DESC",
            (league,),
        ).fetchall()
        return [
            json.loads(x[0])
            for x in rows
            if query.casefold() in str(json.loads(x[0]).get("text", "")).casefold()
        ][:limit]

    def cache_info(self):
        return {
            "responses": self.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0],
            "response_versions": self.db.execute(
                "SELECT COUNT(*) FROM response_history"
            ).fetchone()[0],
            "messages": self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "snapshots": self.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
            "oldest_fetch": self.db.execute("SELECT MIN(fetched) FROM cache").fetchone()[0],
            "latest_fetch": self.db.execute("SELECT MAX(fetched) FROM cache").fetchone()[0],
        }

    def changes(self, league, snapshot_id):
        rows = self.db.execute(
            "SELECT id,payload FROM snapshots WHERE league=? AND id>=? ORDER BY id",
            (league, snapshot_id),
        ).fetchall()
        if not rows or rows[0][0] != snapshot_id:
            raise ValueError("Snapshot does not belong to this league or does not exist")
        first, last = json.loads(rows[0][1]), json.loads(rows[-1][1])
        return {
            "from_snapshot_id": snapshot_id,
            "to_snapshot_id": rows[-1][0],
            "changes": {
                k: {"before": first.get(k), "after": last.get(k)}
                for k in first.keys() | last.keys()
                if first.get(k) != last.get(k)
            },
        }

    def prune(self, days=30):
        cutoff = time.time() - days * 86400
        with self.db:
            self.db.execute("DELETE FROM messages WHERE collected<?", (cutoff,))
            self.db.execute("DELETE FROM snapshots WHERE created<?", (cutoff,))
