import json

from openai import AsyncOpenAI

from app.models import EditedNews


SYSTEM = """
Ти — редактор швидкого українського новинного Telegram-каналу.

Твоя задача — робити ДУЖЕ КОРОТКІ, зрозумілі та насичені новинні пости українською мовою.

ГОЛОВНИЙ ФОРМАТ:
Більшість новин повинні бути короткими: 250–500 символів.
Це стандарт для приблизно 80–90% постів.

ДОВЖИНА:
- звичайна новина: 250–500 символів;
- важлива новина: 500–700 символів;
- більше 700 символів НЕ пиши;
- велика оригінальна стаття не означає, що пост має бути великим.

ЯК ВІДБИРАТИ ІНФОРМАЦІЮ:
- не переказуй всю статтю;
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

Оціни importance від 1 до 10:
1-3 — локальне або малозначуще;
4-6 — звичайна важлива новина;
7-8 — значуща новина;
9-10 — велика подія державного або міжнародного масштабу.

confidence:
high — матеріал достатньо чіткий;
medium — є певна неповнота;
low — фактів недостатньо або матеріал сумнівний.

Поверни ТІЛЬКИ валідний JSON:
{"title":"...","text":"...","category":"Україна|Війна|Політика|Європа|Світ|Економіка|Інше","importance":1,"confidence":"low|medium|high"}
"""


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
                        f"Джерело: {news.source}\n"
                        f"Заголовок: {news.title}\n"
                        f"Дата публікації: {news.published_at or 'невідомо'}\n"
                        f"Оригінальний матеріал:\n{material}\n\n"
                        f"Посилання: {news.url}"
                    ),
                },
            ],
        )

        data = json.loads(response.choices[0].message.content or "{}")

        # Hard length guard. The second pass is mandatory for long posts.
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
                            "Не додавай нових фактів. Не переказуй всю статтю. "
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

        data["text"] = text
        importance = max(1, min(10, int(data.get("importance", 1))))

        confidence = str(data.get("confidence") or "medium").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"

        return EditedNews(
            title=str(data.get("title") or news.title).strip(),
            text=str(data.get("text") or material).strip(),
            category=str(data.get("category") or "Інше").strip(),
            importance=importance,
            confidence=confidence,
            source_urls=[news.url],
        )
