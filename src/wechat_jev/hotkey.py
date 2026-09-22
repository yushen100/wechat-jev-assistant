from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable


WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
VK_J = 0x4A
HOTKEY_ID = 0x4A45


class HotkeyListener:
    """在独立消息线程中监听 Ctrl+Alt+J，不触碰微信窗口。"""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="全局快捷键",
        )
        self._thread.start()
        self._ready.wait(timeout=2)

    def stop(self) -> None:
        thread_id = self._thread_id
        if thread_id:
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0

    def _run(self) -> None:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        self._thread_id = int(kernel32.GetCurrentThreadId())
        registered = bool(
            user32.RegisterHotKey(
                None,
                HOTKEY_ID,
                MOD_CONTROL | MOD_ALT,
                VK_J,
            )
        )
        self._ready.set()
        if not registered:
            self._thread_id = 0
            return
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                    self.callback()
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._thread_id = 0
