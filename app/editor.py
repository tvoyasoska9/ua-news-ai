import asyncio
import json
import re
from html import escape as html_escape
from html.parser import HTMLParser

from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from rapidfuzz import fuzz

from app.models import EditedNews


SYSTEM = """
Ти — редактор українського новинного Telegram-каналу.

Твоя задача: перекласти матеріал українською та унікально переформулювати його.

ГОЛОВНЕ ПРАВИЛО ОБСЯГУ:
Пиши стислий Telegram-пост, а не повний переклад статті.

- збережи всі ключові факти, необхідні для розуміння новини;
- прибирай повтори, другорядні деталі та редакційний шум;
- не додавай нові факти, пояснення або контекст;
- коротке джерело -> короткий результат;
- довгу статтю стискай до суті без втрати головних фактів;
- зазвичай достатньо приблизно 80–180 слів;
- не роздувай текст лише для того, щоб повторити обсяг оригіналу.

ЗАГОЛОВОК І ОСНОВНИЙ ТЕКСТ — НЕ ПОВТОРЮЮТЬ ОДНЕ ОДНОГО:
- title коротко повідомляє головний факт;
- text одразу дає НОВІ деталі, яких ще немає в title;
- категорично не переписуй title ще раз у першому реченні text іншими словами;
- не дублюй один і той самий факт у title і text;
- якщо в короткому оригіналі немає окремих деталей, достатніх для text без повтору,
  дозволено повернути порожній text: "".
- краще короткий пост без повтору, ніж штучно роздутий текст із дублюванням.

ФОРМАТУВАННЯ:
У полі text дозволений тільки безпечний Telegram HTML:
<b>...</b>, <i>...</i>, <u>...</u>, <s>...</s>, <blockquote>...</blockquote>.
Якщо в оригінальному повідомленні є виділення, зберігай його логіку:
важливі виділені фрагменти повинні залишатися виділеними, а звичайний текст —
звичайним. Не роби весь текст жирним лише тому, що частина була виділена.
Зберігай абзаци та загальну структуру оригіналу настільки точно, наскільки це
можливо після перекладу й перефразування.

ДЖЕРЕЛА ТА ПРОМО БЛОКИ — АБСОЛЮТНЕ ТАБУ В ГОТОВІЙ НОВИНІ:
У title і text категорично заборонено згадувати назву каналу/сайту, @username,
t.me, Telegram-канал як джерело, посилання на оригінал, слова «Джерело»,
«Источник», «Source» разом із походженням інформації.
Також категорично заборонені будь-які рекламні або підписні вставки з оригіналу:
«Підписатись», «Підписатися», «Подписаться», «Subscribe», назва чужого каналу
разом із закликом підписатися, кнопки, слогани та footer-підписи чужих каналів.
Не розкривай походження матеріалу і не перенось у результат чужий промо/footer
ні в якому вигляді.

ВИКОРИСТОВУЙ ЛИШЕ ФАКТИ З НАДАНОГО МАТЕРІАЛУ.
Нічого не вигадуй і не додавай власних оцінок.

EVENT_KEY:
Створи короткий нейтральний стабільний ключ події (5–12 слів).
Він має описувати саме фактичну подію за схемою:
ЩО СТАЛОСЯ + ДЕ/З КИМ + головний об'єкт.
Не використовуй емоційні формулювання, заклики або редакційні слова.
Для різних постів про ту саму подію event_key повинен бути максимально схожим,
щоб система не надсилала дублікати.

Оціни importance від 1 до 10:
1-2 — дрібне;
3-5 — звичайна новина;
6-8 — значуща;
9-10 — велика подія.

РЕЖИМ ШИРОКОГО ОХОПЛЕННЯ:
Не відсіюй реальні новини лише через невеликий масштаб, але не створюй повтор
про те саме фактичне повідомлення іншими словами.

Поверни ТІЛЬКИ валідний JSON:
{"title":"...","text":"HTML-текст або порожній рядок","event_key":"...","category":"Україна|Війна|Політика|Європа|Світ|Економіка|Інше","importance":1,"confidence":"low|medium|high"}
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
PROMO_LINE_RE = re.compile(
    r"(?im)^\s*(?:[^\n]{0,120}?[|•])?\s*(?:підписатись|підписатися|подписаться(?:\s+на\s+канал)?|subscribe(?:\s+now)?)\s*[!…]*\s*$"
)


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

    # Replace with spaces, never an empty string. This prevents words on the
    # two sides of a removed source/username from being glued together.
    text = TELEGRAM_URL_RE.sub(" ", text)
    text = SOURCE_LABEL_RE.sub(" ", text)
    text = TELEGRAM_SOURCE_RE.sub(" ", text)
    text = MENTION_RE.sub(" ", text)
    for alias in sorted(_source_aliases(source), key=len, reverse=True):
        text = re.sub(re.escape(alias), " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(?:за даними|повідомляє|повідомив|зазначає)\s+(?:телеграм[-\s]?канал|канал)\b",
        " ", text,
    )

    # Remove Telegram/channel promotion footers generically, including
    # "Україна Online | Підписатись" and "Інформатор | Підписатися".
    # These may belong to a channel other than the configured source.
    lines = []
    for line in text.splitlines():
        if PROMO_LINE_RE.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines)

    # Inline footer variants at the end of an otherwise normal line.
    text = re.sub(
        r"(?i)\s*(?:[|•—–-]\s*)[^\n|•]{1,100}?\s*[|•]\s*"
        r"(?:підписатись|підписатися|подписаться(?:\s+на\s+канал)?|subscribe(?:\s+now)?)"
        r"\s*[!…]*\s*$",
        "",
        text,
    )
    text = re.sub(
        r"(?i)\s*[|•—–-]\s*"
        r"(?:підписатись|підписатися|подписаться(?:\s+на\s+канал)?|subscribe(?:\s+now)?)"
        r"\s*[!…]*(?=\s|$)",
        " ",
        text,
    )

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip(" \n—–-:;,|•")


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
        result = re.sub(r"[ \t]+\n", "\n", result)
        result = re.sub(r"\n[ \t]+", "\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()


def sanitize_news_html(value, source=""):
    parser = _SafeHTML(source)
    parser.feed(str(value or ""))
    parser.close()
    return parser.get_html()


def _plain(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _first_paragraph(value):
    parts = re.split(r"\n\s*\n+", _plain(value))
    return next((part.strip() for part in parts if part.strip()), "")


def _title_repeated_in_body(title, body):
    title_plain = _plain(title)
    first = _first_paragraph(body)
    if len(title_plain) < 18 or len(first) < 18:
        return False

    title_norm = re.sub(r"[^\w\s]", " ", title_plain.lower())
    first_norm = re.sub(r"[^\w\s]", " ", first.lower())
    title_norm = re.sub(r"\s+", " ", title_norm).strip()
    first_norm = re.sub(r"\s+", " ", first_norm).strip()

    if not title_norm or not first_norm:
        return False

    # Direct containment catches "headline, then the same headline again".
    if title_norm in first_norm and len(first_norm) <= max(len(title_norm) * 2.4, len(title_norm) + 70):
        return True

    token_set = fuzz.token_set_ratio(title_norm, first_norm)
    token_sort = fuzz.token_sort_ratio(title_norm, first_norm)
    return token_set >= 92 and token_sort >= 72 and len(first_norm) <= len(title_norm) * 2.5


def _trim_to_sentence_boundary(value, limit):
    """Keep complete sentences only; never silently return a broken fragment."""
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
    return text


def _finish_at_sentence_boundary(value):
    """Repair a locally truncated model response without inventing text."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text[-1] in ".!?…»”)]}":
        return text
    boundary = max(text.rfind(mark) for mark in ".!?…")
    if boundary >= max(40, int(len(text) * 0.45)):
        return text[:boundary + 1].rstrip()
    return ""


