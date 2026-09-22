from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class FetchedEvent:
    url: str
    final_url: str
    sha256: str
    path: str
    mime: str
    size: int
    status_code: int
    fetched_at: str
    depth: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExtractedEvent:
    source_url: str
    source_sha256: str
    normalized_path: str
    title: str
    mime: str
    text_chars: int
    extracted_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
