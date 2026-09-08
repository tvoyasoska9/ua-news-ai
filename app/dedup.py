import re
from rapidfuzz import fuzz


def normalize(text):
    text = (text or "").lower()
    return re.sub(r"[^\w\s]", " ", text).strip()


def score(a, b):
    a = normalize(a)
    b = normalize(b)
    if not a or not b:
        return 0
    return fuzz.token_set_ratio(a, b)


def is_similar_title(candidate, recent_titles, threshold=90):
    return any(score(candidate.title, title) >= threshold for title in recent_titles)


def is_duplicate_event(event_key, recent_events, threshold=78):
    """Detect the same event even when different sources use different headlines."""
    for old_key in recent_events:
        if score(event_key, old_key) >= threshold:
            return True
    return False
