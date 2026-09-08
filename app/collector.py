import asyncio
import hashlib
import re
import aiohttp
import feedparser
from app.models import RawNews
from app.sources import RSS_SOURCES

def clean_html(text):
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())

def fingerprint(news):
    normalized = re.sub(r"\W+", " ", news.title.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

async def fetch_source(session, source):
    try:
        async with session.get(source["url"], timeout=aiohttp.ClientTimeout(total=25)) as response:
            response.raise_for_status()
            body = await response.text()
    except Exception:
        return []

    feed = feedparser.parse(body)
    result = []
    for entry in feed.entries[:40]:
        title = clean_html(entry.get("title", ""))
        url = (entry.get("link") or "").strip()
        summary = clean_html(entry.get("summary") or entry.get("description") or "")
        if title and url:
            result.append(RawNews(title, summary, url, source["name"], source.get("priority", 5)))
    return result

async def collect_news():
    headers = {"User-Agent": "UA-News-AI/1.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        groups = await asyncio.gather(*(fetch_source(session, s) for s in RSS_SOURCES))
    return [item for group in groups for item in group]
