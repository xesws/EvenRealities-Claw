"""语音链路：PTT → PCM → ASR → 确认窗口 → agent → 流式上屏。

这一层不直接构造帧，只调用 `HudDevice` 的写屏方法 —— 屏幕归设备层管，
它自己只负责"什么时候该显示什么"。这样 MCP 表面能与语音链路共用同一块屏，
并由设备层的租约仲裁两者。
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..asr import AsrEngine, StablePrefixTracker
from ..config import Config
from ..device import HudDevice, style_header
from ..formatting import tail_window
from ..openclaw import OpenClawClient

log = logging.getLogger(__name__)


def fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


class VoicePipeline:
    def __init__(self, hud: HudDevice, cfg: Config, asr: AsrEngine, claw: OpenClawClient,
                 session_key: str):
        self.hud = hud
        self.cfg = cfg
        self.asr = asr
        self.claw = claw
        self.session_key = session_key

        self._pcm = bytearray()
        self._listen_started = 0.0
        self._last_audio = 0.0
        self._tracker = StablePrefixTracker()
        self._partial_task: asyncio.Task | None = None
        self._confirm_task: asyncio.Task | None = None

        self._style_sent = False
        self._last_question = ""

    # ---------------------------------------------------------------- 生命周期

    @property
    def listening(self) -> bool:
        return self.hud.listening

    def on_detach(self) -> None:
        """WS 断开：正在录音就取消（PCM 已经不会再来了）。"""
        if self.listening:
            asyncio.ensure_future(self.cancel_listening())

    def reset(self) -> None:
        """`reset` 消息：回到干净状态。

        修 S5b：`_style_sent` 以前**从不复位** —— agent 会话被重置之后，
        小屏风格指令再也不会重新注入，模型会按默认习惯输出长文与 markdown。
        """
        self._style_sent = False
        self._last_question = ""

    # ---------------------------------------------------------------- 音频入口

    async def feed_pcm(self, data: bytes) -> None:
        if not self.listening:
            return
        self._pcm.extend(data)
        self._last_audio = time.monotonic()
        max_bytes = int(self.cfg.asr.max_utterance_seconds * 16000) * 2
        if len(self._pcm) >= max_bytes:  # 软上限：自动松手（R10）
            await self.ptt_stop()

    # ---------------------------------------------------------------- 聆听

    async def ptt_start(self) -> None:
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()  # 确认期重按 = 重说
        self.hud.cancel_timer()
        self._pcm.clear()
        self._tracker.reset()
        self.hud.listening = True
        self._listen_started = time.monotonic()
        self._last_audio = self._listen_started
        # 用户开口 ⇒ 屏幕无条件归语音链路（会抢占 MCP 的写屏租约）
        self.hud.emit("S2", self.hud.status_line("S2", "0:00"), "", urgent=True)
        if self._partial_task is None or self._partial_task.done():
            self._partial_task = asyncio.ensure_future(self._partial_loop())

    async def _partial_loop(self) -> None:
        hud = self.hud
        interval = self.cfg.asr.partial_interval_ms / 1000
        try:
            while self.listening:
                await asyncio.sleep(interval)
                if not self.listening:
                    break
                elapsed = time.monotonic() - self._listen_started
                # mic 看门狗（R2）：>800ms 无音频帧 = mic 被抢/链路断
                if time.monotonic() - self._last_audio > 0.8 and elapsed > 1.0:
                    hud.emit("S2", hud.status_line("S2", word="无音频", glyph=hud.glyphs["warning"]),
                             "麦克风没有声音\n请松开重试", urgent=True)
                    continue
                status = hud.status_line("S2", fmt_secs(elapsed))
                if len(self._pcm) < 3200:
                    hud.emit("S2", status, "")
                    continue
                text = await self.asr.partial(bytes(self._pcm))
                if not self.listening:
                    break
                stable, tail = self._tracker.update(text)
                shown = (stable + tail).strip()
                body = tail_window(shown + hud.glyphs["cursor"],
                                   hud.layout.body.inner_width,
                                   hud.layout.body.max_lines,
                                   ellipsis=hud.glyphs["ellipsis"]) if shown else ""
                hud.emit("S2", status, body)
        except asyncio.CancelledError:
            pass

    async def cancel_listening(self) -> None:
        self.hud.listening = False
        self._pcm.clear()
        self.hud.emit_idle(urgent=True)

    async def ptt_stop(self) -> None:
        hud = self.hud
        if not self.listening:
            return
        hud.listening = False
        pcm = bytes(self._pcm)
        self._pcm.clear()
        if len(pcm) < 16000:  # <0.5s 视为误触
            hud.emit_idle(urgent=True)
            return
        hud.emit("S3", hud.status_line("S3"), "", urgent=True)
        result = await self.asr.final(pcm)
        if not result.text:
            hud.emit("S8", hud.status_line("S8", word="未听清"), "没听清，请再说一次", urgent=True)
            hud.start_timer(lambda: hud.idle_after(5))
            return
        if self.claw.session_busy(self.session_key):
            hud.emit("S8", hud.status_line("S8", word="占用", glyph=hud.glyphs["warning"]),
                     "上一条还在跑\n点「打断」后再说", urgent=True)
            hud.start_timer(lambda: hud.idle_after(8))
            return
        low_conf = result.avg_logprob < self.cfg.composer.low_conf_threshold
        wait = (self.cfg.composer.confirm_seconds_low_conf if low_conf
                else self.cfg.composer.confirm_seconds)
        hint = "请核对文字…" if low_conf else ""
        hud.emit("S3", f'{hud.glyphs["transcribing"]} {self.cfg.openclaw.agent_name}',
                 result.text, hint, urgent=True)
        self._confirm_task = asyncio.ensure_future(self._confirm_then_send(result.text, wait))

    async def _confirm_then_send(self, text: str, wait: float) -> None:
        try:
            await asyncio.sleep(wait)  # 确认窗口：期间重按 PTT = 重说（任务被取消）
        except asyncio.CancelledError:
            return
        await self.dispatch(text)

    # ---------------------------------------------------------------- 发给 agent

    async def dispatch(self, text: str) -> None:
        hud = self.hud
        self._last_question = text
        message = text if self._style_sent else style_header(hud.layout.body) + text
        hud.paginator.reset()
        started = time.monotonic()
        hud.emit("S4", hud.status_line("S4", "0s"), text, urgent=True)

        async def thinking_timer() -> None:
            try:
                while hud.state == "S4":
                    await asyncio.sleep(1)
                    if hud.state != "S4":
                        break
                    s = int(time.monotonic() - started)
                    extra = "\n仍在思考·点「打断」可停止" if s > 30 else ""
                    hud.emit("S4", hud.status_line("S4", f"{s}s"), self._last_question + extra)
            except asyncio.CancelledError:
                pass

        hud.start_timer(thinking_timer)

        try:
            await self.claw.chat_send(self.session_key, message, self.on_agent_event)
        except Exception as exc:
            log.exception("chat_send failed")
            hud.cancel_timer()
            hud.emit("S8", hud.status_line("S8"), f"无法连接 agent\n{str(exc)[:24]}", urgent=True)
            hud.start_timer(lambda: hud.idle_after(15))

    async def on_agent_event(self, kind: str, payload: str, extra: str) -> None:
        """agent 事件回调。`kind ∈ partial | final | error | tool`。

        修 S1：`tool` 分支以前是死代码 —— `_STATE_LABEL` 里定义了 S5 工具态，
        但没有任何地方会进入它。现在接活了。当前的 OpenClaw 适配器还不会发这个 kind
        （它的 chat 事件只有 delta/final/error），M6 换上带工具调用的真 agent 后即生效；
        本回调本身有单测直接驱动，不依赖具体 agent 实现。
        """
        hud = self.hud
        if kind == "partial":
            self._on_reply_text(payload, final=False)
        elif kind == "final":
            self._style_sent = True
            self._on_reply_text(payload, final=True)
        elif kind == "tool":
            hud.cancel_timer()
            hud.emit("S5", hud.status_line("S5", word=payload[:12] or None),
                     extra or self._last_question, urgent=True)
        elif kind == "error":
            hud.cancel_timer()
            hud.emit("S8", hud.status_line("S8"), f"{payload[:30]}\n按住说话可重试", urgent=True)
            hud.start_timer(lambda: hud.idle_after(15))

    def _on_reply_text(self, full_text: str, final: bool) -> None:
        hud = self.hud
        hud.cancel_timer()
        p = hud.paginator
        p.set_text(full_text)   # 内部已按 follow 跟到末页并夹紧 cur（修 F7）
        if not final:
            # 用户往回翻过 → 页脚带暂停标记，提示「新内容还在来，但画面钉住了」
            suffix = "" if p.follow else hud.glyphs["paused"]
            hud.emit("S6", hud.status_line("S6"), p.page_text(), p.footer(suffix))
        else:
            hud.emit("S7", hud.status_line("S7"), p.page_text(), p.footer(), urgent=True)
            linger = (self.cfg.composer.final_short_linger_seconds if p.total == 1
                      else self.cfg.composer.reading_idle_seconds)
            hud.start_timer(lambda: hud.idle_after(linger))

    # ---------------------------------------------------------------- 打断

    async def abort(self, silent: bool = False) -> None:
        hud = self.hud
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()
        if self.listening:
            await self.cancel_listening()
            return
        hud.cancel_timer()
        try:
            await self.claw.abort(self.session_key)
        except Exception:
            pass
        if silent:
            return
        p = hud.paginator
        if p.total > 1 or p.page_text(0):
            hud.emit("S7", hud.status_line("S7", word="已打断", glyph=hud.glyphs["stopped"]),
                     p.page_text(), p.footer(), urgent=True)
            hud.start_timer(lambda: hud.idle_after(self.cfg.composer.reading_idle_seconds))
        else:
            hud.emit_idle(urgent=True)
