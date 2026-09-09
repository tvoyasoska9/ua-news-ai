# Telegram channels are the PRIMARY editorial sources.
# All configured Telegram channels are intentionally equal in priority.
# RSS/websites are only secondary fallback sources.

RSS_SOURCES = [
    {"name": "Українська правда", "url": "https://www.pravda.com.ua/rss/", "priority": 20},
    {"name": "BBC News Україна", "url": "https://feeds.bbci.co.uk/ukrainian/rss.xml", "priority": 10},
]

TELEGRAM_SOURCES = [
    {"name": "Ukrinformator", "username": "ukrinformator", "priority": 100},
    {"name": "Truexa News UA", "username": "truexanewsua", "priority": 100},
    {"name": "ОКО | Україна", "username": "oko_ua", "priority": 100},
    {"name": "UA Online", "username": "UaOnlii", "priority": 100},
    {"name": "Advocat Prava", "username": "advocatprava7", "priority": 100},
]
