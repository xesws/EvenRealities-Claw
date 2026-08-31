"""语音链路：PTT / ASR / agent 编排。只通过 device.HudDevice 写屏。"""
from .pipeline import VoicePipeline, fmt_secs

__all__ = ["VoicePipeline", "fmt_secs"]
