from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from app.models import EditedNews
from app.editor import strip_source_mentions


class NewsBot:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self.pending = {}

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
        return bool(
            update.effective_chat
            and update.effective_chat.id == self.settings.moderation_chat_id
        )

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
                "📊 <b>СТАТИСТИКА ДЖЕРЕЛ</b>\n\n"
                "За останні 24 години ще немає достатньо даних. "
                "Статистика почне накопичуватися після встановлення цього оновлення.",
                parse_mode="HTML",
            )
            return

        total_found = sum(r["found"] for r in rows)
        total_passed = sum(r["passed_ai"] for r in rows)
        total_moderation = sum(r["moderation"] for r in rows)
        total_published = sum(r["published"] for r in rows)

        lines = [
            "📊 <b>СТАТИСТИКА ДЖЕРЕЛ</b>",
            "🕒 За останні 24 години",
            "",
        ]

        for r in rows:
            source = escape(r["source"])
            lines += [
                f"📡 <b>{source}</b>",
                f"📥 Знайдено: <b>{r['found']}</b>",
                f"🤖 Пройшло AI: <b>{r['passed_ai']}</b>",
                f"🟡 На модерацію: <b>{r['moderation']}</b>",
                f"📤 Опубліковано: <b>{r['published']}</b>",
                "",
            ]

        lines += [
            "━━━━━━━━━━━━━━",
            f"📥 <b>Усього знайдено: {total_found}</b>",
            f"🤖 Пройшло AI: <b>{total_passed}</b>",
            f"🟡 На модерацію: <b>{total_moderation}</b>",
            f"📤 Опубліковано: <b>{total_published}</b>",
        ]

        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def testnews(self, update, context):
        if self.allowed(update):
            item = EditedNews(
                "Тестова новина",
                "Це тест системи модерації.",
                "Тест",
                5,
                "high",
                [],
            )
            await self.send_for_moderation(
                item,
                "test://" + str(uuid4()),
                None,
                datetime.now().astimezone().isoformat(),
                "Тест",
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

    def moderation_text(self, item, published_at=None, source=None, original_url=None):
        # PUBLIC NEWS BODY: still stripped. The source is shown only below in a
        # clearly separated ADMIN block and is never reused for publication.
        title = strip_source_mentions(item.title, source)
        body = strip_source_mentions(item.text, source)

        lines = [
            "🟡 <b>НА ПЕРЕВІРКУ</b>",
            "",
            f"🇺🇦 <b>{escape(title)}</b>",
            "",
            escape(body),
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
            lines.append(
                f'🔗 <a href="{escape(safe_url, quote=True)}">Відкрити оригінальну публікацію</a>'
            )

        lines += [
            "",
            '<b><a href="https://t.me/ukr24live">УКРАЇНА 🇺🇦 LIVE 24</a></b>',
        ]
        return "\n".join(lines)

    def publish_text(self, item):
        # Absolute public gate: source-like fragments are removed immediately
        # before public delivery. ADMIN metadata is never part of this function.
        title = strip_source_mentions(item.title)
        body = strip_source_mentions(item.text)
        return (
            f"<b>{escape(title)}</b>\n\n"
            f"{escape(body)}\n\n"
            f'<b><a href="https://t.me/ukr24live">УКРАЇНА 🇺🇦 LIVE 24</a></b>'
        )

    def keyboard(self, item_id):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{item_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{item_id}"),
        ]])

    async def download_photo(self, image_url):
        if not image_url:
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        }

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(image_url, allow_redirects=True) as response:
                    if response.status >= 400:
                        return None

                    content_type = response.headers.get("Content-Type", "").lower()
                    if not content_type.startswith("image/"):
                        return None

                    data = await response.read()
                    if len(data) < 10_000:
                        return None

                    photo = BytesIO(data)
                    photo.name = "news.jpg"
                    return photo
        except Exception:
            return None

    async def send_for_moderation(
        self,
        item,
        url,
        image_url=None,
        published_at=None,
        source=None,
        media_type=None,
        media_path=None,
    ):
        item_id = str(uuid4())
        # Source and original URL are ADMIN-ONLY metadata in pending storage.
        self.pending[item_id] = (
            item, url, image_url, media_type, media_path, source
        )
        keyboard = self.keyboard(item_id)
        text = self.moderation_text(
            item, published_at, source, original_url=url
        )

        if media_path and Path(media_path).exists():
            try:
                if media_type == "video":
                    with open(media_path, "rb") as media:
                        await self.app.bot.send_video(
                            chat_id=self.settings.moderation_chat_id,
                            video=media,
                            caption=text if len(text) <= 1000 else None,
                            parse_mode="HTML" if len(text) <= 1000 else None,
                            reply_markup=keyboard if len(text) <= 1000 else None,
                        )
                    if len(text) > 1000:
                        await self.app.bot.send_message(
                            chat_id=self.settings.moderation_chat_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    return
                if media_type == "photo":
                    with open(media_path, "rb") as media:
                        await self.app.bot.send_photo(
                            chat_id=self.settings.moderation_chat_id,
                            photo=media,
                            caption=text if len(text) <= 1000 else None,
                            parse_mode="HTML" if len(text) <= 1000 else None,
                            reply_markup=keyboard if len(text) <= 1000 else None,
                        )
                    if len(text) > 1000:
                        await self.app.bot.send_message(
                            chat_id=self.settings.moderation_chat_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    return
            except Exception:
                pass

        photo = await self.download_photo(image_url) if image_url else None
        if photo and len(text) <= 1000:
            try:
                await self.app.bot.send_photo(
                    chat_id=self.settings.moderation_chat_id,
                    photo=photo,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass
        if photo:
            try:
                photo.seek(0)
                await self.app.bot.send_photo(
                    chat_id=self.settings.moderation_chat_id, photo=photo
                )
            except Exception:
                pass
        await self.app.bot.send_message(
            chat_id=self.settings.moderation_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    async def callback(self, update, context):
        query = update.callback_query
        if not query or not query.data:
            return

        if query.message.chat_id != self.settings.moderation_chat_id:
            await query.answer("Немає доступу", show_alert=True)
            return

        await query.answer()
        action, item_id = query.data.split(":", 1)
        payload = self.pending.get(item_id)

        if not payload:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        item, url, image_url, media_type, media_path, source = payload

        if action == "publish":
            published_with_media = False
            # Only item title/body enter the public post.
            post_text = self.publish_text(item)

            if media_path and Path(media_path).exists():
                try:
                    if media_type == "video":
                        with open(media_path, "rb") as media:
                            await self.app.bot.send_video(
                                chat_id=self.settings.publish_channel_id,
                                video=media,
                                caption=post_text if len(post_text) <= 1000 else None,
                                parse_mode="HTML" if len(post_text) <= 1000 else None,
                            )
                        if len(post_text) > 1000:
                            await self.app.bot.send_message(
                                chat_id=self.settings.publish_channel_id,
                                text=post_text,
                                parse_mode="HTML",
                            )
                    elif media_type == "photo":
                        with open(media_path, "rb") as media:
                            await self.app.bot.send_photo(
                                chat_id=self.settings.publish_channel_id,
                                photo=media,
                                caption=post_text if len(post_text) <= 1000 else None,
                                parse_mode="HTML" if len(post_text) <= 1000 else None,
                            )
                        if len(post_text) > 1000:
                            await self.app.bot.send_message(
                                chat_id=self.settings.publish_channel_id,
                                text=post_text,
                                parse_mode="HTML",
                            )
                    published_with_media = True
                except Exception:
                    published_with_media = False

            if image_url and not published_with_media:
                photo = await self.download_photo(image_url)
                if photo:
                    try:
                        if len(post_text) <= 1000:
                            await self.app.bot.send_photo(
                                chat_id=self.settings.publish_channel_id,
                                photo=photo,
                                caption=post_text,
                                parse_mode="HTML",
                            )
                        else:
                            await self.app.bot.send_photo(
                                chat_id=self.settings.publish_channel_id,
                                photo=photo,
                            )
                            await self.app.bot.send_message(
                                chat_id=self.settings.publish_channel_id,
                                text=post_text,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        published_with_media = True
                    except Exception:
                        published_with_media = False

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
        if media_path:
            try:
                Path(media_path).unlink(missing_ok=True)
            except Exception:
                pass
        await query.edit_message_reply_markup(reply_markup=None)
