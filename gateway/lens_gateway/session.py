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
from dataclasses import replace
from typing import Awaitable, Callable

from .asr import AsrEngine, StablePrefixTracker
from .config import Config
from .openclaw import OpenClawClient
from .formatting import DEFAULT_LAYOUT, Box, Paginator, glyph_set, tail_window

log = logging.getLogger(__name__)

SendFunc = Callable[[dict], Awaitable[None]]

# HUD 状态 → 语义字形名 + 中文词。字形本身由 GlyphSet 决定（真机字库外的字形已被换掉）。
_STATE_LABEL: dict[str, tuple[str, str]] = {
    "S0": ("idle", ""),
    "S2": ("listening", "聆听"),
    "S3": ("transcribing", "转写中"),
    "S4": ("thinking", "思考"),
    "S5": ("tool", "工具"),
    "S6": ("answering", "回答"),
    "S7": ("done", "完成"),
    "S8": ("error", "出错"),
}

# 一个 CJK 字形在 G2 上的宽度，用来把「像素预算」翻译成模型能理解的「多少个汉字」
_CJK_PX = 20


def style_header(box: Box) -> str:
    """按**真实版式**生成注入给模型的小屏风格指令（红队 R7：独立会话 + 输出风格）。

    以前这里硬编码「一页约 85 个汉字 / 不超过 170 字」，而那是按 5 行 × 17 字算的 ——
    真实版式是 8 行 × 28 字。写死的数字会让模型按错误的预算写作，每页都溢出。
    """
    per_line = box.inner_width // _CJK_PX
    per_page = box.max_lines * per_line
    return (
        f"[系统指令：用户正通过智能眼镜 HUD 与你对话。屏幕一页约 {per_page} 个汉字"
        f"（{box.max_lines} 行 × 每行约 {per_line} 字）。"
        "回答要求：先结论后细节；短句；不用 markdown、表格、代码块；"
        f"非必要不超过 {per_page * 2} 字；列表用「一是…二是…」行文。]\n\n"
    )


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

        # 版式与字形：像素版式是唯一真源；字形表在构造时按 G2 字库自校验
        self.layout = DEFAULT_LAYOUT
        if cfg.composer.body_safety_px:
            body = self.layout.body
            self.layout = replace(self.layout, body=replace(body, safety_px=cfg.composer.body_safety_px))
        self.glyphs = glyph_set(cfg.composer.glyph_profile, cfg.composer.glyph_overrides or None)
        self.badge = cfg.openclaw.agent_label   # 修 S3：这个配置项以前从未被读取

        # PTT / 聆听
        self._pcm = bytearray()
        self._listening = False
        self._listen_started = 0.0
        self._last_audio = 0.0
        self._tracker = StablePrefixTracker()
        self._partial_task: asyncio.Task | None = None

        # 回复 / 阅读。页码与跟随标志都在 Paginator 内部（修 F7：页脚与正文必然同源）
        self._paginator = Paginator(box=self.layout.body, glyphs=self.glyphs)
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

    # ---------- 状态条 ----------

    def _status(self, state: str, suffix: str = "", *, word: str | None = None,
                glyph: str | None = None) -> str:
        """组状态条：徽记 + 字形 + 中文词 (+ 附加信息)。

        修 S2：以前翻页时状态条被就地重写成 ``f"工 {glyph}"``，把「回答」「完成」
        这些词丢掉了 —— 同一个状态在首次渲染和翻页后长得不一样。现在只有这一个入口。
        """
        name, default_word = _STATE_LABEL[state]
        parts = [self.badge, glyph if glyph is not None else self.glyphs[name]]
        w = default_word if word is None else word
        if w:
            parts.append(w)
        if suffix:
            parts.append(suffix)
        return " ".join(p for p in parts if p)

    # ---------- 帧发送 ----------

    def _build(self, state: str, status: str, body: str, foot: str, **meta) -> dict:
        self.seq += 1
        return {
            "type": "frame", "seq": self.seq, "state": state,
            "containers": {"status": status, "body": body, "foot": foot},
            "meta": {"rec": self._listening, "agent": "gongbu",
                     "page": {"cur": self._paginator.cur + 1, "total": self._paginator.total}, **meta},
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
        self._emit("S0", self.glyphs["idle"], "", "", urgent=urgent)

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
        self._emit("S2", self._status("S2", "0:00"), "", urgent=True)
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
                    self._emit("S2", self._status("S2", word="无音频",
                                                  glyph=self.glyphs["warning"]),
                               "麦克风没有声音\n请松开重试", urgent=True)
                    continue
                status = self._status("S2", _fmt_secs(elapsed))
                if len(self._pcm) < 3200:
                    self._emit("S2", status, "")
                    continue
                text = await self.asr.partial(bytes(self._pcm))
                if not self._listening:
                    break
                stable, tail = self._tracker.update(text)
                shown = (stable + tail).strip()
                body = tail_window(shown + self.glyphs["cursor"],
                                   self.layout.body.inner_width,
                                   self.layout.body.max_lines,
                                   ellipsis=self.glyphs["ellipsis"]) if shown else ""
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
        self._emit("S3", self._status("S3"), "", urgent=True)
        result = await self.asr.final(pcm)
        if not result.text:
            self._emit("S8", self._status("S8", word="未听清"),
                       "没听清，请再说一次", urgent=True)
            self._start_timer(lambda: self._idle_after(5))
            return
        if self.claw.session_busy(self.session_key):
            self._emit("S8", self._status("S8", word="占用", glyph=self.glyphs["warning"]),
                       "上一条还在跑\n点「打断」后再说", urgent=True)
            self._start_timer(lambda: self._idle_after(8))
            return
        low_conf = result.avg_logprob < self.cfg.composer.low_conf_threshold
        wait = (self.cfg.composer.confirm_seconds_low_conf if low_conf
                else self.cfg.composer.confirm_seconds)
        hint = "请核对文字…" if low_conf else ""
        self._emit("S3", f'{self.glyphs["transcribing"]} {self.cfg.openclaw.agent_name}',
                   result.text, hint, urgent=True)
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
        message = text if self._style_sent else style_header(self.layout.body) + text
        self._paginator.reset()
        started = time.monotonic()
        self._emit("S4", self._status("S4", "0s"), text, urgent=True)

        async def thinking_timer() -> None:
            try:
                while self.state == "S4":
                    await asyncio.sleep(1)
                    if self.state != "S4":
                        break
                    s = int(time.monotonic() - started)
                    extra = "\n仍在思考·点「打断」可停止" if s > 30 else ""
                    self._emit("S4", self._status("S4", f"{s}s"), self._last_question + extra)
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
                self._emit("S8", self._status("S8"), f"{payload[:30]}\n按住说话可重试", urgent=True)
                self._start_timer(lambda: self._idle_after(15))

        try:
            await self.claw.chat_send(self.session_key, message, on_event)
        except Exception as exc:
            log.exception("chat_send failed")
            self._cancel_timer()
            self._emit("S8", self._status("S8"), f"无法连接 agent\n{str(exc)[:24]}", urgent=True)
            self._start_timer(lambda: self._idle_after(15))

    def _on_reply_text(self, full_text: str, final: bool) -> None:
        self._cancel_timer()
        p = self._paginator
        p.set_text(full_text)   # 内部已按 follow 跟到末页并夹紧 cur（修 F7）
        if not final:
            # 用户往回翻过 → 页脚带暂停标记，提示「新内容还在来，但画面钉住了」
            suffix = "" if p.follow else self.glyphs["paused"]
            self._emit("S6", self._status("S6"), p.page_text(), p.footer(suffix))
        else:
            self._emit("S7", self._status("S7"), p.page_text(), p.footer(), urgent=True)
            linger = (self.cfg.composer.final_short_linger_seconds if p.total == 1
                      else self.cfg.composer.reading_idle_seconds)
            self._start_timer(lambda: self._idle_after(linger))

    # ---------- 翻页 / 打断 ----------

    def _turn_page(self, delta: int) -> None:
        """翻页。触发源解耦：镜腿 / 手机按钮 / MCP / 语音都走这一个入口。"""
        if self.state not in ("S6", "S7"):
            return
        p = self._paginator
        if not p.turn(delta):
            return   # 已在边界：不动，也不发冗余帧
        suffix = "" if (self.state != "S6" or p.follow) else self.glyphs["paused"]
        if self.state == "S7":
            self._start_timer(lambda: self._idle_after(self.cfg.composer.reading_idle_seconds))
        self._emit(self.state, self._status(self.state), p.page_text(),
                   p.footer(suffix), urgent=True)

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
            self._emit("S7", self._status("S7", word="已打断", glyph=self.glyphs["stopped"]),
                       self._paginator.page_text(), self._paginator.footer(), urgent=True)
            self._start_timer(lambda: self._idle_after(self.cfg.composer.reading_idle_seconds))
        else:
            self._emit_idle(urgent=True)
