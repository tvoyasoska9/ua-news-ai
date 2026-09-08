import asyncio
import logging

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
        self.recent_titles = []

    async def run_once(self):
        items = await collect_news()
        log.info("Collected %s candidates", len(items))
        items.sort(key=lambda x: x.priority, reverse=True)

        for raw in items:
            key = fingerprint(raw)

            if self.db.exists(raw.url, key):
                continue

            if is_similar(raw, self.recent_titles):
                self.db.add(raw.url, key, raw.title, raw.source, "duplicate")
                continue

            self.db.add(raw.url, key, raw.title, raw.source, "processing")
            self.recent_titles.append(raw.title)
            self.recent_titles = self.recent_titles[-300:]

            try:
                edited = await self.editor.edit(raw)
            except Exception:
                log.exception("AI editing failed")
                self.db.set_status(raw.url, "error")
                continue

            if edited.confidence == "low":
                self.db.set_status(raw.url, "low_confidence")
            elif edited.importance < self.settings.min_importance_to_send:
                self.db.set_status(raw.url, "low_priority")
            else:
                try:
                    await self.bot.send_for_moderation(
                        edited,
                        raw.url,
                        raw.image_url,
                        raw.published_at,
                        raw.source,
                    )
                    self.db.set_status(raw.url, "moderation")
                except Exception:
                    log.exception("Failed to send news for moderation")
                    self.db.set_status(raw.url, "error")

    async def run_forever(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Pipeline iteration failed")

            await asyncio.sleep(self.settings.check_interval_minutes * 60)
