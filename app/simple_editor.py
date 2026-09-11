import json
import re
from difflib import SequenceMatcher
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
7. EVERY Ukrainian source block must be genuinely paraphrased into fresh Ukrainian wording. Do NOT merely copy the original and make cosmetic edits.
8. Do not copy full source sentences verbatim, except for unavoidable proper names, exact numbers, official titles, direct quotations, or very short fixed phrases.
9. Preserve the same facts, numbers, names, dates, places and meaning, but change sentence construction and wording wherever naturally possible.
10. If the source is not Ukrainian, translate it into natural Ukrainian and still rewrite it as an original news text.
11. Do not invent emoji, bullets, slogans, opinions or facts.
12. The title must be a concise factual Ukrainian headline and must also be written in original wording, not copied verbatim from the source.
13. The first body block must NEVER restate the headline as a sentence. The headline and body have different jobs: the headline announces the news; the body immediately adds new facts. If the source begins by repeating the headline, omit only that repeated sentence from the first body block while preserving all following factual content.

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
        if overlap >= 0.72:
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
                if not isinstance(result, list) or len(result) != len(blocks):
                    raise ValueError("AI changed block count")

                edited_blocks = []
                for source, value in zip(blocks, result):
                    value = clean(value)
                    if not value and source["text"]:
                        raise ValueError("AI returned empty factual block")
                    edited_blocks.append({"type": source["type"], "text": value})

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
