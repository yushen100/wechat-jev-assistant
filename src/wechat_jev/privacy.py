from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[邮箱]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证号]"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){16,19}(?!\d)"), "[银行卡号]"),
    (re.compile(r"(https?://[^\s?#]+)(?:\?[^\s#]*)?(?:#[^\s]*)?"), r"\1[参数已隐藏]"),
)

EMOJI_XML_RE = re.compile(
    r"<msg>\s*<emoji\b|\b(?:fromusername|tousername)\s*=",
    re.IGNORECASE,
)
INTERNAL_ID_RE = re.compile(r"\bwxid_[A-Za-z0-9_-]+\b", re.IGNORECASE)
SAFE_MESSAGE_FIELDS = (
    "speaker",
    "text",
    "is_self",
    "message_type",
    "confidence",
    "speaker_confidence",
)


def redact_text(text: str) -> str:
    result = text
    for pattern, replacement in RULES:
        result = pattern.sub(replacement, result)
    result = INTERNAL_ID_RE.sub("[微信内部ID]", result)
    return result


def safe_display_text(text: str, message_type: str = "") -> str:
    if message_type == "sticker" or EMOJI_XML_RE.search(text):
        return "[动画表情]"
    return redact_text(text)


def redact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留分析必要字段，禁止把数据库身份和游标元数据带出本机。"""
    redacted = []
    for item in messages:
        safe = {
            key: item[key]
            for key in SAFE_MESSAGE_FIELDS
            if key in item
        }
        safe["text"] = safe_display_text(
            str(item.get("text", "")),
            str(item.get("message_type", "")),
        )
        redacted.append(safe)
    return redacted


def anonymize_state(state: dict[str, Any]) -> dict[str, Any]:
    """生成仅供 TypeSafe 使用的匿名副本；本地状态不受影响。"""
    outbound = deepcopy(state)
    conversation_type = str(state.get("conversation", {}).get("type", "unknown"))
    real_names: list[str] = []
    for message in state.get("messages", []):
        speaker = str(message.get("speaker", "")).strip()
        if speaker and speaker not in {"我方", "对方", "未知群成员"} and speaker not in real_names:
            real_names.append(speaker)
    aliases = {
        name: (
            "对方" if conversation_type == "private"
            else f"成员{chr(ord('A') + index)}"
        )
        for index, name in enumerate(real_names)
    }

    def anonymous_message(message: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: message[key]
            for key in SAFE_MESSAGE_FIELDS
            if key in message
        }
        speaker = str(message.get("speaker", ""))
        result["speaker"] = aliases.get(speaker, speaker)
        result["text"] = safe_display_text(
            str(message.get("text", "")),
            str(message.get("message_type", "")),
        )
        return result

    outbound["conversation"] = {
        "contact": "当前群聊" if conversation_type == "group" else "当前私聊",
        "type": conversation_type,
    }
    outbound["messages"] = [
        anonymous_message(message)
        for message in state.get("messages", [])
    ]
    outbound["participants"] = [
        aliases.get(str(speaker), str(speaker))
        for speaker in state.get("participants", [])
    ]
    outbound["participant_context"] = {
        aliases.get(str(speaker), str(speaker)): [
            anonymous_message(message) for message in messages
        ]
        for speaker, messages in state.get("participant_context", {}).items()
    }
    if state.get("focus_message"):
        outbound["focus_message"] = anonymous_message(state["focus_message"])
    evaluation = state.get("reply_evaluation")
    if evaluation:
        outbound["reply_evaluation"] = {
            "reply_index": evaluation.get("reply_index"),
            "reply": anonymous_message(evaluation.get("reply", {})),
            "responses": [
                anonymous_message(message)
                for message in evaluation.get("responses", [])
            ],
            "context_before": [
                anonymous_message(message)
                for message in evaluation.get("context_before", [])
            ],
        }
    return outbound

