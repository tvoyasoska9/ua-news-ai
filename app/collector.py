import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from app.models import RawNews
from app.sources import TELEGRAM_SOURCES

log = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = int(os.getenv("MAX_ARTICLE_CHARS", "5000"))

# Real-time Telegram monitoring only.
TELEGRAM_POST_LIMIT = 20
TELEGRAM_MAX_AGE_HOURS = 1.5
TELEGRAM_MAX_PER_CHANNEL_PER_POLL = 4


def fingerprint(news):
    # URL is checked separately by the database. This catches exact reposts
    # with the same normalized headline even when the URL changes.
    normalized = re.sub(r"\W+", " ", news.title.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_telegram_client = None
_telegram_lock = asyncio.Lock()


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

        log.info("Telegram monitor connected successfully")
        return _telegram_client


def _trim_plain_to_sentence_boundary(value, limit):
    """Limit AI input without cutting a Telegram post in the middle of a sentence."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text

    boundary = max(text.rfind(mark, 0, limit + 1) for mark in ".!?…")
    if boundary >= max(40, int(limit * 0.45)):
        return text[:boundary + 1].rstrip()

    grace_end = min(len(text), limit + 240)
    candidates = [text.find(mark, limit, grace_end) for mark in ".!?…"]
    candidates = [pos for pos in candidates if pos != -1]
    if candidates:
        return text[:min(candidates) + 1].rstrip()

    # A broken fact is worse than a slightly longer prompt.
    return text


def telegram_formatted_text(message):
    """Return Telegram message text with its original formatting as safe HTML."""
    raw = (message.message or "").strip()
    if not raw:
        return ""
    try:
        from telethon.extensions import html as telethon_html
        return telethon_html.unparse(raw, message.entities or [])
    except Exception:
        return raw


_TELEGRAM_PROMO_RE = re.compile(
    r"(?iu)(?:"
    r"підписатись|підписатися|підписуйся(?:\s+на\s+[^\n|•]{1,80})?|"
    r"подписаться(?:\s+на\s+канал)?|subscribe(?:\s+now)?|"
    r"надіслати\s+новину|прислать\s+новость|send\s+news"
    r")"
)


def _clean_telegram_post_text(value, username=""):
    """Remove only trailing channel branding/CTA before the AI ever sees it.

    Telegram channels frequently append the channel name and a subscription or
    "send news" call-to-action on the same line as the factual post.  This is
    source noise, not news, so it must be removed at collection time rather
    than hoping the model will ignore it.
    """
    lines = [line.strip() for line in str(value or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    while lines:
        last = lines[-1]
        matches = list(_TELEGRAM_PROMO_RE.finditer(last))
        if not matches:
            break

        match = matches[-1]
        # Only a trailing CTA is promotional. Never delete factual text after a
        # CTA that appears in the middle of a legitimate sentence.
        tail = last[match.end():].strip(" \t.!…‼️❗️")
        if tail:
            break

        before = last[:match.start()].rstrip()
        if not before:
            lines.pop()
            continue

        # A standalone footer such as "Channel Name | Підписатись" may be
        # removed as a whole, but an inline footer can follow real factual text
        # on the very same line. Distinguish the two before deleting anything.
        if before.endswith(("|", "•")):
            label = before[:-1].strip()
            if label and len(label) <= 100 and not re.search(r"[.!?…]", label):
                lines.pop()
                continue

        # Inline footer after a complete factual sentence.
        boundary = max(before.rfind(mark) for mark in ".!?…")
        if boundary >= 0:
            lines[-1] = before[:boundary + 1].rstrip()
            break

        # No safe sentence boundary: remove only the CTA and keep the factual
        # part rather than dropping the entire post.
        lines[-1] = before.rstrip(" |•—–-")
        break

    # A source username can occasionally be copied as a standalone final line.
    if username:
        aliases = {
            username.lower().lstrip("@"),
            ("@" + username.lower().lstrip("@")),
        }
        while lines and lines[-1].lower().strip() in aliases:
            lines.pop()

    return "\n".join(lines).strip()


def _telegram_candidate_title(plain_text):
    """Build metadata from the first factual line without truncating the post."""
    lines = [line.strip() for line in str(plain_text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    sentence = _trim_plain_to_sentence_boundary(first, 260)
    if sentence:
        return " ".join(sentence.split())
    return " ".join(first.split())[:260].rstrip(" ,;:—–-")


async def _download_telegram_media(client, username, messages):
    media_paths, media_types = [], []
    media_dir = Path(os.getenv("TELEGRAM_MEDIA_DIR", "/tmp/ua-news-media"))
    media_dir.mkdir(parents=True, exist_ok=True)
    for index, message in enumerate(messages, start=1):
        media_type = "photo" if message.photo else None
        if message.video or (message.document and getattr(message.document, "mime_type", "").startswith("video/")):
            media_type = "video"
        if not media_type:
            continue
        try:
            suffix = ".mp4" if media_type == "video" else ".jpg"
            target = media_dir / f"{username}_{message.id}_{index}{suffix}"
            result = await client.download_media(message, file=str(target))
            if result:
                media_paths.append(str(result))
                media_types.append(media_type)
        except Exception:
            log.exception("Failed to download Telegram media from @%s message %s", username, message.id)
    return media_paths, media_types


async def materialize_telegram_media(news):
    if not news.source.startswith("Telegram:") or not news.media_messages:
        return news
    client = _telegram_client
    if client is None or not client.is_connected():
        raise RuntimeError("Telegram monitor is not connected")
    username = news.source.split("@", 1)[-1].strip()
    paths, types = await _download_telegram_media(client, username, news.media_messages)
    news.media_paths = paths
    news.media_types = types
    news.media_path = paths[0] if paths else None
    news.media_type = "album" if len(paths) > 1 else (types[0] if types else None)
    log.info("Downloaded %s media files only for selected candidate @%s", len(paths), username)
    return news


async def materialize_news(news):
    """Materialize selected Telegram media only after local screening."""
    await materialize_telegram_media(news)
    return news


async def fetch_telegram_source(client, source):
    """Collect text and metadata first; defer all media downloads."""
    username = source["username"]
    try:
        entity = await client.get_entity(username)
        messages = await client.get_messages(entity, limit=TELEGRAM_POST_LIMIT)
    except Exception:
        log.exception("Failed to read Telegram source: @%s", username)
        return []

    now_utc = datetime.now(timezone.utc)
    groups, by_group = [], {}
    for message in messages:
        if message.date and message.date.astimezone(timezone.utc) < now_utc - timedelta(hours=TELEGRAM_MAX_AGE_HOURS):
            continue
        key = ("album", message.grouped_id) if message.grouped_id else ("message", message.id)
        if key not in by_group:
            by_group[key] = []
            groups.append(by_group[key])
        by_group[key].append(message)

    items = []
    deferred_media = 0
    for group in groups:
        group.sort(key=lambda m: m.id)
        text = ""
        for message in group:
            candidate = telegram_formatted_text(message)
            if candidate:
                text = candidate
                break
        if not text:
            continue

        first = group[0]
        published_at = min((m.date for m in group if m.date), default=None)
        published_at = published_at.astimezone(timezone.utc).isoformat() if published_at else None
        supported_media = [
            m for m in group
            if m.photo or m.video or (m.document and getattr(m.document, "mime_type", "").startswith("video/"))
        ]
        deferred_media += len(supported_media)

        # Cleanup happens here, before title generation and before OpenAI.
        # This prevents channel branding such as "ТРУХА | Надіслати новину"
        # from contaminating either the metadata or the model input.
        plain_text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
        plain_text = _clean_telegram_post_text(plain_text, username)
        if not plain_text:
            continue

        title = _telegram_candidate_title(plain_text)
        summary = _trim_plain_to_sentence_boundary(plain_text, MAX_ARTICLE_CHARS)
        items.append(RawNews(
            title=title,
            summary=summary,
            url=f"https://t.me/{username}/{first.id}",
            source=f"Telegram: @{username}",
            priority=source.get("priority", 100),
            published_at=published_at,
            media_messages=supported_media,
        ))
        if len(items) >= TELEGRAM_MAX_PER_CHANNEL_PER_POLL:
            break

    log.info("Telegram source @%s checked: %s posts, %s media files deferred", username, len(items), deferred_media)
    return items

async def collect_telegram_news(settings):
    if not TELEGRAM_SOURCES:
        return []

    client = await get_telegram_client(settings)
    log.info("Telegram monitor checking %s equal-priority channels", len(TELEGRAM_SOURCES))

    groups = await asyncio.gather(
        *(fetch_telegram_source(client, source) for source in TELEGRAM_SOURCES),
        return_exceptions=True,
    )

    channel_groups = []
    for source, group in zip(TELEGRAM_SOURCES, groups):
        if isinstance(group, list):
            # Every channel is treated identically: same cap, same priority,
            # newest-first inside that channel.
            channel_groups.append(
                (
                    source["username"],
                    sorted(group, key=lambda x: x.published_at or "", reverse=True),
                )
            )
        else:
            log.error("Telegram source @%s failed during collection", source["username"])
            channel_groups.append((source["username"], []))

    # Strict round-robin collection: one item from each channel per round.
    items = []
    round_index = 0
    while True:
        added = False
        for username, group in channel_groups:
            if round_index < len(group):
                items.append(group[round_index])
                added = True
        if not added:
            break
        round_index += 1

    counts = ", ".join(f"@{u}={len(g)}" for u, g in channel_groups)
    log.info(
        "Telegram monitor collected %s candidates with equal round-robin rotation (%s)",
        len(items), counts
    )
    return items


async def collect_news(settings):
    """Collect news exclusively from configured Telegram channels."""
    telegram_items = await collect_telegram_news(settings)
    log.info(
        "Telegram-only collection complete: %s candidates from configured channels",
        len(telegram_items),
    )
    return list(telegram_items)

async def close_telegram_client():
    global _telegram_client
    client = _telegram_client
    _telegram_client = None
    if client is None:
        return
    try:
        if client.is_connected():
            await client.disconnect()
            log.info("Telegram monitor disconnected")
    except Exception:
        log.exception("Failed to disconnect Telegram monitor")
