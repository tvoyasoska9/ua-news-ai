import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from app.collector import collect_news, fingerprint, materialize_news
from app.dedup import is_similar_title, is_duplicate_event
from app.editor import NewsEditor, QualityError

log = logging.getLogger(__name__)

# Real-time moderation: posts older than this are historical backlog, not
# breaking/current news. They are not sent just because the queue was slow.
MAX_QUEUE_AGE_MINUTES = 75
MAX_QUEUE_SIZE = 24
CYCLE_TIMEOUT_SECONDS = 120


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

        reopened = self.db.reopen_recent_quality_rejections(hours=24)
        if reopened:
            log.info("Reopened %s recent quality-rejected candidate(s) after quality-gate update", reopened)

        self.recent_titles = self.db.get_recent_titles(limit=1000)
        self.recent_events = self.db.get_recent_event_keys(limit=1000)

        self.queue = deque()
        self.queue_keys = set()
        self.queue_events = []
        self._last_cleanup = datetime.min.replace(tzinfo=timezone.utc)
        # Updated by every healthy pipeline cycle. The main process uses this
        # heartbeat to detect a silently stalled pipeline task.
        self.last_activity = time.monotonic()

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

        # The publication target is based ONLY on successfully published news.
        # Pending moderation cards, rejected cards and in-memory queue items are
        # deliberately excluded: they must never consume or block the user's
        # daily publication quota.
        published_today = self.db.daily_count("published_news")
        if published_today >= self.settings.max_published_news_per_day:
            log.info(
                "Published-news target reached | published=%s target=%s",
                published_today,
                self.settings.max_published_news_per_day,
            )
            return
        remaining_target = self.settings.max_published_news_per_day - published_today

        # Do not generate an unlimited moderation backlog. Pending cards already
        # represent candidates waiting for a human decision, and in-memory queue
        # items are about to become cards. Once they can fill the remaining daily
        # publication target, stop before collection/media/AI work entirely.
        # Keep monitoring continuously throughout the day. Pending moderation
        # cards must NOT stop collection: otherwise one unattended card can make
        # the bot appear dead even though new source posts are arriving.
        pending_fresh = self.db.pending_count(max_age_minutes=MAX_QUEUE_AGE_MINUTES)
        queue_capacity = max(0, MAX_QUEUE_SIZE - len(self.queue))
        if queue_capacity <= 0:
            log.info(
                "In-memory queue full | published=%s/%s pending=%s queue=%s; moderation worker will drain it",
                published_today,
                self.settings.max_published_news_per_day,
                pending_fresh,
                len(self.queue),
            )
            return

        items = await collect_news(self.settings)

        # collect_news() is Telegram-only. Preserve the collector's strict
        # round-robin order across configured channels.
        log.info(
            "Collected %s Telegram candidates; remaining publication capacity: %s",
            len(items),
            remaining_target,
        )

        # Cheap duplicate screening happens before OpenAI. Keep titles seen in
        # this very cycle as well, otherwise five channels can spend five AI
        # calls on the same event before the first result reaches moderation.
        pre_ai_titles = list(self.recent_titles[-1000:])
        ai_attempts = 0

        for raw in items:
            key = fingerprint(raw)

            if self.db.exists(raw.url):
                status = self.db.get_status(raw.url) or "unknown"
                # Transient/model-side quality failures must remain retryable.
                # Do not silently turn a temporary bad generation into a
                # permanently lost news item.
                # Only genuine transient failures are retried. A candidate that
                # already failed deterministic validation must not consume every
                # later cycle and block newer news from reaching moderation.
                if status != "error_retry":
                    log.info(
                        "Candidate skipped | reason=already_handled | status=%s | source=%s | title=%s",
                        status,
                        raw.source,
                        raw.title[:120],
                    )
                    _cleanup_media(raw.media_path, raw.media_paths)
                    continue
                log.info(
                    "Retrying transiently failed candidate | status=%s | source=%s | title=%s",
                    status,
                    raw.source,
                    raw.title[:120],
                )

            # Do not spend AI calls on historical backlog.
            if _is_too_old(raw.published_at):
                log.info("Candidate skipped | reason=stale | source=%s | title=%s", raw.source, raw.title[:120])
                self.db.add(raw.url, key, raw.title, raw.source, "stale")
                _cleanup_media(raw.media_path, raw.media_paths)
                continue

            if is_similar_title(raw, pre_ai_titles, threshold=82):
                log.info("Candidate skipped | reason=title_duplicate | source=%s | title=%s", raw.source, raw.title[:120])
                self.db.add(raw.url, key, raw.title, raw.source, "duplicate")
                _cleanup_media(raw.media_path, raw.media_paths)
                continue

            # A restart or a sudden source backlog must not burn the entire API
            # balance in a single minute. The publication target is enforced
            # only on successful publication, so moderation cards themselves do
            # not reduce the number of candidates that may be prepared.
            # Throughput is limited by the cycle cap and queue capacity, never
            # by pending moderation cards.
            cycle_cap = min(self.settings.max_ai_candidates_per_cycle, max(1, queue_capacity))
            if ai_attempts >= cycle_cap:
                log.info(
                    "AI cycle capacity reached (%s candidate(s); published=%s/%s pending=%s queue=%s)",
                    cycle_cap,
                    published_today,
                    self.settings.max_published_news_per_day,
                    pending_fresh,
                    len(self.queue),
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

                # A source item may reveal an older publication time after
                # media materialization; stop here before spending an OpenAI request.
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
                try:
                    edited = await self.editor.edit(raw)
                    self.db.record_metric(raw.url, raw.source, "model_completed")
                except QualityError as quality_error:
                    # A deterministic gate failure is retryable once. The first
                    # generation may be too literal or too short even when the
                    # underlying news is valid. Make one explicit fresh rewrite
                    # instead of permanently losing the news.
                    reason = str(quality_error)
                    self.db.record_metric(raw.url, raw.source, "ai_quality_failed")
                    if not self._consume_model_slot():
                        self.db.set_status(raw.url, "daily_model_limit")
                        log.warning("Daily model-call limit reached before repair (%s)", self.settings.max_model_calls_per_day)
                        _cleanup_media(raw.media_path, raw.media_paths)
                        break
                    self.db.record_metric(raw.url, raw.source, "ai_repair")
                    try:
                        edited = await self.editor.repair(raw, reason)
                        self.db.record_metric(raw.url, raw.source, "model_completed")
                    except QualityError as repair_error:
                        # Availability takes priority over stylistic rejection:
                        # keep processing subsequent candidates without stopping
                        # the monitoring loop.
                        self.db.set_status(raw.url, "quality_rejected_final")
                        log.warning(
                            "Draft rejected after repair; continuing pipeline | source=%s | title=%s | reason=%s",
                            raw.source,
                            raw.title[:100],
                            str(repair_error),
                        )
                        _cleanup_media(raw.media_path, raw.media_paths)
                        continue
                    except Exception:
                        log.exception("AI repair failed")
                        self.db.release_daily("model_calls")
                        self.db.set_status(raw.url, "error_retry")
                        self.db.record_metric(raw.url, raw.source, "ai_error")
                        _cleanup_media(raw.media_path, raw.media_paths)
                        continue
            except Exception:
                log.exception("AI editing failed")
                # The model did not produce a usable response, so return the
                # reserved quota slot. A transient transport failure must not
                # silently consume the daily AI budget.
                self.db.release_daily("model_calls")
                self.db.set_status(raw.url, "error_retry")
                self.db.record_metric(raw.url, raw.source, "ai_error")
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
            "Daily usage | published_news: %s/%s | model_calls: %s/%s",
            self.db.daily_count("published_news"),
            self.settings.max_published_news_per_day,
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
                log.exception("Failed to send queued news for moderation")
                self.db.set_status(url, "error")
                await asyncio.sleep(5)

    async def run_forever(self):
        worker = asyncio.create_task(self.moderation_worker(), name="moderation-worker")
        try:
            while True:
                self.last_activity = time.monotonic()
                log.info("Pipeline cycle started")
                try:
                    await asyncio.wait_for(
                        self.run_once(),
                        timeout=CYCLE_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    log.exception(
                        "Pipeline cycle timed out after %s seconds; continuing with the next cycle",
                        CYCLE_TIMEOUT_SECONDS,
                    )
                except Exception:
                    log.exception("Pipeline iteration failed")

                self.last_activity = time.monotonic()
                interval_seconds = self.settings.check_interval_minutes * 60
                log.info("Pipeline cycle finished; next check in %s seconds", interval_seconds)
                await asyncio.sleep(interval_seconds)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
