import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from app.collector import collect_news, fingerprint, materialize_news
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


def _queue_sort_key(item):
    # Telegram-only pipeline: newest fresh post is always first.
    edited, url, image_url, published_at, source, media_type, media_path, media_paths, media_types = item
    return (-_published_timestamp(published_at),)


def _event_signature(edited):
    # Store both the AI factual key and the final headline. This gives the
    # duplicate detector two independent descriptions of the same event.
    parts = [str(getattr(edited, "event_key", "") or "").strip(),
             str(getattr(edited, "title", "") or "").strip()]
    return " | ".join(part for part in parts if part)


def _cleanup_media(path, paths=None):
    all_paths = ([path] if path else []) + list(paths or [])
    for candidate in all_paths:
        if not candidate:
            continue
        try:
            from pathlib import Path
            Path(candidate).unlink(missing_ok=True)
        except Exception:
            pass


class NewsPipeline:
    def __init__(self, settings, db, bot):
        self.settings = settings
        self.db = db
        self.bot = bot
        self.editor = NewsEditor(
            settings.openai_api_key,
            settings.openai_model,
            settings.max_article_chars,
            settings.max_completion_tokens,
            settings.ai_max_retries,
        )

        self.recent_titles = self.db.get_recent_titles(limit=1000)
        self.recent_events = self.db.get_recent_event_keys(limit=1000)

        self.queue = deque()
        self.queue_keys = set()
        self.queue_events = []
        self._last_cleanup = datetime.min.replace(tzinfo=timezone.utc)

    def _prepared_news_limit_reached(self):
        return self.db.daily_count("prepared_news") >= self.settings.max_prepared_news_per_day

    def _consume_model_slot(self):
        return self.db.try_consume_daily("model_calls", self.settings.max_model_calls_per_day)

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
            media_paths = item[7] if len(item) > 7 else []
            for path in ([media_path] if media_path else []) + list(media_paths):
                if path:
                    try:
                        from pathlib import Path
                        Path(path).unlink(missing_ok=True)
                    except Exception:
                        pass

        self.queue = deque(kept)
        self.queue_keys = {item[1] for item in kept}
        self.queue_events = [
            _event_signature(item[0])
            for item in kept
        ]

    async def run_once(self):
        now = datetime.now(timezone.utc)
        if now - self._last_cleanup >= timedelta(hours=6):
            self.db.cleanup_history(self.settings.history_retention_days)
            self._last_cleanup = now

        items = await collect_news(self.settings)

        # collect_news() is Telegram-only. Preserve the collector's strict
        # round-robin order across configured channels.
        log.info("Collected %s Telegram candidates from configured channels", len(items))

        if self._prepared_news_limit_reached():
            log.info(
                "Daily prepared-news limit reached before processing | prepared_news: %s/%s | model_calls: %s/%s",
                self.db.daily_count("prepared_news"),
                self.settings.max_prepared_news_per_day,
                self.db.daily_count("model_calls"),
                self.settings.max_model_calls_per_day,
            )
            return

        # Cheap duplicate screening happens before OpenAI. Keep titles seen in
        # this very cycle as well, otherwise five channels can spend five AI
        # calls on the same event before the first result reaches moderation.
        pre_ai_titles = list(self.recent_titles[-1000:])
        ai_attempts = 0

        for raw in items:
            key = fingerprint(raw)

            if self.db.exists(raw.url, key):
                _cleanup_media(raw.media_path, raw.media_paths)
                continue

            # Do not spend AI calls on historical backlog.
            if _is_too_old(raw.published_at):
                self.db.add(raw.url, key, raw.title, raw.source, "stale")
                _cleanup_media(raw.media_path, raw.media_paths)
                continue

            if is_similar_title(raw, pre_ai_titles, threshold=82):
                self.db.add(raw.url, key, raw.title, raw.source, "duplicate")
                _cleanup_media(raw.media_path, raw.media_paths)
                continue

            # A restart or a sudden source backlog must not burn the entire API
            # balance in a single minute. Remaining fresh items are retried on
            # the next cycle and are still protected by their age limit.
            if ai_attempts >= self.settings.max_ai_candidates_per_cycle:
                log.info(
                    "AI cycle limit (%s) reached; remaining candidates wait for the next poll",
                    self.settings.max_ai_candidates_per_cycle,
                )
                break

            self.db.add(raw.url, key, raw.title, raw.source, "processing")
            pre_ai_titles.append(raw.title)
            pre_ai_titles = pre_ai_titles[-1000:]
            ai_attempts += 1
            # Admin-only analytics: record where the candidate actually came from.
            self.db.record_metric(raw.url, raw.source, "found")

            try:
                # All cheap screening is complete. Only the selected Telegram
                # candidate may now download its original media.
                await materialize_news(raw)

                # Some RSS feeds omit publication dates. If the article page
                # reveals an old publication time, stop here before spending an
                # OpenAI request.
                if _is_too_old(raw.published_at):
                    self.db.set_status(raw.url, "stale")
                    _cleanup_media(raw.media_path, raw.media_paths)
                    # This candidate never reached OpenAI, so do not let a
                    # late-discovered article date consume the cycle's AI cap.
                    ai_attempts -= 1
                    try:
                        pre_ai_titles.remove(raw.title)
                    except ValueError:
                        pass
                    continue

                if not self._consume_model_slot():
                    self.db.set_status(raw.url, "daily_model_limit")
                    log.warning("Daily model-call limit reached (%s)", self.settings.max_model_calls_per_day)
                    break
                self.db.record_metric(raw.url, raw.source, "model_started")
                edited = await self.editor.edit(raw)
                self.db.record_metric(raw.url, raw.source, "model_completed")
            except Exception:
                log.exception("AI editing failed")
                self.db.set_status(raw.url, "error")
                _cleanup_media(raw.media_path, raw.media_paths)
                continue

            if edited.confidence == "low":
                log.info("Low-confidence candidate kept for human moderation: %s", raw.title)

            if edited.importance < self.settings.min_importance_to_send:
                self.db.set_status(raw.url, "low_priority")
                _cleanup_media(raw.media_path, raw.media_paths)
                continue

            # It survived AI editing and the importance gate.
            self.db.record_metric(raw.url, raw.source, "passed_ai")

            event_key = _event_signature(edited)

            all_recent_events = self.recent_events + self.queue_events
            if is_duplicate_event(event_key, all_recent_events, threshold=82):
                self.db.set_event_key(raw.url, event_key)
                self.db.set_status(raw.url, "duplicate")
                _cleanup_media(raw.media_path, raw.media_paths)
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
                    list(raw.media_paths),
                    list(raw.media_types),
                )
            )
            self.db.set_status(raw.url, "queued")
            self._resort_and_trim_queue()

        log.info("Moderation queue size: %s", len(self.queue))
        log.info(
            "Daily usage | prepared_news: %s/%s | model_calls: %s/%s",
            self.db.daily_count("prepared_news"),
            self.settings.max_prepared_news_per_day,
            self.db.daily_count("model_calls"),
            self.settings.max_model_calls_per_day,
        )

    async def moderation_worker(self):
        while True:
            if not self.queue:
                await asyncio.sleep(1)
                continue

            edited, url, image_url, published_at, source, media_type, media_path, media_paths, media_types = self.queue.popleft()
            self.queue_keys.discard(url)

            if _is_too_old(published_at):
                self.db.set_status(url, "stale")
                log.info("Skipped stale queued news (%s): %s", published_at, edited.title[:80])
                continue

            event_key = _event_signature(edited)
            try:
                self.queue_events.remove(event_key)
            except ValueError:
                pass

            if not self.db.try_consume_daily(
                "prepared_news", self.settings.max_prepared_news_per_day
            ):
                self.db.set_status(url, "daily_limit")
                _cleanup_media(media_path, media_paths)
                log.info("Daily prepared-news limit reached (%s)", self.settings.max_prepared_news_per_day)
                continue

            try:
                await self.bot.send_for_moderation(
                    edited,
                    url,
                    image_url,
                    published_at,
                    source,
                    media_type,
                    media_path,
                    media_paths,
                    media_types,
                )
                self.db.set_status(url, "moderation")
                self.db.record_metric(url, source, "moderation")
                log.info(
                    "Sent fresh news to moderation. Waiting %s seconds before next item.",
                    self.settings.moderation_interval_seconds,
                )
                await asyncio.sleep(self.settings.moderation_interval_seconds)
            except Exception:
                self.db.release_daily("prepared_news")
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
