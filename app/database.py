import os
import sqlite3
from datetime import datetime, timedelta, timezone


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
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS source_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "url TEXT NOT NULL,"
            "source TEXT NOT NULL,"
            "stage TEXT NOT NULL,"
            "created_at TEXT NOT NULL,"
            "UNIQUE(url, stage))"
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

    def get_source(self, url):
        row = self.conn.execute(
            "SELECT source FROM news WHERE url=? LIMIT 1", (url,)
        ).fetchone()
        return row[0] if row and row[0] else "Невідомо"

    def record_metric(self, url, source, stage):
        if not url or not source:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO source_metrics "
            "(url,source,stage,created_at) VALUES (?,?,?,?)",
            (url, source, stage, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_source_stats(self, hours=24):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            """
            SELECT
                source,
                SUM(CASE WHEN stage='found' THEN 1 ELSE 0 END) AS found,
                SUM(CASE WHEN stage='passed_ai' THEN 1 ELSE 0 END) AS passed_ai,
                SUM(CASE WHEN stage='moderation' THEN 1 ELSE 0 END) AS moderation,
                SUM(CASE WHEN stage='published' THEN 1 ELSE 0 END) AS published
            FROM source_metrics
            WHERE created_at >= ?
            GROUP BY source
            ORDER BY found DESC, moderation DESC, published DESC, source COLLATE NOCASE
            """,
            (since,),
        ).fetchall()
        return [
            {
                "source": row[0],
                "found": int(row[1] or 0),
                "passed_ai": int(row[2] or 0),
                "moderation": int(row[3] or 0),
                "published": int(row[4] or 0),
            }
            for row in rows
        ]

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
