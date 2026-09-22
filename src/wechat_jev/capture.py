from __future__ import annotations

import re
import ctypes
import hashlib
import os
import time
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import psutil
import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab

from .models import CaptureRegion, ChatMessage


EXPECTED_EXE = Path(r"D:\微信\Weixin\Weixin.exe")
TIME_RE = re.compile(r"^(?:昨天|今天|星期[一二三四五六日天]|周[一二三四五六日天])?\s*\d{1,2}:\d{2}$")


NEW_MESSAGE_RE = re.compile(r"^\d+\s*条新消息$")
CHAT_HEADER_RE = re.compile(r"^\d+\s*[（(]\d+[）)]$")


class CaptureError(RuntimeError):
    pass


@dataclass(slots=True)
class WindowContext:
    hwnd: int
    rect: tuple[int, int, int, int]
    title: str


def get_foreground_wechat() -> WindowContext:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or win32gui.IsIconic(hwnd):
        raise CaptureError("请先打开目标微信聊天窗口")
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        path = Path(psutil.Process(pid).exe())
    except (psutil.Error, OSError) as exc:
        raise CaptureError("无法确认当前窗口进程") from exc
    if path.name.lower() != "weixin.exe" or path.resolve() != EXPECTED_EXE.resolve():
        raise CaptureError("请先打开目标微信聊天窗口")
    rect = win32gui.GetWindowRect(hwnd)
    if rect[2] - rect[0] < 500 or rect[3] - rect[1] < 400:
        raise CaptureError("微信窗口过小，请先放大窗口")
    return WindowContext(hwnd=hwnd, rect=rect, title=win32gui.GetWindowText(hwnd).strip() or "当前会话")


def find_visible_wechat() -> WindowContext:
    candidates: list[WindowContext] = []

    def visit(hwnd: int, _extra: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            path = Path(psutil.Process(pid).exe())
            if path.name.lower() != "weixin.exe" or path.resolve() != EXPECTED_EXE.resolve():
                return True
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] >= 500 and rect[3] - rect[1] >= 400:
                candidates.append(WindowContext(hwnd, rect, win32gui.GetWindowText(hwnd).strip() or "当前会话"))
        except (psutil.Error, OSError, win32gui.error):
            pass
        return True

    win32gui.EnumWindows(visit, None)
    if not candidates:
        raise CaptureError("没有找到可见的微信主窗口，请先打开目标聊天")
    return max(candidates, key=lambda item: (item.rect[2] - item.rect[0]) * (item.rect[3] - item.rect[1]))


def capture_chat(context: WindowContext, region: CaptureRegion) -> Image.Image:
    region.validate()
    left, top, right, bottom = context.rect
    width, height = right - left, bottom - top
    bbox = (
        round(left + width * region.left),
        round(top + height * region.top),
        round(left + width * region.right),
        round(top + height * region.bottom),
    )
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def capture_chat_title(context: WindowContext, region: CaptureRegion) -> Image.Image:
    """截取整窗顶部标题栏；标题位置不应受消息区校准范围影响。"""
    left, top, right, bottom = context.rect
    width, height = right - left, bottom - top
    bbox = (
        left + 6,
        top + min(34, max(0, round(height * 0.035))),
        right - max(90, round(width * 0.10)),
        top + min(94, max(80, round(height * 0.085))),
    )
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def _image_fingerprint(image: Image.Image) -> str:
    sample = image.convert("L").resize((64, 64))
    try:
        return hashlib.sha1(sample.tobytes()).hexdigest()
    finally:
        sample.close()


def _perceptual_hash(image: Image.Image) -> int:
    """用于判断滚动前后画面是否基本不变，不用于保存聊天内容。"""
    sample = image.convert("L").resize((9, 8))
    try:
        values = list(sample.getdata())
    finally:
        sample.close()
    bits = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            bits = (bits << 1) | int(values[offset + column] > values[offset + column + 1])
    return bits


