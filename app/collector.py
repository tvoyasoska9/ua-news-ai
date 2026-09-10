import asyncio
import hashlib
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
import feedparser
from bs4 import BeautifulSoup

from app.models import RawNews
from app.sources import RSS_SOURCES, TELEGRAM_SOURCES

log = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = 24000

# Real-time first. We inspect only a small recent window on every poll so a
# historical backlog cannot flood moderation.
TELEGRAM_POST_LIMIT = 20
TELEGRAM_MAX_AGE_HOURS = 1.5
TELEGRAM_MAX_PER_CHANNEL_PER_POLL = 4

MIN_IMAGE_BYTES = 10_000
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}


def clean_html(text):
    soup = BeautifulSoup(text or "", "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def fingerprint(news):
    # URL is checked separately by the database. This catches exact reposts
    # with the same normalized headline even when the URL changes.
    normalized = re.sub(r"\W+", " ", news.title.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_datetime(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def extract_rss_date(entry):
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        normalized = normalize_datetime(value)
        if normalized:
            return normalized
    return None


def meta_content(soup, keys):
    for key in keys:
        tag = soup.find("meta", attrs={"property": key})
        if tag and tag.get("content"):
            return html.unescape(tag["content"]).strip()
        tag = soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            return html.unescape(tag["content"]).strip()
    return None


def extract_meta_date(soup):
    candidates = [
        meta_content(soup, ["article:published_time"]),
        meta_content(soup, ["og:published_time"]),
        meta_content(soup, ["date", "datePublished", "publishdate", "pubdate"]),
    ]
    for value in candidates:
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            normalized = normalize_datetime(value)
            if normalized:
                return normalized
    return None


def extract_image_candidates(entry, soup, page_url):
    candidates = []

    for key in ("media_content", "media_thumbnail"):
        values = entry.get(key, [])
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if isinstance(value, dict) and value.get("url"):
                candidates.append(value["url"])

    for enclosure in entry.get("enclosures", []) or []:
        if isinstance(enclosure, dict):
            media_url = enclosure.get("href") or enclosure.get("url")
            media_type = (enclosure.get("type") or "").lower()
            if media_url and (not media_type or media_type.startswith("image/")):
                candidates.append(media_url)

    for key in (
        "og:image:secure_url",
        "og:image",
        "twitter:image:src",
        "twitter:image",
    ):
        value = meta_content(soup, [key])
        if value:
            candidates.append(value)

    article = soup.find("article") or soup.find("main") or soup.body
    if article:
        for img in article.find_all("img"):
            for attr in (
                "src",
                "data-src",
                "data-original",
                "data-lazy-src",
                "data-image",
            ):
                value = img.get(attr)
                if value:
                    candidates.append(value)

            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                for part in srcset.split(","):
                    value = part.strip().split(" ")[0]
                    if value:
                        candidates.append(value)

    unique = []
    seen = set()
    for candidate in candidates:
        candidate = html.unescape(str(candidate)).strip()
        if not candidate or candidate.startswith("data:"):
            continue
        media_url = urljoin(page_url, candidate)
        if media_url.startswith(("http://", "https://")) and media_url not in seen:
            seen.add(media_url)
            unique.append(media_url)

    return unique


def extract_article_text(soup):
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()

    root = soup.find("article")
    if root is None:
        root = soup.find("main")
    if root is None:
        candidates = soup.find_all(["div", "section"])
        root = max(candidates, key=lambda x: len(x.get_text(" ", strip=True)), default=soup.body)

    if root is None:
        return ""

    paragraphs = []
    for node in root.find_all(["p", "h2", "h3", "li"]):
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) >= 40:
            paragraphs.append(text)

    text = "\n\n".join(paragraphs)
    if len(text) < 250:
        text = " ".join(root.get_text(" ", strip=True).split())

    return text[:MAX_ARTICLE_CHARS]


async def fetch_image_info(session, url):
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
            headers=REQUEST_HEADERS,
        ) as response:
            if response.status >= 400:
                return None

            content_type = response.headers.get("Content-Type", "").lower()
            if not content_type.startswith("image/"):
                return None

            length = int(response.headers.get("Content-Length") or 0)
            if length and length < MIN_IMAGE_BYTES:
                return None

            chunk = await response.content.read(1024)
            if len(chunk) < 64:
                return None

            return (length or len(chunk), str(response.url))
    except Exception:
        return None


async def choose_best_image(session, candidates):
    if not candidates:
        return None

    checks = await asyncio.gather(
        *(fetch_image_info(session, url) for url in candidates[:8]),
        return_exceptions=True,
    )

    valid = [item for item in checks if isinstance(item, tuple)]
    if not valid:
        return None

    valid.sort(key=lambda item: item[0], reverse=True)
    return valid[0][1]


async def fetch_article(session, entry, url):
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=18),
            allow_redirects=True,
            headers=REQUEST_HEADERS,
        ) as response:
            if response.status >= 400:
                return "", None, None

            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type:
                return "", None, None

            page_html = await response.text(errors="ignore")
            final_url = str(response.url)
    except Exception:
        return "", None, None

    soup = BeautifulSoup(page_html, "html.parser")
    text = extract_article_text(soup)
    published_at = extract_meta_date(soup)
    candidates = extract_image_candidates(entry, soup, final_url)
    image_url = await choose_best_image(session, candidates)
    return text, published_at, image_url


