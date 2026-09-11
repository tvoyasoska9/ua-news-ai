import json
import os
import sqlite3
from datetime import datetime, timezone

class SimpleDatabase:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS simple_seen (
                url TEXT PRIMARY KEY,
                source_text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS simple_pending (
                item_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                url TEXT NOT NULL,
                media TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_simple_seen_created_at ON simple_seen(created_at DESC)")

    def seen(self, url):
        return self.conn.execute("SELECT 1 FROM simple_seen WHERE url=?", (url,)).fetchone() is not None

    def claim(self, url, source_text):
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO simple_seen(url,source_text,status,created_at) VALUES(?,?,?,?)",
            (url, source_text, "processing", now),
        )
        return cur.rowcount == 1

    def mark(self, url, source_text, status):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO simple_seen(url,source_text,status,created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                source_text=excluded.source_text,
                status=excluded.status
            """,
            (url, source_text, status, now),
        )

    def release(self, url):
        self.conn.execute("DELETE FROM simple_seen WHERE url=? AND status='processing'", (url,))

    def set_status(self, url, status):
        self.conn.execute("UPDATE simple_seen SET status=? WHERE url=?", (status, url))

    def recent_texts(self, limit=5000):
        return [x[0] for x in self.conn.execute(
            "SELECT source_text FROM simple_seen ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()]

    def save_pending(self, item_id, title, text, url, media):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO simple_pending(item_id,title,text,url,media,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (item_id, title, text, url, json.dumps(media), datetime.now(timezone.utc).isoformat()),
        )

    def get_pending(self, item_id):
        row = self.conn.execute(
            "SELECT title,text,url,media FROM simple_pending WHERE item_id=?", (item_id,)
        ).fetchone()
        if not row:
            return None
        media = json.loads(row[3])
        # Old pending cards stored only cached Bot API file_ids as a list.
        # New cards also retain the untouched local source files.
        if isinstance(media, list):
            media = {"cached": media, "local": []}
        if not isinstance(media, dict):
            media = {"cached": [], "local": []}
        media.setdefault("cached", [])
        media.setdefault("local", [])
        return {
            "title": row[0], "text": row[1], "url": row[2], "media": media,
        }

    def delete_pending(self, item_id):
        self.conn.execute("DELETE FROM simple_pending WHERE item_id=?", (item_id,))

    def close(self):
        self.conn.close()
