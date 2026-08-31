"""遥测缓存与上行通路（协议 v1.1）。

核心断言只有一条：**网关永远不会报出它其实不知道的东西**。
没数据就是 None、拉来的值不冒充新鲜值、戒指的电量不算眼镜的、断线后如实标 stale。
"""
from __future__ import annotations

import asyncio

import pytest

from lens_gateway.device import TelemetryStore
from lens_gateway.session import DeviceSession
from tests.test_device import Sink, make_config
from tests.test_session import FakeAsr, FakeClaw

GLASSES = {
    "model": "g2",
    "sn": "G2SN-ABCD1234",
    "isGlasses": True,
    "connectType": "connected",
    "connected": True,
    "batteryLevel": 86,
    "isCharging": False,
    "isWearing": True,
    "isInCase": False,
}
RING = {**GLASSES, "model": "ring1", "sn": "R1SN-9999", "isGlasses": False, "batteryLevel": 41}


class TestStore:
    def test_no_data_is_none_not_zeros(self):
        """从未上报过必须返回 None。返回 battery=0 的默认结构就是在编数据。"""
        assert TelemetryStore(stale_seconds=60).snapshot() is None

    def test_push_roundtrip(self):
        st = TelemetryStore(stale_seconds=60)
        assert st.update(GLASSES, "push") is True
        snap = st.snapshot()
        assert snap["model"] == "g2"
        assert snap["batteryLevel"] == 86
        assert snap["source"] == "push"
        assert snap["stale"] is False
        assert snap["age_ms"] >= 0

    def test_sn_is_masked(self):
        """SN 是可跨服务关联的设备指纹，遥测要经 MCP 交给第三方模型厂商。"""
        st = TelemetryStore(stale_seconds=60)
        st.update(GLASSES, "push")
        assert st.snapshot()["sn"] == "…1234"
        assert "ABCD" not in str(st.snapshot())

    def test_ring_is_rejected_and_counted(self):
        st = TelemetryStore(stale_seconds=60)
        assert st.update(RING, "push") is False
        assert st.snapshot() is None, "戒指的电量绝不能变成眼镜的电量"
        assert st.diagnostics()["rejected"]["not_glasses"] == 1

    def test_unverified_model_is_rejected(self):
        """插件没能确认型号（isGlasses 缺省）时同样不能收——"大概是眼镜"不算数。"""
        st = TelemetryStore(stale_seconds=60)
        assert st.update({k: v for k, v in GLASSES.items() if k != "isGlasses"}, "push") is False
        assert st.diagnostics()["rejected"]["not_glasses"] == 1

    def test_unknown_source_rejected(self):
        st = TelemetryStore(stale_seconds=60)
        assert st.update(GLASSES, "guess") is False
        assert st.diagnostics()["rejected"]["bad_source"] == 1

    def test_bad_payload_rejected(self):
        st = TelemetryStore(stale_seconds=60)
        assert st.update("86%", "push") is False
        assert st.update(None, "push") is False
        assert st.diagnostics()["rejected"]["bad_payload"] == 2

    def test_unknown_fields_are_dropped(self):
        """字段集必须是我们审过的——遥测会流向第三方 LLM 厂商。"""
        st = TelemetryStore(stale_seconds=60)
        st.update({**GLASSES, "wifiSsid": "家里的路由器", "gps": [30.1, 120.2]}, "push")
        snap = st.snapshot()
        assert "wifiSsid" not in snap and "gps" not in snap

    def test_poll_is_labelled_as_possibly_cached(self):
        """官方没说明 getDeviceInfo 是否真读设备，poll 不能标成新鲜值。"""
        st = TelemetryStore(stale_seconds=60)
        st.update(GLASSES, "poll")
        snap = st.snapshot()
        assert snap["source"] == "poll"
        assert "缓存" in snap["source_note"]

    def test_goes_stale_with_age(self):
        st = TelemetryStore(stale_seconds=60)
        st.update(GLASSES, "push", now=1000.0)
        assert st.snapshot(now=1000.0 + 59)["stale"] is False
        fresh_off = st.snapshot(now=1000.0 + 61)
        assert fresh_off["stale"] is True
        assert fresh_off["batteryLevel"] == 86, "过期不等于丢弃——仍返回最后已知值"
        assert fresh_off["age_ms"] == 61_000

    def test_rejects_nonpositive_stale_window(self):
        with pytest.raises(ValueError):
            TelemetryStore(stale_seconds=0)


