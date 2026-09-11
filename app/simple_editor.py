import json
import re
from difflib import SequenceMatcher
from html import escape

from openai import AsyncOpenAI

from app.models import EditedNews

SYSTEM = """You process exactly one Telegram news post.

GOAL:
Create ONE concise, natural Ukrainian Telegram news post from the source facts. The result must read as a single coherent news item, not as several paraphrases of the same event.

ABSOLUTE ANTI-REPETITION RULES:
1. State each factual event ONCE. Never repeat the same event in the title and then again in the body using different wording.
2. The title is the headline. The body must immediately add NEW information that is not already conveyed by the headline.
3. Do not write a lead sentence that merely rephrases the headline.
4. Do not repeat the same event across multiple sentences with synonyms (for example: "влучення", then "приліт", then "удар" about the same incident).
5. If the source itself repeats the same fact, COLLAPSE that repetition. Keep the unique additional detail only.
6. Prefer the shortest wording that preserves all UNIQUE facts. Do not pad the post.
7. A good result should move forward: HEADLINE -> new details -> additional unique facts. Never circle back to restate what was already said.

FACTUAL RULES:
8. Preserve all meaningful unique facts, numbers, names, dates, places and direct quotations.
9. Do not invent facts, opinions, explanations or certainty that the source does not contain.
10. Ukrainian source text must be genuinely rewritten into fresh Ukrainian wording, not copied verbatim except unavoidable names, exact numbers, official titles and direct quotations.
11. If the source is not Ukrainian, translate it into natural Ukrainian.
12. Preserve useful quote formatting: QUOTE source blocks remain QUOTE blocks. Normal blocks remain NORMAL blocks where possible.

COMPOSITION:
13. Return a concise factual headline.
14. Remove redundant sentences instead of paraphrasing them again.
15. Do NOT force the same number of blocks as the source. Redundant source blocks may be omitted. Never create duplicate blocks.
16. The first normal block must contain genuinely NEW information beyond the title.

RETURN JSON ONLY:
{"title":"...","blocks":["block 1","block 2","..."]}

The blocks array contains only useful, non-redundant content in logical order.
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

def _word_set(value):
    return {
        w for w in re.findall(r"[a-zа-яіїєґ0-9]+", str(value or "").lower(), flags=re.UNICODE)
        if len(w) > 2
    }

def _is_too_close_to_source(source, edited):
    source = re.sub(r"\s+", " ", str(source or "")).strip().lower()
    edited = re.sub(r"\s+", " ", str(edited or "")).strip().lower()
    if len(source) < 45 or len(edited) < 45:
        return False
    return SequenceMatcher(None, source, edited).ratio() >= 0.96

def _strip_repeated_lead(title, text):
    text = str(text or "").strip()
    if not text:
        return text

    # Compare the headline with the first sentence, not only with the whole
    # block. This catches the common case where the AI repeats the headline
    # and then appends new information.
    sentences = re.split(r"(?<=[.!?…])\s+", text, maxsplit=1)
    lead = sentences[0].strip()
    rest = sentences[1].strip() if len(sentences) > 1 else ""

    a, b = plain_norm(title), plain_norm(lead)
    if a and b and (a == b or a in b or b in a):
        return rest

    ta, tb = _word_set(title), _word_set(lead)
    if ta and tb:
        overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
        # A near-identical first sentence is a repeated headline even when
        # punctuation, word order, or one location phrase differs.
        if overlap >= 0.55:
            return rest
    return text

def remove_repeated_headline(title, blocks):
    if not blocks:
        return blocks
    blocks = [dict(x) for x in blocks]
    blocks[0]["text"] = _strip_repeated_lead(title, blocks[0]["text"])
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
                if not isinstance(result, list) or not result:
                    raise ValueError("AI returned no usable blocks")

                edited_blocks = []
                for i, value in enumerate(result):
                    value = clean(value)
                    if not value:
                        continue
                    source_type = blocks[min(i, len(blocks) - 1)]["type"]
                    edited_blocks.append({"type": source_type, "text": value})
                if not edited_blocks:
                    raise ValueError("AI returned empty blocks")

                if any(
                    _is_too_close_to_source(source["text"], edited["text"])
                    for source, edited in zip(blocks, edited_blocks)
                ):
                    raise ValueError("AI copied source wording instead of paraphrasing")

                edited_blocks = remove_repeated_headline(title, edited_blocks)
                text = render_blocks(edited_blocks)
                if title and (text or len(blocks) == 1):
                    return EditedNews(title, text, "news", 10, "high", [], "")
                raise ValueError("incomplete model output")
            except Exception as exc:
                last_error = exc

        raise last_error or ValueError("editor failed")
