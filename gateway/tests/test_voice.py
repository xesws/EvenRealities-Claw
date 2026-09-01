"""mic 看门狗（R2 / B2）。

这条路径此前**一个测试都没有**，而它恰好是真机上最容易误伤的地方：
按下 PTT 之后要等 BLE 下发开麦命令、固件启麦、首块 PCM 经 BLE 回传，
旧代码给的宽限是硬编码 1.0s，而 partial 循环 700ms 才检查一次 ⇒ 真实宽限 1.4s。

这里测的是「两种沉默用两个判据」：还没出声是启麦慢，出过声又断是链路断。
"""
from __future__ import annotations

import asyncio

import pytest

from lens_gateway.config import AsrConfig
from lens_gateway.session import DeviceSession
from tests.test_device import Sink, make_config
from tests.test_session import FakeAsr, FakeClaw

#: 一帧 100ms 的 16kHz s16le 静音。内容不重要，重要的是"有帧到达"这件事。
FRAME = b"\x00\x00" * 1600


def make_session(**asr_over) -> DeviceSession:
    cfg = make_config(asr_over=asr_over)
    return DeviceSession("dev_v", cfg, FakeAsr(), FakeClaw(cfg.openclaw))


def warned(sink: Sink) -> bool:
    return any("麦克风没有声音" in f["containers"]["body"] for f in sink.frames)


class TestMicWatchdog:
    """启麦慢 vs 链路断：同样是"没有音频"，等待预算差好几倍。"""

    async def test_slow_mic_startup_is_not_an_error(self):
        """宽限期内一声不吭是正常的 —— 固件还在启麦。"""
        s = make_session(partial_interval_ms=20, mic_warmup_seconds=0.40)
        sink = Sink()
        s.attach(sink)
        await s.voice.ptt_start()
        await asyncio.sleep(0.20)          # 只走了宽限期的一半
        assert not warned(sink)
        await s.voice.cancel_listening()

    async def test_silence_past_the_warmup_budget_warns(self):
        s = make_session(partial_interval_ms=20, mic_warmup_seconds=0.15)
        sink = Sink()
        s.attach(sink)
        await s.voice.ptt_start()
        await asyncio.sleep(0.30)
        assert warned(sink)
        await s.voice.cancel_listening()

    async def test_audio_arriving_in_time_clears_the_warmup(self):
        """首帧在宽限内到达 ⇒ 之后按 gap 判据走，不再受 warmup 影响。"""
        s = make_session(partial_interval_ms=20, mic_warmup_seconds=0.40,
                         mic_gap_seconds=1.0)
        sink = Sink()
        s.attach(sink)
        await s.voice.ptt_start()
        for _ in range(6):                  # 持续喂帧，跨过原 warmup 时点
            await s.voice.feed_pcm(FRAME)
            await asyncio.sleep(0.08)
        assert not warned(sink)
        await s.voice.cancel_listening()

    async def test_stream_dying_midway_warns_on_the_short_budget(self):
        """出过声之后断掉，用的是短得多的 gap 预算 —— 不能等到 warmup 才发现。"""
        s = make_session(partial_interval_ms=20, mic_warmup_seconds=5.0,
                         mic_gap_seconds=0.10)
        sink = Sink()
        s.attach(sink)
        await s.voice.ptt_start()
        await s.voice.feed_pcm(FRAME)
        await asyncio.sleep(0.30)            # 远小于 warmup(5.0)，远大于 gap(0.10)
        assert warned(sink), "音频断流后应按 gap 判据立刻告警，而不是等 warmup"
        await s.voice.cancel_listening()

    async def test_a_new_ptt_resets_the_audio_flag(self):
        """上一轮说过话，不能让这一轮直接落到 gap 判据上（那样启麦慢就会误报）。"""
        s = make_session(partial_interval_ms=20, mic_warmup_seconds=0.40,
                         mic_gap_seconds=0.05)
        sink = Sink()
        s.attach(sink)
        await s.voice.ptt_start()
        await s.voice.feed_pcm(FRAME)
        await s.voice.cancel_listening()

        sink.frames.clear()
        await s.voice.ptt_start()            # 新一轮：还没出过声，应走 warmup
        await asyncio.sleep(0.20)            # > gap(0.05)，< warmup(0.40)
        assert not warned(sink)
        await s.voice.cancel_listening()


class TestB2Regression:
    """把 B2 的具体数字钉住 —— 这是真机误报的直接成因。"""

    def test_default_warmup_covers_the_ble_startup_path(self):
        """默认宽限必须显著大于旧的硬编码 1.0s。

        1.4s（1.0s 条件 + 700ms 检查间隔）要塞下 WS RTT + BLE 下发 + 固件启麦 +
        首块 PCM 回传 + 插件攒 200ms + WS 上行。真机标定前先给 2.5s。
        """
        assert AsrConfig().mic_warmup_seconds >= 2.5

    def test_gap_is_much_shorter_than_warmup(self):
        """两个预算必须拉开，否则拆开判据就没意义了。"""
        cfg = AsrConfig()
        assert cfg.mic_gap_seconds < cfg.mic_warmup_seconds / 2

    async def test_no_warning_at_the_old_1_4s_mark(self):
        """回归防线：旧代码在这个时刻会报「麦克风没有声音」，现在不该报。"""
        s = make_session(partial_interval_ms=100)   # 用生产默认的 2.5s warmup
        sink = Sink()
        s.attach(sink)
        await s.voice.ptt_start()
        await asyncio.sleep(1.5)
        assert not warned(sink)
        await s.voice.cancel_listening()

    @pytest.mark.parametrize("bad", [
        {"mic_warmup_seconds": 0},
        {"mic_gap_seconds": -1},
        {"mic_warmup_seconds": 30.0},        # 超过 max_utterance_seconds(25)
    ])
    def test_nonsense_budgets_are_rejected_at_load(self, bad):
        with pytest.raises(ValueError):
            AsrConfig(**bad)
