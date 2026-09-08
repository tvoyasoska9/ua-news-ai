import asyncio
import logging
from collections import deque

from app.collector import collect_news, fingerprint
from app.dedup import is_similar
from app.editor import NewsEditor

log = logging.getLogger(__name__)


class NewsPipeline:
    def __init__(self, settings, db, bot):
        self.settings = settings
        self.db = db
        self.bot = bot
        self.editor = NewsEditor(settings.openai_api_key, settings.openai_model)

        # Keep titles from previous runs too, not only the current process.
        self.recent_titles = self.db.get_recent_titles(limit=1000)
        self.queue = deque()
        self.queue_keys = set()

    async def run_once(self):
        items = await collect_news()
        log.info("Collected %s candidates", len(items))
        items.sort(key=lambda x: x.priority, reverse=True)

        for raw in items:
            key = fingerprint(raw)

            if self.db.exists(raw.url, key):
                continue

            # Prevent the same story from another source from being proposed again.
            if is_similar(raw, self.recent_titles, threshold=85):
                self.db.add(raw.url, key, raw.title, raw.source, "duplicate")
                continue

            self.db.add(raw.url, key, raw.title, raw.source, "processing")
            self.recent_titles.append(raw.title)
            self.recent_titles = self.recent_titles[-1000:]

            try:
                edited = await self.editor.edit(raw)
            except Exception:
                log.exception("AI editing failed")
                self.db.set_status(raw.url, "error")
                continue

            if edited.confidence == "low":
                self.db.set_status(raw.url, "low_confidence")
                continue

            if edited.importance < self.settings.min_importance_to_send:
                self.db.set_status(raw.url, "low_priority")
                continue

            # A second duplicate check after AI processing catches near-identical items
            # discovered in the same collection cycle.
            if raw.url in self.queue_keys:
                self.db.set_status(raw.url, "duplicate")
                continue

            self.queue.append((edited, raw.url, raw.image_url, raw.published_at, raw.source))
            self.queue_keys.add(raw.url)
            self.db.set_status(raw.url, "queued")

        log.info("Moderation queue size: %s", len(self.queue))

    async def moderation_worker(self):
        while True:
            if not self.queue:
                await asyncio.sleep(2)
                continue

            edited, url, image_url, published_at, source = self.queue.popleft()
            self.queue_keys.discard(url)

            try:
                await self.bot.send_for_moderation(
                    edited,
                    url,
                    image_url,
                    published_at,
                    source,
                )
                self.db.set_status(url, "moderation")
                log.info(
                    "Sent one news item to moderation. Waiting %s seconds before next item.",
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