class TestUplink:
    @pytest.fixture()
    def session(self) -> DeviceSession:
        cfg = make_config()
        return DeviceSession("dev_t", cfg, FakeAsr(), FakeClaw(cfg.openclaw))

    async def test_push_message_lands_in_the_store(self, session):
        await session.handle_text({"type": "telemetry", "data": GLASSES})
        assert session.snapshot()["telemetry"]["batteryLevel"] == 86
        assert session.snapshot()["telemetry"]["source"] == "push"

    async def test_ring_push_is_dropped(self, session):
        await session.handle_text({"type": "telemetry", "data": RING})
        assert session.snapshot()["telemetry"] is None
        assert session.snapshot()["telemetry_diagnostics"]["rejected"]["not_glasses"] == 1

    async def test_cmd_roundtrip(self, session):
        sent: list[dict] = []

        async def send(obj: dict) -> None:
            sent.append(obj)

        session.attach(send)
        cid = await session.request_telemetry()
        assert sent[-1] == {"type": "cmd", "cmd": "telemetry", "id": cid}

        await session.handle_text({"type": "cmd_result", "id": cid, "ok": True, "data": GLASSES})
        assert session.snapshot()["telemetry"]["source"] == "poll"

    async def test_offline_request_is_a_noop(self, session):
        assert await session.request_telemetry() is None

    async def test_unclaimed_cmd_result_is_ignored(self, session):
        """没有对应 id 的回执必须丢掉——否则伪造一条 cmd_result 就能往缓存里塞数据。"""
        await session.handle_text({"type": "cmd_result", "id": "c999", "ok": True, "data": GLASSES})
        assert session.snapshot()["telemetry"] is None

    async def test_cmd_result_is_single_use(self, session):
        sent: list[dict] = []

        async def send(obj: dict) -> None:
            sent.append(obj)

        session.attach(send)
        cid = await session.request_telemetry()
        await session.handle_text({"type": "cmd_result", "id": cid, "ok": True, "data": GLASSES})
        session.telemetry._record = None                      # 模拟缓存被清
        await session.handle_text({"type": "cmd_result", "id": cid, "ok": True, "data": GLASSES})
        assert session.snapshot()["telemetry"] is None, "同一个 id 不能被重放两次"

    async def test_failed_cmd_result_does_not_poison_the_cache(self, session):
        sent: list[dict] = []

        async def send(obj: dict) -> None:
            sent.append(obj)

        session.attach(send)
        await session.handle_text({"type": "telemetry", "data": GLASSES})
        cid = await session.request_telemetry()
        await session.handle_text({"type": "cmd_result", "id": cid, "ok": False, "error": "no_bridge"})
        snap = session.snapshot()["telemetry"]
        assert snap["batteryLevel"] == 86 and snap["source"] == "push", "失败的拉取不能覆盖已知值"

    async def test_reconnect_clears_pending_commands(self, session):
        async def send(obj: dict) -> None:
            pass

        session.attach(send)
        cid = await session.request_telemetry()
        session.detach()
        session.attach(send)                                   # 新连接
        await session.handle_text({"type": "cmd_result", "id": cid, "ok": True, "data": GLASSES})
        assert session.snapshot()["telemetry"] is None, "旧连接的回执不该被新连接认领"

    async def test_telemetry_survives_disconnect_and_is_marked_stale(self, session):
        async def send(obj: dict) -> None:
            pass

        session.attach(send)
        await session.handle_text({"type": "telemetry", "data": GLASSES})
        session.detach()
        snap = session.snapshot()
        assert snap["online"] is False
        assert snap["telemetry"]["batteryLevel"] == 86, "断线后仍返回最后已知值"
        # 时间推进后自动 stale（这里直接改采样时刻，避免测试睡 60 秒）
        session.telemetry._record.sampled_at -= 999
        assert session.snapshot()["telemetry"]["stale"] is True


