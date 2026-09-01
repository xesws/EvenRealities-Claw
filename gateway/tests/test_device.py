"""设备抽象层（`lens_gateway.device`）单测。

覆盖 M3 验收清单：帧节流与 coalescing、seq 单调、状态迁移、
租约冲突/过期/抢占、`page()` 四种触发源等价，外加 S1（工具态）与 S4（会话 TTL）。

**所有参数显式注入**，不读任何默认值 —— 默认值改了测试也必须照样成立，
否则测的是配置而不是行为。
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import time

import pytest

from lens_gateway.config import (AgentConfig, AsrConfig, ComposerConfig, Config,
                                 OpenClawConfig)
from lens_gateway.device import EXTERNAL_STATE, HudDevice, LeaseHeld, LeaseInvalid, style_header
from lens_gateway.device.hud import HUD_MESSAGES

THROTTLE_MS = 120
LONG_TEXT = "".join(f"第{i}段内容，用来把正文撑到多页，好让翻页有东西可翻。" for i in range(30))


def make_config(*, agent: AgentConfig | None = None, asr_over: dict | None = None,
                **composer_over) -> Config:
    """全字段显式构造，杜绝"测试其实在测默认值"。"""
    composer = dict(
        glyph_profile="symbol",
        glyph_overrides={},
        body_safety_px=0,
        throttle_ms=THROTTLE_MS,
        confirm_seconds=1.2,
        confirm_seconds_low_conf=3.0,
        low_conf_threshold=-0.9,
        reading_idle_seconds=60.0,
        final_short_linger_seconds=15.0,
        session_ttl_seconds=86400.0,
    )
    composer.update(composer_over)
    return Config(
        host="127.0.0.1",
        port=18443,
        plugin_dist="",
        openclaw=OpenClawConfig(url="ws://127.0.0.1:1/none", config_path="/dev/null",
                                agent_label="工", agent_name="工部"),
        asr=AsrConfig(**{**dict(partial_model="tiny", final_model="base", language="zh",
                                compute_type="int8", cpu_threads=1, hotwords="",
                                partial_interval_ms=700, partial_tail_seconds=12.0,
                                max_utterance_seconds=25.0,
                                mic_warmup_seconds=2.5, mic_gap_seconds=0.8),
                        **(asr_over or {})}),
        agent=agent or AgentConfig(provider="openclaw", url="ws://127.0.0.1:1/none",
                                   connect_timeout=1.0, budget_ms=8000,
                                   agent_label="答", agent_name="小龙虾"),
        composer=ComposerConfig(**composer),
    )


class Sink:
    """收帧的假 WS。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    @property
    def seqs(self) -> list[int]:
        return [f["seq"] for f in self.frames]

    @property
    def states(self) -> list[str]:
        return [f["state"] for f in self.frames]


@pytest.fixture()
def hud() -> HudDevice:
    return HudDevice("dev_test", make_config())


@pytest.fixture()
def attached(hud: HudDevice) -> tuple[HudDevice, Sink]:
    sink = Sink()
    hud.attach(sink)
    return hud, sink


# ------------------------------------------------------------------ 节流


class TestThrottle:
    async def test_same_state_is_coalesced(self, attached):
        hud, sink = attached
        hud.emit("S2", "聆听", "第一帧", urgent=True)     # 状态迁移，立即发
        await asyncio.sleep(0)
        assert len(sink.frames) == 1

        for i in range(5):                                # 同状态高频刷新
            hud.emit("S2", "聆听", f"刷新{i}")
        await asyncio.sleep(0)
        assert len(sink.frames) == 1, "节流窗口内不应该多发"

        await asyncio.sleep(THROTTLE_MS / 1000 + 0.05)
        assert len(sink.frames) == 2, "窗口结束应补发一帧"
        # coalescing：只留最新的那一帧，中间四帧被丢弃
        assert sink.frames[-1]["containers"]["body"] == "刷新4"

    async def test_state_change_bypasses_throttle(self, attached):
        hud, sink = attached
        hud.emit("S2", "聆听", "a", urgent=True)
        hud.emit("S4", "思考", "b")        # 未标 urgent，但状态变了 ⇒ 必须立刻发
        await asyncio.sleep(0)
        assert sink.states == ["S2", "S4"]

    async def test_offline_emit_updates_frame_without_send(self):
        hud = HudDevice("dev_off", make_config())
        hud.emit("S6", "回答", "离线时的内容", urgent=True)
        assert hud.current_frame["containers"]["body"] == "离线时的内容"
        sink = Sink()
        resume = hud.attach(sink)          # 重连重放（红队 R1/R6）
        assert resume is hud.current_frame
        assert sink.frames == [], "attach 本身不发帧，由服务器把 resume 帧写出去"


