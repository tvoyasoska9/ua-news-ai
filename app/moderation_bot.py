import logging
from datetime import datetime
from html import escape
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import Application, CallbackQueryHandler

log = logging.getLogger(__name__)
KYIV = ZoneInfo("Europe/Kyiv")
CAPTION_LIMIT = 1024
CHANNEL_NAME = "УКРАЇНА 🇺🇦 LIVE 24"
CHANNEL_HANDLE = "ukr24live"

class ModerationBot:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self.app.add_handler(CallbackQueryHandler(self.callback))

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

    def keyboard(self, item_id):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("PUBLISH", callback_data="publish:" + item_id),
            InlineKeyboardButton("REJECT", callback_data="reject:" + item_id)
        ]])

    def render(self, title, text, include_signature=False):
        title = escape(str(title or ""), quote=False)
        body = str(text or "").strip()
        post = f"🇺🇦 <b>{title}</b>" + (f"\n\n{body}" if body else "")
        if include_signature:
            post += f"\n\n<a href=\"https://t.me/{CHANNEL_HANDLE}\"><b>{CHANNEL_NAME}</b></a>"
        return post

    def source_time_ukraine(self, raw):
        value = getattr(raw, "published_at", None)
        if not value:
            return "невідомо"
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KYIV)
            return dt.astimezone(KYIV).strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            return str(value)

    def moderator_info(self, raw):
        return (
            "\n\n━━━━━━━━━━━━━━━━\n"
            f"🕒 <b>Опубліковано в Україні:</b> {self.source_time_ukraine(raw)}\n"
            f"🔗 <a href=\"{escape(str(raw.url), quote=True)}\">Оригінальний пост</a>"
        )

    def _media_from_paths(self, raw):
        return [(p, k) for p, k in zip(raw.media_paths or [], raw.media_types or [])
                if p and Path(p).exists() and k in {"photo", "video"}]

    def _cached_from_messages(self, messages, media):
        cached = []
        for message, (_, kind) in zip(messages, media):
            if kind == "video" and message.video:
                cached.append((message.video.file_id, kind))
            elif kind == "photo" and message.photo:
                cached.append((message.photo[-1].file_id, kind))
        return cached

    async def _send_local_media(self, chat_id, media, caption, reply_markup=None):
        if not media:
            message = await self.app.bot.send_message(
                chat_id, caption, parse_mode="HTML", reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            return [], message

        if len(media) == 1 and len(caption) <= CAPTION_LIMIT:
            path, kind = media[0]
            with open(path, "rb") as handle:
                if kind == "photo":
                    message = await self.app.bot.send_photo(
                        chat_id, handle, caption=caption, parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                else:
                    message = await self.app.bot.send_video(
                        chat_id, handle, caption=caption, parse_mode="HTML",
                        reply_markup=reply_markup
                    )
            return self._cached_from_messages([message], media), message

        handles = []
        try:
            payload = []
            for index, (path, kind) in enumerate(media):
                handle = open(path, "rb")
                handles.append(handle)
                kwargs = {"caption": caption, "parse_mode": "HTML"} if index == 0 and len(caption) <= CAPTION_LIMIT else {}
                payload.append(InputMediaVideo(handle, **kwargs) if kind == "video" else InputMediaPhoto(handle, **kwargs))
            messages = await self.app.bot.send_media_group(chat_id, payload)
            cached = self._cached_from_messages(messages, media)
        finally:
            for handle in handles:
                handle.close()

        if reply_markup:
            if len(caption) > CAPTION_LIMIT:
                await self.app.bot.send_message(
                    chat_id, caption, parse_mode="HTML", reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            else:
                await self.app.bot.send_message(
                    chat_id, "🛠 <b>Модерація поста</b>", parse_mode="HTML",
                    reply_markup=reply_markup
                )
        elif len(caption) > CAPTION_LIMIT:
            await self.app.bot.send_message(chat_id, caption, parse_mode="HTML", disable_web_page_preview=True)

        return cached, messages[0] if messages else None

    async def send_for_moderation(self, item, raw):
        item_id = str(uuid4())
        media = self._media_from_paths(raw)
        caption = self.render(item.title, item.text) + self.moderator_info(raw)
        cached, _ = await self._send_local_media(
            self.settings.moderation_chat_id, media, caption, self.keyboard(item_id)
        )
        # Keep the original downloaded source files for publication. The
        # moderation copy is only a preview/cache; publishing from its file_id
        # can use Telegram's processed variant instead of the original upload.
        self.db.save_pending(
            item_id, item.title, item.text, raw.url,
            {
                "cached": cached,
                "local": [[path, kind] for path, kind in media],
            },
        )

    async def _publish_media(self, media, caption):
        # IMPORTANT: prefer the untouched files downloaded directly from the
        # source channel. The Bot API file_id created for moderation can refer
        # to Telegram's processed/transcoded copy.
        local = []
        cached = []

        if isinstance(media, dict):
            local = [
                (str(entry[0]), str(entry[1]))
                for entry in (media.get("local") or [])
                if isinstance(entry, (list, tuple)) and len(entry) == 2
                and Path(str(entry[0])).exists()
                and str(entry[1]) in {"photo", "video"}
            ]
            cached = [
                (str(entry[0]), str(entry[1]))
                for entry in (media.get("cached") or [])
                if isinstance(entry, (list, tuple)) and len(entry) == 2
                and str(entry[1]) in {"photo", "video"}
            ]
        else:
            # Backward compatibility with already-created moderation cards.
            cached = list(media or [])

        if local:
            # Re-upload the original source bytes directly to the publication
            # channel. Do not reuse the moderation preview's cached file_id.
            await self._send_local_media(
                self.settings.publish_channel_id, local, caption, None
            )
            return

        if not cached:
            await self.app.bot.send_message(
                self.settings.publish_channel_id, caption,
                parse_mode="HTML", disable_web_page_preview=True
            )
            return

        if len(cached) == 1 and len(caption) <= CAPTION_LIMIT:
            fid, kind = cached[0]
            if kind == "photo":
                await self.app.bot.send_photo(
                    self.settings.publish_channel_id, fid, caption=caption, parse_mode="HTML"
                )
            else:
                await self.app.bot.send_video(
                    self.settings.publish_channel_id, fid, caption=caption,
                    parse_mode="HTML", supports_streaming=True
                )
            return

        payload = []
        for index, (fid, kind) in enumerate(cached):
            kwargs = {"caption": caption, "parse_mode": "HTML"} if index == 0 and len(caption) <= CAPTION_LIMIT else {}
            payload.append(
                InputMediaVideo(fid, supports_streaming=True, **kwargs)
                if kind == "video" else InputMediaPhoto(fid, **kwargs)
            )
        await self.app.bot.send_media_group(self.settings.publish_channel_id, payload)
        if len(caption) > CAPTION_LIMIT:
            await self.app.bot.send_message(
                self.settings.publish_channel_id, caption,
                parse_mode="HTML", disable_web_page_preview=True
            )

    def _cleanup_original_media(self, media):
        if not isinstance(media, dict):
            return
        for entry in media.get("local") or []:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            try:
                Path(str(entry[0])).unlink(missing_ok=True)
            except Exception:
                log.exception("failed to remove temporary source media")

    async def callback(self, update, context):
        q = update.callback_query
        if not q or q.message.chat_id != self.settings.moderation_chat_id:
            return

        action, item_id = q.data.split(":", 1)
        data = self.db.get_pending(item_id)
        if not data:
            await q.answer("Already processed")
            return

        await q.answer()
        if action == "reject":
            self.db.set_status(data["url"], "rejected")
            self._cleanup_original_media(data.get("media"))
            self.db.delete_pending(item_id)
            await q.edit_message_reply_markup(reply_markup=None)
            return
        if action != "publish":
            return

        try:
            await self._publish_media(
                data["media"] or [],
                self.render(data["title"], data["text"], include_signature=True),
            )
            self.db.set_status(data["url"], "published")
            self._cleanup_original_media(data.get("media"))
            self.db.delete_pending(item_id)
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            log.exception("publish failed")
