"""会话装配层：语音链路回调（S1/S5b）与会话回收（S4）。

`DeviceSession` 本身只做装配与路由，所以这里测的是"路由到位了没有"
以及那些**以前是死代码**的分支现在真的能走到。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from lens_gateway.config import Config
from lens_gateway.session import DeviceSession
from tests.test_device import Sink, make_config


class FakeAsr:
    """VoicePipeline 只用到 partial/final 两个方法。"""

    def __init__(self) -> None:
        self.cfg = None

    async def partial(self, pcm: bytes) -> str:
        return ""

    async def final(self, pcm: bytes):
        raise AssertionError("本文件不该走到 final")


class FakeClaw:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.sent: list[tuple[str, str]] = []

    def session_busy(self, key: str) -> bool:
        return False

    async def chat_send(self, key: str, message: str, on_event) -> None:
        self.sent.append((key, message))


@pytest.fixture()
def session() -> DeviceSession:
    cfg = make_config()
    return DeviceSession("dev_s", cfg, FakeAsr(), FakeClaw(cfg.openclaw))


class TestAgentEvents:
    async def test_tool_branch_is_alive(self, session):
        """修 S1：S5「工具态」以前在 `STATE_LABEL` 里有定义，却没有任何入口。"""
        sink = Sink()
        session.attach(sink)
        await session.voice.on_agent_event("tool", "web_search", "正在检索天气")
        await asyncio.sleep(0)
        assert session.state == "S5"
        frame = session.current_frame
        assert "web_search" in frame["containers"]["status"]
        assert frame["containers"]["body"] == "正在检索天气"

    async def test_tool_status_is_truncated(self, session):
        await session.voice.on_agent_event("tool", "一个特别长的工具名字超过十二个汉字上限", "")
        assert len(session.current_frame["containers"]["status"]) < 30

    async def test_tool_cancels_the_thinking_timer(self, session):
        """工具态必须接管计时器，否则思考计秒会把工具画面刷掉。"""
        hud = session.hud
        hud.start_timer(lambda: hud.idle_after(0.05))
        await session.voice.on_agent_event("tool", "grep", "在翻代码")
        await asyncio.sleep(0.15)
        assert session.state == "S5"

    async def test_error_branch_falls_back_to_idle(self, session):
        await session.voice.on_agent_event("error", "上游超时", "")
        assert session.state == "S8"
        assert "上游超时" in session.current_frame["containers"]["body"]

    async def test_final_marks_style_as_sent(self, session):
        await session.voice.on_agent_event("final", "这是回答。", "")
        assert session.state == "S7"
        assert session.voice._style_sent is True


class TestReset:
    async def test_reset_restores_style_injection(self, session):
        """修 S5b：`_style_sent` 以前从不复位 —— agent 会话重置之后，
        小屏风格指令再也不会注入，模型会退回长文与 markdown。"""
        await session.voice.on_agent_event("final", "回答", "")
        assert session.voice._style_sent is True

        await session.handle_text({"type": "reset"})
        assert session.voice._style_sent is False
        assert session.state == "S0"

        await session.voice.dispatch("再问一次")
        key, message = session.voice.claw.sent[-1]
        assert message.startswith("[系统指令"), "reset 之后必须重新注入小屏风格指令"
        assert message.endswith("再问一次")


class TestRouting:
    async def test_page_message_routes_to_hud(self, session):
        long_text = "".join(f"第{i}句话，凑够多页。" for i in range(60))
        session.hud.paginator.set_text(long_text)
        session.hud.emit("S7", session.hud.status_line("S7"),
                         session.hud.paginator.page_text(), "", urgent=True)
        last = session.hud.paginator.cur
        await session.handle_text({"type": "page", "dir": "prev"})
        assert session.hud.paginator.cur == last - 1
        await session.handle_text({"type": "page", "dir": "next"})
        assert session.hud.paginator.cur == last

    async def test_unknown_message_is_ignored(self, session):
        await session.handle_text({"type": "没见过的类型"})   # 加法安全：不能炸
        assert session.state == "S0"

    async def test_readonly_views_track_the_hud(self, session):
        assert session.snapshot()["device_id"] == "dev_s"
        assert session.seq == session.hud.seq
        assert session.last_active == session.hud.last_active


class TestSessionTtl:
    """修 S4：`self.sessions` 以前只增不减。"""

    @pytest.fixture()
    def server(self, tmp_path, monkeypatch):
        from lens_gateway import server as srv

        monkeypatch.setattr(srv, "STATE_DIR", tmp_path)
        monkeypatch.setattr(srv, "jwt_secret", lambda: b"0" * 32)
        return srv.LensServer(make_config(session_ttl_seconds=10.0))

    def _add(self, server, device_id: str, *, online: bool, idle: float) -> DeviceSession:
        s = DeviceSession(device_id, server.cfg, FakeAsr(), FakeClaw(server.cfg.openclaw))
        if online:
            s.attach(Sink())
        s.hud.last_active = time.monotonic() - idle
        server.sessions[device_id] = s
        return s

    def test_evicts_only_offline_and_silent(self, server):
        self._add(server, "dev_old", online=False, idle=60)     # 离线且超时 → 回收
        self._add(server, "dev_fresh", online=False, idle=1)    # 离线但刚动过 → 留
        self._add(server, "dev_live", online=True, idle=60)     # 在线，多久没动都留

        assert server.sweep_sessions() == ["dev_old"]
        assert sorted(server.sessions) == ["dev_fresh", "dev_live"]

    async def test_eviction_cancels_the_timer(self, server):
        """回收必须连计时器一起收掉，否则被丢弃的会话仍挂着 asyncio 任务。"""
        s = self._add(server, "dev_old", online=False, idle=60)
        s.hud.start_timer(lambda: s.hud.idle_after(999))
        task = s.hud._timer_task
        server.sweep_sessions()
        await asyncio.sleep(0)
        assert task.cancelled()
        assert s.hud._timer_task is None

    def test_zero_ttl_disables_eviction(self, tmp_path, monkeypatch):
        from lens_gateway import server as srv

        monkeypatch.setattr(srv, "STATE_DIR", tmp_path)
        monkeypatch.setattr(srv, "jwt_secret", lambda: b"0" * 32)
        s = srv.LensServer(make_config(session_ttl_seconds=0.0))
        self._add(s, "dev_old", online=False, idle=10_000)
        assert s.sweep_sessions() == []
        assert "dev_old" in s.sessions


class TestStartupHooks:
    """回归：aiohttp 的 on_startup 钩子只能注册一次。

    这是真出过的事故 —— 新加的清扫钩子与已有的 `_on_startup` **同名**，
    Python 类体里后定义的覆盖先定义的，于是 `build_app` 里两处 append 变成
    「同一个 warmup 注册两遍」。静音 warmup 要 ~12s 且全程持 ASR 锁，
    第二遍正好卡住用户第一句话的 final：现象是「说完十秒字才上屏」，
    看起来像 ASR 慢，其实一次解码只要 0.35s。
    """

    @pytest.fixture()
    def server(self, tmp_path, monkeypatch):
        from lens_gateway import server as srv

        monkeypatch.setattr(srv, "STATE_DIR", tmp_path)
        monkeypatch.setattr(srv, "jwt_secret", lambda: b"0" * 32)
        return srv.LensServer(make_config())

    def test_startup_registered_exactly_once(self, server):
        app = server.build_app()
        assert app.on_startup.count(server._on_startup) == 1
        assert app.on_cleanup.count(server._on_cleanup) == 1

    async def test_startup_starts_the_sweeper_and_cleanup_stops_it(self, server, monkeypatch):
        warmups = []

        async def fake_warmup() -> None:
            warmups.append(1)

        monkeypatch.setattr(server, "_warmup", fake_warmup)
        app = server.build_app()
        for hook in app.on_startup:
            await hook(app)
        await asyncio.sleep(0)
        assert warmups == [1], "warmup 必须且只能跑一次"
        assert server._sweeper is not None and not server._sweeper.done()

        for hook in app.on_cleanup:
            await hook(app)
        await asyncio.sleep(0)
        # `_sweep_loop` 自己吞掉 CancelledError 做干净收尾，所以是 done 而非 cancelled
        assert server._sweeper.done()


class TestAsrWarmupIdempotence:
    async def test_second_warmup_is_a_noop(self, monkeypatch):
        from lens_gateway.asr import AsrEngine
        from lens_gateway.config import AsrConfig

        eng = AsrEngine(AsrConfig())
        loads, decodes = [], []
        monkeypatch.setattr(eng, "_load", lambda: loads.append(1))
        monkeypatch.setattr(eng, "_decode", lambda m, a, b: (decodes.append(1), ("", -1.0))[1])
        eng._partial_model = object()
        eng._final_model = eng._partial_model

        await eng.warmup()
        assert eng.ready is True and loads == [1] and len(decodes) == 1
        await eng.warmup()          # 第二次必须直接返回，绝不再持锁跑静音解码
        assert loads == [1] and len(decodes) == 1

    async def test_warmup_holds_the_lock_only_once(self, monkeypatch):
        """warmup 期间锁是被独占的；幂等之后第二次调用不会再抢锁。"""
        from lens_gateway.asr import AsrEngine
        from lens_gateway.config import AsrConfig

        eng = AsrEngine(AsrConfig())
        monkeypatch.setattr(eng, "_load", lambda: None)
        monkeypatch.setattr(eng, "_decode", lambda m, a, b: ("", -1.0))
        eng._partial_model = eng._final_model = object()
        await eng.warmup()
        assert eng._lock.locked() is False
        await eng.warmup()
        assert eng._lock.locked() is False