# ------------------------------------------------------------------ seq


class TestSeq:
    async def test_seq_strictly_increasing_even_when_coalesced(self, attached):
        hud, sink = attached
        for i in range(8):
            hud.emit("S6", "回答", f"{i}")
        await asyncio.sleep(THROTTLE_MS / 1000 + 0.05)
        assert sink.seqs == sorted(sink.seqs)
        assert len(set(sink.seqs)) == len(sink.seqs)
        # 被合并掉的帧同样占了 seq —— 客户端看到的是"跳号"而不是"回退"
        assert hud.seq >= sink.seqs[-1]
        assert hud.current_frame["seq"] == hud.seq

    async def test_seq_survives_reconnect(self, attached):
        hud, sink = attached
        hud.emit("S6", "回答", "一", urgent=True)
        await asyncio.sleep(0)
        before = hud.seq
        hud.detach()
        hud.emit("S7", "完成", "二", urgent=True)   # 离线期间照样推进
        await asyncio.sleep(0)
        sink2 = Sink()
        hud.attach(sink2)
        hud.emit("S0", "待机", "", urgent=True)
        await asyncio.sleep(0)
        assert sink2.frames[0]["seq"] > before, "重连后的第一帧必须比断前更新"
        assert len(sink2.frames) == 1, "断线期间的帧不该补发到新连接上"


# ------------------------------------------------------------------ 状态迁移


class TestState:
    async def test_emit_idle_resets_state_and_cancels_timer(self, attached):
        hud, sink = attached
        hud.emit("S7", hud.status_line("S7"), "答案", urgent=True)
        hud.start_timer(lambda: hud.idle_after(999))
        assert hud._timer_task is not None
        hud.emit_idle(urgent=True)
        await asyncio.sleep(0)
        assert hud.state == "S0"
        assert hud._timer_task is None

    def test_status_line_keeps_the_word(self, hud):
        """修 S2：同一状态在首帧与翻页后必须长得一样。"""
        first = hud.status_line("S6")
        assert "回答" in first and hud.badge in first
        assert hud.status_line("S6") == first

    def test_status_line_accepts_override(self, hud):
        line = hud.status_line("S8", word="未听清", glyph=hud.glyphs["warning"])
        assert "未听清" in line and hud.glyphs["warning"] in line

    def test_status_line_does_not_print_the_badge_twice(self):
        """徽记和词撞车时只印一次。

        英文部署里 `agent_label` 和 `agent_name` 都是产品名，「即将发给哪个 agent」
        那一帧（`voice/pipeline.py` 的确认帧）会传 `word=agent_name` ⇒ 曾经渲染成
        `Lens → Lens`。在 576px 一行里那读起来像重复渲染的故障。
        """
        cfg = make_config(
            locale="en",
            agent=AgentConfig(provider="lens", url="ws://127.0.0.1:1/none",
                              connect_timeout=1.0, budget_ms=12000,
                              agent_label="Lens", agent_name="Lens"))
        hud = HudDevice("dev_en", cfg)
        line = hud.status_line("S3", word=cfg.agent_name)
        assert line.count("Lens") == 1, line
        # 退回该状态的默认词，而不是留下一个空荡荡的箭头
        assert line == f"Lens {hud.glyphs['transcribing']} Heard", line

    def test_status_line_still_prints_a_word_that_differs_from_the_badge(self):
        """反向验证：去重不能顺手把正常的名字也吃掉（中文默认就是这一路）。"""
        hud = HudDevice("dev_zh", make_config())      # 徽记「答」/ 名字「小龙虾」
        line = hud.status_line("S3", word="小龙虾")
        assert "小龙虾" in line and "转写中" not in line, line

    def test_style_header_derives_budget_from_layout(self, hud):
        """修 F/S：给模型的字数预算必须由真实像素版式算出，不是硬编码 85。"""
        header = style_header(hud.layout.body)
        per_line = hud.layout.body.inner_width // 20
        assert f"{hud.layout.body.max_lines} 行" in header
        assert f"每行约 {per_line} 字" in header
        assert f"{hud.layout.body.max_lines * per_line} 个汉字" in header


