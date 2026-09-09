import re
from rapidfuzz import fuzz


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"[^\w\s]", " ", text).strip()


def score(a, b):
    a = normalize(a)
    b = normalize(b)
    if not a or not b:
        return 0
    return fuzz.token_set_ratio(a, b)


def _event_scores(a, b):
    a = normalize(a)
    b = normalize(b)
    if not a or not b:
        return 0, 0, 0
    return (
        fuzz.token_set_ratio(a, b),
        fuzz.token_sort_ratio(a, b),
        fuzz.ratio(a, b),
    )


def is_similar_title(candidate, recent_titles, threshold=90):
    return any(score(candidate.title, title) >= threshold for title in recent_titles)


def is_duplicate_event(event_key, recent_events, threshold=86):
    """
    Strong duplicate protection for the same factual event written in different
    words. We require both high keyword overlap and meaningful phrase overlap,
    which is stricter than blindly matching one common word but catches simple
    paraphrases that the old 92% token-set rule missed.
    """
    for old_key in recent_events:
        token_set, token_sort, direct = _event_scores(event_key, old_key)

        if token_set >= 96:
            return True

        if token_set >= threshold and token_sort >= 68:
            return True

        if direct >= 90:
            return True

    return False
