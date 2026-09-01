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
class AgentConfig:
    """选哪个 agent，以及自研 agent 的连接参数。

    默认 `openclaw`：P0 的验收标准是**行为零变化**，所以默认值必须保持现状。
    """

    provider: str = "openclaw"                 # openclaw | lens
    url: str = "ws://127.0.0.1:18790"          # provider=lens 时的 Lens Agent Protocol 端点
    connect_timeout: float = 10.0
    budget_ms: int = 8000                      # 单轮延迟预算，超时即降级收尾
    agent_label: str = "答"                    # 状态条徽记（自研 agent）
    agent_name: str = "小龙虾"

    def __post_init__(self) -> None:
        if self.provider not in ("openclaw", "lens"):
            raise ValueError(f'agent.provider 只能是 "openclaw" 或 "lens"：{self.provider!r}')
        if self.budget_ms <= 0:
            raise ValueError(f"agent.budget_ms 必须为正：{self.budget_ms}")


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

    #: 按下 PTT 之后，等**第一块** PCM 的宽限期。
    #:
    #: 这段时间里要塞下：WS 下行 RTT + Flutter 下发 BLE 开麦命令 + 固件启麦 +
    #: 首块 PCM 经 BLE 回传 + 插件攒够 200ms + WS 上行。旧代码把它硬编码成 1.0s，
    #: 而 partial 循环 700ms 才检查一次 ⇒ 真实宽限只有 1.4s，真机上几乎必然误报
    #: 「麦克风没有声音」。默认放宽到 2.5s，**真机标定后回填**（B2）。
    mic_warmup_seconds: float = 2.5
    #: 音频已经在流了之后，多久没有新帧算链路断（mic 被别的 App 抢走 / BLE 掉线）。
    #: 与 warmup 是两件事：启麦慢是正常的，流到一半断掉不是。
    mic_gap_seconds: float = 0.8

    def __post_init__(self) -> None:
        # 配置来自用户手写的 JSON，类型错误比越界更常见（写成 "2.5" 而不是 2.5）。
        # 不先转类型的话，下面的比较会抛 TypeError，网关带着一句
        # "'<=' not supported between instances of 'str' and 'int'" 起不来 ——
        # 用户看不出是哪个键写错了。
        for field_name in ("mic_warmup_seconds", "mic_gap_seconds", "max_utterance_seconds"):
            try:
                object.__setattr__(self, field_name, float(getattr(self, field_name)))
            except (TypeError, ValueError):
                raise ValueError(
                    f"asr.{field_name} 必须是数字，当前是 {getattr(self, field_name)!r}") from None
        if self.mic_warmup_seconds <= 0 or self.mic_gap_seconds <= 0:
            raise ValueError("mic_warmup_seconds / mic_gap_seconds 必须为正数")
        if self.max_utterance_seconds <= self.mic_warmup_seconds:
            # 否则看门狗还没来得及判定，整句就先被超长截断了
            raise ValueError("max_utterance_seconds 必须大于 mic_warmup_seconds")


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
    session_ttl_seconds: float = 86400.0  # 修 S4：离线且静默超过此时长的会话被回收（0=永不）
    telemetry_stale_seconds: float = 60.0  # 遥测超过此时长未更新即标记 stale（协议 v1.1）
    battery_warn_percent: int = 15        # 低于此电量在页脚提示一次（0=关闭）

    def __post_init__(self) -> None:
        if self.throttle_ms < 0:
            raise ValueError(f"throttle_ms 不能为负：{self.throttle_ms}")
        if self.body_safety_px < 0 or self.body_safety_px >= 200:
            raise ValueError(f"body_safety_px 应在 [0, 200)：{self.body_safety_px}")
        for name, val in (("confirm_seconds", self.confirm_seconds),
                          ("confirm_seconds_low_conf", self.confirm_seconds_low_conf),
                          ("reading_idle_seconds", self.reading_idle_seconds),
                          ("final_short_linger_seconds", self.final_short_linger_seconds),
                          ("session_ttl_seconds", self.session_ttl_seconds)):
            if val < 0:
                raise ValueError(f"{name} 不能为负：{val}")
        if self.telemetry_stale_seconds <= 0:
            raise ValueError(f"telemetry_stale_seconds 必须为正：{self.telemetry_stale_seconds}")
        if not 0 <= self.battery_warn_percent <= 100:
            raise ValueError(f"battery_warn_percent 应在 [0, 100]：{self.battery_warn_percent}")


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8443
    plugin_dist: str = ""       # 插件构建产物目录（空=自动找 ../plugin/dist）
    #: 是否信任 X-Forwarded-For 作为客户端来源（**只有确实跑在反代后面才可以开**）。
    #: 直连时它是攻击者可随意伪造的请求头，开着等于让配对节流的按来源那一层形同虚设。
    trust_forwarded_for: bool = False
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    composer: ComposerConfig = field(default_factory=ComposerConfig)

    @property
    def agent_label(self) -> str:
        """状态条徽记：跟着当前 provider 走，而不是恒定读 openclaw 那一份。"""
        return (self.agent.agent_label if self.agent.provider == "lens"
                else self.openclaw.agent_label)

    @property
    def agent_name(self) -> str:
        return (self.agent.agent_name if self.agent.provider == "lens"
                else self.openclaw.agent_name)

    @staticmethod
    def load(path: Path | None = None) -> "Config":
        path = path or STATE_DIR / "config.json"
        cfg = Config()
        if path.exists():
            raw = json.loads(path.read_text())
            for key in ("host", "port", "plugin_dist", "trust_forwarded_for"):
                if key in raw:
                    setattr(cfg, key, raw[key])
            for key, cls in (("openclaw", OpenClawConfig), ("agent", AgentConfig),
                             ("asr", AsrConfig), ("composer", ComposerConfig)):
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


def control_secret() -> str:
    """控制面 / 管理 API 的共享密钥（首次生成，0600 落盘到状态目录）。

    **为什么不能继续用 loopback 判据**：原来的 `_require_loopback` 按 peername 判断，
    而本项目推荐的 TLS 方案（REPORT §6.4）正是 caddy `reverse_proxy 127.0.0.1:8443` ——
    反代之后**所有**请求的 peername 都变成 127.0.0.1，判据整体失效。
    由于 `host` 默认 `0.0.0.0`，这意味着反代一上，任何人都能 `POST /admin/pair-code`
    拿一个配对码把自己的手机配上来。这是今天就存在的活隐患，不是 MCP 才引入的。

    真正的边界因此落到**文件权限**上：能读状态目录的人 = 能管这台网关的人。
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / "control.secret"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32))
        f.chmod(0o600)
    return f.read_text().strip()


def jwt_secret() -> bytes:
    """进程间稳定的 JWT 签名密钥（首次生成，0600 落盘到状态目录）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / "jwt.secret"
    if not f.exists():
        f.write_bytes(secrets.token_bytes(32))
        f.chmod(0o600)
    return f.read_bytes()