def _hash_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _message_key(message: ChatMessage) -> tuple[bool, str, str, str]:
    return (
        message.is_self,
        message.message_type,
        " ".join(message.text.split()),
        message.visual_fingerprint,
    )


def _normalized_text(message: ChatMessage) -> str:
    return re.sub(r"[^0-9A-Za-z一-鿿]+", "", message.text).lower()


def _messages_equivalent(left: ChatMessage, right: ChatMessage) -> bool:
    if left.is_self != right.is_self or left.message_type != right.message_type:
        return False
    if left.message_type == "visual":
        try:
            return _hash_distance(
                int(left.visual_fingerprint, 16), int(right.visual_fingerprint, 16)
            ) <= 10
        except (TypeError, ValueError):
            return bool(left.visual_fingerprint) and left.visual_fingerprint == right.visual_fingerprint
    left_text, right_text = _normalized_text(left), _normalized_text(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    shortest = min(len(left_text), len(right_text))
    if shortest < 4:
        return False
    return SequenceMatcher(None, left_text, right_text).ratio() >= 0.78


def _overlap_pairs(older: list[ChatMessage], newer: list[ChatMessage]) -> list[tuple[int, int]]:
    """在两屏交界处做模糊 LCS，对 OCR 漏字、漏行和图片轻微裁剪容错。"""
    old_start = max(0, len(older) - 60)
    left = older[old_start:]
    right = newer[:60]
    rows, columns = len(left), len(right)
    scores = [[0] * (columns + 1) for _ in range(rows + 1)]
    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            if _messages_equivalent(left[row - 1], right[column - 1]):
                scores[row][column] = scores[row - 1][column - 1] + 1
            else:
                scores[row][column] = max(scores[row - 1][column], scores[row][column - 1])
    pairs: list[tuple[int, int]] = []
    row, column = rows, columns
    while row and column:
        if (
            _messages_equivalent(left[row - 1], right[column - 1])
            and scores[row][column] == scores[row - 1][column - 1] + 1
        ):
            pairs.append((old_start + row - 1, column - 1))
            row -= 1
            column -= 1
        elif scores[row - 1][column] >= scores[row][column - 1]:
            row -= 1
        else:
            column -= 1
    pairs.reverse()
    if len(pairs) < 2:
        return []
    # 真正的跨屏重叠应贴近旧屏尾部和新屏头部；远处偶然相同不算。
    if len(older) - 1 - pairs[-1][0] > 4 or pairs[0][1] > 4:
        return []
    return pairs


def _overlap_size(older: list[ChatMessage], newer: list[ChatMessage]) -> int:
    return len(_overlap_pairs(older, newer))


def merge_older_page(older: list[ChatMessage], newer: list[ChatMessage]) -> list[ChatMessage]:
    """把较旧的一屏接到现有消息前，并去掉滚动重叠部分。"""
    if not older:
        return list(newer)
    if not newer:
        return list(older)
    pairs = _overlap_pairs(older, newer)
    # 旧屏保留到最后一个重叠锚点；新屏只追加该锚点之后的内容。
    merged = older + newer[pairs[-1][1] + 1:] if pairs else older + newer
    for order, item in enumerate(merged):
        item.order = order
    return merged


def _scroll_notches_for_region(context: WindowContext, region: CaptureRegion) -> int:
    """按校准聊天区的实际像素高度计算约 90% 屏的标准滚轮次数。"""
    window_height = context.rect[3] - context.rect[1]
    region_height = max(1, round(window_height * (region.bottom - region.top)))
    # Windows 默认每格滚轮约三行；微信一行按约 18 像素估算。
    # 上限避免极端 DPI/错误校准造成一次滚动过远。
    return max(4, min(40, round(region_height * 0.90 / 54)))


def _scroll_chat(context: WindowContext, region: CaptureRegion, direction: int) -> int:
    left, top, right, bottom = context.rect
    width, height = right - left, bottom - top
    x = round(left + width * ((region.left + region.right) / 2))
    y = round(top + height * ((region.top + region.bottom) / 2))
    target = win32gui.WindowFromPoint((x, y)) or context.hwnd
    notches = _scroll_notches_for_region(context, region)
    position = win32api.MAKELONG(x & 0xFFFF, y & 0xFFFF)
    # 必须拆成标准滚轮事件；微信会限制单个超大 wheel delta，导致看似设置
    # 了十几格，实际上只移动一小段。
    for _ in range(notches):
        delta = direction * win32con.WHEEL_DELTA
        win32gui.PostMessage(
            target,
            win32con.WM_MOUSEWHEEL,
            win32api.MAKELONG(0, delta & 0xFFFF),
            position,
        )
        time.sleep(0.006)
    return notches


def _scroll_to_bottom(
    context: WindowContext, region: CaptureRegion, max_steps: int = 16
) -> bool:
    """持续向下翻页直到画面不再变化，避免反向等次数仍停在历史位置。"""
    previous = capture_chat(context, region)
    try:
        previous_hash = _perceptual_hash(previous)
    finally:
        previous.close()
    for _ in range(max_steps):
        _scroll_chat(context, region, -1)
        time.sleep(0.16)
        current = capture_chat(context, region)
        try:
            current_hash = _perceptual_hash(current)
        finally:
            current.close()
        if _hash_distance(current_hash, previous_hash) <= 2:
            return True
        previous_hash = current_hash
    return False


def capture_recent_messages(
    context: WindowContext,
    region: CaptureRegion,
    ocr: "RapidOcrEngine",
    max_messages: int = 100,
    max_pages: int = 80,
    progress: Callable[[int | str], None] | None = None,
) -> tuple[list[ChatMessage], float, list[str]]:
    """从当前聊天位置向上翻页读取消息，完成后尽量恢复原滚动位置。"""
    collected: list[ChatMessage] = []
    confidences: list[float] = []
    warnings: list[str] = []
    pages_processed = 0
    scroll_count = 0
    baseline_hash: int | None = None
    previous_hash: int | None = None
    reached_top = False
    restored = True
    unverified_page_boundaries = 0
    deduplicated_count = 0

    # 每次约翻一整屏，并立即 OCR。按去重后的有效消息数计数，不拿重复项凑 100 条。
    try:
        for page_index in range(max_pages):
            image = capture_chat(context, region)
            try:
                current_hash = _perceptual_hash(image)
                if page_index == 0:
                    baseline_hash = current_hash
                elif previous_hash is not None and _hash_distance(current_hash, previous_hash) <= 2:
                    reached_top = True
                    break
                previous_hash = current_hash
                page_messages, page_confidence, page_warnings = ocr.recognize(image, 200)
            finally:
                image.close()
            pages_processed += 1
            if collected and _overlap_size(page_messages, collected) == 0:
                unverified_page_boundaries += 1
            before_merge = len(page_messages) + len(collected)
            collected = merge_older_page(page_messages, collected)
            deduplicated_count += before_merge - len(collected)
            confidences.append(page_confidence)
            warnings.extend(page_warnings)
            if progress:
                progress(
                    f"已读 {len(collected)}/{max_messages} 条有效消息"
                    f"（{pages_processed} 屏，去重 {deduplicated_count} 条）"
                )
            if len(collected) >= max_messages:
                break
            if page_index == max_pages - 1:
                break
            _scroll_chat(context, region, 1)
            scroll_count += 1
            time.sleep(0.24)
    finally:
        # 使用相同的一屏步长恢复到用户触发分析时的位置。
        for _ in range(scroll_count):
            _scroll_chat(context, region, -1)
            time.sleep(0.05)
        time.sleep(0.20)
        restored = _scroll_to_bottom(context, region)

    selected = collected[-max_messages:]
    if len(selected) >= 9:
        keys = [_message_key(message) for message in selected]
        triplets = [tuple(keys[index:index + 3]) for index in range(len(keys) - 2)]
        repeated_windows = sum(count - 1 for count in Counter(triplets).values())
        if repeated_windows / len(triplets) >= 0.35:
            raise CaptureError("跨屏去重校验失败：检测到大量重复消息，本次结果未保存也未提交分析")
    named_speakers = {
        message.speaker for message in selected
        if not message.is_self and message.speaker not in {"对方", "未知群成员"}
    }
    if named_speakers:
        for message in selected:
            if not message.is_self and message.speaker == "对方":
                message.speaker = "未知群成员"
                message.sender_source = "unknown"
                message.speaker_confidence = min(message.speaker_confidence, 0.35)
    for order, message in enumerate(selected):
        message.order = order
    if len(selected) < max_messages:
        if not reached_top:
            raise CaptureError(
                f"补读后仍不足 {max_messages} 条：{pages_processed} 屏只得到 "
                f"{len(selected)} 条有效消息，本次未提交分析"
            )
        reason = "已到达当前可加载的最早位置"
        warnings.append(f"未读满 {max_messages} 条：只识别到 {len(selected)} 条有效消息（{reason}）")
    if not restored:
        warnings.append("未能确认聊天滚动位置完全恢复，请检查微信当前位置")
    if unverified_page_boundaries:
        warnings.append(
            f"读取到 {len(selected)} 条，但有 {unverified_page_boundaries} 处跨屏没有共同消息，"
            "无法确认它们是连续的最近消息"
        )
    elif pages_processed > 1:
        warnings.append(f"已通过 {pages_processed - 1} 处跨屏重叠校验")
    if deduplicated_count:
        warnings.append(f"跨屏合并已去除 {deduplicated_count} 条重复识别")
    uncertain_senders = sum(message.speaker == "未知群成员" for message in selected)
    uncertain_visuals = sum(
        message.message_type == "visual" and message.content_source == "placeholder"
        for message in selected
    )
    if uncertain_senders:
        warnings.append(f"有 {uncertain_senders} 条群聊消息未可靠识别成员，已标记为未知群成员")
    if uncertain_visuals:
        warnings.append(f"有 {uncertain_visuals} 条图片或表情没有可靠文字，只保留类型占位")
    visual_owners: dict[str, set[str]] = {}
    for message in selected:
        if message.visual_fingerprint:
            visual_owners.setdefault(message.visual_fingerprint, set()).add(message.speaker)
    if any(len(owners) > 1 for owners in visual_owners.values()):
        warnings.append("检测到相同图片在不同发送者之间归属冲突，请核对区域或结果")
    low_identity = sum(
        not message.is_self and message.speaker_confidence < 0.6 for message in selected
    )
    if low_identity:
        warnings.append(f"有 {low_identity} 条消息的身份可靠度偏低")
    unique_warnings = list(dict.fromkeys(warnings))
    average = (
        sum(message.confidence for message in selected) / len(selected)
        if selected else (sum(confidences) / len(confidences) if confidences else 0.0)
    )
    return selected, average, unique_warnings


def display_profile(context: WindowContext) -> str:
    monitor = win32api.MonitorFromWindow(context.hwnd, 2)
    info = win32api.GetMonitorInfo(monitor)
    left, top, right, bottom = info["Monitor"]
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(context.hwnd))
    except Exception:
        dpi = 96
    return f"{right - left}x{bottom - top}@{dpi}"


