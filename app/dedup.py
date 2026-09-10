import re
from rapidfuzz import fuzz


STOPWORDS = {
    "у", "в", "та", "і", "й", "на", "до", "за", "про", "з", "із", "для",
    "через", "під", "над", "від", "що", "це", "як", "по", "the", "a",
}


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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


def _meaningful_tokens(text):
    return {
        token for token in normalize(text).split()
        if len(token) >= 3 and token not in STOPWORDS
    }


def is_similar_title(candidate, recent_titles, threshold=82):
    """Cheap pre-AI duplicate gate.

    It intentionally blocks only strong headline overlap. This happens before
    OpenAI, so cross-source reposts do not consume API credit first.
    """
    candidate_title = getattr(candidate, "title", candidate) or ""
    candidate_tokens = _meaningful_tokens(candidate_title)
    if len(candidate_tokens) < 2:
        return False

    for old in recent_titles:
        old_tokens = _meaningful_tokens(old)
        if len(old_tokens) < 2:
            continue
        common = candidate_tokens & old_tokens
        smaller = min(len(candidate_tokens), len(old_tokens))
        overlap = len(common) / smaller if smaller else 0
        fuzzy = score(candidate_title, old)

        if fuzzy >= 92:
            return True
        if fuzzy >= threshold and len(common) >= 2 and overlap >= 0.70:
            return True

    return False


def is_duplicate_event(event_key, recent_events, threshold=82):
    """Semantic duplicate protection after AI for paraphrased factual events."""
    candidate_tokens = _meaningful_tokens(event_key)

    for old_key in recent_events:
        token_set, token_sort, direct = _event_scores(event_key, old_key)

        if token_set >= 95:
            return True

        if token_set >= threshold and token_sort >= 60:
            return True

        if direct >= 88:
            return True

        old_tokens = _meaningful_tokens(old_key)
        if not candidate_tokens or not old_tokens:
            continue

        common = candidate_tokens & old_tokens
        smaller = min(len(candidate_tokens), len(old_tokens))
        overlap = len(common) / smaller if smaller else 0

        if len(common) >= 4 and overlap >= 0.72:
            return True

    return False
