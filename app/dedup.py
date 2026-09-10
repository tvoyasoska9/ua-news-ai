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


def is_similar_title(candidate, recent_titles, threshold=86):
    # Raw headlines from different channels are often paraphrases, so 90 was
    # too strict and allowed obvious cross-source repeats into the AI stage.
    return any(score(candidate.title, title) >= threshold for title in recent_titles)


def is_duplicate_event(event_key, recent_events, threshold=82):
    """
    Semantic duplicate protection for the same factual event.

    The check intentionally combines fuzzy phrase matching with overlap of
    meaningful tokens. A single common word is not enough, but paraphrases such
    as "yellow danger because of UAV threat in Kyiv" are blocked even when the
    wording is changed by another source.
    """
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

        # Strong factual-key overlap catches AI paraphrases that fuzzy ratios
        # can miss because word order changed.
        if len(common) >= 4 and overlap >= 0.72:
            return True

    return False
