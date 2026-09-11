import json
import re
from openai import AsyncOpenAI
from app.models import EditedNews

SYSTEM = """You process exactly one Telegram news post.

GOAL:
Return the SAME news in clean Ukrainian. This is NOT summarization.

MANDATORY RULES:
1. Read the ENTIRE source post from beginning to end.
2. Preserve EVERY factual statement, number, name, date, place, quote and meaningful detail.
3. NEVER summarize, shorten, merge away facts, omit paragraphs or invent information.
4. If the source is not Ukrainian, translate ALL factual content into Ukrainian.
5. If the source is already Ukrainian, only lightly paraphrase wording where useful.
6. Remove ONLY channel names, usernames, subscription prompts, advertising, reaction/footer noise and source branding.
7. Keep the original paragraph order and paragraph structure. Do not turn normal paragraphs into a list.
8. Do NOT invent emoji, bullets, blockquotes, slogans, opinions or additional sentences.
9. The title must be a short factual Ukrainian headline.
10. The body must continue the news and MUST NOT repeat the title as its first paragraph.
11. Keep all remaining source paragraphs complete and natural.
12. Return JSON only:
   {"title":"...","text":"..."}

TEXT FORMATTING RULE:
Use normal paragraphs separated by one blank line. Do not use list markers unless the source itself is explicitly a list.
"""

NOISE = re.compile(
    r"(?im)^.*(?:t\.me/|subscribe|підписатись|підписатися|подписаться|надіслати новину|прислать новость|підписатися на канал).*$"
)

def clean(value):
    lines = []
    for line in str(value or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            lines.append("")
            continue
        if NOISE.search(line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def norm(value):
    return re.sub(r"[^\wіїєґа-я0-9]+", "", str(value or "").lower(), flags=re.UNICODE)

def remove_repeated_headline(title, text):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not paragraphs:
        return ""

    a = norm(title)
    first = paragraphs[0]
    b = norm(first)

    if a and b and (a == b or (len(a) > 20 and (a in b or b in a))):
        paragraphs.pop(0)

    return "\n\n".join(paragraphs).strip()

class SimpleNewsEditor:
    def __init__(self, api_key, model, max_completion_tokens=8000):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.max_completion_tokens = max(1200, int(max_completion_tokens or 8000))

    async def _call(self, material):
        response = await self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            max_completion_tokens=self.max_completion_tokens,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": material},
            ],
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    async def edit(self, news):
        material = clean(news.summary or news.title)
        if not material:
            raise ValueError("empty source post")

        last_error = None
        for _ in range(3):
            try:
                data = await self._call(material)
                title = clean(data.get("title")) or clean(news.title)
                text = remove_repeated_headline(title, clean(data.get("text")))
                if title and text:
                    return EditedNews(title, text, "news", 10, "high", [], "")
                last_error = ValueError("incomplete model output")
            except Exception as exc:
                last_error = exc

        raise last_error or ValueError("editor failed")
