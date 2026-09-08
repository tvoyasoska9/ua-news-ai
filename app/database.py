import os
import sqlite3
from datetime import datetime, timezone


class Database:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS news ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "url TEXT UNIQUE,"
            "fingerprint TEXT UNIQUE,"
            "title TEXT NOT NULL,"
            "event_key TEXT,"
            "source TEXT,"
            "status TEXT NOT NULL,"
            "created_at TEXT NOT NULL)"
        )

        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(news)").fetchall()
        }
        if "event_key" not in columns:
            self.conn.execute("ALTER TABLE news ADD COLUMN event_key TEXT")

        self.conn.commit()

    def close(self):
        self.conn.close()

    def exists(self, url, fingerprint):
        return self.conn.execute(
            "SELECT 1 FROM news WHERE url=? OR fingerprint=? LIMIT 1",
            (url, fingerprint)
        ).fetchone() is not None

    def add(self, url, fingerprint, title, source, status="seen"):
        self.conn.execute(
            "INSERT OR IGNORE INTO news "
            "(url,fingerprint,title,event_key,source,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                url,
                fingerprint,
                title,
                "",
                source,
                status,
                datetime.now(timezone.utc).isoformat(),
            )
        )
        self.conn.commit()

    def set_event_key(self, url, event_key):
        self.conn.execute(
            "UPDATE news SET event_key=? WHERE url=?",
            (event_key, url),
        )
        self.conn.commit()

    def set_status(self, url, status):
        self.conn.execute("UPDATE news SET status=? WHERE url=?", (status, url))
        self.conn.commit()

    def get_recent_titles(self, limit=1000):
        rows = self.conn.execute(
            "SELECT title FROM news ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [row[0] for row in rows if row and row[0]]

    def get_recent_event_keys(self, limit=1000):
        rows = self.conn.execute(
            "SELECT event_key FROM news "
            "WHERE event_key IS NOT NULL AND event_key != '' "
            "ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [row[0] for row in rows if row and row[0]]
