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

СПОЧАТКУ ПРОАНАЛІЗУЙ ЗМІСТ, А НЕ ОФОРМЛЕННЯ:
- прочитай ВЕСЬ матеріал від початку до кінця;
- визнач факти, твердження, причини, наслідки, оцінки спікерів і висновки;
- не вирішуй, що є «головним», лише за першим реченням, емодзі, жирним шрифтом,
  довжиною абзацу, розділовими знаками або позицією тексту;
- кожен змістовний абзац перевір окремо: якщо він додає новий факт, цей факт має
  залишитися в результаті;
- заборонено брати перші 2–3 рядки та механічно обривати решту матеріалу;
- заборонено завершувати text незакінченим реченням або фрагментом думки.

ГОЛОВНЕ ПРАВИЛО ОБСЯГУ:
Пиши стислий Telegram-пост, а не повний переклад статті.

- збережи всі ключові факти, необхідні для розуміння новини;
- прибирай повтори, другорядні деталі та редакційний шум;
- не додавай нові факти, пояснення або контекст;
- коротке джерело -> короткий результат;
- довгу статтю стискай до суті без втрати головних фактів;
- зазвичай достатньо приблизно 80–180 слів;
- не роздувай текст лише для того, щоб повторити обсяг оригіналу;
- але НІКОЛИ не скорочуй короткий або середній Telegram-пост настільки, щоб
  зникли окремі фактичні абзаци, ключові ризики, причини чи висновки автора;
- якщо оригінал уже короткий/середній, збережи практично весь його фактичний
  зміст і всі змістовні абзаци, а не лише перше речення.

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
PROMO_CTA_RE = re.compile(
    r"(?i)(?:"
    r"підписатись|підписатися|підписуйся(?:\s+на\s+[^\n|•]{1,60})?|"
    r"подписаться(?:\s+на\s+канал)?|subscribe(?:\s+now)?|"
    r"надіслати\s+новину|прислать\s+новость|send\s+news"
    r")"
)
GENERIC_TITLES = {"новина", "news", "новости", "повідомлення", "повідомлення дня"}


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


def _strip_promo_footer(text):
    """Remove only a trailing Telegram promo/CTA without deleting the news itself."""
    lines = str(text or "").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return ""

    last = lines[-1].strip()
    matches = list(PROMO_CTA_RE.finditer(last))
    if not matches:
        return "\n".join(lines)

    match = matches[-1]
    # Only treat it as a footer when the CTA is actually at the end of the line.
    # Closing HTML tags after a CTA do not count as factual trailing text.
    tail = re.sub(r"</?[^>]+>", "", last[match.end():]).strip(" \t.!…‼️❗️")
    if tail:
        return "\n".join(lines)

    before = last[:match.start()].rstrip()

    # A standalone footer such as "Україна Online | Підписатись".
    if before.endswith(("|", "•")):
        label = before[:-1].strip()
        if label and len(label) <= 100 and not re.search(r"[.!?…]", label):
            lines.pop()
            return "\n".join(lines).rstrip()

    # Inline footer after a real sentence:
    # "... — монітори. ТРУХА⚡️Україна | Надіслати новину"
    # "... автомобілі. Підписуйся на ОКО"
    boundary = max(before.rfind(mark) for mark in ".!?…")
    if boundary >= 0:
        lines[-1] = before[:boundary + 1].rstrip()
        return "\n".join(lines).rstrip()

    # If there is no safe factual sentence boundary, never erase the whole
    # candidate. Remove only the CTA itself and leave the factual text intact.
    lines[-1] = before.rstrip(" |•—–-")
    return "\n".join(lines).rstrip()


