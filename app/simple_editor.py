import json
import re
from html import escape

from openai import AsyncOpenAI

from app.models import EditedNews

SYSTEM = """You process exactly one Telegram news post.

GOAL:
Return the same news in clean Ukrainian WITHOUT destroying the original Telegram composition.

CRITICAL POST-INTEGRITY RULES:
1. Read every source block from beginning to end.
2. Return EXACTLY the same number of blocks, in EXACTLY the same order.
3. Never merge blocks. Never split blocks. Never delete a factual block. Never add a new block.
4. Each block has a fixed type: NORMAL or QUOTE. Translate/paraphrase only its content; the application will restore the visual Telegram formatting.
5. Preserve every factual statement, number, name, date, place and meaningful detail.
6. This is NOT summarization. Do not shorten the post.
7. If source is not Ukrainian, translate all factual content into Ukrainian. If it is Ukrainian, only lightly edit wording.
8. Do not invent emoji, bullets, slogans, opinions or facts.
9. The title must be a concise factual Ukrainian headline.
10. The first body block must not mechanically repeat the title if it is the same headline.

RETURN JSON ONLY:
{"title":"...","blocks":["translated block 1","translated block 2","..."]}

The blocks array length MUST equal the source blocks array length exactly.
"""

NOISE = re.compile(r"(?im)^.*(?:t\.me/|subscribe|підписатись|підписатися|подписаться|надіслати новину|прислать новость).*$")

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
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

def plain_norm(value):
    value = re.sub(r"<[^>]+>", " ", str(value or "")).lower()
    value = re.sub(r"[^\wіїєґа-я0-9]+", "", value, flags=re.UNICODE)
    return value

def source_blocks(news):
    blocks = []
    for block in (getattr(news, "blocks", None) or []):
        if not isinstance(block, dict):
            continue
        text = clean(block.get("text"))
        if text:
            kind = "quote" if str(block.get("type") or "").lower() == "quote" else "normal"
            blocks.append({"type": kind, "text": text})
    if blocks:
        return blocks
    material = clean(news.summary or news.title)
    return [{"type": "normal", "text": p.strip()} for p in re.split(r"\n\s*\n+", material) if p.strip()]

def remove_repeated_headline(title, blocks):
    if not blocks:
        return blocks
    first = blocks[0]["text"].strip()
    a, b = plain_norm(title), plain_norm(first)
    if a and b and (a == b or (len(a) > 20 and (a in b or b in a))):
        blocks = [dict(x) for x in blocks]
        blocks[0]["text"] = ""
    return [x for x in blocks if x["text"].strip()]

def render_blocks(blocks):
    rendered = []
    for block in blocks:
        text = escape(block["text"], quote=False)
        rendered.append(f"<blockquote>{text}</blockquote>" if block["type"] == "quote" else text)
    return "\n\n".join(rendered).strip()

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
        return json.loads(response.choices[0].message.content or "{}")

    async def edit(self, news):
        blocks = source_blocks(news)
        if not blocks:
            raise ValueError("empty source post")

        payload = {"source_blocks": [
            {"index": i + 1, "type": block["type"].upper(), "text": block["text"]}
            for i, block in enumerate(blocks)
        ]}

        last_error = None
        for _ in range(3):
            try:
                data = await self._call(json.dumps(payload, ensure_ascii=False))
                title = clean(data.get("title")) or clean(news.title)
                result = data.get("blocks")
                if not isinstance(result, list) or len(result) != len(blocks):
                    raise ValueError("AI changed block count")

                edited_blocks = []
                for source, value in zip(blocks, result):
                    value = clean(value)
                    if not value and source["text"]:
                        raise ValueError("AI returned empty factual block")
                    edited_blocks.append({"type": source["type"], "text": value})

                edited_blocks = remove_repeated_headline(title, edited_blocks)
                text = render_blocks(edited_blocks)
                if title and (text or len(blocks) == 1):
                    return EditedNews(title, text, "news", 10, "high", [], "")
                raise ValueError("incomplete model output")
            except Exception as exc:
                last_error = exc

        raise last_error or ValueError("editor failed")
