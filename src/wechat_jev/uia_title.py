from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class UiaText:
    text: str
    left: int
    top: int
    right: int
    bottom: int


_IGNORED = {
    "微信", "聊天信息", "语音聊天", "视频聊天", "更多", "最小化", "最大化", "关闭",
}


def choose_header_title(
    items: list[UiaText], window_rect: tuple[int, int, int, int]
) -> str | None:
    """从 UIA 文本中挑选微信聊天页左上方的会话标题。"""
    win_left, win_top, win_right, win_bottom = window_rect
    width = max(1, win_right - win_left)
    height = max(1, win_bottom - win_top)
    candidates: list[tuple[int, int, str]] = []
    for item in items:
        text = " ".join(item.text.split()).strip()
        if not text or text in _IGNORED or len(text) > 100:
            continue
        # 标题只可能位于窗口顶部约 12%，并避开左侧会话列表。
        center_y = (item.top + item.bottom) // 2
        if not (win_top <= center_y <= win_top + int(height * 0.13)):
            continue
        if item.left < win_left + int(width * 0.16):
            continue
        if item.left > win_left + int(width * 0.78):
            continue
        # 排除纯时间、计数和窗口按钮文字。
        if re.fullmatch(r"[\d\s:：()（）+-]+", text):
            continue
        candidates.append((item.top, item.left, text))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def read_wechat_title(hwnd: int, window_rect: tuple[int, int, int, int]) -> str | None:
    """被动读取 UIA 控件树；不激活窗口，不调用任何输入接口。"""
    try:
        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=hwnd)
        items: list[UiaText] = []
        for control in window.descendants(control_type="Text"):
            try:
                rect = control.rectangle()
                items.append(UiaText(
                    text=str(control.window_text() or ""),
                    left=int(rect.left), top=int(rect.top),
                    right=int(rect.right), bottom=int(rect.bottom),
                ))
            except Exception:
                continue
        return choose_header_title(items, window_rect)
    except Exception:
        return None
