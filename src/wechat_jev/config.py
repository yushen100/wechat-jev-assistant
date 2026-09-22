from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import CaptureRegion


APP_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = APP_DIR / "data"
LOG_DIR = APP_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
MESSAGE_LIMIT_OPTIONS = (100, 150, 200, 250)


@dataclass(slots=True)
class AppConfig:
    hotkey: str = "Ctrl+Alt+J"
    region: CaptureRegion = field(default_factory=CaptureRegion)
    max_messages: int = 100
    ocr_min_confidence: float = 0.45
    low_answer_confidence: float = 0.35
    autostart: bool = False
    region_profiles: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "AppConfig":
        if not CONFIG_PATH.exists():
            return cls()
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raw["region"] = CaptureRegion(**raw.get("region", {}))
        # 旧配置可能保存过实验值；只接受界面提供的四个稳定档位。
        configured_limit = int(raw.get("max_messages", 100))
        raw["max_messages"] = (
            configured_limit
            if configured_limit in MESSAGE_LIMIT_OPTIONS
            else 100
        )
        return cls(**raw)

    def set_message_limit(self, value: int) -> None:
        if value not in MESSAGE_LIMIT_OPTIONS:
            raise ValueError("分析条数只支持 100、150、200、250")
        self.max_messages = value
        self.save()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = CONFIG_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, CONFIG_PATH)

    def region_for(self, profile_key: str) -> CaptureRegion:
        stored = self.region_profiles.get(profile_key)
        return CaptureRegion(**stored) if stored else self.region

    def save_region(self, profile_key: str, region: CaptureRegion) -> None:
        region.validate()
        self.region = region
        self.region_profiles[profile_key] = asdict(region)
        self.save()


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
