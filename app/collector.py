import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from app.models import RawNews
from app.sources import TELEGRAM_SOURCES

log = logging.getLogger(__name__)
TELEGRAM_POST_LIMIT = 100
MAX_NEWS_AGE_MINUTES = 60
_telegram_client = None
_telegram_lock = asyncio.Lock()

PROMO_RE = re.compile(r"(?iu)^(?:.*(?:підписатись|підписатися|подписаться|subscribe|надіслати новину|прислать новость|send news).*)$")

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
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

def _plain_message_text(message):
    raw = (message.message or "").strip()
    return BeautifulSoup(raw, "html.parser").get_text("\n") if raw else ""

def _quote_texts(message):
    quotes = []
    try:
        for entity, text in message.get_entities_text():
            if "blockquote" in entity.__class__.__name__.lower():
                cleaned = clean_source_text(text)
                if cleaned:
                    quotes.append(re.sub(r"\s+", " ", cleaned).strip())
    except Exception:
        pass
    return quotes

def _message_blocks(message, username):
    text = clean_source_text(_plain_message_text(message), username)
    if not text:
        return []
    quote_norm = _quote_texts(message)
    blocks = []
    for paragraph in re.split(r"\n\s*\n+", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        normalized = re.sub(r"\s+", " ", paragraph).strip()
        is_quote = any(normalized == q or normalized in q or q in normalized for q in quote_norm if q)
        blocks.append({"type": "quote" if is_quote else "normal", "text": paragraph})
    return blocks

def _title_from_blocks(blocks):
    for block in blocks:
        if block["type"] == "normal" and block["text"].strip():
            return re.sub(r"\s+", " ", block["text"]).strip()[:300]
    return re.sub(r"\s+", " ", blocks[0]["text"]).strip()[:300] if blocks else ""

def _media_suffix(message, kind):
    if kind == "photo":
        return ".jpg"

    # Do not blindly rename every source video to .mp4. Telegram channels can
    # contain video documents with another real container/extension; forcing an
    # MP4 suffix can make downstream upload/inspection mis-detect the media.
    name = ""
    try:
        name = str(getattr(getattr(message, "file", None), "name", "") or "")
    except Exception:
        pass
    suffix = Path(name).suffix.lower()
    if suffix in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}:
        return suffix

    mime = ""
    try:
        mime = str(getattr(getattr(message, "document", None), "mime_type", "") or "").lower()
    except Exception:
        pass
    return {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
        "video/x-matroska": ".mkv",
        "video/x-msvideo": ".avi",
    }.get(mime, ".mp4")

async def fetch_telegram_source(client, source):
    # Strict rolling window: only source posts published within the last
    # 60 minutes from the actual moment of collection are eligible.
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=MAX_NEWS_AGE_MINUTES)
    username = source["username"]
    try:
        entity = await client.get_entity(username)
        messages = await client.get_messages(entity, limit=TELEGRAM_POST_LIMIT)
    except Exception:
        log.exception("Failed to read @%s", username)
        return []

    groups, grouped = [], {}
    for message in messages:
        key = ("album", message.grouped_id) if message.grouped_id else ("message", message.id)
        if key not in grouped:
            grouped[key] = []
            groups.append(grouped[key])
        grouped[key].append(message)

    items = []
    for group in groups:
        group.sort(key=lambda m: m.id)
        blocks = []
        for message in group:
            for block in _message_blocks(message, username):
                if block["text"] and (not blocks or block != blocks[-1]):
                    blocks.append(block)
        if not blocks:
            continue

        summary = "\n\n".join(block["text"] for block in blocks)
        first = group[0]
        published_dt = min((m.date for m in group if m.date), default=None)
        if published_dt:
            if published_dt.tzinfo is None:
                published_dt = published_dt.replace(tzinfo=timezone.utc)
            else:
                published_dt = published_dt.astimezone(timezone.utc)
            if published_dt < cutoff or published_dt > now:
                continue
        published_at = published_dt.isoformat() if published_dt else None
        media_messages = [
            m for m in group
            if m.photo or m.video or (m.document and str(getattr(m.document, "mime_type", "")).startswith("video/"))
        ]
        items.append(RawNews(
            title=_title_from_blocks(blocks),
            summary=summary,
            url=f"https://t.me/{username}/{first.id}",
            source=f"Telegram: @{username}",
            published_at=published_at,
            media_messages=media_messages,
            blocks=blocks,
        ))
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
        suffix = _media_suffix(message, kind)
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
