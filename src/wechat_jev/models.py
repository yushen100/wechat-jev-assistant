from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class ChatMessage:
    speaker: str
    text: str
    order: int
    confidence: float
    is_self: bool = False
    speaker_confidence: float = 1.0
    message_type: str = "text"
    sender_source: str = "position"
    content_source: str = "text_ocr"
    visual_fingerprint: str = ""
    message_id: str = ""
    created_at: int = 0
    sender_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CaptureRegion:
    left: float = 0.25
    top: float = 0.10
    right: float = 0.98
    bottom: float = 0.82

    def validate(self) -> None:
        values = (self.left, self.top, self.right, self.bottom)
        if not all(0.0 <= value <= 1.0 for value in values):
            raise ValueError("区域比例必须在 0 到 1 之间")
        if self.right - self.left < 0.2 or self.bottom - self.top < 0.2:
            raise ValueError("聊天区域过小")
