import asyncio
import logging
import time
from app.collector import collect_news, materialize_news
from app.simple_dedup import is_duplicate_text
from app.simple_editor import SimpleNewsEditor

log=logging.getLogger(__name__)

class NewsRunner:
    def __init__(self, settings, db, bot):
        self.settings=settings
        self.db=db
        self.bot=bot
        self.editor=SimpleNewsEditor(settings.openai_api_key,settings.openai_model,settings.max_completion_tokens)
        self.last_activity=time.monotonic()

    async def once(self):
        recent=self.db.recent_texts()
        for raw in await collect_news(self.settings):
            if self.db.seen(raw.url):
                continue
            if is_duplicate_text(raw.summary,recent):
                self.db.mark(raw.url,raw.summary,"duplicate")
                continue
            edited=await self.editor.edit(raw)
            await materialize_news(raw)
            await self.bot.send_for_moderation(edited,raw)
            self.db.mark(raw.url,raw.summary,"proposed")
            recent.append(raw.summary)

    async def forever(self):
        while True:
            try:
                await self.once()
            except Exception:
                log.exception("news cycle error")
            self.last_activity=time.monotonic()
            await asyncio.sleep(self.settings.check_interval_seconds)
