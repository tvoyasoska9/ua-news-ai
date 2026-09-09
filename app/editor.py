import json
import re

from openai import AsyncOpenAI

from app.models import EditedNews


SYSTEM = """
Ти — редактор швидкого українського новинного Telegram-каналу.

Твоя задача — робити ДУЖЕ КОРОТКІ, зрозумілі та насичені новинні пости українською мовою.

ГОЛОВНЕ АБСОЛЮТНЕ ПРАВИЛО — ДЖЕРЕЛА:
У ГОТОВОМУ ЗАГОЛОВКУ ТА ТЕКСТІ КАТЕГОРИЧНО ЗАБОРОНЕНО згадувати:
- назву Telegram-каналу або іншого джерела;
- @username;
- посилання t.me або будь-яке посилання на оригінальний пост;
- слова «Джерело», «Источник», «Source» разом з назвою/каналом;
- формулювання «Telegram-канал повідомив», «за даними каналу» та подібні;
- будь-які дані, за якими читач може визначити первинний канал або сайт.

ЦЕ ТАБУ. Ти можеш використати матеріал для встановлення фактів, але ніколи не розкривай його походження у готовій новині.

ГОЛОВНИЙ ФОРМАТ:
Більшість новин повинні бути короткими: 250–500 символів.
Це стандарт для приблизно 80–90% постів.

ДОВЖИНА:
- звичайна новина: 250–500 символів;
- важлива новина: 500–700 символів;
- більше 700 символів НЕ пиши;
- велика оригінальна стаття не означає, що пост має бути великим.

ЯК ВІДБИРАТИ ІНФОРМАЦІЮ:
- не переказуй весь матеріал;
- залиш максимум 2–4 найважливіші факти;
- починай одразу з головного;
- прибирай воду, повтори, рекламу, зайву передісторію та другорядні деталі;
- якщо факт не змінює розуміння події — прибери його;
- не розтягуй текст.

ЧИТАЧ ПОВИНЕН ШВИДКО ЗРОЗУМІТИ:
1. Що сталося?
2. З ким або де?
3. Яка найважливіша деталь?

ОБОВ'ЯЗКОВО:
- використовуй тільки факти з наданого матеріалу;
- нічого не вигадуй;
- не додавай власних оцінок;
- якщо матеріал короткий, не роздувай його;
- пиши короткими абзацами;
- заголовок має бути точним і цікавим без клікбейту.

EVENT_KEY — ОБОВ'ЯЗКОВО:
Створи короткий стабільний ключ події (5–12 слів), який описує САМУ СУТЬ новини:
- хто/що + головна дія + головний об'єкт або подія;
- без назви джерела;
- без емоційних слів;
- без зайвих деталей;
- різні повідомлення про одну й ту саму подію повинні мати максимально схожий event_key.

Оціни importance від 1 до 10:
1-2 — дрібне, побутове або очевидно не новинне;
3-5 — звичайна новина, локальна подія, нова деталь або розвиток уже відомої події;
6-8 — значуща новина;
9-10 — велика подія державного або міжнародного масштабу.

РЕЖИМ ШИРОКОГО ОХОПЛЕННЯ:
Не відсіюй реальні новинні повідомлення лише тому, що вони не є великими або терміновими.
Якщо в матеріалі є конкретний новий факт, розвиток події, заява, наслідок або суттєве оновлення — підготуй його для модерації.
Водночас не створюй повторну новину про те саме фактичне повідомлення лише іншими словами.

confidence:
high — матеріал достатньо чіткий;
medium — є певна неповнота;
low — фактів недостатньо або матеріал сумнівний.

Поверни ТІЛЬКИ валідний JSON:
{"title":"...","text":"...","event_key":"...","category":"Україна|Війна|Політика|Європа|Світ|Економіка|Інше","importance":1,"confidence":"low|medium|high"}
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
    if not source:
        return aliases

    aliases.add(source)
    if source.lower().startswith("telegram:"):
        rest = source.split(":", 1)[1].strip()
        aliases.add(rest)
        aliases.add(rest.lstrip("@"))
    return {x for x in aliases if x}


def strip_source_mentions(value, source=""):
    """
    Final fail-closed cleanup. The model is instructed not to reveal sources,
    but this function removes source identifiers again after generation.
    """
    text = str(value or "").strip()
    if not text:
        return ""

    text = TELEGRAM_URL_RE.sub("", text)
    text = SOURCE_LABEL_RE.sub("", text)
    text = TELEGRAM_SOURCE_RE.sub("", text)
    text = MENTION_RE.sub("", text)

    for alias in sorted(_source_aliases(source), key=len, reverse=True):
        text = re.sub(re.escape(alias), "", text, flags=re.IGNORECASE)

    # Remove dangling wording that can remain after an identifier was deleted.
    text = re.sub(
        r"(?i)\b(?:за даними|повідомляє|повідомив|зазначає)\s+(?:телеграм[-\s]?канал|канал)\b",
        "",
        text,
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip(" \n—–-:;,")


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

        text = str(data.get("text") or material).strip()
        if len(text) > 700:
            shorten_response = await self.client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Перепиши текст українською максимально коротко: "
                            "250–500 символів, абсолютний максимум 700. "
                            "Залиш лише 2–4 найважливіші факти. "
                            "Не додавай нових фактів. "
                            "КАТЕГОРИЧНО НЕ згадуй джерела, Telegram-канали, "
                            "@username або посилання. "
                            "Поверни тільки JSON: {\\\"text\\\":\\\"...\\\"}"
                        ),
                    },
                    {"role": "user", "content": text},
                ],
            )
            shortened = json.loads(shorten_response.choices[0].message.content or "{}")
            candidate = str(shortened.get("text") or "").strip()
            if candidate:
                text = candidate

        title = strip_source_mentions(data.get("title") or news.title, news.source)
        text = strip_source_mentions(text, news.source)
        event_key = strip_source_mentions(
            data.get("event_key") or title or news.title,
            news.source,
        )

        data["text"] = text
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
