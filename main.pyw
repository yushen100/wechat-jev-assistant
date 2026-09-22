from __future__ import annotations

import ctypes
import logging
from logging.handlers import RotatingFileHandler
import sys
import tkinter as tk
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_jev.config import LOG_DIR, ensure_runtime_dirs  # noqa: E402
from wechat_jev.ui import AssistantApp  # noqa: E402

_INSTANCE_MUTEX = None


def acquire_single_instance() -> bool:
    """避免重复双击启动出多个助手窗口。"""
    global _INSTANCE_MUTEX
    _INSTANCE_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, r"Local\WechatJevAssistant")
    return bool(_INSTANCE_MUTEX) and ctypes.windll.kernel32.GetLastError() != 183


def configure_logging() -> None:
    ensure_runtime_dirs()
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=512_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("wechat_jev")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)


def main() -> None:
    if not acquire_single_instance():
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TypeSafe.WechatJevAssistant")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    configure_logging()
    root = tk.Tk()
    AssistantApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
