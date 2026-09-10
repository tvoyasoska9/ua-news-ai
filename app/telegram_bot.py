from datetime import datetime
import re
from html import escape
from io import BytesIO
import logging
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from app.models import EditedNews
from app.editor import sanitize_news_html, strip_source_mentions

log = logging.getLogger(__name__)


class NewsBot:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self.pending = {}
        self.db.cleanup_pending()

        self.app.add_handler(CallbackQueryHandler(self.callback))
        self.app.add_handler(CommandHandler("status", self.status))
        self.app.add_handler(CommandHandler("sources", self.sources))
        self.app.add_handler(CommandHandler("stats", self.stats))
        self.app.add_handler(CommandHandler("testnews", self.testnews))

    async def start(self):
        await self.app.initialize()
        await self.app.start()
        if self.app.updater:
            await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def stop(self):
        if self.app.updater:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    def allowed(self, update):
        return bool(update.effective_chat and update.effective_chat.id == self.settings.moderation_chat_id)

    async def status(self, update, context):
        if self.allowed(update):
            await update.effective_message.reply_text("🟢 UA News AI працює.")

    async def sources(self, update, context):
        if self.allowed(update):
            from app.sources import RSS_SOURCES, TELEGRAM_SOURCES
            lines = ["📰 RSS-джерела:"]
            lines += ["• " + s["name"] for s in RSS_SOURCES]
            lines += ["", "📢 Telegram-канали:"]
            lines += ["• @" + s["username"] for s in TELEGRAM_SOURCES]
            await update.effective_message.reply_text("\n".join(lines))

    async def stats(self, update, context):
        if not self.allowed(update):
            return
        rows = self.db.get_source_stats(hours=24)
        if not rows:
            await update.effective_message.reply_text(
                "📊 <b>СТАТИСТИКА ДЖЕРЕЛ</b>\n\nЗа останні 24 години ще немає достатньо даних.",
                parse_mode="HTML",
            )
            return

        lines = ["📊 <b>СТАТИСТИКА ДЖЕРЕЛ</b>", "🕒 За останні 24 години", ""]
        totals = {"found": 0, "passed_ai": 0, "moderation": 0, "published": 0}
        for r in rows:
            for key in totals:
                totals[key] += r[key]
            lines += [
                f"📡 <b>{escape(r['source'])}</b>",
                f"📥 Знайдено: <b>{r['found']}</b>",
                f"🤖 Пройшло AI: <b>{r['passed_ai']}</b>",
                f"🟡 На модерацію: <b>{r['moderation']}</b>",
                f"📤 Опубліковано: <b>{r['published']}</b>",
                "",
            ]
        lines += [
            "━━━━━━━━━━━━━━",
            f"📥 <b>Усього знайдено: {totals['found']}</b>",
            f"🤖 Пройшло AI: {totals['passed_ai']}",
            f"🟡 На модерацію: {totals['moderation']}",
            f"📤 Опубліковано: {totals['published']}",
        ]
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

    async def testnews(self, update, context):
        if self.allowed(update):
            item = EditedNews("Тестова новина", "Це тест системи модерації.", "Тест", 5, "high", [])
            await self.send_for_moderation(
                item, "test://" + str(uuid4()), None,
                datetime.now().astimezone().isoformat(), "Тест"
            )

    def format_date(self, published_at):
        if not published_at:
            return "Невідомо"
        try:
            dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            return dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return published_at

    def _safe_original_url(self, url):
        try:
            parsed = urlparse(url or "")
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return url
        except Exception:
            pass
        return None

    def _public_body(self, item, source=""):
        title = escape(strip_source_mentions(item.title, source))
        body = sanitize_news_html(item.text, source).strip()
        return title, body

    def moderation_text(self, item, published_at=None, source=None, original_url=None):
        title, body = self._public_body(item, source)
        signature = '<b><a href="https://t.me/ukr24live">УКРАЇНА 🇺🇦 LIVE 24</a></b>'

        lines = [
            "🟡 <b>НА ПЕРЕВІРКУ</b>",
            "",
            f"🇺🇦 <b>{title}</b>",
        ]
        if body:
            # Exactly one empty line between the news text and channel signature.
            lines += ["", body, "", signature]
        else:
            lines += ["", signature]

        lines += [
            "",
            "━━━━━━━━━━━━━━",
            f"📅 Опубліковано: <b>{escape(self.format_date(published_at))}</b>",
            f"📊 Важливість: <b>{item.importance}/10</b>",
            f"📂 Категорія: {escape(item.category)}",
            f"🔍 Впевненість: {escape(item.confidence)}",
            "",
            "🔐 <b>АДМІН-ІНФОРМАЦІЯ</b>",
            f"📡 Джерело: <b>{escape(source or 'Невідомо')}</b>",
        ]
        safe_url = self._safe_original_url(original_url)
        if safe_url:
            lines.append(f'🔗 <a href="{escape(safe_url, quote=True)}">Відкрити оригінальну публікацію</a>')
        return "\n".join(lines)

    def publish_text(self, item):
        title, body = self._public_body(item)

        # Normalize all accidental extra line breaks before publication.
        # The channel signature must always have exactly one empty line before it.
        title = re.sub(r"\n{2,}", "\n", title).strip()
        body = re.sub(r"\n{3,}", "\n\n", body).strip()

        signature = '<b><a href="https://t.me/ukr24live">УКРАЇНА 🇺🇦 LIVE 24</a></b>'
        parts = [f"<b>{title}</b>"]
        if body:
            parts.append(body)

        return "\n\n".join(parts + [signature])

    def keyboard(self, item_id):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{item_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{item_id}"),
        ]])

    def _serialize_pending(self, payload):
        item = payload["item"]
        return {
            "item": {
                "title": item.title,
                "text": item.text,
                "category": item.category,
                "importance": item.importance,
                "confidence": item.confidence,
                "source_urls": list(item.source_urls or []),
                "event_key": item.event_key or "",
            },
            "url": payload.get("url"),
            "image_url": payload.get("image_url"),
            "source": payload.get("source"),
            "media_type": payload.get("media_type"),
            "media_path": payload.get("media_path"),
            "media_paths": list(payload.get("media_paths") or []),
            "media_types": list(payload.get("media_types") or []),
            "cached_media": [
                [file_id, kind] for file_id, kind in (payload.get("cached_media") or [])
            ],
        }

    def _deserialize_pending(self, data):
        if not data or not isinstance(data.get("item"), dict):
            return None
        raw = data["item"]
        try:
            item = EditedNews(
                raw["title"],
                raw["text"],
                raw["category"],
                int(raw["importance"]),
                raw["confidence"],
                raw.get("source_urls") or [],
                raw.get("event_key") or "",
            )
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "item": item,
            "url": data.get("url"),
            "image_url": data.get("image_url"),
            "source": data.get("source"),
            "media_type": data.get("media_type"),
            "media_path": data.get("media_path"),
            "media_paths": list(data.get("media_paths") or []),
            "media_types": list(data.get("media_types") or []),
            "cached_media": [
                (entry[0], entry[1])
                for entry in (data.get("cached_media") or [])
                if isinstance(entry, (list, tuple)) and len(entry) == 2
            ],
        }

    def _persist_pending(self, item_id):
        payload = self.pending.get(item_id)
        if payload:
            self.db.save_pending(item_id, self._serialize_pending(payload))

    async def download_photo(self, image_url):
        if not image_url:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            headers = {"User-Agent": "Mozilla/5.0"}
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(image_url, allow_redirects=True) as response:
                    if response.status >= 400 or not response.headers.get("Content-Type", "").lower().startswith("image/"):
                        return None
                    data = await response.read()
                    if len(data) < 10_000:
                        return None
                    photo = BytesIO(data)
                    photo.name = "news.jpg"
                    return photo
        except Exception:
            log.exception("Failed to download fallback image: %s", image_url)
            return None

    def _media_lists(self, media_type=None, media_path=None, media_paths=None, media_types=None):
        paths = list(media_paths or [])
        types = list(media_types or [])
        if not paths and media_path:
            paths = [media_path]
            types = [media_type or "photo"]
        if len(types) < len(paths):
            types += [media_type or "photo"] * (len(paths) - len(types))
        return [(p, t) for p, t in zip(paths, types) if p and Path(p).exists() and t in {"photo", "video"}]

    @staticmethod
    def _file_id_from_message(message, kind):
        try:
            if kind == "video" and message.video:
                return message.video.file_id
            if kind == "photo" and message.photo:
                return message.photo[-1].file_id
        except Exception:
            pass
        return None

    async def _send_media_group(self, chat_id, media_items, caption=None):
        opened = []
        try:
            payload = []
            for index, (path, kind) in enumerate(media_items):
                handle = open(path, "rb")
                opened.append(handle)
                cap = caption if index == 0 and caption and len(caption) <= 1024 else None
                if kind == "video":
                    payload.append(InputMediaVideo(handle, caption=cap, parse_mode="HTML" if cap else None))
                else:
                    payload.append(InputMediaPhoto(handle, caption=cap, parse_mode="HTML" if cap else None))
            return await self.app.bot.send_media_group(chat_id=chat_id, media=payload)
        finally:
            for handle in opened:
                try:
                    handle.close()
                except Exception:
                    pass

    async def _send_cached_media_group(self, chat_id, cached_media, caption=None):
        payload = []
        for index, (file_id, kind) in enumerate(cached_media):
            cap = caption if index == 0 and caption and len(caption) <= 1024 else None
            if kind == "video":
                payload.append(InputMediaVideo(file_id, caption=cap, parse_mode="HTML" if cap else None))
            else:
                payload.append(InputMediaPhoto(file_id, caption=cap, parse_mode="HTML" if cap else None))
        return await self.app.bot.send_media_group(chat_id=chat_id, media=payload)

    async def _send_single_media(self, chat_id, path, kind, caption=None):
        with open(path, "rb") as media:
            if kind == "video":
                return await self.app.bot.send_video(chat_id=chat_id, video=media, caption=caption, parse_mode="HTML" if caption else None)
            return await self.app.bot.send_photo(chat_id=chat_id, photo=media, caption=caption, parse_mode="HTML" if caption else None)

    async def _send_cached_single_media(self, chat_id, file_id, kind, caption=None):
        if kind == "video":
            return await self.app.bot.send_video(chat_id=chat_id, video=file_id, caption=caption, parse_mode="HTML" if caption else None)
        return await self.app.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption, parse_mode="HTML" if caption else None)

    def _cache_messages(self, messages, kinds):
        cached = []
        for message, kind in zip(messages or [], kinds):
            file_id = self._file_id_from_message(message, kind)
            if file_id:
                cached.append((file_id, kind))
        return cached

    async def send_for_moderation(
        self, item, url, image_url=None, published_at=None, source=None,
        media_type=None, media_path=None, media_paths=None, media_types=None,
    ):
        item_id = str(uuid4())
        media_items = self._media_lists(media_type, media_path, media_paths, media_types)
        self.pending[item_id] = {
            "item": item,
            "url": url,
            "image_url": image_url,
            "source": source,
            "media_type": media_type,
            "media_path": media_path,
            "media_paths": list(media_paths or []),
            "media_types": list(media_types or []),
            "cached_media": [],
        }
        self._persist_pending(item_id)

        text = self.moderation_text(item, published_at, source, original_url=url)
        keyboard = self.keyboard(item_id)

        try:
            if len(media_items) > 1:
                messages = await self._send_media_group(self.settings.moderation_chat_id, media_items)
                self.pending[item_id]["cached_media"] = self._cache_messages(messages, [kind for _, kind in media_items])
                self._persist_pending(item_id)
                await self.app.bot.send_message(
                    chat_id=self.settings.moderation_chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                return

            if len(media_items) == 1:
                path, kind = media_items[0]
                if len(text) <= 1024:
                    message = await self._send_single_media(self.settings.moderation_chat_id, path, kind, text)
                else:
                    message = await self._send_single_media(self.settings.moderation_chat_id, path, kind)
                    await self.app.bot.send_message(
                        chat_id=self.settings.moderation_chat_id,
                        text=text,
                        parse_mode="HTML",
                    )
                cached = self._file_id_from_message(message, kind)
                if cached:
                    self.pending[item_id]["cached_media"] = [(cached, kind)]
                    self._persist_pending(item_id)
                await self.app.bot.send_message(
                    chat_id=self.settings.moderation_chat_id,
                    text="Оберіть дію для цієї новини:",
                    reply_markup=keyboard,
                )
                return
        except Exception:
            log.exception("Failed to send original media to moderation; falling back to text card")

        photo = await self.download_photo(image_url) if image_url else None
        if photo:
            try:
                await self.app.bot.send_photo(chat_id=self.settings.moderation_chat_id, photo=photo)
            except Exception:
                log.exception("Failed to send fallback image to moderation")
        await self.app.bot.send_message(
            chat_id=self.settings.moderation_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    async def _publish_media(self, chat_id, post_text, cached_media, local_media):
        media = list(cached_media or [])
        source = "cached"
        if not media:
            media = list(local_media or [])
            source = "local"
        if not media:
            return False

        caption = post_text if len(post_text) <= 1024 else None
        sent_any = False

        if len(media) > 1:
            try:
                if source == "cached":
                    await self._send_cached_media_group(chat_id, media, caption)
                else:
                    await self._send_media_group(chat_id, media, caption)
                sent_any = True
            except Exception:
                log.exception("Media group publication failed; trying media one by one")
                for index, entry in enumerate(media):
                    try:
                        cap = caption if index == 0 else None
                        if source == "cached":
                            await self._send_cached_single_media(chat_id, entry[0], entry[1], cap)
                        else:
                            await self._send_single_media(chat_id, entry[0], entry[1], cap)
                        sent_any = True
                    except Exception:
                        log.exception("Failed to publish one media item")
        else:
            entry = media[0]
            try:
                if source == "cached":
                    await self._send_cached_single_media(chat_id, entry[0], entry[1], caption)
                else:
                    await self._send_single_media(chat_id, entry[0], entry[1], caption)
                sent_any = True
            except Exception:
                log.exception("Single media publication failed")

        if sent_any and len(post_text) > 1024:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=post_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        return sent_any

    async def callback(self, update, context):
        query = update.callback_query
        if not query or not query.data:
            return
        if query.message.chat_id != self.settings.moderation_chat_id:
            await query.answer("Немає доступу", show_alert=True)
            return

        allowed_users = self.settings.moderation_allowed_user_ids
        if allowed_users and (not query.from_user or query.from_user.id not in allowed_users):
            await query.answer("У вас немає прав модератора", show_alert=True)
            return

        try:
            action, item_id = query.data.split(":", 1)
        except ValueError:
            await query.answer("Некоректна дія", show_alert=True)
            return

        if action not in {"publish", "reject"}:
            await query.answer("Некоректна дія", show_alert=True)
            return
        payload = self.pending.get(item_id)
        if not payload:
            saved = self.db.get_pending(item_id)
            payload = self._deserialize_pending(saved)
            if payload:
                self.pending[item_id] = payload

        if not payload:
            await query.answer(
                "Дані цієї новини вже недоступні. Надішліть її на модерацію повторно.",
                show_alert=True,
            )
            return

        await query.answer()

        item = payload["item"]
        url = payload["url"]
        image_url = payload["image_url"]
        source = payload["source"]
        media_type = payload["media_type"]
        media_path = payload["media_path"]
        media_paths = payload["media_paths"]
        media_types = payload["media_types"]
        cached_media = payload.get("cached_media", [])

        try:
            if action == "publish":
                post_text = self.publish_text(item)
                local_media = self._media_lists(media_type, media_path, media_paths, media_types)
                published_with_media = await self._publish_media(
                    self.settings.publish_channel_id,
                    post_text,
                    cached_media,
                    local_media,
                )

                if image_url and not published_with_media:
                    photo = await self.download_photo(image_url)
                    if photo:
                        try:
                            await self.app.bot.send_photo(
                                chat_id=self.settings.publish_channel_id,
                                photo=photo,
                                caption=post_text if len(post_text) <= 1024 else None,
                                parse_mode="HTML" if len(post_text) <= 1024 else None,
                            )
                            if len(post_text) > 1024:
                                await self.app.bot.send_message(
                                    chat_id=self.settings.publish_channel_id,
                                    text=post_text,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True,
                                )
                            published_with_media = True
                        except Exception:
                            log.exception("Fallback image publication failed")

                if not published_with_media:
                    await self.app.bot.send_message(
                        chat_id=self.settings.publish_channel_id,
                        text=post_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

                self.db.set_status(url, "published")
                self.db.record_metric(url, source, "published")
                await query.message.reply_text("✅ Опубліковано.")
            else:
                self.db.set_status(url, "rejected")
                await query.message.reply_text("❌ Відхилено.")

            self.pending.pop(item_id, None)
            self.db.delete_pending(item_id)

            for path in set(([media_path] if media_path else []) + list(media_paths or [])):
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass

            await query.edit_message_reply_markup(reply_markup=None)

        except Exception:
            log.exception("Failed to process moderation action: %s", action)
            await query.answer("Помилка публікації. Спробуйте ще раз.", show_alert=True)