class NewsEditor:
    """One API call per unique candidate.

    Cost control is intentional here: validation must never trigger another
    full OpenAI rewrite. The prompt handles quality, while deterministic local
    checks only remove an obviously duplicated first paragraph.
    """

    def __init__(
        self,
        api_key,
        model,
        max_material_chars=7000,
        max_completion_tokens=400,
        max_retries=2,
    ):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.max_material_chars = max(1500, min(int(max_material_chars), 6000))
        self.max_completion_tokens = max(250, min(int(max_completion_tokens), 700))
        self.max_retries = max(0, min(int(max_retries), 3))

    async def _request(self, messages):
        transient = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
        for attempt in range(self.max_retries + 1):
            try:
                return await self.client.chat.completions.create(
                    model=self.model,
                    response_format={"type": "json_object"},
                    max_completion_tokens=self.max_completion_tokens,
                    messages=messages,
                )
            except transient:
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(min(8, 1.5 * (2 ** attempt)))

    async def edit(self, news):
        raw_material = strip_source_mentions(str(news.summary or news.title or ""), news.source)
        material = _trim_to_sentence_boundary(raw_material, self.max_material_chars)
        original_plain = _plain(material)
        original_len = len(original_plain)

        system = SYSTEM + """

ДОДАТКОВИЙ КОНТРОЛЬ ЯКОСТІ — ВИКОНАЙ ЙОГО В ЦЬОМУ Ж ЄДИНОМУ ЗАПИТІ:
- Не створюй повторний варіант тексту після відповіді.
- Перед відповіддю самостійно перевір, що text не починається переказом title.
- Пиши стисло: передай суть і ключові факти без повторів та другорядних деталей.
- Не намагайся зберігати повний обсяг оригіналу.
- Для короткого поста не вигадуй окремий вступ або висновок.
- Якщо title вже містить весь факт, у text залишай тільки деталі, яких у title немає.
- Поверни готовий результат з першої спроби.
"""

        response = await self._request([
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "Внутрішні метадані для перевірки фактів. "
                    "НЕ включай джерело, username або посилання в результат.\n"
                    f"Заголовок матеріалу: {news.title}\n"
                    f"Дата публікації: {news.published_at or 'невідомо'}\n"
                    f"Оригінальний матеріал ({original_len} символів без HTML):\n{material}"
                ),
            },
        ])

        data = json.loads(response.choices[0].message.content or "{}")
        title = strip_source_mentions(data.get("title") or news.title, news.source)
        text = sanitize_news_html(data.get("text") or "", news.source)
        text = _finish_at_sentence_boundary(text)
        event_key = strip_source_mentions(data.get("event_key") or title or news.title, news.source)

        # Never pay for a second AI call just because the model repeated the
        # headline. Remove only the repeated opening paragraph locally.
        if _title_repeated_in_body(title, text):
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
            if paragraphs:
                paragraphs = paragraphs[1:]
            text = "\n\n".join(paragraphs).strip()

        # Last-resort local guard. We do not ask the API to rewrite again.
        # If a model response is wildly longer than the source, keeping the
        # first paragraphs is preferable to silently spending on a second call.
        plain_result = _plain(text)
        if original_len >= 120 and len(plain_result) > int(original_len * 1.35) + 80:
            limit = int(original_len * 1.25) + 60
            plain_text = _plain(text)

            # Never cut a news item in the middle of a sentence. Prefer the
            # last completed sentence within the limit; if there is no safe
            # boundary, keep the original rather than publishing a fragment.
            boundaries = [plain_text.rfind(mark, 0, limit + 1) for mark in ".!?…"]
            boundary = max(boundaries)
            if boundary >= max(40, int(limit * 0.45)):
                text = plain_text[:boundary + 1].strip()
            else:
                grace_end = min(len(plain_text), limit + 180)
                candidates = [
                    plain_text.find(mark, limit, grace_end)
                    for mark in ".!?…"
                ]
                candidates = [pos for pos in candidates if pos != -1]
                if candidates:
                    text = plain_text[:min(candidates) + 1].strip()

            # Re-sanitize after the local guard and never leave open HTML.
            text = sanitize_news_html(text, news.source)

        importance = max(1, min(10, int(data.get("importance", 1))))
        confidence = str(data.get("confidence") or "medium").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"

        return EditedNews(
            title=title or "Новина",
            text=text,
            category=str(data.get("category") or "Інше").strip(),
            importance=importance,
            confidence=confidence,
            source_urls=[],
            event_key=event_key or title or news.title,
        )
