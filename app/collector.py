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
from bs4 import BeautifulSoup

from app.models import RawNews
from app.sources import TELEGRAM_SOURCES

log = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = int(os.getenv("MAX_ARTICLE_CHARS", "5000"))
RSS_POST_LIMIT = 10

# Real-time first. We inspect only a small recent window on every poll so a
# historical backlog cannot flood moderation.
TELEGRAM_POST_LIMIT = 20
TELEGRAM_MAX_AGE_HOURS = 1.5
TELEGRAM_MAX_PER_CHANNEL_PER_POLL = 4

MIN_IMAGE_BYTES = 10_000
# RSS feeds repeat the same article URLs on every poll. Keep successful article
# extraction in memory so a one-minute polling interval does not re-download
# unchanged pages over and over.
ARTICLE_CACHE_TTL_SECONDS = 30 * 60
_article_cache = {}
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
    now = asyncio.get_running_loop().time()
    cached = _article_cache.get(url)
    if cached and cached[0] > now:
        return cached[1]

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
    result = (text, published_at, image_url)
    _article_cache[url] = (now + ARTICLE_CACHE_TTL_SECONDS, result)

    # Prevent an extremely long-running process from retaining an unlimited
    # number of old URLs in memory.
    if len(_article_cache) > 2000:
        expired = [key for key, value in _article_cache.items() if value[0] <= now]
        for key in expired:
            _article_cache.pop(key, None)
    return result


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
    entries = feed.entries[:RSS_POST_LIMIT]

    async def build_news(entry):
        title = clean_html(entry.get("title", ""))
        url = (entry.get("link") or "").strip()
        summary = clean_html(entry.get("summary") or entry.get("description") or "")

        if not title or not url:
            return None

        # Keep RSS collection cheap: the feed itself already gives us enough
        # metadata for URL/title/date duplicate screening. Do not download the
        # full article or probe images until this candidate actually survives
        # all local filters and is selected for AI processing.
        material = summary or title

        return RawNews(
            title=title,
            summary=material,
            url=url,
            source=source["name"],
            priority=source.get("priority", 5),
            published_at=extract_rss_date(entry),
            rss_entry=entry,
        )

    items = await asyncio.gather(
        *(build_news(entry) for entry in entries),
        return_exceptions=True,
    )
    return [item for item in items if isinstance(item, RawNews)]


async def materialize_rss_article(news):
    """Download the full RSS article only after the candidate passes local screening."""
    if news.rss_entry is None:
        return news

    headers = {"User-Agent": "Mozilla/5.0 (compatible; UA-News-AI/1.2)"}
    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=8)

    async with aiohttp.ClientSession(
        headers=headers,
        connector=connector,
        timeout=timeout,
    ) as session:
        article_text, article_date, image_url = await fetch_article(
            session,
            news.rss_entry,
            news.url,
        )

    if article_text:
        news.summary = article_text
    if article_date and not news.published_at:
        news.published_at = article_date
    if image_url:
        news.image_url = image_url

    # Drop the feed entry after materialization so queued objects do not retain
    # unnecessary parser metadata in memory.
    news.rss_entry = None
    log.info("Materialized full RSS article only for selected candidate: %s", news.url)
    return news


async def materialize_news(news):
    """Materialize selected Telegram media only after local screening."""
    await materialize_telegram_media(news)
    return news


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