async def fetch_source(session, source):
    try:
        async with session.get(
            source["url"],
            timeout=aiohttp.ClientTimeout(total=25),
        ) as response:
            response.raise_for_status()
            body = await response.text()
    except Exception:
        log.exception("Failed to fetch RSS source: %s", source.get("name"))
        return []

    feed = feedparser.parse(body)
    entries = feed.entries[:30]

    async def build_news(entry):
        title = clean_html(entry.get("title", ""))
        url = (entry.get("link") or "").strip()
        summary = clean_html(entry.get("summary") or entry.get("description") or "")

        if not title or not url:
            return None

        article_text, article_date, image_url = await fetch_article(session, entry, url)

        material = article_text if len(article_text) >= 200 else summary
        if not material:
            material = title

        return RawNews(
            title=title,
            summary=material,
            url=url,
            source=source["name"],
            priority=source.get("priority", 5),
            image_url=image_url,
            published_at=article_date or extract_rss_date(entry),
        )

    items = await asyncio.gather(
        *(build_news(entry) for entry in entries),
        return_exceptions=True,
    )
    return [item for item in items if isinstance(item, RawNews)]


async def collect_rss_news():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; UA-News-AI/1.2)"
    }
    connector = aiohttp.TCPConnector(limit=20)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        headers=headers,
        connector=connector,
        timeout=timeout,
    ) as session:
        groups = await asyncio.gather(
            *(fetch_source(session, source) for source in RSS_SOURCES),
            return_exceptions=True,
        )

    return [
        item
        for group in groups
        if isinstance(group, list)
        for item in group
    ]


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


async def fetch_telegram_source(client, source):
    """
    Collect Telegram posts while treating every grouped_id album as ONE news item.
    All photos/videos belonging to the album are downloaded and kept in order.
    """
    username = source["username"]
    try:
        entity = await client.get_entity(username)
        messages = await client.get_messages(entity, limit=TELEGRAM_POST_LIMIT)
    except Exception:
        log.exception("Failed to read Telegram source: @%s", username)
        return []

    now_utc = datetime.now(timezone.utc)

    # Build logical posts: grouped_id => one album; ordinary messages stay alone.
    groups = []
    by_group = {}
    for message in messages:
        if message.date:
            msg_time = message.date.astimezone(timezone.utc)
            if msg_time < now_utc - timedelta(hours=TELEGRAM_MAX_AGE_HOURS):
                continue

        key = ("album", message.grouped_id) if message.grouped_id else ("message", message.id)
        if key not in by_group:
            by_group[key] = []
            groups.append(by_group[key])
        by_group[key].append(message)

    items = []
    media_count = 0

    for group in groups:
        # Telegram may return album parts in reverse/newest order. Preserve the
        # original message order for the published media group.
        group.sort(key=lambda m: m.id)
        text = ""
        for message in group:
            candidate = telegram_formatted_text(message)
            if candidate:
                text = candidate
                break

        has_media = any(m.media for m in group)
        if not text and not has_media:
            continue

        first = group[0]
        published_at = (
            min(m.date for m in group if m.date).astimezone(timezone.utc).isoformat()
            if any(m.date for m in group) else None
        )
        message_id = first.id
        url = f"https://t.me/{username}/{message_id}"

        media_paths = []
        media_types = []
        media_dir = Path(os.getenv("TELEGRAM_MEDIA_DIR", "/tmp/ua-news-media"))
        media_dir.mkdir(parents=True, exist_ok=True)

        for index, message in enumerate(group, start=1):
            media_type = None
            if message.photo:
                media_type = "photo"
            elif message.video or (
                message.document
                and getattr(message.document, "mime_type", "").startswith("video/")
            ):
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
                    media_count += 1
            except Exception:
                log.exception(
                    "Failed to download %s from @%s message %s",
                    media_type, username, message.id
                )

        # The current editor needs factual text. A pure media post cannot be
        # safely rewritten, so clean downloaded files and skip it.
        if not text:
            for path in media_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass
            continue

        title = " ".join(BeautifulSoup(text, "html.parser").get_text(" ", strip=True).split())[:180]
        primary_type = (
            "album" if len(media_paths) > 1
            else (media_types[0] if media_types else None)
        )

        items.append(
            RawNews(
                title=title,
                summary=text[:MAX_ARTICLE_CHARS],
                url=url,
                source=f"Telegram: @{username}",
                priority=source.get("priority", 100),
                image_url=None,
                published_at=published_at,
                media_type=primary_type,
                media_path=media_paths[0] if media_paths else None,
                media_paths=media_paths,
                media_types=media_types,
            )
        )

        if len(items) >= TELEGRAM_MAX_PER_CHANNEL_PER_POLL:
            break

    log.info(
        "Telegram source @%s checked: %s logical posts collected, %s media files preserved",
        username, len(items), media_count
    )
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
    rss_task = asyncio.create_task(collect_rss_news())
    telegram_task = asyncio.create_task(collect_telegram_news(settings))

    rss_items, telegram_items = await asyncio.gather(
        rss_task,
        telegram_task,
        return_exceptions=True,
    )

    if isinstance(rss_items, Exception):
        log.exception("RSS collection failed", exc_info=rss_items)
        rss_items = []
    if isinstance(telegram_items, Exception):
        log.exception("Telegram collection failed", exc_info=telegram_items)
        telegram_items = []

    log.info(
        "Source collection complete: %s Telegram candidates (PRIMARY), %s RSS candidates (SECONDARY)",
        len(telegram_items), len(rss_items)
    )
    return list(telegram_items) + list(rss_items)
