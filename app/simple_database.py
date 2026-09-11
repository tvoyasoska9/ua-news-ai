import json
import os
import sqlite3
from datetime import datetime, timezone

class SimpleDatabase:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
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
        self.conn.commit()

    def seen(self, url):
        return self.conn.execute("SELECT 1 FROM simple_seen WHERE url=?", (url,)).fetchone() is not None

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
        self.conn.commit()

    def set_status(self, url, status):
        self.conn.execute("UPDATE simple_seen SET status=? WHERE url=?", (status, url))
        self.conn.commit()

    def recent_texts(self, limit=5000):
        return [
            x[0] for x in self.conn.execute(
                "SELECT source_text FROM simple_seen ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def save_pending(self, item_id, title, text, url, media):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO simple_pending(item_id,title,text,url,media,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (item_id, title, text, url, json.dumps(media), datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_pending(self, item_id):
        row = self.conn.execute(
            "SELECT title,text,url,media FROM simple_pending WHERE item_id=?", (item_id,)
        ).fetchone()
        return None if not row else {
            "title": row[0],
            "text": row[1],
            "url": row[2],
            "media": json.loads(row[3]),
        }

    def delete_pending(self, item_id):
        self.conn.execute("DELETE FROM simple_pending WHERE item_id=?", (item_id,))
        self.conn.commit()

    def close(self):
        self.conn.close()