# ------------------------------------------------------------------ 翻页


class TestPaging:
    def _load(self, hud: HudDevice, state: str = "S6", *, to_first: bool = True) -> None:
        """灌入多页正文。

        `set_text` 不移动读者，装载后自然停在首页，所以 `to_first` 只是把
        每次装载的起点显式钉死，跟测别的行为的用例互不干扰。
        """
        hud.paginator.reset()          # 清空，让每次装载的起点一致
        hud.paginator.set_text(LONG_TEXT)
        if to_first:
            hud.paginator.turn(-hud.paginator.total)
        hud.emit(state, hud.status_line(state), hud.paginator.page_text(),
                 hud.paginator.footer(), urgent=True)

    async def test_four_trigger_sources_are_equivalent(self, attached):
        """镜腿 / 手机按钮 / MCP / 语音 —— 四个触发源必须产生完全相同的结果。"""
        hud, sink = attached
        self._load(hud)
        assert hud.paginator.total > 2, "语料不够长，翻页测试无效"

        frames = {}
        for src in ("glasses", "phone", "mcp", "voice"):
            self._load(hud)
            assert hud.page(1, source=src) is True
            f = hud.current_frame
            frames[src] = (f["state"], f["containers"], f["meta"]["page"])
        assert len(set(map(repr, frames.values()))) == 1, f"触发源之间不等价：{frames}"

    async def test_page_returns_false_at_boundary(self, attached):
        hud, _ = attached
        self._load(hud)                                    # 首页
        assert hud.page(-1, source="glasses") is False, "首页再往前不应发冗余帧"
        seq_before = hud.seq
        hud.page(-1, source="glasses")
        assert hud.seq == seq_before, "到边界不能发帧"

        hud.page(hud.paginator.total, source="glasses")    # 一路翻到末页
        assert hud.paginator.at_last
        assert hud.page(1, source="glasses") is False, "末页再往后同理"

    async def test_page_ignored_when_not_pageable(self, attached):
        hud, _ = attached
        hud.paginator.set_text(LONG_TEXT)
        hud.emit("S2", hud.status_line("S2"), "聆听中", urgent=True)
        assert hud.page(1, source="glasses") is False

    async def test_page_preserves_status_word(self, attached):
        """修 S2 的回归：翻页后状态条不能退化成只剩徽记和字形。"""
        hud, _ = attached
        self._load(hud)
        hud.page(1, source="phone")
        assert "回答" in hud.current_frame["containers"]["status"]


# ------------------------------------------------------------------ 租约（W1）


