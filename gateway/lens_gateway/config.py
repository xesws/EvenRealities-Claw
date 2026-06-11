"""Lens Gateway 配置。

状态目录默认 ~/.lens-gateway/（配置、设备库、JWT 密钥都在这里，绝不进 git 仓库）。
OpenClaw 网关 token 在运行时从其配置文件读取，永不出服务器、永不写入本仓库。
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

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
    wrap_chars: int = 17        # 每行汉字数（真机实测后校准）
    lines_per_page: int = 5
    throttle_ms: int = 500      # 同状态内容帧最小间隔（2Hz）
    confirm_seconds: float = 1.2     # 转写确认停留
    confirm_seconds_low_conf: float = 3.0
    low_conf_threshold: float = -0.9  # avg_logprob 低于此值视为低置信
    reading_idle_seconds: float = 60.0  # 阅读态无操作回待机
    final_short_linger_seconds: float = 15.0


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
                if key in raw:
                    setattr(cfg, key, cls(**{**cls().__dict__, **raw[key]}))
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
