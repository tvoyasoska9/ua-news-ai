import json
import re
from openai import AsyncOpenAI
from app.models import EditedNews

SYSTEM = """Read the whole source post. Keep every factual detail and every meaningful paragraph. Never summarize or truncate. Translate everything into Ukrainian when needed. Lightly paraphrase only. Remove only source branding, usernames, links, advertisements, subscribe prompts, and footer noise. Add no facts. Return JSON only with title and text. text must contain the complete news body."""

NOISE = re.compile(r"(?im)^.*(?:t\.me/|subscribe|@[A-Za-z0-9_]{3,}).*$")

def clean(value):
    return "\n".join(x.rstrip() for x in str(value or "").splitlines() if not NOISE.search(x)).strip()

class SimpleNewsEditor:
    def __init__(self, api_key, model, max_completion_tokens=8000):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.max_completion_tokens = max_completion_tokens

    async def edit(self, news):
        material = str(news.summary or news.title or "").strip()
        response = await self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            max_completion_tokens=self.max_completion_tokens,
            messages=[{"role":"system","content":SYSTEM},{"role":"user","content":material}],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        title = clean(data.get("title")) or clean(news.title)
        text = clean(data.get("text"))
        if not title or not text:
            raise ValueError("incomplete model output")
        return EditedNews(title, text, "news", 10, "high", [], "")
