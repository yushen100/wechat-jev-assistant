from __future__ import annotations

import json
import logging
import queue
import re
import threading
import tkinter as tk
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

import win32gui
from PIL import Image, ImageDraw

from .autostart import set_autostart
from .capture import CaptureError, RapidOcrEngine, WindowContext, display_profile, find_visible_wechat, get_foreground_wechat
from .config import APP_DIR, DATA_DIR, MESSAGE_LIMIT_OPTIONS, AppConfig
from .crypto_store import EncryptedHistoryStore
from .hotkey import HotkeyListener
from .models import CaptureRegion, ChatMessage
from .message_source import DatabaseFirstMessageSource
from .privacy import redact_messages
from .region_selector import RegionSelector
from .typesafe_client import TypeSafeClient, TypeSafeError, build_reply_evaluation, limit_messages_for_state, select_focus_participants


LOG = logging.getLogger("wechat_jev")
APP_ICON_PNG = APP_DIR / "assets" / "0922_jev助手图标_v1.png"
APP_ICON_ICO = APP_DIR / "assets" / "0922_jev助手图标_v1.ico"

SPEAKER_PALETTE = (
    "#1565C0",
    "#C62828",
    "#6A1B9A",
    "#EF6C00",
    "#00838F",
    "#AD1457",
    "#4527A0",
    "#558B2F",
)

KEY_RESULT_COLORS = {
    "后续确在回应我：": "#1565C0",
    "实际效果：": "#EF6C00",
    "有效性：": "#2E7D32",
}

SUMMARY_RESULT_COLORS = {
    "阶段：": "#6A1B9A",
    "需求：": "#1565C0",
    "意图：": "#C62828",
    "紧张：": "#EF6C00",
    "建议：": "#2E7D32",
    "字面含义：": "#00838F",
    "在乎试探 ": "#6A1B9A",
    "直接回答风险 ": "#EF6C00",
    "先回应感受 ": "#1565C0",
    "冲突升级 ": "#C62828",
    "已经缓和 ": "#2E7D32",
    "停止解释 ": "#616161",
}


def history_record_people(record: dict[str, Any]) -> list[str]:
    people = []
    for message in record.get("raw_messages", []):
        speaker = str(message.get("speaker", "")).strip()
        if speaker and speaker not in {"我方", "对方", "未知群成员"} and speaker not in people:
            people.append(speaker)
    conversation_type = str(
        record.get("redacted_state", {}).get("conversation", {}).get("type", "")
    )
    if conversation_type == "private":
        contact = history_record_contact(record)
        if contact and contact not in people:
            people.append(contact)
    return people


def history_record_contact(record: dict[str, Any]) -> str:
    contact = str(record.get("contact", "")).strip()
    if contact and contact not in {"微信", "Weixin", "当前会话"}:
        return contact
    for warning in record.get("warnings", []):
        match = re.search(r"匹配[“『\"]([^”』\"]+)[”』\"]", str(warning))
        if match:
            return match.group(1).strip()
    return contact or "当前会话"


def fuzzy_history_match(query: str, values: list[str]) -> bool:
    key = "".join(query.lower().split())
    if not key:
        return True
    for value in values:
        candidate = "".join(str(value).lower().split())
        if key in candidate or candidate in key:
            return True
        if min(len(key), len(candidate)) >= 2:
            threshold = 0.50 if max(len(key), len(candidate)) <= 2 else 0.68
            if SequenceMatcher(None, key, candidate).ratio() >= threshold:
                return True
    return False


def history_local_time(record: dict[str, Any]) -> str:
    value = str(record.get("created_at", ""))
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value.replace("T", " ")[:19]


def history_record_matches(
    record: dict[str, Any], person: str = "", conversation: str = "", date: str = ""
) -> bool:
    person_key = person.strip().lower()
    conversation_key = conversation.strip().lower()
    date_key = date.strip()
    if person_key and not fuzzy_history_match(
        person_key, history_record_people(record)
    ):
        return False
    if conversation_key and not fuzzy_history_match(
        conversation_key, [history_record_contact(record)]
    ):
        return False
    if date_key and not history_local_time(record).startswith(date_key):
        return False
    return True


