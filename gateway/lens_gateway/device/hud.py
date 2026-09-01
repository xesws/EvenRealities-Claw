"""HUD 设备抽象：帧构造、节流与合并、状态条、分页、计时器、**帧租约**。

这一层只关心"屏幕上现在是什么"，完全不知道麦克风、ASR、agent 的存在 ——
语音链路在 `lens_gateway.voice`，MCP 表面在独立进程，两边都通过本类写屏。

## 为什么需要租约（W1）

一旦 MCP 工具（`hud_show` 等）也能写屏，屏幕就有了**两个写者**，于是有两个必须回答的问题：

1. **谁在控制？** MCP 协议本身没有 session 概念，服务器分不清是哪个客户端在写
   （同一个 `hud_show` 可能来自两个并发的 LLM 客户端）。租约给出答案：
   一次只有一个持有者，冲突返回结构化错误而不是"最后写入者赢"。
2. **用户开口时怎么办？** 用户按下 PTT 的那一刻，屏幕必须立刻归语音链路。
   所以租约是**可被抢占的**：任何非外部写入都会撤销当前租约，并把抢占事件
   记进缓冲区供 MCP 客户端轮询（MCP 2026-07-28 是无状态的，服务器不能主动推送）。

## 顺带修掉的 S7

`emit()` 以前**从不取消 `_timer_task`**。一轮问答结束后挂着一个 15/60s 的
`idle_after`，MCP 写屏之后那个定时器到点，会把三个 container 全部清空 ——
屏幕自己黑掉，而且看不出是谁干的。现在外部渲染会接管计时器。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from ..config import Config
from ..formatting import DEFAULT_LAYOUT, Box, Layout, Paginator, glyph_set

log = logging.getLogger(__name__)

SendFunc = Callable[[dict], Awaitable[None]]

#: HUD 状态 → (语义字形名, 中文词)。字形本身由 GlyphSet 决定 ——
#: 真机字库外的字形已经在 `formatting.glyphs` 的 import 期校验中被挡掉了。
#: 状态 → (字形语义名, 屏上的词)。**两套词，一套字形。**
#:
#: 字形不随语言变（`◆` 在哪种语言里都是 `◆`），词要变。分开放是因为字形还要
#: 过 G2 字库自校验，而词只是词。英文词全部 ≤10 字符：状态条右边还要放计时、
#: 页码这些东西，而这块屏一行只有 576px。
STATE_LABELS: dict[str, dict[str, tuple[str, str]]] = {
    "zh": {
        "S0": ("idle", ""),
        "S2": ("listening", "聆听"),
        "S3": ("transcribing", "转写中"),
        "S4": ("thinking", "思考"),
        "S5": ("tool", "工具"),
        "S6": ("answering", "回答"),
        "S7": ("done", "完成"),
        "S8": ("error", "出错"),
        "S9": ("tool", ""),      # 外部（MCP）渲染，标题由持有者给
    },
    "en": {
        "S0": ("idle", ""),
        "S2": ("listening", "Listening"),
        "S3": ("transcribing", "Heard"),
        "S4": ("thinking", "Thinking"),
        "S5": ("tool", "Tool"),
        "S6": ("answering", "Answer"),
        "S7": ("done", "Done"),
        "S8": ("error", "Error"),
        "S9": ("tool", ""),
    },
}

#: 兼容：外部（含测试）曾直接 import 这张表。默认语言的那一份。
#: 网关自己写死、会直接上屏的文案。
#:
#: 跟 `STATE_LABELS` 放在一起，是因为它们是同一件事：**屏幕上的每一个字都得跟着
#: locale 走**。散落在 `voice/pipeline.py` 各个 `emit()` 调用里的中文字面量曾经
#: 让英文演示的屏幕上蹦出「与 agent 断开／按住说话可重试」—— 平时跑不到的错误分支，
#: 单测和人的眼睛都盖不住。有扫描测试盯着这张表以外的漏网之鱼（tests/test_device.py）。
HUD_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "mic_silent_word": "无音频",
        "mic_silent": "麦克风没有声音\n请松开重试",
        "not_heard_word": "未听清",
        "not_heard": "没听清，请再说一次",
        "busy_word": "占用",
        "busy": "上一条还在跑\n点「打断」后再说",
        "still_thinking": "\n仍在思考·点「打断」可停止",
        "agent_unreachable": "无法连接 agent",
        "agent_error_hint": "按住说话可重试",
        "aborted_word": "已打断",
        "link_lost": "与 agent 的连接在这一轮发送过程中断开",
        "reminder_title": "提醒",
    },
    "en": {
        "mic_silent_word": "no audio",
        "mic_silent": "The microphone picked up nothing\nRelease and try again",
        "not_heard_word": "unclear",
        "not_heard": "I did not catch that. Please say it again.",
        "busy_word": "busy",
        "busy": "Still working on the last one\nTap Stop, then speak",
        "still_thinking": "\nStill thinking - tap Stop to cancel",
        "agent_unreachable": "Cannot reach the agent",
        "agent_error_hint": "Hold to talk to retry",
        "aborted_word": "stopped",
        "link_lost": "Lost the connection to the agent mid-turn",
        "reminder_title": "Reminder",
    },
}

STATE_LABEL: dict[str, tuple[str, str]] = STATE_LABELS["zh"]

#: 外部渲染态。协议对未知 state 的要求是"原样显示"，所以加它是**加法安全**的。
EXTERNAL_STATE = "S9"

#: 可以翻页的状态：回答中 / 阅读中 / 外部渲染
PAGEABLE = ("S6", "S7", EXTERNAL_STATE)

#: 抢占事件缓冲区上限。MCP 客户端轮询取走，取不走的老事件直接丢。
_EVENT_BUFFER = 64

#: 一个 CJK 字形在 G2 上的宽度，用来把「像素预算」翻译成模型能理解的「多少个汉字」
_CJK_PX = 20


class LeaseError(RuntimeError):
    """租约相关错误的基类。`code` 会原样进入给 MCP 客户端的结构化错误。"""

    code = "LEASE_ERROR"

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


class LeaseHeld(LeaseError):
    """别人正持有租约。"""

    code = "LEASE_HELD"

    def __init__(self, holder: str, expires_in_ms: int):
        super().__init__(f"帧租约被 {holder} 持有，{expires_in_ms}ms 后过期")
        self.holder = holder
        self.expires_in_ms = expires_in_ms

    def as_dict(self) -> dict:
        return {**super().as_dict(), "holder": self.holder, "expires_in_ms": self.expires_in_ms}


class LeaseInvalid(LeaseError):
    """租约不存在、已过期、或已被用户抢占。"""

    code = "LEASE_INVALID"


@dataclass
class Lease:
    id: str
    holder: str
    expires_at: float          # time.monotonic() 基准

    def expires_in_ms(self, now: float | None = None) -> int:
        return max(0, int((self.expires_at - (now or time.monotonic())) * 1000))

    def alive(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) < self.expires_at

    def as_dict(self) -> dict:
        return {"lease_id": self.id, "holder": self.holder, "expires_in_ms": self.expires_in_ms()}


def style_header(box: Box) -> str:
    """按**真实版式**生成注入给模型的小屏风格指令（红队 R7：独立会话 + 输出风格）。

    以前这里硬编码「一页约 85 个汉字 / 不超过 170 字」，那是按 5 行 × 17 字算的 ——
    真实版式是 8 行 × 每行约 28 字。写死的数字会让模型按错误的预算写作，每页都溢出。
    """
    per_line = box.inner_width // _CJK_PX
    per_page = box.max_lines * per_line
    return (
        f"[系统指令：用户正通过智能眼镜 HUD 与你对话。屏幕一页约 {per_page} 个汉字"
        f"（{box.max_lines} 行 × 每行约 {per_line} 字）。"
        "回答要求：先结论后细节；短句；不用 markdown、表格、代码块；"
        f"非必要不超过 {per_page * 2} 字；列表用「一是…二是…」行文。]\n\n"
    )


class HudDevice:
    """一台眼镜的屏幕。**唯一的写屏入口**，语音链路与 MCP 都经由它。"""

    def __init__(self, device_id: str, cfg: Config):
        self.device_id = device_id
        self.cfg = cfg
        self._send: SendFunc | None = None

        self.state = "S0"
        self.seq = 0
        self.current_frame: dict = {}

        #: 由语音层维护，只影响 `meta.rec`（手机端的 ●REC 指示）。
        #: 放在这里而不是让 `_build` 反向读语音层的私有字段，是 A/B 分层的关键一刀。
        self.listening = False

        # 版式与字形：像素版式是唯一真源；字形表在构造时按 G2 字库自校验
        self.layout: Layout = DEFAULT_LAYOUT
        if cfg.composer.body_safety_px:
            body = self.layout.body
            self.layout = replace(self.layout, body=replace(body, safety_px=cfg.composer.body_safety_px))
        self.glyphs = glyph_set(cfg.composer.glyph_profile, cfg.composer.glyph_overrides or None)
        self.badge = cfg.agent_label            # 修 S3：这个配置项以前从未被读取
        #: 屏上文字的语言。字形表不随它变（字形另有 `glyph_profile`）。
        self.labels = STATE_LABELS[cfg.composer.locale]
        self.messages = HUD_MESSAGES[cfg.composer.locale]
        #: W6：接的是测试替身时，状态条徽记会被替换成醒目标记（见 note_agent）。
        #: `_agent_probe` 一旦绑定就**每帧现算**，`_agent_production` 退化成它的兜底值。
        self._agent_production = True
        self._agent_probe: Callable[[], bool] | None = None

        self.paginator = Paginator(box=self.layout.body, glyphs=self.glyphs)

        # 节流
        self._last_frame_ts = 0.0
        self._pending: dict | None = None
        self._flush_task: asyncio.Task | None = None

        # 计时器（思考计秒 / 待机回落 / 外部渲染保持）
        self._timer_task: asyncio.Task | None = None

        # 租约与事件
        self._lease: Lease | None = None
        self._events: deque[dict] = deque(maxlen=_EVENT_BUFFER)
        self._event_seq = 0

        self.last_active = time.monotonic()   # 修 S4：给会话 TTL 用

        # 低电量提示（DESIGN.md §4.4：「电量仅 <15% 页脚出现一次」）。
        # 在 M4 打通遥测上行之前这条承诺**没有数据源**，只能是空话。
        self._battery_armed = True            # 回到阈值以上就重新武装，避免反复刷屏
        self._battery_pending: int | None = None

        self.emit_idle(urgent=True)

    # ---------------------------------------------------------------- 连接

    def attach(self, send: SendFunc) -> dict:
        """新 WS 绑定；返回 resume 帧（断前画面，红队 R1/R6）。"""
        self._send = send
        self.last_active = time.monotonic()
        return self.current_frame

    def detach(self) -> None:
        self._send = None
        self.last_active = time.monotonic()

    @property
    def online(self) -> bool:
        return self._send is not None

    # ---------------------------------------------------------------- 状态条


    def msg(self, key: str) -> str:
        """取一条本地化的上屏文案。key 不存在直接炸 —— 屏幕上出现 KeyError
        总比出现一段用户看不懂的语言好，而且这只会在开发期发生。"""
        return self.messages[key]

    def status_line(self, state: str, suffix: str = "", *, word: str | None = None,
                    glyph: str | None = None) -> str:
        """组状态条：徽记 + 字形 + 词 (+ 附加信息)。

        修 S2：以前翻页时状态条被就地重写成 ``f"{badge} {glyph}"``，把「回答」「完成」
        这些词丢掉了 —— 同一个状态在首次渲染和翻页后长得不一样。现在只有这一个入口。
        """
        name, default_word = self.labels[state]
        badge = self.badge if self.agent_production else f"{self.badge}?"
        parts = [badge, glyph if glyph is not None else self.glyphs[name]]
        w = default_word if word is None else word
        # 徽记和词撞车时不印两遍。中文默认配置（徽记「答」/ 名字「小龙虾」）天然
        # 不会撞，英文却几乎必然撞：`agent_label` 和 `agent_name` 都是产品名 ⇒
        # 「即将发给哪个 agent」那一帧渲染成 `Lens → Lens`，在 576px 一行里读起来
        # 像是重复渲染的故障。徽记本来就已经说明了是哪个 agent，退回该状态的默认词。
        if w == self.badge:   # 比未加「?」的原徽记，非生产态同样要去重
            w = default_word
        if w:
            parts.append(w)
        if suffix:
            parts.append(suffix)
        return " ".join(p for p in parts if p)

    @property
    def agent_production(self) -> bool:
        """本帧要不要打「?」—— **每次取帧时现算**，不是一轮取样一次。

        为什么必须现算：`agent_is_trusted()` 在"还没握上手"时返回 True（那时对端
        身份是"未知"而不是"已知是替身"）。只要在某个时刻取样一次就把值钉住，
        冷启动 / 断线重连后的那一段时间里屏幕就会替替身打掩护 —— 而那恰恰是
        演示里最关键的第一句。现算把这个时序窗口整个消掉：连接什么时候握上，
        下一帧的徽记就什么时候变。

        探针出错不该把屏幕带崩，回落到上次已知值。
        """
        if self._agent_probe is None:
            return self._agent_production
        try:
            self._agent_production = bool(self._agent_probe())
        except Exception:                                   # pragma: no cover - 防御
            log.debug("agent 溯源探针失败，沿用上次已知身份", exc_info=True)
        return self._agent_production

    def bind_agent_probe(self, probe: Callable[[], bool]) -> None:
        """绑定 W6 溯源探针（一般是 `lambda: agent_is_trusted(provider)`）。"""
        self._agent_probe = probe

    def note_agent(self, production: bool) -> None:
        """W6 · agent 溯源在屏幕上的那一半。

        接的是测试替身（`demo/fake_openclaw.py` 这类）时，状态条徽记变成
        ``工?`` —— **眼镜上一眼就能看出这不是真 agent**。这比只写进日志强：
        演示时如果谁偷偷把 agent 换成替身，屏幕自己会告状。

        `?` U+003F 是 ASCII，必然在字库内，不需要走字形降级表。

        绑定了探针之后这个值只是兜底 —— 探针每帧都会把它覆盖掉。
        """
        self._agent_production = bool(production)

    # ---------------------------------------------------------------- 电量

    def note_battery(self, level: int | None, charging: bool | None) -> None:
        """收到一次遥测后调用。低于阈值则**安排一次**页脚提示。

        规则来自 DESIGN.md 的"坚决不显示"清单：电量平时**根本不上屏** ——
        眼镜屏只有 8 行，常驻一个电量数字是纯粹的信息浪费。只有跌破阈值时
        在页脚出现一次，之后除非充上电再掉下去，不会再出现第二次。

        注意原设计写的是「⚡15%」，但 `⚡` U+26A1 **不在 G2 字库**
        （Misc Symbols 只支持 U+2605–2667），真机上会被静默丢弃，只剩一个数字。
        实际用的是字库内的 `▁`（形如快见底的电量条），见 `_replaces.battery_low`。
        """
        threshold = self.cfg.composer.battery_warn_percent
        if threshold <= 0 or level is None:
            return
        if charging or level > threshold:
            self._battery_armed = True
            return
        if self._battery_armed:
            self._battery_armed = False
            self._battery_pending = level

    def _with_battery(self, foot: str) -> str:
        if self._battery_pending is None:
            return foot
        tag = f'{self.glyphs["battery_low"]}{self._battery_pending}%'
        return f"{foot} {tag}".strip()

    # ---------------------------------------------------------------- 帧发送

    def _build(self, state: str, status: str, body: str, foot: str, **meta) -> dict:
        self.seq += 1
        return {
            "type": "frame", "seq": self.seq, "state": state,
            "containers": {"status": status, "body": body, "foot": foot},
            "meta": {"rec": self.listening, "agent": self.cfg.agent_name,
                     "page": {"cur": self.paginator.cur + 1, "total": self.paginator.total},
                     **meta},
        }

    def emit(self, state: str, status: str, body: str, foot: str = "",
             urgent: bool = False, *, external: bool = False, **meta) -> None:
        """写一帧。

        `external=False`（默认，即语音链路或本地逻辑）会**抢占**当前租约 ——
        用户开口的那一刻，屏幕无条件归用户。
        """
        if not external:
            self._preempt_lease("local_render")
        urgent = urgent or state != self.state
        self.state = state
        self.last_active = time.monotonic()
        foot = self._with_battery(foot)
        frame = self._build(state, status, body, foot, **meta)
        self.current_frame = frame
        if self._send is None:
            return
        now = time.monotonic()
        throttle = self.cfg.composer.throttle_ms / 1000
        if urgent or now - self._last_frame_ts >= throttle:
            self._last_frame_ts = now
            self._pending = None
            # 这一帧确实会发出去，低电量提示的"出现一次"到此兑现。
            # 只在真的发出时清零：被节流合并掉的帧不算数，否则提示会悄无声息地丢掉。
            self._battery_pending = None
            asyncio.ensure_future(self._safe_send(self._send, frame))
        else:
            self._pending = frame  # coalescing：只留最新
            if self._flush_task is None or self._flush_task.done():
                delay = throttle - (now - self._last_frame_ts)
                self._flush_task = asyncio.ensure_future(self._flush_later(self._send, delay))

    async def _flush_later(self, send: SendFunc, delay: float) -> None:
        await asyncio.sleep(max(delay, 0))
        if self._pending is not None and send is self._send:
            self._last_frame_ts = time.monotonic()
            frame, self._pending = self._pending, None
            self._battery_pending = None   # 合并后的这一帧已经带上了提示，且确实发出去了
            await self._safe_send(send, frame)

    async def _safe_send(self, send: SendFunc | None, frame: dict) -> None:
        """发一帧到**调度那一刻绑定的**那条连接。

        绑定而不是发送时再读 `self._send` 是有意的：帧的发送是 `ensure_future` 排队的，
        如果这中间设备断线又重连，晚到的旧帧会被写进**新**连接 —— 而它的 seq 比
        attach 时重放的 resume 帧还小，客户端就会在恢复现场之后又画回一张更旧的画面。
        这正是本项目一直在消灭的「旧帧撒谎」。绑定之后旧帧只会写向那条正在关闭的连接，
        写失败被静默吞掉。
        """
        if send is None:
            return
        try:
            await send(frame)
        except Exception:
            log.debug("send failed (client gone)")

    def emit_idle(self, urgent: bool = False) -> None:
        self.cancel_timer()
        self.emit("S0", self.glyphs["idle"], "", "", urgent=urgent)

    # ---------------------------------------------------------------- 计时器

    def cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def start_timer(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        self.cancel_timer()
        self._timer_task = asyncio.ensure_future(coro_factory())

    async def idle_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self.emit_idle()

    # ---------------------------------------------------------------- 翻页

    def page(self, delta: int, *, source: str = "unknown") -> bool:
        """翻页。**触发源解耦**：镜腿 / 手机按钮 / MCP / 语音走的都是这一个入口，
        四者完全等价。真机若证实镜腿事件到不了 WebView，只是少一个触发源，不动架构。

        返回是否真的翻动了（已在边界时返回 False，不发冗余帧）。
        """
        if self.state not in PAGEABLE:
            return False
        p = self.paginator
        if not p.turn(delta):
            return False
        log.debug("page(%+d) by %s → %d/%d", delta, source, p.cur + 1, p.total)
        suffix = "" if (self.state != "S6" or p.at_last) else self.glyphs["paused"]
        external = self.state == EXTERNAL_STATE
        if self.state == "S7":
            self.start_timer(lambda: self.idle_after(self.cfg.composer.reading_idle_seconds))
        status = (self.current_frame.get("containers", {}).get("status", "")
                  if external else self.status_line(self.state))
        self.emit(self.state, status, p.page_text(), p.footer(suffix),
                  urgent=True, external=external)
        return True

    # ---------------------------------------------------------------- 租约

    def lease_info(self) -> dict | None:
        if self._lease and self._lease.alive():
            return self._lease.as_dict()
        return None

    def acquire_lease(self, holder: str, ttl_ms: int) -> Lease:
        """取得写屏租约。同一 holder 再次申请 = 续租；被别人持有则抛 `LeaseHeld`。"""
        ttl_ms = max(100, min(int(ttl_ms), 600_000))
        now = time.monotonic()
        cur = self._lease
        if cur and cur.alive(now) and cur.holder != holder:
            raise LeaseHeld(cur.holder, cur.expires_in_ms(now))
        if cur and cur.alive(now) and cur.holder == holder:
            self._lease = replace(cur, expires_at=now + ttl_ms / 1000)
            return self._lease
        self._lease = Lease(id=secrets.token_urlsafe(9), holder=holder,
                            expires_at=now + ttl_ms / 1000)
        self._push_event("lease_acquired", holder=holder, lease_id=self._lease.id)
        return self._lease

    def _check_lease(self, lease_id: str) -> Lease:
        cur = self._lease
        if cur is None or cur.id != lease_id:
            raise LeaseInvalid("租约不存在或已被抢占，请重新申请")
        if not cur.alive():
            raise LeaseInvalid("租约已过期，请重新申请")
        return cur

    def release_lease(self, lease_id: str) -> bool:
        if self._lease and self._lease.id == lease_id:
            self._push_event("lease_released", holder=self._lease.holder, lease_id=lease_id)
            self._lease = None
            return True
        return False

    def _preempt_lease(self, reason: str) -> None:
        cur = self._lease
        if cur is None:
            return
        if cur.alive():
            # 用户开口 / 本地状态机写屏 ⇒ 租约被抢占。MCP 客户端只能轮询，
            # 所以把这件事记进事件缓冲区，它下次 poll 时才知道自己已经不在控制了。
            self._push_event("lease_preempted", holder=cur.holder, lease_id=cur.id, reason=reason)
        self._lease = None

    def render_external(self, lease_id: str, text: str, *, title: str | None = None,
                        hold_ms: int | None = None) -> dict:
        """外部（MCP）写屏。文本走**同一个排版引擎**，所以翻页对它同样有效。

        `hold_ms` 到点后回待机；不给则沿用阅读态的回落时长。
        """
        self._check_lease(lease_id)
        p = self.paginator
        p.set_text(text)
        status = self.status_line(EXTERNAL_STATE, word=title or "")
        self.cancel_timer()
        self.emit(EXTERNAL_STATE, status, p.page_text(), p.footer(),
                  urgent=True, external=True)
        hold = (hold_ms / 1000) if hold_ms else self.cfg.composer.reading_idle_seconds
        if hold > 0:
            self.start_timer(lambda: self.idle_after(hold))
        return self.current_frame

    def clear_external(self, lease_id: str) -> dict:
        self._check_lease(lease_id)
        self.cancel_timer()
        self.paginator.reset()
        self.emit("S0", self.glyphs["idle"], "", "", urgent=True, external=True)
        return self.current_frame

    # ---------------------------------------------------------------- 事件

    def _push_event(self, kind: str, **fields) -> None:
        self._event_seq += 1
        self._events.append({"id": self._event_seq, "kind": kind,
                             "at": time.time(), **fields})

    def drain_events(self, after_id: int = 0) -> list[dict]:
        """取走 `after_id` 之后的事件。MCP 只能轮询（协议无状态、服务器不能主动发请求），
        所以这是拉模型，不是推模型。"""
        return [e for e in self._events if e["id"] > after_id]

    # ---------------------------------------------------------------- 观测

    def snapshot(self) -> dict:
        """给控制面 / MCP `glasses://{id}/frame` 用的只读快照。"""
        return {
            "device_id": self.device_id,
            "online": self.online,
            "state": self.state,
            "seq": self.seq,
            "containers": dict(self.current_frame.get("containers", {})),
            "page": {"cur": self.paginator.cur + 1, "total": self.paginator.total},
            "lease": self.lease_info(),
            "idle_ms": int((time.monotonic() - self.last_active) * 1000),
        }