class RapidOcrEngine:
    def __init__(self) -> None:
        os.environ.setdefault("OMP_NUM_THREADS", "2")
        os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()

    def recognize_title(self, image: Image.Image) -> str:
        result, _ = self._engine(image)
        candidates = []
        for box, text, score in result or []:
            clean = " ".join(str(text).split()).strip()
            # OCR 偶尔会返回孤立 UTF-16 代理字符，无法编码成 UTF-8。
            clean = clean.encode("utf-8", errors="replace").decode("utf-8")
            if not clean or float(score) < 0.70 or clean in {"微信", "Weixin"}:
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            center_x = sum(xs) / len(xs)
            center_y = sum(ys) / len(ys)
            candidates.append((center_y, abs(center_x - image.width / 2), -float(score), clean))
        if not candidates:
            raise CaptureError("无法识别当前会话标题")
        return min(candidates)[3]

    def recognize(self, image: Image.Image, max_messages: int = 8) -> tuple[list[ChatMessage], float, list[str]]:
        result, _ = self._engine(image)
        width = image.width
        text_items: list[dict[str, Any]] = []
        for box, text, score in result or []:
            clean = " ".join(str(text).split()).strip()
            if (
                not clean
                or TIME_RE.match(clean)
                or NEW_MESSAGE_RE.match(clean)
                or clean in {"以下为新消息", "查看更多消息"}
            ):
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            center_x = sum(xs) / len(xs)
            center_y = sum(ys) / len(ys)
            if (
                CHAT_HEADER_RE.match(clean)
                and min(ys) < image.height * 0.06
                and min(xs) < image.width * 0.15
            ):
                continue
            if 0.38 * width <= center_x <= 0.62 * width and len(clean) <= 12:
                continue
            text_items.append(
                {
                    "x1": min(xs), "x2": max(xs), "y1": min(ys), "y2": max(ys),
                    "center_x": center_x, "center_y": center_y, "height": max(ys) - min(ys),
                    "text": clean, "score": float(score),
                    "side": "self" if center_x >= width * 0.56 else "incoming",
                    "kind": "text",
                }
            )

        visual_items = self._detect_visual_items(image)
        # 短文字气泡可能因深色主题和抗锯齿被视觉检测误认为图片。
        # 若块较矮且内部有可靠 OCR 文字，按普通文字气泡处理。
        text_bubble_visuals: set[str] = set()
        for visual in visual_items:
            inner = [
                item for item in text_items
                if item["side"] == visual["side"]
                and float(visual["x1"]) - 5 <= float(item["center_x"]) <= float(visual["x2"]) + 5
                and float(visual["y1"]) - 3 <= float(item["center_y"]) <= float(visual["y2"]) + 3
                and float(item["score"]) >= 0.72
            ]
            if float(visual["height"]) <= 60 and inner:
                text_bubble_visuals.add(str(visual["visual_fingerprint"]))
        visual_items = [
            visual for visual in visual_items
            if str(visual["visual_fingerprint"]) not in text_bubble_visuals
        ]
        outside_text: list[dict[str, Any]] = []
        for item in text_items:
            containing = next(
                (
                    visual for visual in visual_items
                    if item["side"] == visual["side"]
                    and float(visual["x1"]) - 5 <= float(item["center_x"]) <= float(visual["x2"]) + 5
                    and float(visual["y1"]) - 3 <= float(item["center_y"])
                    <= float(visual["y2"]) + (30 if float(visual["height"]) > 60 else 3)
                ),
                None,
            )
            if containing is None:
                outside_text.append(item)
            elif float(item["score"]) >= 0.72:
                containing.setdefault("inner_text", []).append(item)

        for visual in visual_items:
            inner = sorted(visual.pop("inner_text", []), key=lambda item: item["center_y"])
            if inner:
                content = " ".join(str(item["text"]) for item in inner)[:100]
                visual["text"] = f"[图片或表情，识别文字：{content}]"
                visual["score"] = sum(float(item["score"]) for item in inner) / len(inner)
                visual["content_source"] = "image_ocr"
            else:
                visual["text"] = "[图片或表情，内容不确定]"
                visual["score"] = 0.55
                visual["content_source"] = "placeholder"

        items = outside_text + visual_items
        items.sort(key=lambda item: (float(item["center_y"]), float(item["center_x"])))
        if not items:
            raise CaptureError("没有识别到文字、图片或表情消息，请校准截取区域")

        incoming_heights = [
            float(item["height"]) for item in items
            if item["side"] == "incoming" and item.get("kind") == "text"
        ]
        median_height = sorted(incoming_heights)[len(incoming_heights) // 2] if incoming_heights else 20.0
        sender_indices: set[int] = set()
        sender_bindings: dict[int, tuple[str, float]] = {}
        sender_height_limit = max(26.0, median_height * 0.90)
        for index, item in enumerate(items):
            if item["side"] != "incoming" or item.get("kind") != "text":
                continue
            text = str(item["text"])
            if (
                not 1 <= len(text) <= 20
                or float(item["height"]) > sender_height_limit
                or float(item["score"]) < 0.72
                or float(item["x2"]) > width * 0.50
                or text.startswith("[")
            ):
                continue
            for later_index, later in enumerate(items[index + 1:], start=index + 1):
                gap = float(later["y1"]) - float(item["y2"])
                if gap < 0:
                    continue
                # 大图/视频的可见前景可能从容器内部较低的位置才开始。
                # 只要它仍是同侧下一内容块，就按微信固定版式绑定到昵称。
                gap_limit = 115 if later.get("kind") == "visual" else 32
                if gap > gap_limit:
                    break
                if (
                    later["side"] == "incoming"
                    and abs(float(later["x1"]) - float(item["x1"])) <= 120
                    and (
                        later.get("kind") == "visual"
                        or float(later["x1"]) - float(item["x1"]) >= 8
                    )
                    and len(str(later["text"])) > 0
                ):
                    sender_indices.add(index)
                    sender_bindings[later_index] = (text, float(item["score"]))
                    break

        grouped: list[ChatMessage] = []
        last_y: float | None = None
        active_sender: tuple[str, float] | None = None
        page_has_names = bool(sender_bindings)
        known_sender_names = {value[0] for value in sender_bindings.values()}
        for index, item in enumerate(items):
            if index in sender_indices:
                continue
            center_y = float(item["center_y"])
            text = str(item["text"])
            score = float(item["score"])
            is_self = item["side"] == "self"
            if (
                page_has_names
                and not is_self
                and str(item["text"]) in known_sender_names
                and index not in sender_bindings
                and float(item["y2"]) >= image.height * 0.93
            ):
                continue
            if is_self:
                speaker = "我方"
                speaker_confidence = 1.0
                sender_source = "position"
                active_sender = None
            else:
                if index in sender_bindings:
                    active_sender = sender_bindings[index]
                elif last_y is None or center_y - last_y >= 42:
                    active_sender = None
                if active_sender:
                    speaker, speaker_confidence = active_sender
                    sender_source = "nickname_binding"
                elif page_has_names:
                    speaker, speaker_confidence = "未知群成员", 0.35
                    sender_source = "unknown"
                else:
                    speaker, speaker_confidence = "对方", 0.75
                    sender_source = "position"
            message_type = "visual" if item.get("kind") == "visual" else "text"
            content_source = str(item.get("content_source", "text_ocr"))
            can_merge = (
                grouped
                and grouped[-1].speaker == speaker
                and grouped[-1].message_type == "text"
                and message_type == "text"
                and last_y is not None
                and center_y - last_y < 42
            )
            if can_merge:
                grouped[-1].text += text
                grouped[-1].confidence = min(grouped[-1].confidence, score)
            else:
                grouped.append(
                    ChatMessage(
                        speaker=speaker,
                        text=text,
                        order=len(grouped),
                        confidence=score,
                        is_self=is_self,
                        speaker_confidence=speaker_confidence,
                        message_type=message_type,
                        sender_source=sender_source,
                        content_source=content_source,
                        visual_fingerprint=str(item.get("visual_fingerprint", "")),
                    )
                )
            last_y = center_y

        selected = grouped[-max_messages:]
        for order, message in enumerate(selected):
            message.order = order
        average = sum(message.confidence for message in selected) / len(selected)
        warnings: list[str] = []
        if average < 0.65:
            warnings.append("OCR 置信度偏低，请核对原文或重新校准区域")
        participants = list(dict.fromkeys(message.speaker for message in selected if not message.is_self))
        if selected[-1].is_self:
            warnings.append("最后识别到的是我方消息，分析可能不是针对最新对方消息")
        if len(participants) > 1:
            warnings.append(f"已按群聊模式识别 {len(participants)} 位发言者：{'、'.join(participants)}")
        elif participants == ["对方"]:
            warnings.append("未识别到群成员昵称；如为群聊，请扩大框选区域以包含气泡上方昵称")
        return selected, average, warnings

    @staticmethod
    def _detect_visual_items(image: Image.Image) -> list[dict[str, Any]]:
        """检测没有可读文字的图片/表情块，并按左右位置判断发送方。"""
        rgb = image.convert("RGB")
        sample = rgb.resize((64, 64))
        try:
            quantized = [
                (red // 16 * 16, green // 16 * 16, blue // 16 * 16)
                for red, green, blue in sample.getdata()
            ]
            background = Counter(quantized).most_common(1)[0][0]
        finally:
            sample.close()

        width, height = rgb.size
        pixels = rgb.load()
        # 排除两侧头像列，避免头像与图片/表情被合成一个视觉块。
        left_bounds = (max(1, round(width * 0.082)), round(width * 0.48))
        right_bounds = (round(width * 0.52), min(width - 1, round(width * 0.92)))
        row_threshold = max(12, round(width * 0.025))
        active_rows: list[tuple[int, str, int]] = []

        def is_foreground(pixel: tuple[int, int, int]) -> bool:
            return sum(abs(pixel[index] - background[index]) for index in range(3)) > 70

        for y in range(height):
            left_count = sum(is_foreground(pixels[x, y]) for x in range(*left_bounds))
            right_count = sum(is_foreground(pixels[x, y]) for x in range(*right_bounds))
            strongest = max(left_count, right_count)
            if strongest >= row_threshold:
                active_rows.append((y, "self" if right_count > left_count else "incoming", strongest))

        bands: list[list[tuple[int, str, int]]] = []
        for row in active_rows:
            if not bands or row[0] - bands[-1][-1][0] > 3:
                bands.append([row])
            else:
                bands[-1].append(row)

        visuals: list[dict[str, Any]] = []
        for band in bands:
            y1, y2 = band[0][0], band[-1][0]
            if y2 - y1 < 30:
                continue
            side_votes = Counter(row[1] for row in band)
            side = side_votes.most_common(1)[0][0]
            side_x1, side_x2 = right_bounds if side == "self" else left_bounds
            foreground_xs = [
                x
                for y in range(y1, y2 + 1, 2)
                for x in range(side_x1, side_x2, 2)
                if is_foreground(pixels[x, y])
            ]
            if not foreground_xs:
                continue
            x1, x2 = max(side_x1, min(foreground_xs) - 4), min(side_x2, max(foreground_xs) + 5)
            source_crop = rgb.crop((x1, y1, x2, y2 + 1))
            crop = source_crop.resize((48, 48))
            try:
                color_count = len({
                    (red // 24, green // 24, blue // 24)
                    for red, green, blue in crop.getdata()
                })
                fingerprint_sample = source_crop.resize((32, 32)).convert("RGB")
                try:
                    visual_fingerprint = f"{_perceptual_hash(fingerprint_sample):016x}"
                finally:
                    fingerprint_sample.close()
            finally:
                crop.close()
                source_crop.close()
            # 普通纯色文字气泡颜色很少；照片和表情通常有明显色彩/明暗变化。
            if color_count < 10:
                continue
            center_x = (x1 + x2) / 2
            visuals.append(
                {
                    "x1": float(x1),
                    "x2": float(x2),
                    "y1": float(y1),
                    "y2": float(y2),
                    "center_x": center_x,
                    "center_y": (y1 + y2) / 2,
                    "height": float(y2 - y1),
                    "text": "",
                    "score": 0.55,
                    "side": side,
                    "kind": "visual",
                    "visual_fingerprint": visual_fingerprint,
                }
            )
        rgb.close()
        return visuals
