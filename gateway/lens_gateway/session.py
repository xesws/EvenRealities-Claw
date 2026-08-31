"""设备会话 = 一块屏（`device.HudDevice`）+ 一条语音链路（`voice.VoicePipeline`）。

每台配对设备一个常驻会话对象（跨 WS 重连存活，服务器持有全部状态，
重连后重放 current_frame 即恢复现场——红队 R1/R6）。

本文件只做**装配与路由**，没有业务逻辑。这样切开是为了让 MCP 表面能拿到
一个不带麦克风与 agent 包袱的写屏对象（`session.hud`），并由帧租约仲裁
"用户在说话" 与 "外部工具想写屏" 的冲突。分层与租约的理由见 `device/hud.py`。
"""
from __future__ import annotations

import logging

from .asr import AsrEngine
from .config import Config
from .device import HudDevice, style_header
from .openclaw import OpenClawClient
from .voice import VoicePipeline

log = logging.getLogger(__name__)

__all__ = ["DeviceSession", "style_header"]


class DeviceSession:
    def __init__(self, device_id: str, cfg: Config, asr: AsrEngine, claw: OpenClawClient):
        self.device_id = device_id
        self.cfg = cfg
        self.session_key = f"lens:{device_id}"
        self.hud = HudDevice(device_id, cfg)
        self.voice = VoicePipeline(self.hud, cfg, asr, claw, self.session_key)

    # ---------- 只读视图（服务器与控制面用） ----------

    @property
    def state(self) -> str:
        return self.hud.state

    @property
    def seq(self) -> int:
        return self.hud.seq

    @property
    def current_frame(self) -> dict:
        return self.hud.current_frame

    @property
    def last_active(self) -> float:
        return self.hud.last_active

    def snapshot(self) -> dict:
        return self.hud.snapshot()

    # ---------- 连接绑定 ----------

    def attach(self, send) -> dict:
        """新 WS 绑定；返回 resume 帧。"""
        return self.hud.attach(send)

    def detach(self) -> None:
        self.hud.detach()
        self.voice.on_detach()

    # ---------- 客户端消息入口 ----------

    async def handle_text(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "ptt":
            action = msg.get("action")
            if action == "start":
                await self.voice.ptt_start()
            elif action == "stop":
                await self.voice.ptt_stop()
            elif action == "cancel":
                await self.voice.cancel_listening()
        elif mtype == "page":
            self.hud.page(1 if msg.get("dir") != "prev" else -1, source="glasses")
        elif mtype == "abort":
            await self.voice.abort()
        elif mtype == "reset":
            await self.voice.abort(silent=True)
            self.voice.reset()
            self.hud.emit_idle(urgent=True)

    async def handle_binary(self, data: bytes) -> None:
        await self.voice.feed_pcm(data)
