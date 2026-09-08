import asyncio
import hashlib
import html
import logging
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import aiohttp
import feedparser
from bs4 import BeautifulSoup

from app.models import RawNews
from app.sources import RSS_SOURCES

log = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = 24000
MIN_IMAGE_BYTES = 25_000


def clean_html(text):
    soup = BeautifulSoup(text or "", "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def fingerprint(news):
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
            from datetime import datetime
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
            url = enclosure.get("href") or enclosure.get("url")
            media_type = (enclosure.get("type") or "").lower()
            if url and media_type.startswith("image/"):
                candidates.append(url)

    for key in ("og:image:secure_url", "og:image", "twitter:image:src", "twitter:image"):
        value = meta_content(soup, [key])
        if value:
            candidates.append(value)

    article = soup.find("article") or soup.find("main")
    if article:
        for img in article.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if src:
                candidates.append(src)

    unique = []
    seen = set()
    for candidate in candidates:
        url = urljoin(page_url, html.unescape(str(candidate)).strip())
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            unique.append(url)
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
            timeout=aiohttp.ClientTimeout(total=12),
            allow_redirects=True,
        ) as response:
            if response.status >= 400:
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            if not content_type.startswith("image/"):
                return None

            length = int(response.headers.get("Content-Length") or 0)
            if length and length < MIN_IMAGE_BYTES:
                return None

            chunk = await response.content.read(64)
            if not chunk:
                return None

            return (length, url)
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
    entries = feed.entries[:40]

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


async def collect_news():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; UA-News-AI/1.1; +https://example.invalid)"
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