class TestLowBattery:
    """DESIGN.md §4.4：「电量仅 <15% 页脚出现一次」。

    在 M4 打通遥测上行之前，这条承诺**根本没有数据源** —— 网关不知道电量，
    只能是文档里的一句空话。现在它是真的。
    """

    @pytest.fixture()
    def session(self) -> DeviceSession:
        cfg = make_config(battery_warn_percent=15)
        return DeviceSession("dev_b", cfg, FakeAsr(), FakeClaw(cfg.openclaw))

    async def _push(self, session, level: int, charging: bool = False) -> None:
        await session.handle_text({"type": "telemetry",
                                   "data": {**GLASSES, "batteryLevel": level, "isCharging": charging}})

    async def test_healthy_battery_never_shows(self, session):
        sink = Sink()
        session.attach(sink)
        await self._push(session, 86)
        session.hud.emit("S7", "完成", "答案", "1/1", urgent=True)
        await asyncio.sleep(0)
        assert sink.frames[-1]["containers"]["foot"] == "1/1", "平时电量根本不该上屏"

    async def test_low_battery_appears_once(self, session):
        sink = Sink()
        session.attach(sink)
        await self._push(session, 12)

        session.hud.emit("S7", "完成", "答案", "1/2", urgent=True)
        await asyncio.sleep(0)
        foot = sink.frames[-1]["containers"]["foot"]
        assert foot == f'1/2 {session.hud.glyphs["battery_low"]}12%'

        session.hud.emit("S7", "完成", "答案", "2/2", urgent=True)
        await asyncio.sleep(0)
        assert sink.frames[-1]["containers"]["foot"] == "2/2", "第二帧不该再提示"

    async def test_uses_an_in_font_glyph_not_the_lightning_bolt(self, session):
        """原设计写的是「⚡15%」，但 ⚡ U+26A1 不在 G2 字库，真机上会被静默丢弃。"""
        from lens_gateway.formatting import in_font

        assert in_font("⚡") is False
        assert in_font(session.hud.glyphs["battery_low"]) is True

    async def test_rearms_after_charging(self, session):
        sink = Sink()
        session.attach(sink)
        await self._push(session, 12)
        session.hud.emit("S0", "待机", "", "", urgent=True)
        await asyncio.sleep(0)

        await self._push(session, 90, charging=True)   # 充上电，回到阈值以上
        await self._push(session, 11)                  # 又掉下去
        session.hud.emit("S7", "完成", "答案", "1/1", urgent=True)
        await asyncio.sleep(0)
        assert "11%" in sink.frames[-1]["containers"]["foot"]

    async def test_charging_does_not_warn(self, session):
        sink = Sink()
        session.attach(sink)
        await self._push(session, 8, charging=True)
        session.hud.emit("S7", "完成", "答案", "1/1", urgent=True)
        await asyncio.sleep(0)
        assert sink.frames[-1]["containers"]["foot"] == "1/1", "充电中不该报低电量"

    async def test_ring_battery_never_triggers_the_warning(self, session):
        """戒指的 41% 既不进遥测缓存，也不能触发眼镜的低电量提示。"""
        sink = Sink()
        session.attach(sink)
        await session.handle_text({"type": "telemetry", "data": {**RING, "batteryLevel": 9}})
        session.hud.emit("S7", "完成", "答案", "1/1", urgent=True)
        await asyncio.sleep(0)
        assert sink.frames[-1]["containers"]["foot"] == "1/1"

    async def test_hint_survives_throttling(self, session):
        """被节流合并掉的帧不算"出现过" —— 否则提示会悄无声息地丢掉。"""
        sink = Sink()
        session.attach(sink)
        session.hud.emit("S6", "回答", "第一帧", "1/2", urgent=True)   # 吃掉节流窗口
        await asyncio.sleep(0)
        await self._push(session, 7)
        session.hud.emit("S6", "回答", "被合并掉", "1/2")               # 节流内，不发
        session.hud.emit("S6", "回答", "最终这帧", "2/2")               # 也在节流内
        await asyncio.sleep(0)
        assert len(sink.frames) == 1

        await asyncio.sleep(make_config().composer.throttle_ms / 1000 + 0.05)
        assert "7%" in sink.frames[-1]["containers"]["foot"]
        assert sink.frames[-1]["containers"]["body"] == "最终这帧"

    async def test_disabled_by_zero_threshold(self):
        cfg = make_config(battery_warn_percent=0)
        session = DeviceSession("dev_b0", cfg, FakeAsr(), FakeClaw(cfg.openclaw))
        sink = Sink()
        session.attach(sink)
        await self._push(session, 3)
        session.hud.emit("S7", "完成", "答案", "1/1", urgent=True)
        await asyncio.sleep(0)
        assert sink.frames[-1]["containers"]["foot"] == "1/1"
