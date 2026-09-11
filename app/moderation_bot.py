import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton,InlineKeyboardMarkup,InputMediaPhoto,InputMediaVideo,Update
from telegram.ext import Application,CallbackQueryHandler

log=logging.getLogger(__name__)
KYIV=ZoneInfo("Europe/Kyiv")

class ModerationBot:
    def __init__(self,settings,db):
        self.settings=settings
        self.db=db
        self.app=Application.builder().token(settings.telegram_bot_token).build()
        self.app.add_handler(CallbackQueryHandler(self.callback))

    async def start(self):
        await self.app.initialize(); await self.app.start()
        if self.app.updater: await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def stop(self):
        if self.app.updater: await self.app.updater.stop()
        await self.app.stop(); await self.app.shutdown()

    def keyboard(self,item_id):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("PUBLISH",callback_data=f"publish:{item_id}"),
            InlineKeyboardButton("REJECT",callback_data=f"reject:{item_id}")
        ]])

    def render(self,title,text):
        return f"🇺🇦 <b>{title}</b>\n\n{text}"

    def source_time_ukraine(self, raw):
        value=getattr(raw,"published_at",None)
        if not value:
            return "невідомо"
        try:
            dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
            if dt.tzinfo is None:
                dt=dt.replace(tzinfo=KYIV)
            return dt.astimezone(KYIV).strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            return str(value)

    def moderator_info(self, raw):
        published=self.source_time_ukraine(raw)
        return (
            "\n\n━━━━━━━━━━━━━━━━\n"
            f"🕒 <b>Опубліковано в Україні:</b> {published}\n"
            f"🔗 <a href=\"{raw.url}\">Оригінальний пост</a>"
        )

    async def send_for_moderation(self,item,raw):
        media=[(p,k) for p,k in zip(raw.media_paths or [],raw.media_types or []) if p and Path(p).exists() and k in {"photo","video"}]
        cached=[]
        for start in range(0,len(media),10):
            chunk=media[start:start+10]; handles=[]
            try:
                payload=[]
                for path,kind in chunk:
                    h=open(path,"rb"); handles.append(h)
                    payload.append(InputMediaVideo(h) if kind=="video" else InputMediaPhoto(h))
                messages=await self.app.bot.send_media_group(self.settings.moderation_chat_id,payload)
                for message,(_,kind) in zip(messages,chunk):
                    fid=message.video.file_id if kind=="video" and message.video else (message.photo[-1].file_id if kind=="photo" and message.photo else None)
                    if fid: cached.append((fid,kind))
            finally:
                for h in handles: h.close()

        item_id=str(uuid4())
        self.db.save_pending(item_id,item.title,item.text,raw.url,cached)

        text=self.render(item.title,item.text)+self.moderator_info(raw)
        await self.app.bot.send_message(
            self.settings.moderation_chat_id,
            text,
            parse_mode="HTML",
            reply_markup=self.keyboard(item_id),
            disable_web_page_preview=True,
        )

    async def callback(self,update,context):
        q=update.callback_query
        if not q or q.message.chat_id!=self.settings.moderation_chat_id: return
        action,item_id=q.data.split(":",1); data=self.db.get_pending(item_id)
        if not data:
            await q.answer("Already processed")
            return
        await q.answer()
        if action=="reject":
            self.db.set_status(data["url"],"rejected")
            self.db.delete_pending(item_id)
            await q.edit_message_reply_markup(reply_markup=None)
            return
        try:
            media=data["media"] or []
            for start in range(0,len(media),10):
                chunk=media[start:start+10]
                await self.app.bot.send_media_group(
                    self.settings.publish_channel_id,
                    [InputMediaVideo(fid) if kind=="video" else InputMediaPhoto(fid) for fid,kind in chunk]
                )
            await self.app.bot.send_message(
                self.settings.publish_channel_id,
                self.render(data["title"],data["text"]),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            self.db.set_status(data["url"],"published")
            self.db.delete_pending(item_id)
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            log.exception("publish failed")
