import json

from openai import AsyncOpenAI

from app.models import EditedNews


SYSTEM = """
Ти — головний редактор українського новинного Telegram-каналу.

Твоє завдання — перетворити матеріал джерела на КОРОТКИЙ, цікавий, насичений і зрозумілий новинний пост українською мовою.

ГОЛОВНЕ ПРАВИЛО:
Не переказуй всю статтю. Обери лише найважливіші факти, які читачеві потрібно знати.

ДОВЖИНА:
- ЗА ЗАМОВЧУВАННЯМ пиши 400–700 символів.
- Це основний формат для більшості новин — приблизно 80–90% постів.
- Намагайся спочатку вмістити новину в 400–700 символів.
- Дозволено 700–900 символів ТІЛЬКИ якщо без важливого контексту новина буде незрозумілою.
- Звичайна новина НЕ повинна перевищувати 900 символів.
- Не пиши довгий текст лише тому, що оригінальна стаття велика.

ВІДБІР ІНФОРМАЦІЇ:
- Для звичайної новини залишай максимум 3–5 найважливіших фактів.
- Не намагайся зберегти всі деталі з оригінального матеріалу.
- Прибирай воду, повтори, рекламу, зайву передісторію, другорядні деталі та очевидні пояснення.
- Якщо кілька речень повідомляють одну й ту саму інформацію — залиш тільки найсильніше і найточніше.
- Починай текст одразу з головного факту.

ОБОВ'ЯЗКОВО:
- використовуй тільки факти з наданого матеріалу;
- не вигадуй цифри, причини, цитати, наслідки, час або деталі;
- не додавай власних оцінок;
- якщо матеріал короткий, не роздувай його;
- пиши природно, як професійний редактор сучасного новинного Telegram-каналу;
- заголовок має бути точним, змістовним і привертати увагу без клікбейту;
- використовуй короткі абзаци для зручного читання;
- текст повинен бути готовим для Telegram.

ВАЖЛИВО:
Твоя мета — щоб людина швидко прочитала пост і одразу зрозуміла:
1. Що сталося?
2. Де або з ким це сталося?
3. Чому це важливо?

Якщо на ці питання можна відповісти коротко — НЕ додавай більше тексту.

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

        # Hard guard: if the AI still writes a long post, ask it once more
        # to compress the text without adding new facts.
        text = str(data.get("text") or material).strip()
        if len(text) > 900:
            shorten_response = await self.client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Скороти новинний текст українською до 400–700 символів. "
                            "Максимум — 900 символів. Залиш тільки 3–5 найважливіших фактів. "
                            "Не вигадуй нових фактів і не змінюй зміст. "
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
