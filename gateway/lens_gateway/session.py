"""设备会话：HUD 状态机（S0-S8）+ 帧编排 + 节流。

每台配对设备一个常驻会话对象（跨 WS 重连存活，服务器持有全部状态，
重连后重放 current_frame 即恢复现场——红队 R1/R6）。

帧规则（协议第 3 节）：
- 状态切换帧 urgent=True 免节流；
- 同状态内容帧 ≥throttle_ms 间隔，发送队列只保留最新（coalescing）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .asr import AsrEngine, StablePrefixTracker
from .config import Config
from .openclaw import OpenClawClient
from .textkit import Paginator, tail_window

log = logging.getLogger(__name__)

SendFunc = Callable[[dict], Awaitable[None]]

# lens 会话首条消息注入的小屏风格指令（红队 R7：独立会话 + 输出风格）
STYLE_HEADER = (
    "[系统指令：用户正通过智能眼镜 HUD 与你对话。屏幕一页仅约 85 个汉字。"
    "回答要求：先结论后细节；短句；不用 markdown、表格、代码块；"
    "非必要不超过 170 字；列表用「一是…二是…」行文。]\n\n"
)

_STATE_GLYPH = {
    "S0": "·", "S2": "◉ 聆听", "S3": "→", "S4": "◔ 思考",
    "S5": "⚙ 工具", "S6": "▸ 回答", "S7": "✓ 完成", "S8": "✕ 出错",
}


def _fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


class DeviceSession:
    def __init__(self, device_id: str, cfg: Config, asr: AsrEngine, claw: OpenClawClient):
        self.device_id = device_id
        self.cfg = cfg
        self.asr = asr
        self.claw = claw
        self.session_key = f"lens:{device_id}"
        self._send: SendFunc | None = None

        self.state = "S0"
        self.seq = 0
        self.current_frame: dict = {}
        self._style_sent = False

        # PTT / 聆听
        self._pcm = bytearray()
        self._listening = False
        self._listen_started = 0.0
        self._last_audio = 0.0
        self._tracker = StablePrefixTracker()
        self._partial_task: asyncio.Task | None = None

        # 回复 / 阅读
        self._paginator = Paginator(
            lines_per_page=cfg.composer.lines_per_page, wrap_chars=cfg.composer.wrap_chars)
        self._page = 0
        self._follow = True  # 流式自动跟最新页
        self._last_question = ""

        # 节流
        self._last_frame_ts = 0.0
        self._pending: dict | None = None
        self._flush_task: asyncio.Task | None = None

        # 计时器（思考计秒/待机回落等）
        self._timer_task: asyncio.Task | None = None
        self._confirm_task: asyncio.Task | None = None

        self._emit_idle(urgent=True)

    # ---------- 连接绑定 ----------

    def attach(self, send: SendFunc) -> dict:
        """新 WS 绑定；返回 resume 帧。"""
        self._send = send
        return self.current_frame

    def detach(self) -> None:
        self._send = None
        if self._listening:
            asyncio.ensure_future(self._cancel_listening())

    # ---------- 帧发送 ----------

    def _build(self, state: str, status: str, body: str, foot: str, **meta) -> dict:
        self.seq += 1
        return {
            "type": "frame", "seq": self.seq, "state": state,
            "containers": {"status": status, "body": body, "foot": foot},
            "meta": {"rec": self._listening, "agent": "gongbu",
                     "page": {"cur": self._page + 1, "total": self._paginator.total}, **meta},
        }

    def _emit(self, state: str, status: str, body: str, foot: str = "", urgent: bool = False, **meta) -> None:
        urgent = urgent or state != self.state
        self.state = state
        frame = self._build(state, status, body, foot, **meta)
        self.current_frame = frame
        if self._send is None:
            return
        now = time.monotonic()
        throttle = self.cfg.composer.throttle_ms / 1000
        if urgent or now - self._last_frame_ts >= throttle:
            self._last_frame_ts = now
            self._pending = None
            asyncio.ensure_future(self._safe_send(frame))
        else:
            self._pending = frame  # coalescing：只留最新
            if self._flush_task is None or self._flush_task.done():
                delay = throttle - (now - self._last_frame_ts)
                self._flush_task = asyncio.ensure_future(self._flush_later(delay))

    async def _flush_later(self, delay: float) -> None:
        await asyncio.sleep(max(delay, 0))
        if self._pending is not None and self._send is not None:
            self._last_frame_ts = time.monotonic()
            frame, self._pending = self._pending, None
            await self._safe_send(frame)

    async def _safe_send(self, frame: dict) -> None:
        if self._send is None:
            return
        try:
            await self._send(frame)
        except Exception:
            log.debug("send failed (client gone)")

    def _emit_idle(self, urgent: bool = False) -> None:
        self._cancel_timer()
        self._emit("S0", "·", "", "", urgent=urgent)

    # ---------- 计时器 ----------

    def _cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def _start_timer(self, coro_factory) -> None:
        self._cancel_timer()
        self._timer_task = asyncio.ensure_future(coro_factory())

    async def _idle_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._emit_idle()

    # ---------- 客户端消息入口 ----------

    async def handle_text(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "ptt":
            action = msg.get("action")
            if action == "start":
                await self._ptt_start()
            elif action == "stop":
                await self._ptt_stop()
            elif action == "cancel":
                await self._cancel_listening()
        elif mtype == "page":
            self._turn_page(1 if msg.get("dir") != "prev" else -1)
        elif mtype == "abort":
            await self._abort()
        elif mtype == "reset":
            await self._abort(silent=True)
            self._emit_idle(urgent=True)

    async def handle_binary(self, data: bytes) -> None:
        if not self._listening:
            return
        self._pcm.extend(data)
        self._last_audio = time.monotonic()
        max_bytes = int(self.cfg.asr.max_utterance_seconds * 16000) * 2
        if len(self._pcm) >= max_bytes:  # 软上限：自动松手（R10）
            await self._ptt_stop()

    # ---------- 聆听 ----------

    async def _ptt_start(self) -> None:
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()  # 确认期重按 = 重说
        self._cancel_timer()
        self._pcm.clear()
        self._tracker.reset()
        self._listening = True
        self._listen_started = time.monotonic()
        self._last_audio = self._listen_started
        self._emit("S2", "工 ◉ 聆听 0:00", "", urgent=True)
        if self._partial_task is None or self._partial_task.done():
            self._partial_task = asyncio.ensure_future(self._partial_loop())

    async def _partial_loop(self) -> None:
        interval = self.cfg.asr.partial_interval_ms / 1000
        try:
            while self._listening:
                await asyncio.sleep(interval)
                if not self._listening:
                    break
                elapsed = time.monotonic() - self._listen_started
                # mic 看门狗（R2）：>800ms 无音频帧 = mic 被抢/链路断
                if time.monotonic() - self._last_audio > 0.8 and elapsed > 1.0:
                    self._emit("S2", "工 ⚠ 无音频", "麦克风没有声音\n请松开重试", urgent=True)
                    continue
                status = f"工 ◉ 聆听 {_fmt_secs(elapsed)}"
                if len(self._pcm) < 3200:
                    self._emit("S2", status, "")
                    continue
                text = await self.asr.partial(bytes(self._pcm))
                if not self._listening:
                    break
                stable, tail = self._tracker.update(text)
                shown = (stable + tail).strip()
                body = tail_window(shown + "▌", self.cfg.composer.wrap_chars,
                                   self.cfg.composer.lines_per_page) if shown else ""
                self._emit("S2", status, body)
        except asyncio.CancelledError:
            pass

    async def _cancel_listening(self) -> None:
        self._listening = False
        self._pcm.clear()
        self._emit_idle(urgent=True)

    async def _ptt_stop(self) -> None:
        if not self._listening:
            return
        self._listening = False
        pcm = bytes(self._pcm)
        self._pcm.clear()
        if len(pcm) < 16000:  # <0.5s 视为误触
            self._emit_idle(urgent=True)
            return
        self._emit("S3", "工 … 转写中", "", urgent=True)
        result = await self.asr.final(pcm)
        if not result.text:
            self._emit("S8", "工 ✕ 未听清", "没听清，请再说一次", urgent=True)
            self._start_timer(lambda: self._idle_after(5))
            return
        if self.claw.session_busy(self.session_key):
            self._emit("S8", "工 ⚠ 占用", "上一条还在跑\n点「打断」后再说", urgent=True)
            self._start_timer(lambda: self._idle_after(8))
            return
        low_conf = result.avg_logprob < self.cfg.composer.low_conf_threshold
        wait = (self.cfg.composer.confirm_seconds_low_conf if low_conf
                else self.cfg.composer.confirm_seconds)
        hint = "请核对文字…" if low_conf else ""
        self._emit("S3", "→ 工部", result.text, hint, urgent=True)
        self._confirm_task = asyncio.ensure_future(self._confirm_then_send(result.text, wait))

    async def _confirm_then_send(self, text: str, wait: float) -> None:
        try:
            await asyncio.sleep(wait)  # 确认窗口：期间重按 PTT = 重说（任务被取消）
        except asyncio.CancelledError:
            return
        await self._dispatch(text)

    # ---------- 发给 agent ----------

    async def _dispatch(self, text: str) -> None:
        self._last_question = text
        message = text if self._style_sent else STYLE_HEADER + text
        self._paginator.set_text("")
        self._page = 0
        self._follow = True
        started = time.monotonic()
        self._emit("S4", "工 ◔ 思考 0s", text, urgent=True)

        async def thinking_timer() -> None:
            try:
                while self.state == "S4":
                    await asyncio.sleep(1)
                    if self.state != "S4":
                        break
                    s = int(time.monotonic() - started)
                    extra = "\n仍在思考·点「打断」可停止" if s > 30 else ""
                    self._emit("S4", f"工 ◔ 思考 {s}s", self._last_question + extra)
            except asyncio.CancelledError:
                pass

        self._start_timer(thinking_timer)

        async def on_event(kind: str, payload: str, extra: str) -> None:
            if kind == "partial":
                self._on_reply_text(payload, final=False)
            elif kind == "final":
                self._style_sent = True
                self._on_reply_text(payload, final=True)
            elif kind == "error":
                self._cancel_timer()
                self._emit("S8", "工 ✕ 出错", f"{payload[:30]}\n按住说话可重试", urgent=True)
                self._start_timer(lambda: self._idle_after(15))

        try:
            await self.claw.chat_send(self.session_key, message, on_event)
        except Exception as exc:
            log.exception("chat_send failed")
            self._cancel_timer()
            self._emit("S8", "工 ✕ 出错", f"无法连接 agent\n{str(exc)[:24]}", urgent=True)
            self._start_timer(lambda: self._idle_after(15))

    def _on_reply_text(self, full_text: str, final: bool) -> None:
        self._cancel_timer()
        self._paginator.set_text(full_text)
        total = self._paginator.total
        if self._follow:
            self._page = total - 1
        if not final:
            foot = f"‹{total}" if total > 1 else ""
            if not self._follow:
                foot = f"{self._page + 1}/{total} ⏸"
            self._emit("S6", "工 ▸ 回答", self._paginator.page_text(self._page), foot)
        else:
            foot = f"{self._page + 1}/{total} ›" if total > 1 else ""
            self._emit("S7", "工 ✓ 完成", self._paginator.page_text(self._page), foot, urgent=True)
            linger = (self.cfg.composer.final_short_linger_seconds if total == 1
                      else self.cfg.composer.reading_idle_seconds)
            self._start_timer(lambda: self._idle_after(linger))

    # ---------- 翻页 / 打断 ----------

    def _turn_page(self, delta: int) -> None:
        if self.state not in ("S6", "S7"):
            return
        total = self._paginator.total
        new = max(0, min(self._page + delta, total - 1))
        if new == self._page:
            return
        self._page = new
        if self.state == "S6":
            self._follow = new == total - 1  # 翻回最末页恢复跟随
            foot = f"{new + 1}/{total}" + ("" if self._follow else " ⏸")
        else:
            foot = f"{new + 1}/{total} ›"
            self._start_timer(lambda: self._idle_after(self.cfg.composer.reading_idle_seconds))
        glyph = _STATE_GLYPH[self.state]
        self._emit(self.state, f"工 {glyph}", self._paginator.page_text(new), foot, urgent=True)

    async def _abort(self, silent: bool = False) -> None:
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()
        if self._listening:
            await self._cancel_listening()
            return
        self._cancel_timer()
        try:
            await self.claw.abort(self.session_key)
        except Exception:
            pass
        if silent:
            return
        if self._paginator.total > 1 or self._paginator.page_text(0):
            total = self._paginator.total
            self._emit("S7", "工 ⏹ 已打断", self._paginator.page_text(self._page),
                       f"{self._page + 1}/{total} ›" if total > 1 else "", urgent=True)
            self._start_timer(lambda: self._idle_after(self.cfg.composer.reading_idle_seconds))
        else:
            self._emit_idle(urgent=True)
