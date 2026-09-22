from __future__ import annotations

import re


RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[邮箱]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证号]"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){16,19}(?!\d)"), "[银行卡号]"),
    (re.compile(r"(https?://[^\s?#]+)(?:\?[^\s#]*)?(?:#[^\s]*)?"), r"\1[参数已隐藏]"),
)


def redact_text(text: str) -> str:
    result = text
    for pattern, replacement in RULES:
        result = pattern.sub(replacement, result)
    return result


def redact_messages(messages: list[dict]) -> list[dict]:
    return [{**item, "text": redact_text(str(item.get("text", "")))} for item in messages]