def build_speaker_colors(messages: list[dict[str, Any]]) -> dict[str, str]:
    colors: dict[str, str] = {"我方": "#07883D", "未知群成员": "#616161", "对方": "#1565C0"}
    palette_index = 0
    for message in messages:
        speaker = str(message.get("speaker", "")).strip()
        if not speaker or speaker in colors:
            continue
        while SPEAKER_PALETTE[palette_index % len(SPEAKER_PALETTE)] in colors.values():
            palette_index += 1
        colors[speaker] = SPEAKER_PALETTE[palette_index % len(SPEAKER_PALETTE)]
        palette_index += 1
    return colors


class AssistantApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = AppConfig.load()
        self.store = EncryptedHistoryStore(DATA_DIR)
        self.client = TypeSafeClient()
        self.ocr: RapidOcrEngine | None = None
        self.message_source: DatabaseFirstMessageSource | None = None
        self.hotkey = HotkeyListener(self._on_hotkey)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker_lock = threading.Lock()
        self.last_context: WindowContext | None = None
        self.last_messages: list[ChatMessage] = []
        self.tray = None

        self._build_window()
        self.hotkey.start()
        self._start_tray()
        self.root.after(100, self._poll_events)

    def _build_window(self) -> None:
        self.root.title("Jev 对话辅助")
        if APP_ICON_ICO.exists():
            self.root.iconbitmap(default=str(APP_ICON_ICO))
        if APP_ICON_PNG.exists():
            self._window_icon = tk.PhotoImage(file=str(APP_ICON_PNG))
            self.root.iconphoto(True, self._window_icon)
        self.root.geometry("460x720+20+80")
        self.root.minsize(420, 560)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        header = ttk.Frame(self.root, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="Jev 对话辅助", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="就绪：在微信聊天窗口按 Ctrl+Alt+J")
        ttk.Label(self.root, textvariable=self.status_var, padding=(12, 0, 12, 8), foreground="#555").pack(fill="x")

        limit_bar = ttk.LabelFrame(self.root, text="分析条数", padding=(10, 5))
        limit_bar.pack(fill="x", padx=12, pady=(0, 8))
        self.message_limit_var = tk.IntVar(value=self.config.max_messages)
        for value in MESSAGE_LIMIT_OPTIONS:
            ttk.Radiobutton(
                limit_bar,
                text=f"{value} 条",
                value=value,
                variable=self.message_limit_var,
                command=self._change_message_limit,
            ).pack(side="left", expand=True, padx=4)

        self.output = scrolledtext.ScrolledText(
            self.root, wrap="word", font=("Microsoft YaHei UI", 10), padx=12, pady=12, state="disabled"
        )
        self.output.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        actions = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        actions.pack(fill="x")
        ttk.Button(actions, text="重新分析", command=self.reanalyze).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="区域校准", command=self.open_calibration).pack(side="left", padx=6)
        ttk.Button(actions, text="查看历史", command=self.open_history).pack(side="left", padx=6)
        ttk.Button(actions, text="隐藏", command=self.hide).pack(side="right")

        menu = tk.Menu(self.root)
        settings = tk.Menu(menu, tearoff=False)
        self.autostart_var = tk.BooleanVar(value=self.config.autostart)
        settings.add_checkbutton(label="开机启动", variable=self.autostart_var, command=self._toggle_autostart)
        settings.add_command(label="退出程序", command=self.quit)
        menu.add_cascade(label="设置", menu=settings)
        self.root.config(menu=menu)

        self._set_output(
            "使用方法\n\n1. 打开微信目标聊天。\n2. 按 Ctrl+Alt+J。"
            "\n3. 等待会话校验、只读数据库读取和 Jev 分析。"
            "\n\nOCR/UIA 只识别会话标题；程序不会操作输入框或自动发送消息。"
        )

    def _on_hotkey(self) -> None:
        self.events.put(("analyze", None))

    def _change_message_limit(self) -> None:
        value = int(self.message_limit_var.get())
        self.config.set_message_limit(value)
        self.status_var.set(f"已切换为分析最近 {value} 条；下次分析生效")

    def _poll_events(self) -> None:
        try:
            while True:
                name, payload = self.events.get_nowait()
                if name == "analyze":
                    self.start_analysis()
                elif name == "show":
                    self.root.deiconify()
                    self.root.lift()
                elif name == "quit":
                    self.quit()
                elif name == "success":
                    self._show_result(payload)
                elif name == "error":
                    self._show_error(str(payload))
                elif name == "status":
                    self.status_var.set(str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def start_analysis(self, context: WindowContext | None = None) -> None:
        if not self.worker_lock.acquire(blocking=False):
            self.status_var.set("正在分析，请稍候……")
            return
        if context is None:
            try:
                # 必须在悬浮窗 lift 之前锁定微信；否则前台窗口会变成助手自己。
                context = get_foreground_wechat()
            except CaptureError as exc:
                self.worker_lock.release()
                self._show_error(str(exc))
                return
        self.status_var.set(
            f"正在读取聊天记录：0/{self.config.max_messages} 条……"
        )
        self._set_output("正在读取并分析当前微信聊天……\n\n旧分析结果已清除，请稍候。")
        self.root.deiconify()
        self.root.lift()
        threading.Thread(target=self._analysis_worker, args=(context,), daemon=True, name="分析任务").start()

    def _analysis_worker(self, context: WindowContext | None) -> None:
        stage = "准备分析"
        try:
            if context is None:
                raise CaptureError("没有锁定微信目标聊天窗口")
            try:
                win32gui.SetForegroundWindow(context.hwnd)
            except Exception:
                pass
            self.last_context = context
            stage = "读取聊天区域"
            region = self.config.region_for(display_profile(context))
            if self.ocr is None:
                self.ocr = RapidOcrEngine()
                self.message_source = DatabaseFirstMessageSource(self.ocr, self.store)
            assert self.message_source is not None
            batch = self.message_source.read_recent(
                context,
                region,
                self.config.max_messages,
                progress=lambda value: self.events.put(
                    (
                        "status",
                        value if isinstance(value, str)
                        else f"正在识别聊天记录：{value}/{self.config.max_messages} 条……",
                    )
                ),
            )
            messages, ocr_confidence, warnings = batch.messages, batch.confidence, batch.warnings
            resolved_contact = batch.contact_name or context.title
            stage = "校验识别结果"
            if ocr_confidence < self.config.ocr_min_confidence:
                raise CaptureError("OCR 置信度过低，请打开区域校准后重试")
            if not any(not message.is_self for message in messages):
                raise CaptureError("没有识别到对方消息，请校准截取区域")
            self.last_messages = messages
            stage = "整理分析上下文"
            raw_messages = [message.to_dict() for message in messages]
            analysis_messages = [
                message for message in raw_messages
                if message.get("message_type") == "text"
                or (
                    message.get("message_type") == "sticker"
                    and str(message.get("content_source", "")).startswith(
                        "wechat_database:动画表情"
                    )
                )
            ]
            if not any(message.get("speaker") != "我方" for message in analysis_messages):
                raise CaptureError("最近记录中没有可分析的对方文字或动画表情")
            ignored = len(raw_messages) - len(analysis_messages)
            if ignored:
                warnings.append(
                    f"已读取 {len(raw_messages)} 条；只分析 {len(analysis_messages)} 条文字/表情，"
                    f"忽略 {ignored} 条图片、视频、语音、文件或系统消息"
                )
            redacted_all = redact_messages(analysis_messages)
            redacted = limit_messages_for_state(redacted_all)
            if len(redacted) < len(redacted_all):
                warnings.append(
                    f"已读取 {len(redacted_all)} 条；为避免超过 TypeSafe 上下文上限，本次提交最近 {len(redacted)} 条"
                )
            participants, omitted_participants = select_focus_participants(redacted)
            if not participants:
                detected_participants = list(dict.fromkeys(
                    str(message.get("speaker", "")) for message in redacted
                    if message.get("speaker") != "我方"
                ))
                participants = detected_participants[:1]
            if omitted_participants:
                warnings.append(
                    f"群聊人物较多：仅独立分析最近相关的 {len(participants)} 人，"
                    f"其余 {omitted_participants} 人只保留在整体上下文"
                )
            participant_context = {
                speaker: [
                    message for message in redacted
                    if message.get("speaker") == speaker
                ][-8:]
                for speaker in participants
            }
            focus_index, focus_message = next(
                (index, message)
                for index, message in reversed(list(enumerate(redacted)))
                if message.get("speaker") != "我方"
            )
            my_reply_count, reply_evaluation = build_reply_evaluation(redacted)
            state = {
                "conversation": {
                    "contact": resolved_contact,
                    "id": batch.conversation_id,
                    "source": batch.source,
                    "type": batch.conversation_type,
                },
                "messages": redacted,
                "participants": participants,
                "participant_context": participant_context,
                "focus_index": focus_index,
                "focus_message": focus_message,
                "my_reply_count": my_reply_count,
                "reply_evaluation": reply_evaluation,
            }
            self.events.put((
                "status",
                f"已读取 {len(messages)} 条，只提交 {len(redacted)} 条文字/表情，正在调用 Jev……",
            ))
            stage = "调用 TypeSafe"
            result = self.client.evaluate_isolated(state)
            if result.fallback_used:
                warnings.append("TypeSafe 首次响应超时，已自动使用最近 40 条、最多 3 个重点人物完成分析")
            stage = "保存加密历史"
            record = {
                "contact": resolved_contact,
                "raw_messages": raw_messages,
                "redacted_state": state,
                "response": result.payload,
                "elapsed_seconds": result.elapsed_seconds,
                "ocr_confidence": ocr_confidence,
                "warnings": warnings,
            }
            self.store.add(resolved_contact, record)
            self.events.put(("success", record))
            LOG.info("分析成功；消息数=%s；OCR=%.2f；耗时=%.2f", len(messages), ocr_confidence, result.elapsed_seconds)
        except (CaptureError, TypeSafeError) as exc:
            LOG.warning(
                "分析失败；类型=%s；原因=%s",
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            self.events.put(("error", f"{stage}失败：{exc}"))
        except Exception as exc:
            LOG.exception("分析发生未预期异常；类型=%s", type(exc).__name__)
            self.events.put(("error", f"{stage}失败：{type(exc).__name__}"))
        finally:
            self.worker_lock.release()

    def _show_result(self, record: dict[str, Any]) -> None:
        response = record["response"]
        answers = response["answers"]
        low = self.config.low_answer_confidence

        def choice_title(answer: dict[str, Any]) -> str:
            if float(answer.get("confidence", 0)) < low:
                return "无法可靠判断"
            return str(answer.get("choice", "无法判断"))

        raw_messages = record["raw_messages"]
        speaker_colors = build_speaker_colors(raw_messages)
        situation = answers["situation"]
        relationship_need = answers["relationship_need"]
        literal = answers["literal_only"]
        intent = answers["intent"]
        tension = answers["tension"]
        action = answers["next_action"]
        focus = record.get("redacted_state", {}).get("focus_message", {})
        focus_text = str(focus.get("text", "")).replace("\n", " ")
        if len(focus_text) > 72:
            focus_text = focus_text[:69] + "…"
        score = float(tension.get("score", 0))
        level = max(0, min(9, round(score)))
        legend = str(tension.get("legend", {}).get(str(level), "")).strip()
        literal_yes = float(literal.get("noul", 0))
        conversation_type = (
            record.get("redacted_state", {}).get("conversation", {}).get("type", "unknown")
        )
        scene_title = {"group": "群聊", "private": "私聊"}.get(
            conversation_type, "场景未知"
        )

        lines = [
            f"会话：{record['contact']}  ·  {scene_title}  ·  有效上下文 {len(raw_messages)} 条",
            f"我方回复：{int(record.get('redacted_state', {}).get('my_reply_count', 0))} 条",
            f"重点：● {focus.get('speaker', '对方')}：{focus_text or '无可靠文字'}",
            "",
            "【一眼总览】",
            f"阶段：{choice_title(situation)} {float(situation.get('confidence', 0)):.0%}",
            f"需求：{choice_title(relationship_need)} {float(relationship_need.get('confidence', 0)):.0%}",
            f"意图：{choice_title(intent)} {float(intent.get('confidence', 0)):.0%}",
            f"紧张：{score:.1f}/9" + (f"｜{legend}" if legend else ""),
            f"建议：{choice_title(action)} {float(action.get('confidence', 0)):.0%}",
            f"字面含义：是 {literal_yes:.0%}｜否 {1 - literal_yes:.0%}",
            "",
            "【关键判断】",
            (
                f"在乎试探 {float(answers.get('cares_test', {}).get('noul', .5)):.0%}"
                f"｜直接回答风险 {float(answers.get('concrete_reply_risk', {}).get('noul', .5)):.0%}"
                f"｜先回应感受 {float(answers.get('needs_repair_first', {}).get('noul', .5)):.0%}"
            ),
            (
                f"冲突升级 {float(answers.get('conflict_emergency', {}).get('noul', .5)):.0%}"
                f"｜已经缓和 {float(answers.get('deescalated', {}).get('noul', .5)):.0%}"
                f"｜停止解释 {float(answers.get('stop_explaining', {}).get('noul', .5)):.0%}"
            ),
        ]

        reply_evaluation = record.get("redacted_state", {}).get("reply_evaluation")
        if reply_evaluation:
            reply_answer = answers.get("my_reply_effectiveness", {})
            reply_outcome = answers.get("my_reply_outcome", {})
            reply_relevance = answers.get("my_reply_relevance", {})
            reply_score = float(reply_answer.get("score", 0))
            reply_level = max(0, min(9, round(reply_score)))
            reply_legend = str(
                reply_answer.get("legend", {}).get(str(reply_level), "")
            ).strip()
            reply_text = str(reply_evaluation.get("reply", {}).get("text", "")).replace("\n", " ")
            if len(reply_text) > 60:
                reply_text = reply_text[:57] + "…"
            lines.extend([
                "",
                "【我的回答复盘】",
                f"本次回答：{reply_text or '无可靠文字'}",
                f"后续确在回应我：{float(reply_relevance.get('noul', 0)):.0%}",
                f"实际效果：{choice_title(reply_outcome)} {float(reply_outcome.get('confidence', 0)):.0%}",
                f"有效性：{reply_score:.1f}/9" + (f"｜{reply_legend}" if reply_legend else ""),
            ])
        else:
            lines.extend(["", "【我的回答复盘】", "暂无已获得对方后续反馈的我方回答，暂不评分。"])

        participants = [
            str(value) for value in record.get("redacted_state", {}).get("participants", [])
            if value and value not in {"我方", "对方", "未知群成员"}
        ]
        if participants:
            lines.extend(["", "【人物意图】"])
            for index, speaker in enumerate(participants):
                answer = answers.get(f"person_intent_{index}")
                if not answer:
                    continue
                lines.append(
                    f"● {speaker}：{choice_title(answer)} {float(answer.get('confidence', 0)):.0%}"
                )

        important_words = ("未读满", "去重", "无法确认", "偏低", "失败", "未知群成员")
        important_warnings = [
            warning for warning in record.get("warnings", [])
            if any(word in warning for word in important_words)
        ][:2]
        if important_warnings:
            lines.extend(["", "注意：" + "；".join(important_warnings)])
        lines.append("仅作概率辅助，不代表对方真实想法。")
        self._set_output("\n".join(lines))
        self._apply_speaker_colors(speaker_colors)
        self._apply_key_result_colors()
        self._apply_summary_result_colors(self.output)
        self.status_var.set("分析完成")
        self._position_next_to_wechat()
        self.root.deiconify()
        self.root.lift()

    def _show_error(self, message: str) -> None:
        self._set_output(f"无法完成分析\n\n{message}\n\n请确保微信目标聊天位于前台，再按 Ctrl+Alt+J。")
        self.status_var.set("分析失败")
        self.root.deiconify()
        self.root.lift()

    def _set_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def _apply_speaker_colors(self, speaker_colors: dict[str, str]) -> None:
        self.output.configure(state="normal")
        for index, (speaker, color) in enumerate(speaker_colors.items()):
            tag = f"speaker_{index}"
            self.output.tag_configure(tag, foreground=color, font=("Microsoft YaHei UI", 10, "bold"))
            start = "1.0"
            while True:
                found = self.output.search(speaker, start, stopindex="end")
                if not found:
                    break
                line_start = f"{found} linestart"
                line_end = f"{found} lineend"
                line_text = self.output.get(line_start, line_end)
                if f"{speaker}：" in line_text or f"● {speaker}" in line_text:
                    self.output.tag_add(tag, line_start, line_end)
                start = f"{found}+{len(speaker)}c"
        self.output.configure(state="disabled")

    def _apply_key_result_colors(self) -> None:
        self.output.configure(state="normal")
        for index, (prefix, color) in enumerate(KEY_RESULT_COLORS.items()):
            tag = f"key_result_{index}"
            self.output.tag_configure(
                tag,
                foreground=color,
                font=("Microsoft YaHei UI", 10, "bold"),
            )
            start = "1.0"
            while True:
                found = self.output.search(prefix, start, stopindex="end")
                if not found:
                    break
                self.output.tag_add(tag, f"{found} linestart", f"{found} lineend")
                start = f"{found}+{len(prefix)}c"
        self.output.configure(state="disabled")

    def _apply_summary_result_colors(self, widget: tk.Text) -> None:
        widget.configure(state="normal")
        for index, (prefix, color) in enumerate(SUMMARY_RESULT_COLORS.items()):
            tag = f"summary_result_{index}"
            widget.tag_configure(
                tag,
                foreground=color,
                font=("Microsoft YaHei UI", 10, "bold"),
            )
            start = "1.0"
            while True:
                found = widget.search(prefix, start, stopindex="end")
                if not found:
                    break
                line_end = widget.index(f"{found} lineend")
                separator = widget.search("｜", found, stopindex=line_end)
                segment_end = separator if separator else line_end
                widget.tag_add(tag, found, segment_end)
                start = f"{segment_end}+1c" if separator else line_end
        widget.configure(state="disabled")

    def _position_next_to_wechat(self) -> None:
        if not self.last_context:
            return
        left, top, right, _ = self.last_context.rect
        width = 560
        screen_width = self.root.winfo_screenwidth()
        x = right + 8 if right + width + 8 <= screen_width else max(0, left - width - 8)
        y = max(0, top)
        self.root.geometry(f"{width}x720+{x}+{y}")

    def reanalyze(self) -> None:
        if not self.last_context:
            messagebox.showinfo("重新分析", "请在微信目标聊天窗口按 Ctrl+Alt+J。")
            return
        self.start_analysis(self.last_context)

    def open_calibration(self) -> None:
        self.root.withdraw()

        def launch_selector() -> None:
            try:
                context = find_visible_wechat()
                self.last_context = context
                win32gui.SetForegroundWindow(context.hwnd)
                self.root.after(250, lambda: RegionSelector(self.root, context.rect, on_selected, on_cancel))
            except (CaptureError, win32gui.error) as exc:
                self.root.deiconify()
                messagebox.showerror("无法校准", str(exc))

        def on_selected(region: CaptureRegion) -> None:
            assert self.last_context is not None
            profile = display_profile(self.last_context)
            self.config.save_region(profile, region)
            self.root.deiconify()
            self.root.lift()
            messagebox.showinfo("校准完成", f"拖选区域已保存到显示配置 {profile}。回到微信按 Ctrl+Alt+J 验证。")

        def on_cancel() -> None:
            self.root.deiconify()
            self.root.lift()

        self.root.after(150, launch_selector)

    def open_history(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("加密分析历史")
        window.geometry("900x520")
        window.attributes("-topmost", True)
        filters = ttk.Frame(window, padding=(10, 10, 10, 0))
        filters.pack(fill="x")
        person_filter = tk.StringVar()
        conversation_filter = tk.StringVar()
        date_filter = tk.StringVar()
        ttk.Label(filters, text="人物").pack(side="left")
        ttk.Entry(filters, textvariable=person_filter, width=13).pack(side="left", padx=(5, 10))
        ttk.Label(filters, text="会话").pack(side="left")
        ttk.Entry(filters, textvariable=conversation_filter, width=16).pack(side="left", padx=(5, 10))
        ttk.Label(filters, text="日期").pack(side="left")
        ttk.Entry(filters, textvariable=date_filter, width=12).pack(side="left", padx=(5, 5))
        tree = ttk.Treeview(window, columns=("time", "contact", "summary"), show="headings")
        tree.heading("time", text="时间")
        tree.heading("contact", text="会话")
        tree.heading("summary", text="主要意图 / 建议动作")
        tree.column("time", width=170)
        tree.column("contact", width=160)
        tree.column("summary", width=520)
        records = self.store.list_recent()
        by_id = {str(record["id"]): record for record in records}

        def refresh() -> None:
            for value in tree.get_children():
                tree.delete(value)
            person_text = person_filter.get()
            conversation_text = conversation_filter.get()
            date_text = date_filter.get()
            for record in records:
                contact = history_record_contact(record)
                created = history_local_time(record)
                if not history_record_matches(
                    record,
                    person=person_text,
                    conversation=conversation_text,
                    date=date_text,
                ):
                    continue
                answers = record.get("response", {}).get("answers", {})
                intent = answers.get("intent", {}).get("choice", "-")
                action = answers.get("next_action", {}).get("choice", "-")
                tree.insert("", "end", iid=str(record["id"]), values=(created, contact, f"{intent} / {action}"))

        ttk.Button(filters, text="筛选", command=refresh).pack(side="left", padx=8)
        ttk.Button(
            filters,
            text="重置",
            command=lambda: (
                person_filter.set(""),
                conversation_filter.set(""),
                date_filter.set(""),
                refresh(),
            ),
        ).pack(side="left")
        refresh()
        tree.pack(fill="both", expand=True, padx=10, pady=10)

        def show_selected(_: object = None) -> None:
            selected = tree.selection()
            if not selected:
                return
            record = by_id[selected[0]]
            detail = tk.Toplevel(window)
            detail.title("历史详情")
            text = scrolledtext.ScrolledText(detail, wrap="word", width=100, height=32)
            text.pack(fill="both", expand=True)
            answers = record.get("response", {}).get("answers", {})
            low = self.config.low_answer_confidence

            def choice(answer: dict[str, Any]) -> str:
                if float(answer.get("confidence", 0)) < low:
                    return "无法可靠判断"
                return str(answer.get("choice", "无法判断"))

            focus = record.get("redacted_state", {}).get("focus_message", {})
            focus_text = str(focus.get("text", "")).replace("\n", " ")
            if len(focus_text) > 90:
                focus_text = focus_text[:87] + "…"
            tension = answers.get("tension", {})
            tension_score = float(tension.get("score", 0))
            tension_level = max(0, min(9, round(tension_score)))
            tension_legend = str(
                tension.get("legend", {}).get(str(tension_level), "")
            ).strip()
            literal_yes = float(answers.get("literal_only", {}).get("noul", 0))
            state = record.get("redacted_state", {})
            lines = [
                f"会话：{history_record_contact(record)}",
                f"时间：{history_local_time(record)}",
                f"有效上下文：{len(record.get('raw_messages', []))} 条",
                f"我方回复：{int(state.get('my_reply_count', 0))} 条",
                f"重点：● {focus.get('speaker', '对方')}：{focus_text or '无可靠文字'}",
                "",
                "【一眼总览】",
                f"阶段：{choice(answers.get('situation', {}))} {float(answers.get('situation', {}).get('confidence', 0)):.0%}",
                f"需求：{choice(answers.get('relationship_need', {}))} {float(answers.get('relationship_need', {}).get('confidence', 0)):.0%}",
                f"意图：{choice(answers.get('intent', {}))} {float(answers.get('intent', {}).get('confidence', 0)):.0%}",
                f"紧张：{tension_score:.1f}/9" + (f"｜{tension_legend}" if tension_legend else ""),
                f"建议：{choice(answers.get('next_action', {}))} {float(answers.get('next_action', {}).get('confidence', 0)):.0%}",
                f"字面含义：是 {literal_yes:.0%}｜否 {1 - literal_yes:.0%}",
                "",
                "【关键判断】",
                f"在乎试探 {float(answers.get('cares_test', {}).get('noul', .5)):.0%}｜直接回答风险 {float(answers.get('concrete_reply_risk', {}).get('noul', .5)):.0%}｜先回应感受 {float(answers.get('needs_repair_first', {}).get('noul', .5)):.0%}",
                f"冲突升级 {float(answers.get('conflict_emergency', {}).get('noul', .5)):.0%}｜已经缓和 {float(answers.get('deescalated', {}).get('noul', .5)):.0%}｜停止解释 {float(answers.get('stop_explaining', {}).get('noul', .5)):.0%}",
            ]
            reply_evaluation = state.get("reply_evaluation")
            if reply_evaluation:
                reply = str(reply_evaluation.get("reply", {}).get("text", "")).replace("\n", " ")
                reply_score = float(answers.get("my_reply_effectiveness", {}).get("score", 0))
                reply_level = max(0, min(9, round(reply_score)))
                reply_legend = str(answers.get("my_reply_effectiveness", {}).get("legend", {}).get(str(reply_level), "")).strip()
                lines.extend([
                    "", "【我的回答复盘】",
                    f"本次回答：{reply}",
                    f"后续确在回应我：{float(answers.get('my_reply_relevance', {}).get('noul', 0)):.0%}",
                    f"实际效果：{choice(answers.get('my_reply_outcome', {}))} {float(answers.get('my_reply_outcome', {}).get('confidence', 0)):.0%}",
                    f"有效性：{reply_score:.1f}/9" + (f"｜{reply_legend}" if reply_legend else ""),
                ])
            participants = [
                str(value) for value in state.get("participants", [])
                if value and value not in {"我方", "对方", "未知群成员"}
            ]
            if participants:
                lines.extend(["", "【人物意图】"])
                for index, speaker in enumerate(participants):
                    answer = answers.get(f"person_intent_{index}", {})
                    if answer:
                        lines.append(f"● {speaker}：{choice(answer)} {float(answer.get('confidence', 0)):.0%}")
            lines.extend(["", "仅作概率辅助，不代表对方真实想法。"])
            text.insert("1.0", "\n".join(lines))
            for index, (prefix, color) in enumerate(KEY_RESULT_COLORS.items()):
                tag = f"detail_key_{index}"
                text.tag_configure(tag, foreground=color, font=("Microsoft YaHei UI", 10, "bold"))
                start = "1.0"
                while True:
                    found = text.search(prefix, start, stopindex="end")
                    if not found:
                        break
                    text.tag_add(tag, f"{found} linestart", f"{found} lineend")
                    start = f"{found}+{len(prefix)}c"
            self._apply_summary_result_colors(text)
            text.configure(state="disabled")

        def delete_selected() -> None:
            selected = list(tree.selection())
            if not selected or not messagebox.askyesno("确认删除", f"确定删除选中的 {len(selected)} 条历史吗？此操作不可恢复。"):
                return
            selected_ids = [int(value) for value in selected]
            self.store.delete_ids(selected_ids)
            records[:] = [record for record in records if record["id"] not in selected_ids]
            for value in selected:
                by_id.pop(value, None)
                tree.delete(value)

        def delete_all() -> None:
            if not messagebox.askyesno("确认清空", "确定清空全部加密历史吗？此操作不可恢复。"):
                return
            self.store.delete_all()
            records.clear()
            by_id.clear()
            for value in tree.get_children():
                tree.delete(value)

        def delete_filtered() -> None:
            visible = [int(value) for value in tree.get_children()]
            if not visible or not messagebox.askyesno("确认删除筛选结果", f"确定删除当前筛选出的 {len(visible)} 条历史吗？此操作不可恢复。"):
                return
            self.store.delete_ids(visible)
            records[:] = [record for record in records if record["id"] not in visible]
            for value in list(tree.get_children()):
                by_id.pop(value, None)
                tree.delete(value)

        tree.bind("<Double-1>", show_selected)
        controls = ttk.Frame(window, padding=10)
        controls.pack(fill="x")
        ttk.Button(controls, text="查看详情", command=show_selected).pack(side="left")
        ttk.Button(controls, text="删除选中", command=delete_selected).pack(side="left", padx=8)
        ttk.Button(controls, text="删除筛选结果", command=delete_filtered).pack(side="left")
        ttk.Button(controls, text="清空全部", command=delete_all).pack(side="right")

    def _toggle_autostart(self) -> None:
        enabled = self.autostart_var.get()
        try:
            set_autostart(enabled, APP_DIR / "main.pyw")
            self.config.autostart = enabled
            self.config.save()
        except OSError as exc:
            self.autostart_var.set(not enabled)
            messagebox.showerror("设置失败", f"无法修改开机启动：{exc}")

    def _start_tray(self) -> None:
        try:
            import pystray

            if APP_ICON_PNG.exists():
                image = Image.open(APP_ICON_PNG).convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
            else:
                image = Image.new("RGB", (64, 64), "#1565C0")
                draw = ImageDraw.Draw(image)
                draw.ellipse((12, 13, 52, 47), fill="white")
            menu = pystray.Menu(
                pystray.MenuItem("显示", lambda: self.events.put(("show", None))),
                pystray.MenuItem("退出", lambda: self.events.put(("quit", None))),
            )
            self.tray = pystray.Icon("wechat_jev", image, "微信 Jev 助手", menu)
            threading.Thread(target=self.tray.run, daemon=True, name="托盘图标").start()
        except Exception:
            LOG.warning("托盘启动失败", exc_info=True)

    def hide(self) -> None:
        self.root.withdraw()

    def quit(self) -> None:
        self.hotkey.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()
