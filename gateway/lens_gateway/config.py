"""Lens Gateway 配置。

状态目录默认 ~/.lens-gateway/（配置、设备库、JWT 密钥都在这里，绝不进 git 仓库）。
OpenClaw 网关 token 在运行时从其配置文件读取，永不出服务器、永不写入本仓库。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("LENS_STATE_DIR", "~/.lens-gateway")).expanduser()


@dataclass
class OpenClawConfig:
    url: str = "ws://127.0.0.1:18789"
    config_path: str = "~/.openclaw/openclaw.json"  # 运行时从这里读 gateway.auth.token
    agent_label: str = "工"   # 状态条徽记
    agent_name: str = "工部"

    def read_token(self) -> str:
        cfg = json.loads(Path(self.config_path).expanduser().read_text())
        return cfg["gateway"]["auth"]["token"]


@dataclass
class AsrConfig:
    # 本机（4×Neoverse-N1）实测：tiny partial≈670ms；base final RTF≈0.35；small RTF≈1.0 过慢
    partial_model: str = "tiny"    # 聆听态 partial（速度优先，仅供显示）
    final_model: str = "base"      # 松手 final（路由与发送只认它）
    language: str = "zh"
    compute_type: str = "int8"
    cpu_threads: int = 4
    hotwords: str = "工部、格物、都察、Hermes、OpenClaw、小龙虾、眼镜、链路、网关。"
    partial_interval_ms: int = 700
    partial_tail_seconds: float = 12.0  # partial 只解码最近 N 秒，控制 CPU
    max_utterance_seconds: float = 25.0


@dataclass
class ComposerConfig:
    """排版与帧编排。

    注意：这里**没有**「每行几个字」「每页几行」这类旋钮了 —— 它们由
    `formatting.layout` 的像素版式和固件的 27px 固定行高唯一决定
    （官方：*"Pagination is driven by the container's real pixel box,
    not a character budget."*）。旧的 `wrap_chars` / `lines_per_page`
    已移除；`Config.load` 遇到它们会告警并忽略，不会让网关起不来。
    """

    glyph_profile: str = "symbol"        # HUD 字形档位：symbol / cjk / ascii
    glyph_overrides: dict[str, str] = field(default_factory=dict)  # 按语义名覆盖单个字形
    body_safety_px: int = 0              # 正文折行的额外退让像素（默认 0：度量与固件逐位一致）
    throttle_ms: int = 500               # 同状态内容帧最小间隔（2Hz）
    confirm_seconds: float = 1.2         # 转写确认停留
    confirm_seconds_low_conf: float = 3.0
    low_conf_threshold: float = -0.9     # avg_logprob 低于此值视为低置信
    reading_idle_seconds: float = 60.0   # 阅读态无操作回待机
    final_short_linger_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.throttle_ms < 0:
            raise ValueError(f"throttle_ms 不能为负：{self.throttle_ms}")
        if self.body_safety_px < 0 or self.body_safety_px >= 200:
            raise ValueError(f"body_safety_px 应在 [0, 200)：{self.body_safety_px}")
        for name, val in (("confirm_seconds", self.confirm_seconds),
                          ("confirm_seconds_low_conf", self.confirm_seconds_low_conf),
                          ("reading_idle_seconds", self.reading_idle_seconds),
                          ("final_short_linger_seconds", self.final_short_linger_seconds)):
            if val < 0:
                raise ValueError(f"{name} 不能为负：{val}")


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8443
    plugin_dist: str = ""       # 插件构建产物目录（空=自动找 ../plugin/dist）
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    composer: ComposerConfig = field(default_factory=ComposerConfig)

    @staticmethod
    def load(path: Path | None = None) -> "Config":
        path = path or STATE_DIR / "config.json"
        cfg = Config()
        if path.exists():
            raw = json.loads(path.read_text())
            for key in ("host", "port", "plugin_dist"):
                if key in raw:
                    setattr(cfg, key, raw[key])
            for key, cls in (("openclaw", OpenClawConfig), ("asr", AsrConfig), ("composer", ComposerConfig)):
                if key not in raw:
                    continue
                known = cls().__dict__
                section = raw[key]
                unknown = sorted(set(section) - set(known))
                if unknown:
                    # 未知键**告警并忽略**，不再抛 TypeError。
                    # 原来的行为意味着：照着 PROTOCOL.md 写一个已改名的键，网关直接起不来。
                    log.warning("配置 %s 中的未知键已忽略：%s（文件：%s）", key, ", ".join(unknown), path)
                setattr(cfg, key, cls(**{**known, **{k: v for k, v in section.items() if k in known}}))
        return cfg

    def resolve_plugin_dist(self) -> Path | None:
        if self.plugin_dist:
            p = Path(self.plugin_dist).expanduser()
            return p if p.exists() else None
        p = Path(__file__).resolve().parents[2] / "plugin" / "dist"
        return p if p.exists() else None


def jwt_secret() -> bytes:
    """进程间稳定的 JWT 签名密钥（首次生成，0600 落盘到状态目录）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / "jwt.secret"
    if not f.exists():
        f.write_bytes(secrets.token_bytes(32))
        f.chmod(0o600)
    return f.read_bytes()
