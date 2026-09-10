import json
import re
from html import escape as html_escape
from html.parser import HTMLParser

from openai import AsyncOpenAI

from app.models import EditedNews


SYSTEM = """
Ти — редактор українського новинного Telegram-каналу.

Твоя задача: перекласти матеріал українською та унікально переформулювати його,
АЛЕ ЗБЕРЕГТИ ПРИБЛИЗНО ТУ САМУ КІЛЬКІСТЬ ІНФОРМАЦІЇ Й ОБСЯГ, ЩО Є В ОРИГІНАЛІ.

АБСОЛЮТНО ЗАБОРОНЕНО РОЗДУВАТИ ТЕКСТ:
- не додавай нові факти, пояснення, висновки або контекст;
- не перетворюй короткий пост на довгу статтю;
- не додавай фрази на кшталт «ситуація уточнюється», якщо цього немає в матеріалі;
- якщо оригінал короткий — результат теж має бути коротким;
- допускається лише природна різниця обсягу через переклад українською.

ЦІЛЬ ЗА ОБСЯГОМ:
- орієнтуйся на приблизний обсяг оригінального тексту;
- зазвичай результат має бути в межах приблизно 80–120% інформаційного обсягу оригіналу;
- не скорочуй агресивно і не розширюй без потреби.

ФОРМАТУВАННЯ:
У полі text дозволений тільки безпечний Telegram HTML:
<b>...</b>, <i>...</i>, <u>...</u>, <s>...</s>, <blockquote>...</blockquote>.
Якщо в оригінальному повідомленні є виділення, зберігай його логіку:
важливі виділені фрагменти повинні залишатися виділеними, а звичайний текст —
звичайним. Не роби весь текст жирним лише тому, що частина була виділена.
Зберігай абзаци та загальну структуру оригіналу настільки точно, наскільки це
можливо після перекладу й перефразування.

ДЖЕРЕЛА — АБСОЛЮТНЕ ТАБУ В ГОТОВІЙ НОВИНІ:
У title і text категорично заборонено згадувати назву каналу/сайту, @username,
t.me, Telegram-канал як джерело, посилання на оригінал, слова «Джерело»,
«Источник», «Source» разом із походженням інформації.
Не розкривай походження матеріалу ні в якому вигляді.

ВИКОРИСТОВУЙ ЛИШЕ ФАКТИ З НАДАНОГО МАТЕРІАЛУ.
Нічого не вигадуй і не додавай власних оцінок.

EVENT_KEY:
Створи короткий стабільний ключ події (5–12 слів), що описує суть факту.
Різні повідомлення про одну подію повинні мати максимально схожий event_key.

Оціни importance від 1 до 10:
1-2 — дрібне;
3-5 — звичайна новина;
6-8 — значуща;
9-10 — велика подія.

РЕЖИМ ШИРОКОГО ОХОПЛЕННЯ:
Не відсіюй реальні новини лише через невеликий масштаб, але не створюй повтор
про те саме фактичне повідомлення іншими словами.

Поверни ТІЛЬКИ валідний JSON:
{"title":"...","text":"HTML-текст","event_key":"...","category":"Україна|Війна|Політика|Європа|Світ|Економіка|Інше","importance":1,"confidence":"low|medium|high"}
"""

SOURCE_LABEL_RE = re.compile(
    r"(?is)\b(?:джерело|источник|source)\b\s*[:—–-]\s*[^.!?\n]*(?:[.!?]|$)"
)
TELEGRAM_SOURCE_RE = re.compile(
    r"(?is)\b(?:telegram|телеграм)[\s-]*(?:канал|channel)?\s*@?[A-Za-z0-9_]+\b"
)
TELEGRAM_URL_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z0-9_./?=&%-]+"
)
MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}\b")


def _source_aliases(source):
    aliases = set()
    source = (source or "").strip()
    if source:
        aliases.add(source)
        if source.lower().startswith("telegram:"):
            rest = source.split(":", 1)[1].strip()
            aliases.add(rest)
            aliases.add(rest.lstrip("@"))
    return {x for x in aliases if x}


