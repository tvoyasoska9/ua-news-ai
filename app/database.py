import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone


class Database:
    def __init__(self, path="data/news.db"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS news ("
            "url TEXT PRIMARY KEY,"
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
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_news ("
            "item_id TEXT PRIMARY KEY,"
            "payload TEXT NOT NULL,"
            "created_at TEXT NOT NULL)"
        )
        self.conn.commit()

    def get_status(self, url):
        row = self.conn.execute(
            "SELECT status FROM news WHERE url=? LIMIT 1", (url,)
        ).fetchone()
        return row[0] if row else None

    def set_status(self, url, status):
        self.conn.execute(
            "INSERT OR REPLACE INTO news (url,status,created_at) VALUES (?,?,?)",
            (url, status, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def seen(self, url):
        return self.get_status(url) is not None

    def record_metric(self, url, source, stage):
        self.conn.execute(
            "INSERT OR IGNORE INTO source_metrics (url,source,stage,created_at) VALUES (?,?,?,?)",
            (url, source or "Невідомо", stage, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_source_stats(self, hours=24):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT source, stage, COUNT(*) FROM source_metrics "
            "WHERE created_at >= ? GROUP BY source, stage",
            (since,),
        ).fetchall()
        result = {}
        for source, stage, count in rows:
            result.setdefault(source, {
                "source": source,
                "found": 0,
                "passed_ai": 0,
                "moderation": 0,
                "published": 0,
            })
            if stage in result[source]:
                result[source][stage] = count
        return list(result.values())

    def save_pending(self, item_id, payload):
        self.conn.execute(
            "INSERT OR REPLACE INTO pending_news (item_id,payload,created_at) VALUES (?,?,?)",
            (
                item_id,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def get_pending(self, item_id):
        row = self.conn.execute(
            "SELECT payload FROM pending_news WHERE item_id=? LIMIT 1",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def delete_pending(self, item_id):
        self.conn.execute("DELETE FROM pending_news WHERE item_id=?", (item_id,))
        self.conn.commit()

    def cleanup_pending(self, hours=168):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        self.conn.execute(
            "DELETE FROM pending_news WHERE created_at < ?",
            (since,),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
