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

Твоя задача: написати НОВИНУ СВОЇМИ СЛОВАМИ українською мовою на основі оригінального матеріалу. Це не копіювання і не механічна заміна кількох слів. Зберігай усі важливі факти, цифри та зміст, але будуй речення і структуру самостійно.

МОВА — ЖОРСТКА ВИМОГА:
- КОЖЕН матеріал без винятку має бути українською мовою, незалежно від мови джерела;
- якщо оригінал російською, спочатку повністю переклади зміст українською;
- у title, text і event_key не повинно залишатися російських слів або російських фрагментів;
- не повертай оригінальний російський заголовок як fallback.

СПОЧАТКУ ПРОАНАЛІЗУЙ ЗМІСТ, А НЕ ОФОРМЛЕННЯ:
- прочитай ВЕСЬ матеріал від початку до кінця;
- визнач факти, твердження, причини, наслідки, оцінки спікерів і висновки;
- не вирішуй, що є «головним», лише за першим реченням, емодзі, жирним шрифтом,
  довжиною абзацу, розділовими знаками або позицією тексту;
- кожен змістовний абзац перевір окремо: якщо він додає новий факт, цей факт має
  залишитися в результаті;
- заборонено брати перші 2–3 рядки та механічно обривати решту матеріалу;
- заборонено завершувати text незакінченим реченням або фрагментом думки.

ГОЛОВНЕ ПРАВИЛО РЕДАКТУРИ — КРИТИЧНО ВАЖЛИВО:
ПИШИ САМОСТІЙНО, БЕЗ КОПІЮВАННЯ ОРИГІНАЛУ.

- не копіюй речення, їх порядок, синтаксис або готові довгі словосполучення;
- кожен абзац переписуй природною українською новинною мовою;
- зберігай усі важливі факти, цифри, імена, місця та причинно-наслідкові зв'язки;
- дозволено і потрібно прибирати дублювання, повтори одного факту та другорядне словесне «розтягування»;
- НЕ повторюй одну й ту саму інформацію двома-трьома різними формулюваннями;
- коротка новина має залишатися короткою: не роздувай її заради обсягу;
- довгу новину можна стисло й чітко структурувати, але без втрати важливих фактів;
- дозволено об'єднувати кілька речень оригіналу в одне, якщо це не змінює зміст;
- title повідомляє головний факт, а text містить лише нові деталі;
- якщо для body немає нових деталей, поверни text як порожній рядок.

Перед відповіддю перевір: читач повинен отримати той самий фактичний зміст, але текст має звучати як самостійно написана новина, а не як копія джерела.

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
НЕ копіюй речення, їх порядок або синтаксис механічно. Побудуй результат як
самостійно написану новину: факти залишаються ті самі, але формулювання та
побудова речень мають бути власними. Зберігай виділення лише там, де це справді
допомагає читабельності; не відтворюй структуру оригіналу автоматично.

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

# The AI is explicitly asked to translate every source into Ukrainian.  Do not
# allow a Russian fallback title/body to bypass that requirement.
RUSSIAN_EXCLUSIVE_RE = re.compile(r"[ыэёъ]", re.IGNORECASE)
RUSSIAN_MARKERS = {
    "это", "этот", "эта", "эти", "этого", "этим", "этом",
    "что", "чтобы", "который", "которая", "которые", "которого",
    "сегодня", "сейчас", "только", "еще", "после", "также",
    "будет", "было", "были", "между", "почему", "новости",
    "россия", "россии", "россию", "российский", "российские",
    "заявил", "заявила", "сообщил", "сообщила", "сообщает",
    "ударила", "ударили", "всего",
}


def _contains_russian_text(value):
    plain = _plain(value).lower()
    if not plain:
        return False
    if RUSSIAN_EXCLUSIVE_RE.search(plain):
        return True
    tokens = set(re.findall(r"(?u)\\b[а-яіїєґёыэъ'-]+\\b", plain))
    return bool(tokens & RUSSIAN_MARKERS)


