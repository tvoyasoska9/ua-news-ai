import re

def normalize(value):
    text = str(value or "").lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def is_duplicate_text(text, recent_texts):
    candidate = normalize(text)
    if len(candidate) < 30:
        return False
    for old in recent_texts:
        old = normalize(old)
        if candidate == old:
            return True
    return False
