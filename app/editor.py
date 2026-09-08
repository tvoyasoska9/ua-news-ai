import json
from openai import AsyncOpenAI
from app.models import EditedNews

SYSTEM = """
Ти редактор українського новинного Telegram-каналу.
Використовуй тільки факти з вхідного матеріалу.
Не вигадуй цифри, цитати, причини, час або деталі.
Створи короткий природний та унікальний пост українською.
Поверни тільки JSON:
{"title":"...","text":"...","category":"Україна|Війна|Політика|Європа|Світ|Економіка|Інше","importance":1,"confidence":"low|medium|high"}
"""

class NewsEditor:
    def __init__(self, api_key, model):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def edit(self, news):
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Джерело: {news.source}\nЗаголовок: {news.title}\nМатеріал: {news.summary}\nПосилання: {news.url}"}
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        importance = max(1, min(10, int(data.get("importance", 1))))
        return EditedNews(
            title=str(data.get("title") or news.title).strip(),
            text=str(data.get("text") or news.summary).strip(),
            category=str(data.get("category") or "Інше").strip(),
            importance=importance,
            confidence=str(data.get("confidence") or "medium").strip().lower(),
            source_urls=[news.url],
        )
