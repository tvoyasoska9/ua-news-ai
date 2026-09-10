from datetime import datetime
from html import escape
from io import BytesIO
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


class NewsBot:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self.pending = {}
        self.processing_actions = set()

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
            f"🤖 Пройшло AI: <b>{totals['passed_ai']}</b>",
            f"🟡 На модерацію: <b>{totals['moderation']}</b>",
            f"📤 Опубліковано: <b>{totals['published']}</b>",
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
        # Preserve only safe formatting tags; source-like content is stripped
        # again as a final public gate.
        body = sanitize_news_html(item.text, source)
        return title, body

    def moderation_text(self, item, published_at=None, source=None, original_url=None):
        title, body = self._public_body(item, source)
        lines = [
            "🟡 <b>НА ПЕРЕВІРКУ</b>", "",
            f"🇺🇦 <b>{title}</b>",
        ]
        if body:
            lines += ["", body]
        lines += [
            "",
            f"📅 Опубліковано: <b>{escape(self.format_date(published_at))}</b>",
            f"📊 Важливість: <b>{item.importance}/10</b>",
            f"📂 Категорія: {escape(item.category)}",
            f"🔍 Впевненість: {escape(item.confidence)}",
            "",
            "━━━━━━━━━━━━━━",
            "🔐 <b>АДМІН-ІНФОРМАЦІЯ</b>",
            f"📡 Джерело: <b>{escape(source or 'Невідомо')}</b>",
        ]
        safe_url = self._safe_original_url(original_url)
        if safe_url:
            lines.append(f'🔗 <a href="{escape(safe_url, quote=True)}">Відкрити оригінальну публікацію</a>')
        lines += ["", '<b><a href="https://t.me/ukr24live">УКРАЇНА 🇺🇦 LIVE 24</a></b>']
        return "\n".join(lines)

    def publish_text(self, item):
        title, body = self._public_body(item)
        # ADMIN metadata and original URLs never enter this function.
        # A genuinely short source may have no details beyond the headline;
        # in that case do not invent or duplicate a body.
        parts = [f"<b>{title}</b>"]
        if body:
            parts.append(body)
        parts.append('<b><a href="https://t.me/ukr24live">УКРАЇНА 🇺🇦 LIVE 24</a></b>')
        return "\n\n".join(parts)

    def keyboard(self, item_id):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{item_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{item_id}"),
        ]])

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
            return None

    def _media_lists(self, media_type=None, media_path=None, media_paths=None, media_types=None):
        paths = list(media_paths or [])
        types = list(media_types or [])
        if not paths and media_path:
            paths = [media_path]
            types = [media_type or "photo"]
        if len(types) < len(paths):
            types += [media_type or "photo"] * (len(paths) - len(types))
        valid = [(p, t) for p, t in zip(paths, types) if p and Path(p).exists() and t in {"photo", "video"}]
        return valid

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
            await self.app.bot.send_media_group(chat_id=chat_id, media=payload)
            return True
        finally:
            for handle in opened:
                try:
                    handle.close()
                except Exception:
                    pass

    async def _send_single_media(self, chat_id, path, kind, caption=None):
        with open(path, "rb") as media:
            if kind == "video":
                await self.app.bot.send_video(
                    chat_id=chat_id, video=media, caption=caption,
                    parse_mode="HTML" if caption else None,
                )
            else:
                await self.app.bot.send_photo(
                    chat_id=chat_id, photo=media, caption=caption,
                    parse_mode="HTML" if caption else None,
                )

    async def send_for_moderation(
        self, item, url, image_url=None, published_at=None, source=None,
        media_type=None, media_path=None, media_paths=None, media_types=None,
    ):
        item_id = str(uuid4())
        self.pending[item_id] = (
            item, url, image_url, media_type, media_path,
            list(media_paths or []), list(media_types or []), source
        )
        text = self.moderation_text(item, published_at, source, original_url=url)
        keyboard = self.keyboard(item_id)
        media_items = self._media_lists(media_type, media_path, media_paths, media_types)

        try:
            if len(media_items) > 1:
                # Telegram albums cannot carry moderation buttons. Send the full
                # original album first, then the admin card with the buttons.
                await self._send_media_group(self.settings.moderation_chat_id, media_items)
                await self.app.bot.send_message(
                    chat_id=self.settings.moderation_chat_id, text=text,
                    parse_mode="HTML", reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                return
            if len(media_items) == 1:
                path, kind = media_items[0]
                if len(text) <= 1024:
                    await self._send_single_media(self.settings.moderation_chat_id, path, kind, text)
                    # Buttons are sent separately so approval always remains available.
                    await self.app.bot.send_message(
                        chat_id=self.settings.moderation_chat_id,
                        text="Оберіть дію для цієї новини:",
                        reply_markup=keyboard,
                    )
                else:
                    await self._send_single_media(self.settings.moderation_chat_id, path, kind)
                    await self.app.bot.send_message(
                        chat_id=self.settings.moderation_chat_id, text=text,
                        parse_mode="HTML", reply_markup=keyboard,
                    )
                return
        except Exception:
            # Fall back to text moderation card below.
            pass

        photo = await self.download_photo(image_url) if image_url else None
        if photo:
            try:
                await self.app.bot.send_photo(chat_id=self.settings.moderation_chat_id, photo=photo)
            except Exception:
                pass
        await self.app.bot.send_message(
            chat_id=self.settings.moderation_chat_id, text=text,
            parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True,
        )

    async def callback(self, update, context):
        query = update.callback_query
        if not query or not query.data:
            return
        if query.message.chat_id != self.settings.moderation_chat_id:
            await query.answer("Немає доступу", show_alert=True)
            return

        try:
            action, item_id = query.data.split(":", 1)
        except ValueError:
            await query.answer("Некоректна дія", show_alert=True)
            return

        # Telegram can deliver repeated callback updates. Lock the item before
        # publishing so one approval can never create two public posts.
        if item_id in self.processing_actions:
            await query.answer("Дія вже виконується")
            return

        payload = self.pending.get(item_id)
        if not payload:
            await query.answer("Ця новина вже оброблена")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        self.processing_actions.add(item_id)
        try:
            await query.answer()

            item, url, image_url, media_type, media_path, media_paths, media_types, source = payload

            if action == "publish":
                post_text = self.publish_text(item)
                published_with_media = False
                media_items = self._media_lists(media_type, media_path, media_paths, media_types)
                try:
                    if len(media_items) > 1:
                        await self._send_media_group(
                            self.settings.publish_channel_id, media_items,
                            post_text if len(post_text) <= 1024 else None,
                        )
                        if len(post_text) > 1024:
                            await self.app.bot.send_message(
                                chat_id=self.settings.publish_channel_id, text=post_text,
                                parse_mode="HTML", disable_web_page_preview=True,
                            )
                        published_with_media = True
                    elif len(media_items) == 1:
                        path, kind = media_items[0]
                        if len(post_text) <= 1024:
                            await self._send_single_media(
                                self.settings.publish_channel_id, path, kind, post_text
                            )
                        else:
                            await self._send_single_media(self.settings.publish_channel_id, path, kind)
                            await self.app.bot.send_message(
                                chat_id=self.settings.publish_channel_id, text=post_text,
                                parse_mode="HTML", disable_web_page_preview=True,
                            )
                        published_with_media = True
                except Exception:
                    published_with_media = False

                if image_url and not published_with_media:
                    photo = await self.download_photo(image_url)
                    if photo:
                        try:
                            await self.app.bot.send_photo(
                                chat_id=self.settings.publish_channel_id, photo=photo,
                                caption=post_text if len(post_text) <= 1024 else None,
                                parse_mode="HTML" if len(post_text) <= 1024 else None,
                            )
                            if len(post_text) > 1024:
                                await self.app.bot.send_message(
                                    chat_id=self.settings.publish_channel_id, text=post_text,
                                    parse_mode="HTML", disable_web_page_preview=True,
                                )
                            published_with_media = True
                        except Exception:
                            pass

                if not published_with_media:
                    await self.app.bot.send_message(
                        chat_id=self.settings.publish_channel_id, text=post_text,
                        parse_mode="HTML", disable_web_page_preview=True,
                    )

                self.db.set_status(url, "published")
                self.db.record_metric(url, source, "published")
                await query.message.reply_text("✅ Опубліковано.")
            elif action == "reject":
                self.db.set_status(url, "rejected")
                await query.message.reply_text("❌ Відхилено.")
            else:
                await query.answer("Невідома дія", show_alert=True)
                return

            # Remove pending state only after the selected action has completed.
            self.pending.pop(item_id, None)
            for path in set(([media_path] if media_path else []) + list(media_paths or [])):
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass

            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        finally:
            self.processing_actions.discard(item_id)