# Global source/attribution firewall. Source names are never allowed in the
# public title/body, even when the model copied them from a source headline.
_KNOWN_OUTLET_RE = re.compile(
    r"(?iu)^(?:"
    r"hromadske|ukrinform|уніан|interfax|reuters|associated press|ap|bbc|"
    r"суспільне|радіо свобода|рбк[- ]?україна|рбк украина|"
    r"liga\.net|nv|новое время|tsn|тсн|24 канал|"
    r"українська правда|економічна правда|the kyiv independent"
    r")\.?$"
)
_TRAILING_ATTRIBUTION_RE = re.compile(
    r"(?iu)\s*(?:,|—|–|-)\s*"
    r"(?:hromadske|ukrinform|уніан|interfax|reuters|associated press|ap|bbc|"
    r"суспільне|радіо свобода|рбк[- ]?україна|рбк украина|liga\.net|nv|"
    r"новое время|tsn|тсн|24 канал|українська правда|економічна правда|"
    r"the kyiv independent)\.?\s*$"
)
_ATTRIBUTION_PHRASE_RE = re.compile(
    r"(?iu)\b(?:за даними|повідомляє|повідомив|зазначає|пише)\s+"
    r"(?:hromadske|ukrinform|уніан|interfax|reuters|associated press|ap|bbc|"
    r"суспільне|радіо свобода|рбк[- ]?україна|liga\.net|nv|"
    r"українська правда|економічна правда)\b"
)

def _strip_explicit_attribution(value):
    text = str(value or "").strip()
    if not text:
        return ""

    text = _ATTRIBUTION_PHRASE_RE.sub("", text)
    text = _TRAILING_ATTRIBUTION_RE.sub("", text)

    # Remove generic terminal "— outlet" fragments when the terminal fragment
    # is clearly an editorial brand, while preserving ordinary factual clauses.
    parts = re.split(r"\s*(?:—|–)\s*", text)
    if len(parts) >= 2:
        tail = parts[-1].strip(" .")
        if _KNOWN_OUTLET_RE.fullmatch(tail):
            text = " — ".join(parts[:-1])

    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip(" \n—–-,:;")

