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
            "fingerprint TEXT,"
            "title TEXT,"
            "source TEXT,"
            "event_key TEXT,"
            "status TEXT NOT NULL,"
            "created_at TEXT NOT NULL)"
        )

        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(news)").fetchall()}
        for name, definition in {
            "fingerprint": "TEXT",
            "title": "TEXT",
            "source": "TEXT",
            "event_key": "TEXT",
        }.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE news ADD COLUMN {name} {definition}")

        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_news_fingerprint ON news(fingerprint)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_news_created_at ON news(created_at)")

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
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_usage ("
            "usage_date TEXT NOT NULL,"
            "usage_key TEXT NOT NULL,"
            "count INTEGER NOT NULL DEFAULT 0,"
            "PRIMARY KEY (usage_date, usage_key))"
        )
        self.conn.commit()

    def daily_count(self, usage_key):
        usage_date = datetime.now(timezone.utc).date().isoformat()
        row = self.conn.execute(
            "SELECT count FROM daily_usage WHERE usage_date=? AND usage_key=?",
            (usage_date, usage_key),
        ).fetchone()
        return int(row[0]) if row else 0

    def try_consume_daily(self, usage_key, limit):
        """Atomically reserve one persistent daily quota slot."""
        usage_date = datetime.now(timezone.utc).date().isoformat()
        limit = max(0, int(limit))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT count FROM daily_usage WHERE usage_date=? AND usage_key=?",
                (usage_date, usage_key),
            ).fetchone()
            current = int(row[0]) if row else 0
            if current >= limit:
                self.conn.rollback()
                return False
            self.conn.execute(
                "INSERT INTO daily_usage (usage_date,usage_key,count) VALUES (?,?,?) "
                "ON CONFLICT(usage_date,usage_key) DO UPDATE SET count=excluded.count",
                (usage_date, usage_key, current + 1),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def release_daily(self, usage_key):
        """Return one reserved quota slot when the protected action failed."""
        usage_date = datetime.now(timezone.utc).date().isoformat()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT count FROM daily_usage WHERE usage_date=? AND usage_key=?",
                (usage_date, usage_key),
            ).fetchone()
            current = int(row[0]) if row else 0
            if current > 0:
                self.conn.execute(
                    "UPDATE daily_usage SET count=? WHERE usage_date=? AND usage_key=?",
                    (current - 1, usage_date, usage_key),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def exists(self, url, fingerprint=None):
        """Return True only when a candidate is terminally handled.

        Transient/API failures and deterministic quality failures are retryable;
        otherwise one bad model response would permanently erase a real news item.
        """
        retryable = {"error_retry", "quality_failed_retry", "quality_retry_pending", "processing"}
        if fingerprint:
            rows = self.conn.execute(
                "SELECT url,status FROM news WHERE url=? OR fingerprint=?",
                (url, fingerprint),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT url,status FROM news WHERE url=?",
                (url,),
            ).fetchall()

        if not rows:
            return False
        return any((status or "") not in retryable for _, status in rows)

    def add(self, url, fingerprint, title, source, status):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO news (url,fingerprint,title,source,status,created_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "fingerprint=excluded.fingerprint, "
            "title=excluded.title, "
            "source=excluded.source, "
            "status=excluded.status",
            (url, fingerprint, title, source, status, now),
        )
        self.conn.commit()

    def get_status(self, url):
        row = self.conn.execute(
            "SELECT status FROM news WHERE url=? LIMIT 1", (url,)
        ).fetchone()
        return row[0] if row else None

    def set_status(self, url, status):
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "UPDATE news SET status=? WHERE url=?",
            (status, url),
        )
        if cur.rowcount == 0:
            self.conn.execute(
                "INSERT INTO news (url,status,created_at) VALUES (?,?,?)",
                (url, status, now),
            )
        self.conn.commit()

    def seen(self, url):
        return self.exists(url)

    def set_event_key(self, url, event_key):
        self.conn.execute(
            "UPDATE news SET event_key=? WHERE url=?",
            (event_key or "", url),
        )
        self.conn.commit()

    def get_recent_titles(self, limit=1000):
        rows = self.conn.execute(
            "SELECT title FROM news "
            "WHERE title IS NOT NULL AND title != '' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row[0] for row in reversed(rows)]

    def get_recent_event_keys(self, limit=1000):
        rows = self.conn.execute(
            "SELECT event_key FROM news "
            "WHERE event_key IS NOT NULL AND event_key != '' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row[0] for row in reversed(rows)]

    def record_metric(self, url, source, stage):
        self.conn.execute(
            "INSERT OR IGNORE INTO source_metrics "
            "(url,source,stage,created_at) VALUES (?,?,?,?)",
            (
                url,
                source or "Невідомо",
                stage,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def get_ai_stats(self, hours=24):
        """Return exact AI attempt and outcome counters for a time window."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT stage, COUNT(*) FROM source_metrics "
            "WHERE created_at >= ? AND stage LIKE 'ai_%' GROUP BY stage",
            (since,),
        ).fetchall()
        result = {
            "ai_started": 0,
            "ai_completed": 0,
            "ai_repair": 0,
            "ai_error": 0,
        }
        for stage, count in rows:
            if stage in result:
                result[stage] = count
        return result

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
            "INSERT OR REPLACE INTO pending_news "
            "(item_id,payload,created_at) VALUES (?,?,?)",
            (
                item_id,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def pending_count(self, max_age_minutes=None):
        """Count moderation cards, optionally only those still fresh enough to affect capacity."""
        if max_age_minutes is None:
            row = self.conn.execute("SELECT COUNT(*) FROM pending_news").fetchone()
        else:
            since = (
                datetime.now(timezone.utc) - timedelta(minutes=max(0, int(max_age_minutes)))
            ).isoformat()
            row = self.conn.execute(
                "SELECT COUNT(*) FROM pending_news WHERE created_at >= ?",
                (since,),
            ).fetchone()
        return int(row[0]) if row else 0

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
        self.conn.execute(
            "DELETE FROM pending_news WHERE item_id=?",
            (item_id,),
        )
        self.conn.commit()

    def cleanup_pending(self, hours=168):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        self.conn.execute(
            "DELETE FROM pending_news WHERE created_at < ?",
            (since,),
        )
        self.conn.commit()

    def cleanup_history(self, days=30):
        days = max(7, min(int(days), 180))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        self.conn.execute("DELETE FROM source_metrics WHERE created_at < ?", (since,))
        self.conn.execute("DELETE FROM news WHERE created_at < ?", (since,))
        # Pending moderation cards are shorter-lived than the general history.
        self.cleanup_pending(hours=min(days * 24, 168))
        self.conn.commit()

    def close(self):
        self.conn.close()
