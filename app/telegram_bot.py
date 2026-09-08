from html import escape
from uuid import uuid4
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
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
        return bool(update.effective_chat and update.effective_chat.id == self.settings.moderation_chat_id)

    async def status(self, update, context):
        if self.allowed(update):
            await update.effective_message.reply_text("🟢 UA News AI працює.")

    async def sources(self, update, context):
        if self.allowed(update):
            from app.sources import RSS_SOURCES
            await update.effective_message.reply_text("📰 Джерела:\n" + "\n".join("• " + s["name"] for s in RSS_SOURCES))

    async def testnews(self, update, context):
        if self.allowed(update):
            item = EditedNews("Тестова новина", "Це тест системи модерації.", "Тест", 5, "high", [])
            await self.send_for_moderation(item, "test://" + str(uuid4()))

    def moderation_text(self, item):
        sources = "\n".join("• " + u for u in item.source_urls) or "• —"
        return (
            "🟡 <b>НА ПЕРЕВІРКУ</b>\n\n"
            f"🇺🇦 <b>{escape(item.title)}</b>\n\n{escape(item.text)}\n\n"
            f"📊 Важливість: <b>{item.importance}/10</b>\n"
            f"📂 Категорія: {escape(item.category)}\n"
            f"🔍 Впевненість: {escape(item.confidence)}\n\n"
            f"🔗 Джерела:\n{escape(sources)}"
        )

    async def send_for_moderation(self, item, url):
        item_id = str(uuid4())
        self.pending[item_id] = (item, url)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish:{item_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{item_id}"),
        ]])
        await self.app.bot.send_message(
            self.settings.moderation_chat_id,
            self.moderation_text(item),
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
        item, url = payload
        if action == "publish":
            await self.app.bot.send_message(
                self.settings.publish_channel_id,
                f"<b>{escape(item.title)}</b>\n\n{escape(item.text)}",
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
