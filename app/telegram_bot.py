from datetime import datetime
from html import escape
from io import BytesIO
from uuid import uuid4

import aiohttp

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from app.models import EditedNews


class NewsBot:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self.pending = {}

        self.app.add_handler(CallbackQueryHandler(self.callback))
        self.app.add_handler(CommandHandler("status", self.status))
        self.app.add_handler(CommandHandler("sources", self.sources))
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

    def moderation_text(self, item, published_at=None, source=None):
        sources = "\n".join("• " + u for u in item.source_urls) or "• —"

        return (
            "🟡 <b>НА ПЕРЕВІРКУ</b>\n\n"
            f"🇺🇦 <b>{escape(item.title)}</b>\n\n"
            f"{escape(item.text)}\n\n"
            f"📅 Опубліковано: <b>{escape(self.format_date(published_at))}</b>\n"
            f"🌐 Джерело: <b>{escape(source or 'Невідомо')}</b>\n"
            f"📊 Важливість: <b>{item.importance}/10</b>\n"
            f"📂 Категорія: {escape(item.category)}\n"
            f"🔍 Впевненість: {escape(item.confidence)}\n\n"
            f"🔗 Оригінал:\n{escape(sources)}\n\n"
            f'<a href="https://t.me/ukr24live">УКРАЇНА 🇺🇦 LIVE 24</a>'
        )

    def publish_text(self, item):
        return (
            f"<b>{escape(item.title)}</b>\n\n"
            f"{escape(item.text)}\n\n"
            f'<a href="https://t.me/ukr24live">УКРАЇНА 🇺🇦 LIVE 24</a>'
        )

    def keyboard(self, item_id):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{item_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{item_id}"),
        ]])

    async def download_photo(self, image_url):
        """Download the image ourselves so Telegram does not have to hotlink it."""
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
    ):
        item_id = str(uuid4())
        self.pending[item_id] = (item, url, image_url)
        keyboard = self.keyboard(item_id)
        text = self.moderation_text(item, published_at, source)

        # Фото з підписом обмежене Telegram 1024 символами.
        # Якщо службовий текст довший — фото і модерація надсилаються окремо.
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
                self.pending[item_id] = (item, url, None)

        if photo:
            try:
                photo.seek(0)
                await self.app.bot.send_photo(
                    chat_id=self.settings.moderation_chat_id,
                    photo=photo,
                )
            except Exception:
                self.pending[item_id] = (item, url, None)

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

        item, url, image_url = payload

        if action == "publish":
            published_with_photo = False

            post_text = self.publish_text(item)

            if image_url:
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
                        published_with_photo = True
                    except Exception:
                        published_with_photo = False

            if not published_with_photo:
                await self.app.bot.send_message(
                    chat_id=self.settings.publish_channel_id,
                    text=post_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

            self.db.set_status(url, "published")
            await query.message.reply_text("✅ Опубліковано.")

        else:
            self.db.set_status(url, "rejected")
            await query.message.reply_text("❌ Відхилено.")

        self.pending.pop(item_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
