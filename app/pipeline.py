import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from app.collector import collect_news, fingerprint
from app.dedup import is_similar_title, is_duplicate_event
from app.editor import NewsEditor

log = logging.getLogger(__name__)

# Real-time moderation: posts older than this are historical backlog, not
# breaking/current news. They are not sent just because the queue was slow.
MAX_QUEUE_AGE_MINUTES = 75
MAX_QUEUE_SIZE = 24


def _published_timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _is_too_old(value):
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return published < datetime.now(timezone.utc) - timedelta(minutes=MAX_QUEUE_AGE_MINUTES)
    except Exception:
        return False


def _source_tier(item):
    # Configured Telegram channels are PRIMARY. Web/RSS remains SECONDARY.
    return 0 if item.source.startswith("Telegram:") else 1


def _queue_sort_key(item):
    # First source tier, then freshness. Telegram posts are primary; inside
    # each tier the newest item is always sent first.
    edited, url, image_url, published_at, source, media_type, media_path = item
    tier = 0 if str(source).startswith("Telegram:") else 1
    return (tier, -_published_timestamp(published_at))


def _cleanup_media(path):
    if not path:
        return
    try:
        from pathlib import Path
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


class NewsPipeline:
    def __init__(self, settings, db, bot):
        self.settings = settings
        self.db = db
        self.bot = bot
        self.editor = NewsEditor(settings.openai_api_key, settings.openai_model)

        self.recent_titles = self.db.get_recent_titles(limit=1000)
        self.recent_events = self.db.get_recent_event_keys(limit=1000)

        self.queue = deque()
        self.queue_keys = set()
        self.queue_events = []

    def _resort_and_trim_queue(self):
        ordered = sorted(self.queue, key=_queue_sort_key)

        kept = []
        for item in ordered:
            if _is_too_old(item[3]):
                self.db.set_status(item[1], "stale")
                continue
            kept.append(item)

        dropped = kept[MAX_QUEUE_SIZE:]
        kept = kept[:MAX_QUEUE_SIZE]

        for item in dropped:
            self.db.set_status(item[1], "queue_overflow")
            media_path = item[6]
            if media_path:
                try:
                    from pathlib import Path
                    Path(media_path).unlink(missing_ok=True)
                except Exception:
                    pass

        self.queue = deque(kept)
        self.queue_keys = {item[1] for item in kept}
        self.queue_events = [
            (item[0].event_key or item[0].title)
            for item in kept
        ]

    async def run_once(self):
        items = await collect_news(self.settings)

        telegram_count = sum(1 for item in items if _source_tier(item) == 0)
        rss_count = len(items) - telegram_count
        log.info(
            "Collected %s candidates: %s Telegram PRIMARY, %s web/RSS SECONDARY",
            len(items), telegram_count, rss_count,
        )

        # Preserve collector's strict round-robin order for Telegram channels.
        telegram_items = [item for item in items if _source_tier(item) == 0]
        secondary_items = [item for item in items if _source_tier(item) == 1]
        secondary_items.sort(key=lambda item: item.priority, reverse=True)
        items = telegram_items + secondary_items

        for raw in items:
            key = fingerprint(raw)

            if self.db.exists(raw.url, key):
                _cleanup_media(raw.media_path)
                continue

            # Do not spend AI calls on historical backlog.
            if _is_too_old(raw.published_at):
                self.db.add(raw.url, key, raw.title, raw.source, "stale")
                _cleanup_media(raw.media_path)
                continue

            if is_similar_title(raw, self.recent_titles, threshold=90):
                self.db.add(raw.url, key, raw.title, raw.source, "duplicate")
                _cleanup_media(raw.media_path)
                continue

            self.db.add(raw.url, key, raw.title, raw.source, "processing")

            try:
                edited = await self.editor.edit(raw)
            except Exception:
                log.exception("AI editing failed")
                self.db.set_status(raw.url, "error")
                _cleanup_media(raw.media_path)
                continue

            if edited.confidence == "low":
                log.info("Low-confidence candidate kept for human moderation: %s", raw.title)

            if edited.importance < self.settings.min_importance_to_send:
                self.db.set_status(raw.url, "low_priority")
                _cleanup_media(raw.media_path)
                continue

            event_key = edited.event_key or edited.title

            all_recent_events = self.recent_events + self.queue_events
            if is_duplicate_event(event_key, all_recent_events, threshold=86):
                self.db.set_event_key(raw.url, event_key)
                self.db.set_status(raw.url, "duplicate")
                _cleanup_media(raw.media_path)
                log.info("Semantic duplicate skipped: %s", raw.title)
                continue

            self.db.set_event_key(raw.url, event_key)

            self.recent_titles.append(raw.title)
            self.recent_titles = self.recent_titles[-1000:]

            self.recent_events.append(event_key)
            self.recent_events = self.recent_events[-1000:]

            self.queue.append(
                (
                    edited,
                    raw.url,
                    raw.image_url,
                    raw.published_at,
                    raw.source,
                    raw.media_type,
                    raw.media_path,
                )
            )
            self.db.set_status(raw.url, "queued")
            self._resort_and_trim_queue()

        log.info("Moderation queue size: %s", len(self.queue))

    async def moderation_worker(self):
        while True:
            if not self.queue:
                await asyncio.sleep(1)
                continue

            edited, url, image_url, published_at, source, media_type, media_path = self.queue.popleft()
            self.queue_keys.discard(url)

            if _is_too_old(published_at):
                self.db.set_status(url, "stale")
                log.info("Skipped stale queued news (%s): %s", published_at, edited.title[:80])
                continue

            event_key = edited.event_key or edited.title
            try:
                self.queue_events.remove(event_key)
            except ValueError:
                pass

            try:
                await self.bot.send_for_moderation(
                    edited,
                    url,
                    image_url,
                    published_at,
                    source,
                    media_type,
                    media_path,
                )
                self.db.set_status(url, "moderation")
                log.info(
                    "Sent fresh news to moderation. Waiting %s seconds before next item.",
                    self.settings.moderation_interval_seconds,
                )
                await asyncio.sleep(self.settings.moderation_interval_seconds)
            except Exception:
                log.exception("Failed to send queued news for moderation")
                self.db.set_status(url, "error")
                await asyncio.sleep(5)

    async def run_forever(self):
        worker = asyncio.create_task(self.moderation_worker())
        try:
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Pipeline iteration failed")

                await asyncio.sleep(self.settings.check_interval_minutes * 60)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
