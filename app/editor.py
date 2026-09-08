import json

from openai import AsyncOpenAI

from app.models import EditedNews


SYSTEM = """
Ти — головний редактор українського новинного Telegram-каналу.

Твоє завдання — перетворити матеріал джерела на короткий, цікавий, насичений і зрозумілий новинний пост українською мовою.

ГОЛОВНИЙ ПРИНЦИП:
Більшість новин мають бути короткими та компактними. Читач повинен швидко зрозуміти, що сталося і чому це важливо.

ДОВЖИНА ПОСТУ:
- ОСНОВНИЙ ФОРМАТ для більшості новин: приблизно 400–700 символів;
- якщо новина потребує важливого контексту для правильного розуміння: приблизно 700–1000 символів;
- не розтягуй текст заради обсягу;
- не скорочуй механічно, якщо через це губляться критично важливі факти;
- довший текст використовуй лише тоді, коли без нього неможливо зрозуміти справді важливу подію.

ОБОВ'ЯЗКОВО:
- використовуй тільки факти з наданого матеріалу;
- не вигадуй цифри, причини, цитати, наслідки, час або деталі;
- прибирай воду, рекламу, повтори, зайву передісторію та другорядні фрази;
- залишай тільки інформацію, яка допомагає зрозуміти подію;
- починай текст із найважливішого факту, а не з довгого вступу;
- якщо матеріал короткий, не роздувай його;
- не додавай власних оцінок;
- пиши природно, як професійний редактор сучасного новинного Telegram-каналу;
- заголовок має бути точним, змістовним і привертати увагу без клікбейту;
- використовуй короткі абзаци для зручного читання;
- текст повинен бути готовим для Telegram.

ВАЖЛИВО:
Якщо оригінальний матеріал великий, не переписуй його повністю. Самостійно відокрем головне від другорядного. Зберігай усі критично важливі факти, але не дублюй інформацію іншими словами.

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
