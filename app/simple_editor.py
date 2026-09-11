import json
import re
from openai import AsyncOpenAI
from app.models import EditedNews

SYSTEM = """You process one Telegram news post.

MANDATORY RULES:
1. Read the ENTIRE source post.
2. Preserve EVERY factual statement, number, name, date, quote and meaningful paragraph.
3. NEVER summarize, shorten, omit paragraphs or invent information.
4. Translate the complete post into Ukrainian if it is in another language.
5. Lightly paraphrase wording while preserving the exact meaning and all facts.
6. Remove only channel branding, usernames, subscription prompts, advertising and footer noise.
7. The title is ONLY a headline. Do NOT repeat the title as the first sentence or first paragraph of the body.
8. Return JSON only:
   {"title":"short Ukrainian headline","text":"complete Ukrainian news body without repeating the title"}

The body must contain the full rewritten news text, not a summary.
"""

NOISE = re.compile(r"(?im)^.*(?:t\.me/|subscribe|підписатись|підписатися|подписаться|надіслати новину).*$")

def clean(value):
    return "\n".join(
        x.rstrip() for x in str(value or "").splitlines()
        if x.strip() and not NOISE.search(x)
    ).strip()

def norm(value):
    return re.sub(r"[^\wіїєґа-я0-9]+", "", str(value or "").lower(), flags=re.UNICODE)

def remove_repeated_headline(title, text):
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        first = lines[0].strip()
        a, b = norm(title), norm(first)
        # The model often copies the headline as the first body paragraph.
        if a and b and (a == b or a in b or b in a):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
    return "\n".join(lines).strip()

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
            messages=[
                {"role":"system","content":SYSTEM},
                {"role":"user","content":material},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        title = clean(data.get("title")) or clean(news.title)
        text = remove_repeated_headline(title, clean(data.get("text")))
        if not title or not text:
            raise ValueError("incomplete model output")
        return EditedNews(title, text, "news", 10, "high", [], "")
