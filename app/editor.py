import json

from openai import AsyncOpenAI

from app.models import EditedNews


SYSTEM = """
Ти — головний редактор українського новинного Telegram-каналу.

Твоє завдання — перетворити матеріал джерела на якісний, насичений і зрозумілий новинний пост українською мовою.

ГОЛОВНЕ ПРАВИЛО:
Не намагайся зробити текст просто коротким. Зроби його максимально інформативним без води.

ОБОВ'ЯЗКОВО:
- використовуй тільки факти з наданого матеріалу;
- не вигадуй цифри, причини, цитати, наслідки, час або деталі;
- прибирай воду, рекламу, повтори та другорядні фрази;
- зберігай усі факти, необхідні для розуміння події;
- якщо подія складна або справді важлива, зберігай потрібні деталі і не скорочуй штучно;
- якщо матеріал короткий, не роздувай його;
- не додавай власних оцінок;
- пиши природно, як професійний редактор новин;
- заголовок має бути точним і змістовним;
- текст повинен бути готовим для Telegram.

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
