from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .capture import merge_older_page
from .models import ChatMessage


GENERIC_SPEAKERS = {"我方", "对方", "未知群成员"}
NORMALIZE_RE = re.compile(r"[^0-9A-Za-z一-鿿]+")
MEMORY_PARSER_VERSION = 3


def message_anchor(message: ChatMessage | dict[str, Any]) -> str:
    get = message.get if isinstance(message, dict) else lambda key, default=None: getattr(message, key, default)
    fingerprint = str(get("visual_fingerprint", "") or "")
    message_id = str(get("message_id", "") or "")
    if message_id:
        return f"id:{message_id}"
    if fingerprint:
        return f"v:{fingerprint}"
    text = NORMALIZE_RE.sub("", str(get("text", "")).lower())
    return f"t:{int(bool(get('is_self', False)))}:{text}"


def edit_distance(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 1:
        return 99
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + int(left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def canonicalize_speakers(messages: list[ChatMessage], memory_messages: list[dict[str, Any]]) -> list[str]:
    counts = Counter(
        str(item.get("speaker", ""))
        for item in memory_messages
        if item.get("speaker") not in GENERIC_SPEAKERS
        and float(item.get("speaker_confidence", 0)) >= 0.8
    )
    corrections: list[str] = []
    for message in messages:
        if message.speaker in GENERIC_SPEAKERS or message.speaker in counts:
            continue
        candidates = [
            (count, known)
            for known, count in counts.items()
            if len(known) == len(message.speaker) and edit_distance(known, message.speaker) == 1
        ]
        if not candidates:
            continue
        count, canonical = max(candidates)
        if count < 2:
            continue
        original = message.speaker
        message.speaker = canonical
        message.sender_source = "memory_correction"
        message.speaker_confidence = max(message.speaker_confidence, 0.9)
        corrections.append(f"{original}→{canonical}")
    return corrections


def find_matching_memory(
    current: list[ChatMessage], memories: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, int]:
    current_anchors = {message_anchor(message) for message in current if len(message_anchor(message)) > 4}
    best: dict[str, Any] | None = None
    best_overlap = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    for memory in memories:
        if int(memory.get("parser_version", 0) or 0) != MEMORY_PARSER_VERSION:
            continue
        try:
            if datetime.fromisoformat(str(memory.get("updated_at", ""))) < cutoff:
                continue
        except ValueError:
            continue
        stored = memory.get("messages", [])[-100:]
        stored_anchors = {message_anchor(message) for message in stored if len(message_anchor(message)) > 4}
        overlap = len(current_anchors & stored_anchors)
        if overlap > best_overlap:
            best, best_overlap = memory, overlap
    return (best, best_overlap) if best_overlap >= 2 else (None, best_overlap)


def merge_with_memory(
    memory: dict[str, Any], current: list[ChatMessage], max_stored: int = 500
) -> tuple[list[ChatMessage], int, list[str]]:
    stored_dicts = list(memory.get("messages", []))
    corrections = canonicalize_speakers(current, stored_dicts)
    stored = [ChatMessage(**item) for item in stored_dicts]
    before = len(stored)
    merged = merge_older_page(stored, current)
    added = max(0, len(merged) - before)
    return merged[-max_stored:], added, corrections


def new_memory_payload(messages: list[ChatMessage]) -> tuple[str, dict[str, Any]]:
    memory_id = str(uuid.uuid4())
    return memory_id, {
        "parser_version": MEMORY_PARSER_VERSION,
        "messages": [message.to_dict() for message in messages[-500:]],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
