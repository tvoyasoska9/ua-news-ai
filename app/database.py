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
            "source TEXT,"
            "status TEXT NOT NULL,"
            "created_at TEXT NOT NULL)"
        )
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
            "INSERT OR IGNORE INTO news (url,fingerprint,title,source,status,created_at) VALUES (?,?,?,?,?,?)",
            (url, fingerprint, title, source, status, datetime.now(timezone.utc).isoformat())
        )
        self.conn.commit()

    def set_status(self, url, status):
        self.conn.execute("UPDATE news SET status=? WHERE url=?", (status, url))
        self.conn.commit()