def strip_source_mentions(value, source=""):
    text = str(value or "").strip()
    if not text:
        return ""
    text = TELEGRAM_URL_RE.sub("", text)
    text = SOURCE_LABEL_RE.sub("", text)
    text = TELEGRAM_SOURCE_RE.sub("", text)
    text = MENTION_RE.sub("", text)
    for alias in sorted(_source_aliases(source), key=len, reverse=True):
        text = re.sub(re.escape(alias), "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(?:за даними|повідомляє|повідомив|зазначає)\s+(?:телеграм[-\s]?канал|канал)\b",
        "", text,
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip(" \n—–-:;,")


_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "strike", "blockquote", "code", "pre"}


class _SafeHTML(HTMLParser):
    def __init__(self, source=""):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.source = source
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _ALLOWED_TAGS:
            normalized = {"strong": "b", "em": "i", "strike": "s"}.get(tag, tag)
            self.parts.append(f"<{normalized}>")
            self.stack.append(normalized)

    def handle_endtag(self, tag):
        tag = {"strong": "b", "em": "i", "strike": "s"}.get(tag.lower(), tag.lower())
        if tag in self.stack:
            # close tags until the requested tag to avoid broken HTML
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data):
        cleaned = strip_source_mentions(data, self.source)
        if cleaned:
            self.parts.append(html_escape(cleaned, quote=False))

    def get_html(self):
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        result = "".join(self.parts)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()


def sanitize_news_html(value, source=""):
    parser = _SafeHTML(source)
    parser.feed(str(value or ""))
    parser.close()
    return parser.get_html()


class NewsEditor:
    def __init__(self, api_key, model):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def edit(self, news):
        material = news.summary[:24000]
        response = await self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "Внутрішні метадані для перевірки фактів. "
                        "НЕ включай джерело, username або посилання в результат.\n"
                        f"Заголовок матеріалу: {news.title}\n"
                        f"Дата публікації: {news.published_at or 'невідомо'}\n"
                        f"Оригінальний матеріал:\n{material}"
                    ),
                },
            ],
        )

        data = json.loads(response.choices[0].message.content or "{}")
        title = strip_source_mentions(data.get("title") or news.title, news.source)
        text = sanitize_news_html(data.get("text") or material, news.source)
        event_key = strip_source_mentions(data.get("event_key") or title or news.title, news.source)

        # Hard guard against pathological expansion. We do not force a minimum:
        # short originals must remain short.
        plain_original = re.sub(r"<[^>]+>", "", material)
        plain_result = re.sub(r"<[^>]+>", "", text)
        if len(plain_original) >= 80 and len(plain_result) > int(len(plain_original) * 1.35) + 80:
            retry = await self.client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Скороти текст до приблизно такого самого обсягу, як оригінал. "
                            "Не додавай фактів. Збережи зміст, абзаци та HTML-виділення "
                            "(b/i/u/s/blockquote). Не згадуй джерела. "
                            "Поверни тільки JSON: {\\\"text\\\":\\\"...\\\"}"
                        ),
                    },
                    {"role": "user", "content": f"ОРИГІНАЛ:\n{material}\n\nПОТОЧНИЙ ТЕКСТ:\n{text}"},
                ],
            )
            shortened = json.loads(retry.choices[0].message.content or "{}")
            candidate = sanitize_news_html(shortened.get("text") or "", news.source)
            if candidate:
                text = candidate

        importance = max(1, min(10, int(data.get("importance", 1))))
        confidence = str(data.get("confidence") or "medium").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"

        return EditedNews(
            title=title or "Новина",
            text=text or "Деталі уточнюються.",
            category=str(data.get("category") or "Інше").strip(),
            importance=importance,
            confidence=confidence,
            source_urls=[],
            event_key=event_key or title or news.title,
        )
