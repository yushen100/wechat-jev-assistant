from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Callable, Protocol

from .capture import (
    CaptureError,
    RapidOcrEngine,
    WindowContext,
    capture_chat,
    capture_chat_title,
    capture_recent_messages,
)
from .conversation_memory import (
    MEMORY_PARSER_VERSION,
    find_matching_memory,
    merge_with_memory,
    new_memory_payload,
)
from .crypto_store import EncryptedHistoryStore
from .models import CaptureRegion, ChatMessage


@dataclass(slots=True)
class MessageBatch:
    messages: list[ChatMessage]
    confidence: float
    warnings: list[str]
    source: str
    conversation_type: str = "unknown"
    contact_name: str = ""


def _infer_screen_conversation_type(messages: list[ChatMessage]) -> str:
    named = {
        message.speaker for message in messages
        if not message.is_self
        and message.speaker not in {"对方", "未知群成员"}
    }
    return "group" if named else "unknown"


class MessageSource(Protocol):
    def read_recent(
        self,
        context: WindowContext,
        region: CaptureRegion,
        max_messages: int,
        progress: Callable[[int | str], None] | None = None,
    ) -> MessageBatch: ...


class OcrMessageSource:
    """当前消息来源；未来可增加只读 SQLite/JSON/CSV 适配器。"""

    def __init__(self, engine: RapidOcrEngine) -> None:
        self.engine = engine

    def read_recent(
        self,
        context: WindowContext,
        region: CaptureRegion,
        max_messages: int,
        progress: Callable[[int | str], None] | None = None,
    ) -> MessageBatch:
        messages, confidence, warnings = capture_recent_messages(
            context, region, self.engine, max_messages, progress=progress
        )
        return MessageBatch(
            messages, confidence, warnings, "screen_ocr",
            _infer_screen_conversation_type(messages),
        )


class MemoryAwareMessageSource:
    def __init__(self, engine: RapidOcrEngine, store: EncryptedHistoryStore) -> None:
        self.engine = engine
        self.store = store

    def read_recent(
        self,
        context: WindowContext,
        region: CaptureRegion,
        max_messages: int,
        progress: Callable[[int | str], None] | None = None,
    ) -> MessageBatch:
        if progress:
            progress("正在校验当前会话与加密记忆……")
        image = capture_chat(context, region)
        try:
            current, confidence, warnings = self.engine.recognize(image, 200)
        finally:
            image.close()

        memory, overlap = find_matching_memory(current, self.store.list_memories())
        if memory is not None:
            merged, added, corrections = merge_with_memory(memory, current)
            if len(merged) < max_messages:
                if progress:
                    progress(
                        f"会话记忆只有 {len(merged)} 条，正在向上补读到 {max_messages} 条……"
                    )
                scanned, scan_confidence, scan_warnings = capture_recent_messages(
                    context, region, self.engine, max_messages, progress=progress
                )
                merged, added, extra_corrections = merge_with_memory(memory, scanned)
                corrections.extend(extra_corrections)
                warnings.extend(scan_warnings)
                confidence = scan_confidence
            payload = {
                "parser_version": MEMORY_PARSER_VERSION,
                "messages": [message.to_dict() for message in merged],
                "created_at": memory.get("created_at"),
            }
            self.store.upsert_memory(str(memory["memory_id"]), context.title, payload)
            if corrections:
                warnings.append("昵称已按会话记忆纠正：" + "、".join(sorted(set(corrections))))
            warnings.append(
                f"命中加密会话记忆：锚点重合 {overlap} 条，本次补充 {added} 条，"
                f"记忆共 {len(merged)} 条"
            )
            selected = merged[-max_messages:]
            if len(selected) < max_messages:
                warnings.append(
                    f"聊天已到最早位置，会话实际只读到 {len(selected)} 条有效消息"
                )
            average = sum(message.confidence for message in selected) / len(selected)
            return MessageBatch(
                selected, average, list(dict.fromkeys(warnings)),
                "encrypted_memory+screen_ocr", _infer_screen_conversation_type(selected),
            )

        messages, confidence, warnings = capture_recent_messages(
            context, region, self.engine, max_messages, progress=progress
        )
        memory_id, payload = new_memory_payload(messages)
        self.store.upsert_memory(memory_id, context.title, payload)
        warnings.append(f"未命中已有会话记忆，已建立新记忆：{len(messages)} 条")
        return MessageBatch(
            messages, confidence, list(dict.fromkeys(warnings)),
            "screen_ocr+encrypted_memory", _infer_screen_conversation_type(messages),
        )


class DatabaseFirstMessageSource:
    """UIA 优先读取会话标题，OCR 兜底；聊天正文只从数据库读取。"""

    def __init__(self, engine: RapidOcrEngine, store: EncryptedHistoryStore) -> None:
        self.engine = engine
        project = Path(__file__).resolve().parents[2]
        self.bridge = project / "integrations" / "wechatauto_readonly" / "readonly_bridge.py"

    def read_recent(
        self,
        context: WindowContext,
        region: CaptureRegion,
        max_messages: int,
        progress: Callable[[int | str], None] | None = None,
    ) -> MessageBatch:
        if not self.bridge.exists():
            raise CaptureError("数据库只读桥接器不存在")
        if progress:
            progress("正在识别会话标题；不会读取消息区……")
        title_source = "OCR"
        image = capture_chat_title(context, region)
        try:
            title = self.engine.recognize_title(image)
        finally:
            image.close()
        # RapidOCR/Win32 偶尔会带回孤立 UTF-16 代理字符。显式替换，且后续
        # 通过 ASCII 转义 JSON 字节流与隔离进程通信，避免 subprocess 再编码失败。
        title = "".join(
            "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char
            for char in str(title)
        )
        if progress:
            progress(f"已通过 {title_source} 识别会话“{title}”，正在读取数据库文字……")
        try:
            request_bytes = json.dumps(
                {"title": title, "limit": max_messages}, ensure_ascii=True
            ).encode("ascii")
            completed = subprocess.run(
                    [sys.executable, str(self.bridge), "--query-stdin"],
                    input=request_bytes,
                    capture_output=True,
                    timeout=40,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            payload = json.loads(stdout or "{}")
            if completed.returncode == 0 and payload.get("ok"):
                messages = [ChatMessage(**item) for item in payload.get("messages", [])]
                if messages:
                    matched_title = str(payload.get("matched_title") or title)
                    warnings = [
                        f"数据库文字模式：{title_source} 识别“{title}”，"
                        f"匹配“{matched_title}”，读取 {len(messages)} 条"
                    ]
                    return MessageBatch(
                            messages, 1.0, warnings, "wechat_database",
                            str(payload.get("conversation_type", "unknown")),
                            matched_title,
                        )
            raise CaptureError(str(payload.get("error", "数据库桥接器未返回文字消息")))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise CaptureError(f"数据库文字读取失败：{type(exc).__name__}") from exc