def strip_source_mentions(value, source=""):
    text = str(value or "").strip()
    if not text:
        return ""

    # Remove explicit source identifiers first.
    text = TELEGRAM_URL_RE.sub(" ", text)
    text = SOURCE_LABEL_RE.sub(" ", text)
    text = TELEGRAM_SOURCE_RE.sub(" ", text)
    text = MENTION_RE.sub(" ", text)
    for alias in sorted(_source_aliases(source), key=len, reverse=True):
        text = re.sub(re.escape(alias), " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(?:за даними|повідомляє|повідомив|зазначає)\s+(?:телеграм[-\s]?канал|канал)\b",
        " ",
        text,
    )

    # Strip foreign-channel promotion only at the end. The previous regex was
    # too broad: with an inline footer it could consume the entire factual line.
    text = _strip_promo_footer(text)

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
        # Cleanup happens on the complete original string before HTML parsing.
        # Cleaning parser fragments separately can miss a promo split by tags.
        if data:
            self.parts.append(html_escape(data, quote=False))

    def get_html(self):
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        result = "".join(self.parts)
        result = re.sub(r"[ \t]+\n", "\n", result)
        result = re.sub(r"\n[ \t]+", "\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()


def sanitize_news_html(value, source=""):
    # Clean the complete text before parsing so Telegram promo footers cannot
    # survive because HTMLParser split them into separate data fragments.
    cleaned = strip_source_mentions(str(value or ""), source)
    parser = _SafeHTML()
    parser.feed(cleaned)
    parser.close()
    result = parser.get_html()
    # Final whole-text guard for promo text that was wrapped in harmless HTML.
    return strip_source_mentions(result, source)


def _plain(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _first_paragraph(value):
    parts = re.split(r"\n\s*\n+", _plain(value))
    return next((part.strip() for part in parts if part.strip()), "")


def _fallback_title_from_material(value):
    """Use the first factual sentence when the model returns a useless generic title."""
    text = _plain(value)
    text = strip_source_mentions(text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    match = re.search(r"^(.{1,260}?[.!?…])(?:\s|$)", text)
    if match:
        return match.group(1).strip()
    return text[:220].rstrip(" ,;:—–-")


def _is_usable_title(value):
    plain = _plain(value).strip()
    normalized = re.sub(r"\s+", " ", plain.lower()).strip(" .!?:;—–-")
    return len(plain) >= 12 and normalized not in GENERIC_TITLES


def _word_count(value):
    return len(re.findall(r"(?u)\b[\w’'-]+\b", _plain(value)))


def _meaningful_paragraphs(value):
    return [
        _plain(part).strip()
        for part in re.split(r"\n\s*\n+", str(value or ""))
        if len(_plain(part).strip()) >= 20
    ]


def _coverage_too_low(title, text, material):
    """Conservative completeness guard for multi-paragraph Telegram posts."""
    original_words = _word_count(material)
    if original_words < 45:
        return False

    result_words = _word_count(title) + _word_count(text)
    source_paragraphs = _meaningful_paragraphs(material)
    result_paragraphs = _meaningful_paragraphs(text)

    # For short/medium posts the editor should preserve substance, not produce
    # a tiny headline-sized summary. The threshold is intentionally much higher
    # than the old 55% guard because that still allowed whole factual sections
    # to disappear.
    if result_words < int(original_words * 0.70):
        return True

    # If a source contains several substantive paragraphs but the result has
    # collapsed into a tiny fragment, treat it as suspicious even when the word
    # ratio happens to pass because of a long headline.
    if len(source_paragraphs) >= 4 and len(result_paragraphs) <= 1:
        return True

    return False


def _fallback_full_material(material):
    """Lossless fallback for short/medium posts when AI drops factual paragraphs."""
    lines = [line.strip() for line in str(material or "").splitlines() if line.strip()]
    if not lines:
        return "", ""

    first = lines[0]
    title = _fallback_title_from_material(first) or _fallback_title_from_material(material)
    if not title:
        return "", ""

    first_plain = _plain(first).strip()
    title_plain = _plain(title).strip()

    # If the first paragraph is exactly the factual headline, the remaining
    # paragraphs are the body. Otherwise keep the entire material as body so
    # no factual sentence disappears.
    if first_plain and title_plain and first_plain == title_plain:
        body_lines = lines[1:]
    else:
        body_lines = lines

    body = "\n\n".join(body_lines).strip()
    return title, sanitize_news_html(body)


def _material_coverage_too_low(title, text, material):
    return _coverage_too_low(title, text, material)


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
        max_completion_tokens=1100,
        max_retries=2,
    ):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.max_material_chars = max(1500, min(int(max_material_chars), 6000))
        self.max_completion_tokens = max(400, min(int(max_completion_tokens), 1600))
        self.max_retries = max(0, min(int(max_retries), 3))

    async def _request(self, messages, max_completion_tokens=None):
        transient = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
        for attempt in range(self.max_retries + 1):
            try:
                return await self.client.chat.completions.create(
                    model=self.model,
                    response_format={"type": "json_object"},
                    max_completion_tokens=max_completion_tokens or self.max_completion_tokens,
                    messages=messages,
                )
            except transient:
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(min(8, 1.5 * (2 ** attempt)))

    async def edit(self, news):
        raw_input = str(news.summary or news.title or "")
        raw_material = strip_source_mentions(raw_input, news.source)
        material = _trim_to_sentence_boundary(raw_material, self.max_material_chars)
        original_plain = _plain(material)
        original_len = len(original_plain)
        if not material or not original_plain:
            raise ValueError("Candidate lost all factual text during source cleanup")

        # Output capacity scales with the factual amount of the source. This
        # prevents a multi-paragraph post from being forced into a tiny token
        # budget and cut off in the middle of a thought.
        original_words = _word_count(material)
        completion_budget = min(
            self.max_completion_tokens,
            max(500, min(1500, int(original_words * 2.2) + 180)),
        )

        system = SYSTEM + """

ДОДАТКОВИЙ КОНТРОЛЬ ЯКОСТІ:
- Перед формуванням JSON прочитай весь матеріал і перевір зміст кожного абзацу.
- Відбирай факти за змістом, а не за емодзі, жирним шрифтом, першою позицією,
  довжиною рядка чи іншими візуальними ознаками.
- Не залишай лише перший абзац, якщо далі є нові факти.
- Якщо оригінал містить кілька змістовних абзаців, результат повинен передати
  зміст усіх таких абзаців, навіть якщо їх доведеться об'єднати.
- Кожне речення у відповіді має бути завершеним. Ніяких обривів на півслові,
  на середині речення або після незавершеної думки.
- Для короткого/середнього поста не стискай текст механічно до 2–3 рядків.
- Якщо title вже містить головний факт, у text залишай решту важливих деталей.
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
        ], max_completion_tokens=completion_budget)

        data = json.loads(response.choices[0].message.content or "{}")
        title = strip_source_mentions(data.get("title") or "", news.source)
        # A generic placeholder such as "Новина" is never allowed through.
        if not _is_usable_title(title):
            title = _fallback_title_from_material(material)
        if not _is_usable_title(title):
            title = _fallback_title_from_material(news.title)
        if not _is_usable_title(title):
            raise ValueError("AI returned no usable factual title")
        text = sanitize_news_html(data.get("text") or "", news.source)
        text = sanitize_news_html(_finish_at_sentence_boundary(text), news.source)
        event_key = strip_source_mentions(data.get("event_key") or title or news.title, news.source)

        # Never pay for a second AI call just because the model repeated the
        # headline. Remove only the repeated opening paragraph locally.
        if _title_repeated_in_body(title, text):
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
            if paragraphs:
                paragraphs = paragraphs[1:]
            text = "\n\n".join(paragraphs).strip()

        # If the draft is suspiciously short, make one targeted repair call.
        # The repair is asked to analyze the original content again and restore
        # omitted facts; it is not a blind retry of the same prompt.
        if len(original_plain) <= 3200 and _material_coverage_too_low(title, text, material):
            repair_system = SYSTEM + """
РЕЖИМ ВІДНОВЛЕННЯ ПОВНОТИ:
Нижче є ОРИГІНАЛ і ЧЕРНЕТКА, яка могла втратити частину змісту.
Порівняй їх ЗА ЗМІСТОМ абзац за абзацом. Віднови всі фактичні твердження,
які є в оригіналі, але відсутні в чернетці. Не додавай нових фактів.
Не орієнтуйся на емодзі, форматування або перші рядки. Поверни повний
готовий JSON із завершеними реченнями.
"""
            repair_response = await self._request([
                {"role": "system", "content": repair_system},
                {
                    "role": "user",
                    "content": (
                        f"ОРИГІНАЛ:\n{material}\n\n"
                        f"ЧЕРНЕТКА TITLE:\n{title}\n\n"
                        f"ЧЕРНЕТКА TEXT:\n{text}\n\n"
                        "Віднови пропущені факти та збережи природну структуру."
                    ),
                },
            ], max_completion_tokens=completion_budget)

            try:
                repaired = json.loads(repair_response.choices[0].message.content or "{}")
                repaired_title = strip_source_mentions(repaired.get("title") or "", news.source)
                repaired_text = sanitize_news_html(repaired.get("text") or "", news.source)
                repaired_text = sanitize_news_html(_finish_at_sentence_boundary(repaired_text), news.source)
                if _is_usable_title(repaired_title):
                    repaired_event = strip_source_mentions(
                        repaired.get("event_key") or repaired_title or event_key,
                        news.source,
                    )
                    if not _material_coverage_too_low(repaired_title, repaired_text, material):
                        title = repaired_title
                        text = repaired_text
                        event_key = repaired_event
            except Exception:
                # A malformed repair response must never break moderation.
                pass

        # Final lossless guard: if the model still discarded a substantial part
        # of a short/medium source, publish the cleaned factual material rather
        # than an attractive but mutilated summary.
        if len(original_plain) <= 3200 and _material_coverage_too_low(title, text, material):
            fallback_title, fallback_body = _fallback_full_material(material)
            if _is_usable_title(fallback_title):
                title = fallback_title
            text = fallback_body
            event_key = strip_source_mentions(event_key or title, news.source)

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
            title=title,
            text=text,
            category=str(data.get("category") or "Інше").strip(),
            importance=importance,
            confidence=confidence,
            source_urls=[],
            event_key=event_key or title or news.title,
        )
