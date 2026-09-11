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
    media_type: Optional[str] = None
    media_path: Optional[str] = None
    media_paths: Sequence[str] = field(default_factory=list)
    media_types: Sequence[str] = field(default_factory=list)
    media_messages: Sequence[object] = field(default_factory=list, repr=False)
    blocks: Sequence[dict] = field(default_factory=list)

@dataclass
class EditedNews:
    title: str
    text: str
    category: str
    importance: int
    confidence: str
    source_urls: Sequence[str] = field(default_factory=list)
    event_key: str = ""
