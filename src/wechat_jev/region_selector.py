from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageGrab, ImageTk

from .models import CaptureRegion


def region_from_points(start: tuple[int, int], end: tuple[int, int], width: int, height: int) -> CaptureRegion:
    start_x = min(width, max(0, start[0]))
    end_x = min(width, max(0, end[0]))
    start_y = min(height, max(0, start[1]))
    end_y = min(height, max(0, end[1]))
    x1, x2 = sorted((start_x, end_x))
    y1, y2 = sorted((start_y, end_y))
    region = CaptureRegion(left=x1 / width, top=y1 / height, right=x2 / width, bottom=y2 / height)
    region.validate()
    return region


class RegionSelector:
    """在微信窗口截图上提供类似截图工具的拖框选择器。"""

    def __init__(
        self,
        parent: tk.Tk,
        window_rect: tuple[int, int, int, int],
        on_selected: Callable[[CaptureRegion], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self.on_selected = on_selected
        self.on_cancel = on_cancel
        self.left, self.top, self.right, self.bottom = window_rect
        self.width = self.right - self.left
        self.height = self.bottom - self.top
        self.start: tuple[int, int] | None = None
        self.end: tuple[int, int] | None = None
        self.original = ImageGrab.grab(bbox=window_rect, all_screens=True).convert("RGB")
        self.dimmed = ImageEnhance.Brightness(self.original).enhance(0.38)
        try:
            self.font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 16)
        except OSError:
            self.font = ImageFont.load_default()

        self.window = tk.Toplevel(parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(f"{self.width}x{self.height}+{self.left}+{self.top}")
        self.window.configure(cursor="crosshair")
        self.canvas = tk.Canvas(self.window, width=self.width, height=self.height, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.photo: ImageTk.PhotoImage | None = None
        self._render()

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.window.bind("<Escape>", self._cancel)
        self.window.focus_force()
        self.window.grab_set()

    def _render(self) -> None:
        frame = self.dimmed.copy()
        if self.start and self.end:
            x1, x2 = sorted((self.start[0], self.end[0]))
            y1, y2 = sorted((self.start[1], self.end[1]))
            x1, x2 = max(0, x1), min(self.width, x2)
            y1, y2 = max(0, y1), min(self.height, y2)
            if x2 > x1 and y2 > y1:
                frame.paste(self.original.crop((x1, y1, x2, y2)), (x1, y1))
                draw = ImageDraw.Draw(frame)
                draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline="#00e676", width=3)
                label = f" {x2 - x1} × {y2 - y1}  松开鼠标保存 "
                draw.rectangle((x1, max(0, y1 - 26), x1 + max(190, len(label) * 10), y1), fill="#00a854")
                draw.text((x1 + 6, max(2, y1 - 22)), label, fill="white", font=self.font)
        draw = ImageDraw.Draw(frame)
        draw.rounded_rectangle((18, 16, min(self.width - 18, 560), 58), radius=8, fill="#111111")
        draw.text((32, 27), "拖动框选聊天消息区域 · 松开保存 · Esc 取消", fill="white", font=self.font)
        self.photo = ImageTk.PhotoImage(frame)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

    def _press(self, event: tk.Event) -> None:
        self.start = (int(event.x), int(event.y))
        self.end = self.start
        self._render()

    def _move(self, event: tk.Event) -> None:
        if not self.start:
            return
        self.end = (int(event.x), int(event.y))
        self._render()

    def _release(self, event: tk.Event) -> None:
        if not self.start:
            return
        self.end = (int(event.x), int(event.y))
        try:
            region = region_from_points(self.start, self.end, self.width, self.height)
        except ValueError:
            self.start = None
            self.end = None
            self._render()
            return
        self.window.grab_release()
        self.window.destroy()
        self.original.close()
        self.dimmed.close()
        self.on_selected(region)

    def _cancel(self, _event: tk.Event | None = None) -> None:
        self.window.grab_release()
        self.window.destroy()
        self.original.close()
        self.dimmed.close()
        self.on_cancel()
