"""设备会话 = 一块屏（`device.HudDevice`）+ 一条语音链路（`voice.VoicePipeline`）。

每台配对设备一个常驻会话对象（跨 WS 重连存活，服务器持有全部状态，
重连后重放 current_frame 即恢复现场——红队 R1/R6）。

本文件只做**装配与路由**，没有业务逻辑。这样切开是为了让 MCP 表面能拿到
一个不带麦克风与 agent 包袱的写屏对象（`session.hud`），并由帧租约仲裁
"用户在说话" 与 "外部工具想写屏" 的冲突。分层与租约的理由见 `device/hud.py`。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .asr import AsrEngine
from .config import Config
from .device import HudDevice, TelemetryStore, style_header
from .providers import AgentProvider, agent_is_trusted
from .voice import VoicePipeline

log = logging.getLogger(__name__)

SendFunc = Callable[[dict], Awaitable[None]]

__all__ = ["DeviceSession", "style_header"]


class DeviceSession:
    def __init__(self, device_id: str, cfg: Config, asr: AsrEngine, claw: AgentProvider):
        self.device_id = device_id
        self.cfg = cfg
        self.session_key = f"lens:{device_id}"
        self.hud = HudDevice(device_id, cfg)
        self.claw = claw
        # W6：溯源徽记**每帧现算**。绑一个探针比在若干时机点上各取样一次可靠得多 ——
        # 取样式写法必然留下时序窗口（冷启动、断线重连），而窗口里的那几帧
        # 正好是替身在答话却不打标的时候。理由写在 `HudDevice.agent_production`。
        self.hud.bind_agent_probe(lambda: agent_is_trusted(self.claw))
        self.telemetry = TelemetryStore(stale_seconds=cfg.composer.telemetry_stale_seconds)
        self.voice = VoicePipeline(self.hud, cfg, asr, claw, self.session_key,
                                   device_state=self.telemetry.snapshot)

        # 连接归会话所有：HUD 只管画面，遥测命令这类非帧消息走这里
        self._send: SendFunc | None = None
        self._cmd_seq = 0
        #: 已下发但未回执的命令 id → 命令名。没有它就分不清一条 cmd_result
        #: 到底是谁的回执，未来加第二种命令时会把结果串到遥测里去。
        self._pending_cmds: dict[str, str] = {}

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
        """只读快照。遥测**从未上报过就是 None**，调用方必须如实说"没有数据"。"""
        return {**self.hud.snapshot(),
                "telemetry": self.telemetry.snapshot(),
                "telemetry_diagnostics": self.telemetry.diagnostics()}

    # ---------- 连接绑定 ----------

    def attach(self, send: SendFunc) -> dict:
        """新 WS 绑定；返回 resume 帧。"""
        self._send = send
        self._pending_cmds.clear()   # 旧连接的未回执命令永远不会回来了
        # W6 徽记不用在这里取样：探针已在 __init__ 绑好，每帧现算。
        return self.hud.attach(send)

    def detach(self) -> None:
        self._send = None
        self.hud.detach()
        self.voice.on_detach()

    # ---------- 下行命令（协议 v1.1） ----------

    async def request_telemetry(self) -> str | None:
        """向插件下发一次遥测拉取。返回命令 id；设备离线返回 None。

        注意拉取回来的值**不保证新鲜**：官方没说明 `getDeviceInfo()` 是否真的触发
        一次 BLE 读取，手机端很可能直接返回缓存。所以回执会以 `source="poll"` 入库，
        与设备主动上报的 `push` 区分开（理由见 `device/telemetry.py`）。
        """
        return await self._send_cmd("telemetry")

    async def _send_cmd(self, cmd: str) -> str | None:
        if self._send is None:
            return None
        self._cmd_seq += 1
        cid = f"c{self._cmd_seq}"
        self._pending_cmds[cid] = cmd
        try:
            await self._send({"type": "cmd", "cmd": cmd, "id": cid})
        except Exception:
            self._pending_cmds.pop(cid, None)
            log.debug("下发命令失败（客户端已断开）")
            return None
        return cid

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
        elif mtype == "telemetry":
            # 插件在 onDeviceStatusChanged 时主动上报 —— 这是唯一"新鲜"的来源
            if self.telemetry.update(msg.get("data"), "push"):
                self._note_battery(msg.get("data"))
        elif mtype == "cmd_result":
            self._on_cmd_result(msg)

    def _note_battery(self, data: object) -> None:
        """把电量转告设备层。只有被遥测缓存**接受**的记录才会走到这里 ——
        戒指的 41% 不会变成眼镜的低电量告警。"""
        if not isinstance(data, dict):
            return
        level = data.get("batteryLevel")
        self.hud.note_battery(level if isinstance(level, int) else None,
                              data.get("isCharging") is True)

    def _on_cmd_result(self, msg: dict) -> None:
        cmd = self._pending_cmds.pop(str(msg.get("id")), None)
        if cmd is None:
            log.debug("收到无主的 cmd_result，已忽略：%s", msg.get("id"))
            return
        if not msg.get("ok"):
            log.info("命令 %s 在插件侧失败：%s", cmd, msg.get("error"))
            return
        if cmd == "telemetry" and self.telemetry.update(msg.get("data"), "poll"):
            self._note_battery(msg.get("data"))

    async def handle_binary(self, data: bytes) -> None:
        await self.voice.feed_pcm(data)
