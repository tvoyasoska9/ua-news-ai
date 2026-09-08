import asyncio
import hashlib
import html
import re
from urllib.parse import urljoin

import aiohttp
import feedparser

from app.models import RawNews
from app.sources import RSS_SOURCES


def clean_html(text):
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())


def fingerprint(news):
    normalized = re.sub(r"\W+", " ", news.title.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def extract_rss_image(entry):
    for key in ("media_thumbnail", "media_content"):
        values = entry.get(key, [])
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if isinstance(value, dict) and value.get("url"):
                return value["url"].strip()

    enclosures = entry.get("enclosures", [])
    if not isinstance(enclosures, list):
        enclosures = [enclosures]

    for enclosure in enclosures:
        if not isinstance(enclosure, dict):
            continue
        url = enclosure.get("href") or enclosure.get("url")
        media_type = (enclosure.get("type") or "").lower()
        if url and media_type.startswith("image/"):
            return url.strip()

    return None


def extract_meta_image(page_html, page_url):
    patterns = (
        r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']',
    )

    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE)
        if match:
            image_url = html.unescape(match.group(1)).strip()
            if image_url:
                return urljoin(page_url, image_url)

    return None


async def fetch_article_image(session, url):
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=12),
            allow_redirects=True,
        ) as response:
            if response.status >= 400:
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type:
                return None
            page_html = await response.text(errors="ignore")
            return extract_meta_image(page_html, str(response.url))
    except Exception:
        return None


async def fetch_source(session, source):
    try:
        async with session.get(
            source["url"],
            timeout=aiohttp.ClientTimeout(total=25),
        ) as response:
            response.raise_for_status()
            body = await response.text()
    except Exception:
        return []

    feed = feedparser.parse(body)
    entries = feed.entries[:40]

    async def build_news(entry):
        title = clean_html(entry.get("title", ""))
        url = (entry.get("link") or "").strip()
        summary = clean_html(entry.get("summary") or entry.get("description") or "")
        if not title or not url:
            return None

        image_url = extract_rss_image(entry)
        if not image_url:
            image_url = await fetch_article_image(session, url)

        return RawNews(
            title=title,
            summary=summary,
            url=url,
            source=source["name"],
            priority=source.get("priority", 5),
            image_url=image_url,
        )

    items = await asyncio.gather(*(build_news(entry) for entry in entries))
    return [item for item in items if item is not None]


async def collect_news():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; UA-News-AI/1.0)"
    }
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        groups = await asyncio.gather(
            *(fetch_source(session, source) for source in RSS_SOURCES)
        )
    return [item for group in groups for item in group]
