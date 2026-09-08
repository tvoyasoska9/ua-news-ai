import re
from rapidfuzz import fuzz

def normalize(text):
    return re.sub(r"\W+", " ", text.lower()).strip()

def is_similar(candidate, recent_titles, threshold=90):
    a = normalize(candidate.title)
    return any(fuzz.token_set_ratio(a, normalize(title)) >= threshold for title in recent_titles)
