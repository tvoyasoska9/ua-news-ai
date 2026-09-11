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

        # Same story copied through another source often differs only by punctuation,
        # source wording or one short lead-in. Require both high text similarity and
        # substantial factual-token overlap to avoid blocking genuinely new updates.
        if min(len(candidate), len(old_norm)) >= 80:
            ratio = fuzz.ratio(candidate, old_norm)
            token_ratio = fuzz.token_set_ratio(candidate, old_norm)
            old_tokens = set(_tokens(old_norm))
            union = candidate_tokens | old_tokens
            overlap = (len(candidate_tokens & old_tokens) / len(union)) if union else 0.0
            if (ratio >= 90 and overlap >= 0.70) or (token_ratio >= 95 and overlap >= 0.78):
                return True

    return False