def _normalized_for_copy(value):
    text = _plain(_strip_explicit_attribution(value)).lower()
    text = re.sub(r"[^\wіїєґ'’-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _title_is_effectively_copied(title, source_title, material=""):
    a = _normalized_for_copy(title)
    b = _normalized_for_copy(source_title)
    if not a:
        return False

    # Direct title-to-title copy.
    if b and min(len(a), len(b)) >= 18:
        if a == b or (len(a) >= 28 and fuzz.ratio(a, b) >= 93):
            return True

    # A model can copy the first sentence/headline from the material even when
    # the transport title is empty or only an emoji.
    source = _normalized_for_copy(material)
    if len(a) >= 28 and source:
        if a in source:
            return True
        first_sentence = re.split(r"(?<=[.!?…])\s+", _plain(material), maxsplit=1)[0]
        first = _normalized_for_copy(first_sentence)
        if len(first) >= 18 and fuzz.ratio(a, first) >= 93:
            return True
    return False


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
    tail = re.sub(r"</?[^>]+>", "", last[match.end():]).strip(" \t.!…‼️❗️")
    if tail:
        return "\n".join(lines)

    before = last[:match.start()].rstrip()

    if before.endswith(("|", "•")):
        label = before[:-1].strip()
        if label and len(label) <= 100 and not re.search(r"[.!?…]", label):
            lines.pop()
            return "\n".join(lines).rstrip()

    boundary = max(before.rfind(mark) for mark in ".!?…")
    if boundary >= 0:
        lines[-1] = before[:boundary + 1].rstrip()
        return "\n".join(lines).rstrip()

    lines[-1] = before.rstrip(" |•—–-")
    return "\n".join(lines).rstrip()


def strip_source_mentions(value, source=""):
    text = str(value or "").strip()
    if not text:
        return ""

    text = _strip_explicit_attribution(text)
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
    cleaned = strip_source_mentions(str(value or ""), source)
    parser = _SafeHTML()
    parser.feed(cleaned)
    parser.close()
    return strip_source_mentions(parser.get_html(), source)


def _plain(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _first_paragraph(value):
    parts = re.split(r"\n\s*\n+", _plain(value))
    return next((part.strip() for part in parts if part.strip()), "")


def _fallback_title_from_material(value):
    text = strip_source_mentions(_plain(value))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    match = re.search(r"^(.{1,260}?[.!?…])(?:\s|$)", text)
    if match:
        return match.group(1).strip()
    return text[:220].rstrip(" ,;:—–-")


def _is_complete_statement(value):
    plain = _plain(value).strip()
    if not plain:
        return False
    if plain[-1] in ".!?…»”)]}":
        return True
    # Headlines may legitimately omit a final period, but must not end in a
    # dangling connector, dash, colon, or obviously unfinished word fragment.
    if plain.endswith(("—", "–", "-", ":", ",", ";", "…")):
        return False
    tail = plain.split()[-1].lower()
    if tail in {"і", "й", "та", "або", "але", "що", "який", "яка", "яке", "які", "для", "після", "через", "про", "у", "в", "на", "до", "від", "з", "із", "за"}:
        return False
    return len(plain) >= 18


def _is_usable_title(value):
    # News titles may be short, contain names/numbers or be fragments after a
    # full translation. Do not block the entire news pipeline over punctuation
    # heuristics such as _is_complete_statement().
    plain = _plain(value).strip()
    normalized = re.sub(r"\s+", " ", plain.lower()).strip(" .!?:;—–-")
    return len(plain) >= 4 and normalized not in GENERIC_TITLES


def _normalized_tokens(value):
    return re.findall(r"(?u)\b[\w’'-]+\b", _plain(value).lower())


def _has_excessive_source_copy(title, text, material, source_title=""):
    """Reject copied wording, including short Telegram posts."""
    result = _plain(f"{title}\n{text}")
    source = _plain(material)
    source_headline = _plain(source_title)

    if not result or not source:
        return False

    def norm(value):
        return re.sub(r"\s+", " ", _plain(value).lower()).strip()

    title_norm = norm(title)
    if source_headline and len(_normalized_tokens(title)) >= 5:
        headline_norm = norm(source_headline)
        if fuzz.ratio(title_norm, headline_norm) >= 90:
            return True

    source_norm = norm(source)
    result_norm = norm(result)
    source_tokens = _normalized_tokens(source)
    result_tokens = _normalized_tokens(result)

    # A short post is not exempt from rewriting. If nearly every word and the
    # sentence structure are preserved, reject it and force another generation.
    if len(source_tokens) >= 8 and len(result_tokens) >= 8:
        ratio = fuzz.ratio(source_norm, result_norm)
        token_sort = fuzz.token_sort_ratio(source_norm, result_norm)
        if ratio >= 88 and token_sort >= 94:
            return True

    # Short factual phrases and names naturally coincide in Ukrainian news.
    # Only reject long copied runs; six words was so aggressive that legitimate
    # rewrites were repeatedly rejected and the pipeline stopped producing news.
    if len(source_tokens) >= 10 and len(result_tokens) >= 10:
        source_ngrams = {
            tuple(source_tokens[i:i + 7])
            for i in range(len(source_tokens) - 6)
        }
        for i in range(len(result_tokens) - 6):
            if tuple(result_tokens[i:i + 7]) in source_ngrams:
                return True

    return False


def _word_count(value):
    return len(re.findall(r"(?u)\b[\w’'-]+\b", _plain(value)))


def _meaningful_paragraphs(value):
    return [
        _plain(part).strip()
        for part in re.split(r"\n\s*\n+", str(value or ""))
        if len(_plain(part).strip()) >= 20
    ]


def _coverage_too_low(title, text, material):
    """Quality telemetry only.

    Moderation is performed by a human. This pipeline must never stop offering
    fresh news because an automated word/paragraph heuristic dislikes the
    generated length. Only an actually empty response is handled elsewhere.
    """
    return False

def _material_coverage_too_low(title, text, material):
    return _coverage_too_low(title, text, material)


def _is_near_verbatim_copy(title, text, material):
    source = re.sub(r"\s+", " ", _plain(material)).strip().lower()
    result = re.sub(r"\s+", " ", _plain(f"{title}\n{text}")).strip().lower()
    source_tokens = _normalized_tokens(source)
    result_tokens = _normalized_tokens(result)

    if len(source_tokens) < 8 or len(result_tokens) < 8:
        return False

    ratio = fuzz.ratio(source, result)
    token_ratio = fuzz.token_set_ratio(source, result)
    token_sort = fuzz.token_sort_ratio(source, result)

    # No length bypass: short source posts must also be genuinely rewritten.
    return (
        ratio >= 94
        or (ratio >= 91 and token_sort >= 96)
        or (token_ratio >= 99 and token_sort >= 97)
    )


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

    if title_norm in first_norm and len(first_norm) <= max(len(title_norm) * 2.4, len(title_norm) + 70):
        return True

    token_set = fuzz.token_set_ratio(title_norm, first_norm)
    token_sort = fuzz.token_sort_ratio(title_norm, first_norm)
    return token_set >= 92 and token_sort >= 72 and len(first_norm) <= len(title_norm) * 2.5


def _trim_to_sentence_boundary(value, limit):
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
    text = str(value or "").strip()
    if not text:
        return ""
    if text[-1] in ".!?…»”)]}":
        return text
    boundary = max(text.rfind(mark) for mark in ".!?…")
    if boundary >= max(40, int(len(text) * 0.45)):
        return text[:boundary + 1].rstrip()
    return ""


class QualityError(ValueError):
    """The model answered, but the draft failed a deterministic quality gate."""


class NewsEditor:
    """Prepare a draft; quality failures can be repaired with one explicit second call."""

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
        # Never silently cut a normal Telegram source post. The previous 6000-character cap could remove the entire second half of a source before the model even saw it.
        self.max_material_chars = max(20000, min(int(max_material_chars), 20000))
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

    def _prepare_material(self, news):
        raw_input = str(news.summary or news.title or "")
        raw_material = strip_source_mentions(raw_input, news.source)
        # Preserve the complete source whenever possible. Trimming is only a hard
        # safety limit for exceptionally large payloads, never a summarization rule.
        material = raw_material if len(raw_material) <= self.max_material_chars else _trim_to_sentence_boundary(raw_material, self.max_material_chars)
        original_plain = _plain(material)
        if not material or not original_plain:
            raise ValueError("Candidate lost all factual text during source cleanup")

        original_words = _word_count(material)
        completion_budget = min(
            self.max_completion_tokens,
            max(500, min(1500, int(original_words * 2.2) + 180)),
        )
        return material, len(original_plain), completion_budget

    def _build_result(self, data, news, material, original_len):
        title = _strip_explicit_attribution(strip_source_mentions(data.get("title") or "", news.source))

        text = _strip_explicit_attribution(sanitize_news_html(data.get("text") or "", news.source))

        # Some Telegram posts expose only an emoji as their transport title
        # (for example "❗️"). The AI may then return an equally useless title
        # even when it produced a good body. Salvage a factual title from the
        # first complete sentence instead of wasting another API call.
        if not _is_usable_title(title):
            plain_text = _plain(text)
            candidates = re.split(r"(?<=[.!?…])\s+", plain_text, maxsplit=1)
            fallback = candidates[0].strip() if candidates else ""
            if len(fallback) > 120:
                cut = max(fallback.rfind(" ", 0, 120), fallback.rfind(",", 0, 120))
                fallback = fallback[:cut if cut >= 40 else 120].rstrip(" ,:;—–-")
            if _is_usable_title(fallback):
                title = fallback
                # Avoid showing the same sentence twice when it became title.
                if len(candidates) > 1:
                    text = sanitize_news_html(candidates[1].strip(), news.source)
            else:
                source_plain = _plain(material)
                source_parts = re.split(r"(?<=[.!?…])\\s+", source_plain, maxsplit=1)
                fallback = source_parts[0].strip() if source_parts else ""
                if len(fallback) > 120:
                    cut = max(fallback.rfind(" ", 0, 120), fallback.rfind(",", 0, 120))
                    fallback = fallback[:cut if cut >= 40 else 120].rstrip(" ,:;—–-")
                if _is_usable_title(fallback) and not _contains_russian_text(fallback):
                    title = fallback
                else:
                    original_title = strip_source_mentions(str(news.title or ""), news.source)
                    if _is_usable_title(original_title) and not _contains_russian_text(original_title):
                        title = original_title
                    else:
                        # Availability-first fallback: if the model produced a
                        # Ukrainian body but a weak title, derive a short title
                        # from the first meaningful Ukrainian sentence instead
                        # of dropping the entire news item.
                        body_plain = _plain(text).strip()
                        sentence = re.split(r"(?<=[.!?])\s+|\n+", body_plain, maxsplit=1)[0].strip()
                        sentence = sentence[:140].rstrip(" ,:;—–-")
                        if sentence and not _contains_russian_text(sentence):
                            title = sentence
                        else:
                            raise QualityError("no usable Ukrainian factual title")

        # Do not cut the model output at the last punctuation mark: that behavior
        # can remove the entire ending of a post. Preserve the full generated text.
        text = sanitize_news_html(text, news.source)
        title = strip_source_mentions(_strip_explicit_attribution(title), news.source)
        text = sanitize_news_html(_strip_explicit_attribution(text), news.source)
        if not _is_usable_title(title):
            body_plain = _plain(text).strip()
            sentence = re.split(r"(?<=[.!?])\s+|\n+", body_plain, maxsplit=1)[0].strip()[:140].rstrip(" ,:;—–-")
            if sentence and not _contains_russian_text(sentence):
                title = sentence
            else:
                raise QualityError("title became unusable after source attribution cleanup")

        event_key = strip_source_mentions(data.get("event_key") or title or news.title, news.source)

        # Translation is mandatory for every moderation draft.  A Russian
        # source must never reach Telegram simply because the model output or
        # a fallback title passed the other quality checks.
        if _contains_russian_text(title) or _contains_russian_text(text) or _contains_russian_text(event_key):
            raise QualityError("result contains Russian-language text; Ukrainian translation is mandatory")

        # Title and body must not tell the same fact twice. A repeated headline
        # is a formatting failure: regenerate the draft instead of publishing a
        # longer post that says the same thing again in other words.
        if _title_repeated_in_body(title, text):
            raise QualityError("headline is repeated in body")

        # A moderation card must contain an actual rewritten news item, not just
        # a copied headline. These thresholds are deliberately calibrated so a
        # genuinely short source can remain short while multi-paragraph factual
        # posts cannot collapse into one line.
        if _coverage_too_low(title, text, material):
            raise QualityError("draft lost too much factual coverage")

        # The user explicitly requires an independently written post. Names,
        # numbers and unavoidable factual terms may coincide, but copied sentence
        # structure or long verbatim runs are not acceptable.

        plain_result = _plain(text)
        # Do not truncate a valid complete rewrite simply because it is somewhat
        # longer after Ukrainian translation. Only guard against pathological expansion.
        if original_len >= 120 and len(plain_result) > int(original_len * 2.20) + 250:
            limit = int(original_len * 1.25) + 60
            plain_text = _plain(text)
            boundaries = [plain_text.rfind(mark, 0, limit + 1) for mark in ".!?…"]
            boundary = max(boundaries)
            if boundary >= max(40, int(limit * 0.45)):
                text = plain_text[:boundary + 1].strip()
            else:
                grace_end = min(len(plain_text), limit + 180)
                candidates = [plain_text.find(mark, limit, grace_end) for mark in ".!?…"]
                candidates = [pos for pos in candidates if pos != -1]
                if candidates:
                    text = plain_text[:min(candidates) + 1].strip()
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

    def _base_system(self):
        return SYSTEM + """

ДОДАТКОВИЙ КОНТРОЛЬ ЯКОСТІ:
- Пиши кожну новину СВОЇМИ СЛОВАМИ. Оригінал — це джерело фактів, а не шаблон для копіювання.
- Якщо оригінал уже українською, все одно перебудовуй речення та синтаксис, а не міняй 1–2 слова.
- Не дублюй факти між title і text і не повторюй один факт кількома формулюваннями.
- Перед формуванням JSON прочитай весь матеріал і перевір кожен важливий факт.
- Не губи нові факти з наступних абзаців, але прибирай справжні повтори.
- Кожне речення має бути завершеним і природним.
- Короткі новини не роздувай: один чіткий заголовок і лише потрібні нові деталі.
- Довгі новини стискай лише за рахунок повторів і словесної надмірності, а не фактів.
- Якщо title вже містить головний факт, у text залишай тільки нові деталі.
- Перевір фінальний текст на копіювання: він не повинен відтворювати оригінальні речення або їх порядок.
"""

    async def edit(self, news):
        material, original_len, completion_budget = self._prepare_material(news)
        response = await self._request([
            {"role": "system", "content": self._base_system() + "\nПоверни готовий результат з першої спроби."},
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
        return self._build_result(data, news, material, original_len)

    async def repair(self, news, reason):
        """Make one fresh rewrite after a deterministic quality failure."""
        material, original_len, completion_budget = self._prepare_material(news)
        repair_system = self._base_system() + f"""

ПОПЕРЕДНЯ СПРОБА НЕ ПРОЙШЛА ПЕРЕВІРКУ: {reason}

ЗРОБИ НОВУ САМОСТІЙНУ ВЕРСІЮ З НУЛЯ ЗА ОРИГІНАЛЬНИМ МАТЕРІАЛОМ.
Не виправляй старий текст механічно. Особливо важливо:
- напиши текст заново своїми словами природним українським новинним стилем;
- не копіюй речення, порядок речень або довгі словосполучення з оригіналу;
- не губи ключові факти, але прибери реальні повтори та словесну надмірність;
- title і text не повинні дублювати один одного;
- не повертай обірвані речення;
- не додавай жодних нових фактів.
"""
        response = await self._request([
            {"role": "system", "content": repair_system},
            {
                "role": "user",
                "content": (
                    "Створи НОВИЙ виправлений результат тільки з цього матеріалу. "
                    "НЕ включай джерело, username або посилання.\n"
                    f"Заголовок матеріалу: {news.title}\n"
                    f"Дата публікації: {news.published_at or 'невідомо'}\n"
                    f"Оригінальний матеріал ({original_len} символів без HTML):\n{material}"
                ),
            },
        ], max_completion_tokens=completion_budget)
        data = json.loads(response.choices[0].message.content or "{}")
        return self._build_result(data, news, material, original_len)
