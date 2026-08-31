"""设备抽象层：屏幕是什么样、谁在写、怎么翻页，以及这台设备的遥测。

不知道麦克风与 agent 的存在 —— 语音链路在 `lens_gateway.voice`。
"""
from .hud import (
    EXTERNAL_STATE,
    HudDevice,
    Lease,
    LeaseError,
    LeaseHeld,
    LeaseInvalid,
    PAGEABLE,
    STATE_LABEL,
    style_header,
)
from .telemetry import TelemetryRecord, TelemetryStore

__all__ = [
    "EXTERNAL_STATE", "HudDevice", "Lease", "LeaseError", "LeaseHeld",
    "LeaseInvalid", "PAGEABLE", "STATE_LABEL", "style_header",
    "TelemetryRecord", "TelemetryStore",
]