class TestLease:
    def test_acquire_and_conflict(self, hud):
        lease = hud.acquire_lease("mcp-a", 5000)
        assert hud.lease_info()["holder"] == "mcp-a"
        with pytest.raises(LeaseHeld) as ei:
            hud.acquire_lease("mcp-b", 5000)
        err = ei.value.as_dict()
        assert err["code"] == "LEASE_HELD" and err["holder"] == "mcp-a"
        assert err["expires_in_ms"] > 0
        # 同 holder 再申请 = 续租，lease_id 不变
        again = hud.acquire_lease("mcp-a", 9000)
        assert again.id == lease.id

    async def test_lease_expires(self, hud):
        lease = hud.acquire_lease("mcp-a", 100)
        await asyncio.sleep(0.15)
        assert hud.lease_info() is None
        with pytest.raises(LeaseInvalid):
            hud.render_external(lease.id, "过期后不该还能写屏")
        hud.acquire_lease("mcp-b", 1000)     # 过期后别人可以接手

    def test_release(self, hud):
        lease = hud.acquire_lease("mcp-a", 5000)
        assert hud.release_lease(lease.id) is True
        assert hud.release_lease(lease.id) is False
        assert hud.lease_info() is None

    async def test_local_render_preempts(self, attached):
        """用户开口 ⇒ 屏幕无条件归语音链路（租约被抢占并进事件缓冲区）。"""
        hud, _ = attached
        lease = hud.acquire_lease("mcp-a", 60_000)
        hud.render_external(lease.id, "MCP 写的内容")
        assert hud.state == EXTERNAL_STATE

        hud.emit("S2", hud.status_line("S2"), "", urgent=True)   # 本地写屏
        assert hud.lease_info() is None
        with pytest.raises(LeaseInvalid):
            hud.render_external(lease.id, "抢占之后不该还能写")

        kinds = [e["kind"] for e in hud.drain_events()]
        assert kinds == ["lease_acquired", "lease_preempted"]
        ev = hud.drain_events()[-1]
        assert ev["holder"] == "mcp-a" and ev["reason"] == "local_render"

    async def test_external_render_does_not_preempt_itself(self, attached):
        hud, _ = attached
        lease = hud.acquire_lease("mcp-a", 60_000)
        hud.render_external(lease.id, "第一屏")
        hud.render_external(lease.id, "第二屏")
        assert hud.lease_info()["lease_id"] == lease.id

    async def test_external_text_goes_through_the_same_engine(self, attached):
        hud, _ = attached
        lease = hud.acquire_lease("mcp-a", 60_000)
        hud.render_external(lease.id, LONG_TEXT, title="检索结果")
        assert hud.paginator.total > 1, "外部文本必须走同一个分页器"
        assert "检索结果" in hud.current_frame["containers"]["status"]
        # 翻页对外部渲染同样有效，且不会把状态条换成本地文案
        # （set_text 停在首页，所以这里往后翻）
        assert hud.page(1, source="mcp") is True
        assert "检索结果" in hud.current_frame["containers"]["status"]
        assert hud.lease_info() is not None, "外部翻页不应抢占自己的租约"

    async def test_external_hold_falls_back_to_idle(self, attached):
        hud, _ = attached
        lease = hud.acquire_lease("mcp-a", 60_000)
        hud.render_external(lease.id, "短内容", hold_ms=80)
        await asyncio.sleep(0.15)
        assert hud.state == "S0", "hold_ms 到点必须回待机"

    async def test_clear_external(self, attached):
        hud, _ = attached
        lease = hud.acquire_lease("mcp-a", 60_000)
        hud.render_external(lease.id, LONG_TEXT)
        hud.clear_external(lease.id)
        assert hud.state == "S0"
        assert hud.paginator.total == 1

    def test_drain_events_is_incremental(self, hud):
        a = hud.acquire_lease("mcp-a", 5000)
        hud.release_lease(a.id)
        first = hud.drain_events()
        assert [e["id"] for e in first] == [1, 2]
        assert hud.drain_events(after_id=first[-1]["id"]) == []
        hud.acquire_lease("mcp-b", 5000)
        assert [e["kind"] for e in hud.drain_events(after_id=2)] == ["lease_acquired"]

    async def test_snapshot_shape(self, attached):
        hud, _ = attached
        hud.acquire_lease("mcp-a", 5000)
        snap = hud.snapshot()
        assert snap["device_id"] == "dev_test"
        assert snap["online"] is True
        assert snap["lease"]["holder"] == "mcp-a"
        assert snap["page"] == {"cur": 1, "total": 1}
        assert snap["idle_ms"] >= 0
        assert set(snap["containers"]) == {"status", "body", "foot"}


# ------------------------------------------------------------------ S7：计时器不再抹掉外部渲染


