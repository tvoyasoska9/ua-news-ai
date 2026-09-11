import asyncio
import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup

from app.models import RawNews
from app.sources import TELEGRAM_SOURCES

log = logging.getLogger(__name__)

TELEGRAM_POST_LIMIT = 100
_telegram_client = None
_telegram_lock = asyncio.Lock()

PROMO_RE = re.compile(
    r"(?iu)^(?:.*(?:підписатись|підписатися|подписаться|subscribe|надіслати новину|прислать новость|send news).*)$"
)

async def get_telegram_client(settings):
    global _telegram_client
    if _telegram_client and _telegram_client.is_connected():
        return _telegram_client

    async with _telegram_lock:
        if _telegram_client and _telegram_client.is_connected():
            return _telegram_client

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        _telegram_client = TelegramClient(
            StringSession(settings.telegram_session),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await _telegram_client.connect()
        if not await _telegram_client.is_user_authorized():
            raise RuntimeError("TELEGRAM_SESSION is not authorized")
        log.info("Telegram monitor connected")
        return _telegram_client

def clean_source_text(value, username=""):
    # Keep the news body and its paragraph structure. Remove only obvious
    # channel/service noise; do not delete factual lines because they are short.
    lines = []
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if PROMO_RE.match(line):
            continue
        if username and line.lower() in {username.lower(), "@" + username.lower()}:
            continue
        lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _plain_message_text(message):
    raw = (message.message or "").strip()
    if not raw:
        return ""
    return BeautifulSoup(raw, "html.parser").get_text("\n")

def _title(text):
    first = next((x.strip() for x in text.splitlines() if x.strip()), "")
    return re.sub(r"\s+", " ", first)[:300].strip()

async def fetch_telegram_source(client, source):
    username = source["username"]
    try:
        entity = await client.get_entity(username)
        messages = await client.get_messages(entity, limit=TELEGRAM_POST_LIMIT)
    except Exception:
        log.exception("Failed to read @%s", username)
        return []

    groups = []
    grouped = {}
    for message in messages:
        key = ("album", message.grouped_id) if message.grouped_id else ("message", message.id)
        if key not in grouped:
            grouped[key] = []
            groups.append(grouped[key])
        grouped[key].append(message)

    items = []
    for group in groups:
        group.sort(key=lambda m: m.id)

        # Keep every caption/text fragment from the original post/album.
        fragments = []
        for message in group:
            cleaned = clean_source_text(_plain_message_text(message), username)
            if cleaned and cleaned not in fragments:
                fragments.append(cleaned)

        text = "\n\n".join(fragments).strip()
        if not text:
            continue

        first = group[0]
        published_at = min((m.date for m in group if m.date), default=None)
        published_at = published_at.isoformat() if published_at else None

        media_messages = [
            m for m in group
            if m.photo
            or m.video
            or (m.document and str(getattr(m.document, "mime_type", "")).startswith("video/"))
        ]

        items.append(
            RawNews(
                title=_title(text),
                summary=text,
                url=f"https://t.me/{username}/{first.id}",
                source=f"Telegram: @{username}",
                published_at=published_at,
                media_messages=media_messages,
            )
        )

    return items

async def collect_news(settings):
    client = await get_telegram_client(settings)
    groups = await asyncio.gather(
        *(fetch_telegram_source(client, source) for source in TELEGRAM_SOURCES),
        return_exceptions=True,
    )

    items = []
    for source, result in zip(TELEGRAM_SOURCES, groups):
        if isinstance(result, Exception):
            log.error("Source @%s failed: %s", source["username"], result)
            continue
        items.extend(result)

    items.sort(key=lambda x: x.published_at or "", reverse=True)
    log.info("Collected %s source posts", len(items))
    return items

async def materialize_news(news):
    if not news.media_messages:
        return news

    client = _telegram_client
    if client is None or not client.is_connected():
        raise RuntimeError("Telegram monitor is not connected")

    username = news.source.split("@", 1)[-1].strip()
    media_dir = Path("/tmp/ua-news-media")
    media_dir.mkdir(parents=True, exist_ok=True)

    paths, types = [], []
    for index, message in enumerate(news.media_messages, start=1):
        kind = "photo" if message.photo else "video"
        suffix = ".jpg" if kind == "photo" else ".mp4"
        target = media_dir / f"{username}_{message.id}_{index}{suffix}"
        try:
            result = await client.download_media(message, file=str(target))
            if result:
                paths.append(str(result))
                types.append(kind)
        except Exception:
            log.exception("Failed to download media from @%s/%s", username, message.id)

    news.media_paths = paths
    news.media_types = types
    news.media_path = paths[0] if paths else None
    news.media_type = types[0] if len(types) == 1 else ("album" if types else None)
    return news

async def close_telegram_client():
    global _telegram_client
    client = _telegram_client
    _telegram_client = None
    if client and client.is_connected():
        await client.disconnect()
