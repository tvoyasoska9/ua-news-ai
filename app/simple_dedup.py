import hashlib
import re
from rapidfuzz import fuzz

STOP = {
    "і","й","та","в","у","з","із","на","до","за","про","що","це","як","не","а","але",
    "the","and","of","to","in","for","on","with","at","by","from","is","are"
}

def normalize(value):
    text = str(value or "").lower()
    text = re.sub(r"https?://\S+|t\.me/\S+|@\w+", " ", text)
    text = re.sub(r"[^\wіїєґа-я0-9]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()

def fingerprint(value):
    return hashlib.sha256(normalize(value).encode("utf-8")).hexdigest()

def _tokens(value):
    return [x for x in normalize(value).split() if len(x) > 2 and x not in STOP]

def is_duplicate_text(text, recent_texts):
    candidate = normalize(text)
    if len(candidate) < 25:
        return False

    candidate_fp = fingerprint(candidate)
    candidate_tokens = set(_tokens(candidate))

    for old in recent_texts:
        old_norm = normalize(old)
        if not old_norm:
            continue
        if candidate_fp == fingerprint(old_norm):
            return True

        if min(len(candidate), len(old_norm)) >= 60:
            ratio = fuzz.ratio(candidate, old_norm)
            token_ratio = fuzz.token_set_ratio(candidate, old_norm)
            old_tokens = set(_tokens(old_norm))
            union = candidate_tokens | old_tokens
            overlap = (len(candidate_tokens & old_tokens) / len(union)) if union else 0.0
            if (
                (ratio >= 84 and overlap >= 0.58)
                or (token_ratio >= 90 and overlap >= 0.68)
                or (ratio >= 78 and overlap >= 0.82)
            ):
                return True

    return False