class TestTimerOwnership:
    async def test_external_render_takes_over_the_timer(self, attached):
        """修 S7：一轮问答留下的 idle_after 定时器不能把 MCP 写的画面清掉。"""
        hud, _ = attached
        hud.emit("S7", hud.status_line("S7"), "上一轮的回答", urgent=True)
        hud.start_timer(lambda: hud.idle_after(0.05))   # 模拟阅读态回落
        lease = hud.acquire_lease("mcp-a", 60_000)
        hud.render_external(lease.id, "MCP 的内容", hold_ms=60_000)
        await asyncio.sleep(0.15)
        assert hud.state == EXTERNAL_STATE, "旧定时器到点把屏幕清了 —— S7 复发"
        assert hud.current_frame["containers"]["body"].startswith("MCP 的内容")


class TestNoChineseLiteralsReachTheScreen:
    """网关侧会上屏的字符串必须走 `HUD_MESSAGES`，不许写中文字面量。

    英文演示时屏幕上蹦出「与 agent 断开／按住说话可重试」才发现的：
    `voice/pipeline.py` 里九处 `emit()` 直接写死了中文。这些全在错误分支上，
    平时跑不到 —— 单测全绿、人眼也扫不到。

    所以这里不是逐条断言文案，而是**扫源码**：`emit()` 附近出现中文字面量就失败。
    下一个新写的错误分支会自己撞上来。
    """

    #: 只扫会上屏的调用点。日志、注释、docstring 里的中文不管 —— 那是给读代码的人看的。
    EMIT_CONTEXT = re.compile(r"\.emit\(|status_line\(|word=|glyph=")
    CJK = re.compile(r"[一-鿿]")
    STRING = re.compile(r"""(['"])((?:[^'"\\]|\\.)*?)\1""")

    def _offenders(self) -> list[str]:
        root = pathlib.Path(__file__).resolve().parent.parent / "lens_gateway"
        out: list[str] = []
        for f in sorted(root.rglob("*.py")):
            if f.name == "hud.py":
                continue                     # 文案表本身就住在这里
            lines = f.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                st = line.strip()
                if st.startswith(("#", "*", '"""', ":", "'''")):
                    continue
                ctx = "\n".join(lines[max(0, i - 3): i + 1])
                if not self.EMIT_CONTEXT.search(ctx):
                    continue
                for m in self.STRING.finditer(line):
                    if self.CJK.search(m.group(2)):
                        rel = f.relative_to(root.parent)
                        out.append(f"{rel}:{i + 1}  {st[:88]}")
                        break
        return out

    def test_emit_sites_carry_no_chinese_literals(self) -> None:
        bad = self._offenders()
        assert bad == [], (
            "这些地方直接把中文写进了会上屏的字符串，英文模式下会原样出现在眼镜上。\n"
            "请挪进 device/hud.py 的 HUD_MESSAGES，再用 hud.msg(key) 取：\n  "
            + "\n  ".join(bad))

    def test_every_message_key_exists_in_both_locales(self) -> None:
        """两种语言的 key 必须完全一致 —— 缺一个就是运行时 KeyError 上屏。"""
        locales = list(HUD_MESSAGES)
        assert len(locales) > 1
        base = set(HUD_MESSAGES[locales[0]])
        for loc in locales[1:]:
            assert set(HUD_MESSAGES[loc]) == base, f"{loc} 与 {locales[0]} 的 key 对不上"

    def test_english_messages_have_no_chinese(self) -> None:
        for key, text in HUD_MESSAGES["en"].items():
            assert not self.CJK.search(text), f'HUD_MESSAGES["en"][{key!r}] 还是中文：{text!r}'

    def test_the_scanner_actually_looks_at_something(self) -> None:
        """反向验证：把扫描器指向中文文案表本身，必须扫出东西。

        否则「零违规」可能只是因为它什么都没在看。
        """
        hud_src = (pathlib.Path(__file__).resolve().parent.parent
                   / "lens_gateway" / "device" / "hud.py").read_text(encoding="utf-8")
        assert self.CJK.search(hud_src), "扫描器的正则连中文都匹配不到"
        assert self.STRING.search(hud_src)
