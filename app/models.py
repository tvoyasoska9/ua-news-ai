from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass
class RawNews:
    title: str
    summary: str
    url: str
    source: str
    priority: int = 5
    image_url: Optional[str] = None
    published_at: Optional[str] = None
    media_type: Optional[str] = None  # photo | video | album
    media_path: Optional[str] = None  # backward-compatible first media path
    media_paths: Sequence[str] = field(default_factory=list)  # all media in original post/album
    media_types: Sequence[str] = field(default_factory=list)  # matching types for media_paths


@dataclass
class EditedNews:
    title: str
    text: str
    category: str
    importance: int
    confidence: str
    source_urls: Sequence[str] = field(default_factory=list)
    event_key: str = ""
