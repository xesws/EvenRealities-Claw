"""Agent provider 抽象与 W6 溯源。

这一组测试守的是一句话：**"演示里没有 fake" 这个说法必须可被验证**。
可验证的前提是替身无法伪装成真 agent —— 所以本文件把"替身被识别 → 被公开标注
→ 标记出现在眼镜屏幕上"这条链路整个钉住。
"""
from __future__ import annotations

import asyncio

import pytest

from lens_gateway.config import AgentConfig, Config, OpenClawConfig
from lens_gateway.device import HudDevice
from lens_gateway.providers import (UNKNOWN_AGENT, AgentInfo, LensAgentClient,
                                    OpenClawClient, agent_is_trusted, build_provider)
from lens_gateway.session import DeviceSession
from tests.test_device import Sink, make_config
from tests.test_session import FakeAsr, FakeClaw


class _Stub:
    """只实现 `agent_is_trusted` 用到的那两项。"""

    def __init__(self, *, connected: bool, production: bool) -> None:
        self.connected = asyncio.Event()
        if connected:
            self.connected.set()
        self._info = AgentInfo(backend="openclaw", production=production)

    def info(self) -> AgentInfo:
        return self._info


class TestFactory:
    def test_default_is_openclaw(self):
        """P0 的验收标准是行为零变化 —— 默认值必须还是 openclaw。"""
        assert build_provider(Config()).__class__ is OpenClawClient

    def test_lens_provider_selected_by_config(self):
        cfg = Config(agent=AgentConfig(provider="lens", url="ws://127.0.0.1:9/x"))
        assert build_provider(cfg).__class__ is LensAgentClient

    def test_unknown_provider_rejected_at_config_time(self):
        """配错 provider 要在构造配置时就炸，而不是等到用户说第一句话。"""
        with pytest.raises(ValueError, match="provider"):
            AgentConfig(provider="gpt")

    def test_two_providers_disagree_on_who_owns_style(self):
        """AGENT-LAYER §4.1：自研 agent 自己承担小屏契约，网关不再越权注入。"""
        assert OpenClawClient(OpenClawConfig()).injects_style is True
        assert LensAgentClient(AgentConfig(provider="lens")).injects_style is False


class TestLabelFollowsProvider:
    def test_badge_and_name_switch_with_provider(self):
        """徽记以前恒读 openclaw 那一份 —— 换了 provider 屏幕上还是"工"。"""
        oc = Config(agent=AgentConfig(provider="openclaw"),
                    openclaw=OpenClawConfig(agent_label="工", agent_name="工部"))
        assert (oc.agent_label, oc.agent_name) == ("工", "工部")
        lens = Config(agent=AgentConfig(provider="lens", agent_label="答",
                                        agent_name="小龙虾"))
        assert (lens.agent_label, lens.agent_name) == ("答", "小龙虾")


class TestTrustRule:
    def test_not_connected_means_unknown_not_fake(self):
        """还没握手就往屏幕上写「?」等于喊狼来了，用户会学会忽略它。"""
        assert agent_is_trusted(_Stub(connected=False, production=False)) is True

    def test_connected_production_agent_is_trusted(self):
        assert agent_is_trusted(_Stub(connected=True, production=True)) is True

    def test_connected_fixture_is_marked(self):
        assert agent_is_trusted(_Stub(connected=True, production=False)) is False


class TestHelloParsing:
    """握手响应 → AgentInfo。替身自报 `fixture`，真网关不会发这个字段。"""

    def _client(self) -> OpenClawClient:
        return OpenClawClient(OpenClawConfig(url="ws://127.0.0.1:1/x", agent_name="工部"))

    def test_fixture_flag_at_top_level(self):
        info = self._client()._read_hello({"protocol": 3, "fixture": True})
        assert info.production is False and "替身" in info.note

    def test_fixture_flag_nested_in_server(self):
        info = self._client()._read_hello(
            {"protocol": 3, "server": {"name": "fake-openclaw", "fixture": True}})
        assert info.production is False and info.name == "fake-openclaw"

    def test_real_gateway_has_no_fixture_flag(self):
        info = self._client()._read_hello({"protocol": 3, "server": {"name": "工部网关"}})
        assert info.production is True and info.backend == "openclaw"
        assert "替身" not in info.note

    def test_endpoint_is_recorded(self):
        """溯源要能回答"连到哪儿了"，否则换个地址就无从对质。"""
        assert self._client()._read_hello({}).endpoint == "ws://127.0.0.1:1/x"

    def test_unknown_agent_placeholder_is_not_production(self):
        assert UNKNOWN_AGENT.production is False and UNKNOWN_AGENT.backend == "none"


class TestHudMarker:
    def test_badge_clean_by_default(self):
        hud = HudDevice("dev_x", make_config())
        assert hud.status_line("S4").startswith("工 ")

    def test_badge_marked_after_note_agent_false(self):
        hud = HudDevice("dev_x", make_config())
        hud.note_agent(False)
        assert hud.status_line("S4").startswith("工? ")

    def test_marker_is_ascii_so_it_always_renders(self):
        """标记本身要是画得出来的字符 —— 用个字库外的符号就等于没标。
        G2 会把字库外字符**静默丢弃**（不画豆腐块），那样标记等于不存在。"""
        from lens_gateway.formatting.metrics import missing_codepoints
        hud = HudDevice("dev_x", make_config())
        hud.note_agent(False)
        assert missing_codepoints(hud.status_line("S4")) == []


class TestMarkerReachesTheScreen:
    """S0 待机帧只画一个 `·`，**故意没有徽记** —— 8 行的屏幕上待机态不该有噪音，
    而且它也没有对 agent 身份作出任何声明。所以断言落在第一个带徽记的帧上。"""

    @staticmethod
    async def _first_badged(session, sink) -> str:
        session.hud.emit("S2", session.hud.status_line("S2"), "", urgent=True)
        await asyncio.sleep(0)   # emit 排一个发送任务，让它跑一轮
        return sink.frames[-1]["containers"]["status"].split(" ")[0]

    async def test_marked_without_waiting_for_a_dispatch(self):
        """以前只在 dispatch 时才标记 —— 那样用户问出第一句话之前，
        S2 聆听帧一直在不声不响地冒充真 agent。"""
        cfg = make_config()
        session = DeviceSession("dev_m", cfg, FakeAsr(),
                                FakeClaw(cfg.openclaw, production=False))
        sink = Sink()
        session.attach(sink)
        assert await self._first_badged(session, sink) == "工?"

    async def test_real_agent_leaves_the_screen_clean(self):
        cfg = make_config()
        session = DeviceSession("dev_m", cfg, FakeAsr(), FakeClaw(cfg.openclaw))
        sink = Sink()
        session.attach(sink)
        assert await self._first_badged(session, sink) == "工"

    async def test_idle_screen_stays_bare(self):
        """待机帧不该因为 W6 多长出一个徽记来。"""
        cfg = make_config()
        session = DeviceSession("dev_m", cfg, FakeAsr(),
                                FakeClaw(cfg.openclaw, production=False))
        resume = session.attach(Sink())
        assert resume["containers"]["status"] == session.hud.glyphs["idle"]


class TestStyleInjection:
    async def test_gateway_injects_style_for_third_party_agent(self):
        cfg = make_config()
        claw = FakeClaw(cfg.openclaw)
        session = DeviceSession("dev_i", cfg, FakeAsr(), claw)
        session.attach(Sink())
        await session.voice.dispatch("今天天气")
        assert claw.sent and "一页" in claw.sent[0][1], "第三方 agent 必须由网关注入小屏风格"

    async def test_gateway_stays_out_of_the_way_for_own_agent(self):
        """自研 agent 的 system prompt 自带契约；网关再塞一遍是越权 + 白烧 token，
        还会破坏它的缓存前缀稳定性（AGENT-LAYER §7.2）。"""
        cfg = make_config()
        claw = FakeClaw(cfg.openclaw)
        claw.injects_style = False
        session = DeviceSession("dev_i", cfg, FakeAsr(), claw)
        session.attach(Sink())
        await session.voice.dispatch("今天天气")
        assert claw.sent and claw.sent[0][1] == "今天天气"


class TestLensDeltaContract:
    """Lens Agent Protocol v1：`delta.text` 恒为**完整正文**，网关不得猜测。

    这组用例来自一个真实缺陷：网关照抄了 openclaw 那套「增量块 / 累计全文」启发式，
    而模型在发 tool_calls 之前会先流一段散文（M6 实测事实 4）、工具跑完后 agent 会把
    正文清零重新组织 —— 新文本不以旧文本开头，启发式落到 `+=` 分支，
    于是「现在几点」这种最普通的问题会在屏幕上拼出二次方级重复的乱码。
    """

    @staticmethod
    def _client():
        from lens_gateway.providers.lens import LensAgentClient, _Run
        seen: list[tuple[str, str]] = []

        async def cb(kind: str, text: str, extra: str) -> None:
            seen.append((kind, text))

        c = LensAgentClient.__new__(LensAgentClient)
        c._runs = {"r1": _Run(run_id="r1", session_key="s", callback=cb)}
        c._session_runs = {}
        return c, seen

    @staticmethod
    async def _feed(c, state: str, text: str | None = None, **kw) -> None:
        payload = {"runId": "r1", "state": state, **kw}
        if text is not None:
            payload["message"] = {"content": [{"type": "text", "text": text}]}
        await c._on_event({"event": "chat", "payload": payload})

    async def test_delta_replaces_never_appends(self):
        c, seen = self._client()
        for t in ("现", "现在", "现在是下午三点。"):
            await self._feed(c, "delta", t)
        assert [t for _, t in seen] == ["现", "现在", "现在是下午三点。"]

    async def test_text_after_a_tool_call_replaces_the_pre_tool_prose(self):
        """★ 回归：工具轮之后正文从头来，不能和工具前那段散文拼在一起。"""
        c, seen = self._client()
        for t in ("让我", "让我查", "让我查一下时间"):
            await self._feed(c, "delta", t)
        await self._feed(c, "tool", tool={"name": "now", "label": "查时间", "phase": "start"})
        for t in ("现", "现在", "现在是", "现在是下午三点。"):
            await self._feed(c, "delta", t)

        final_body = seen[-1][1]
        assert final_body == "现在是下午三点。", f"屏幕上是：{final_body!r}"
        # 旧实现会拼出「让我查一下时间现现在现在是现在是下午三点。」
        assert "让我" not in final_body, "工具前的散文不该留在屏幕上"
        assert final_body.count("现在是") == 1, "出现了重复累加"

    async def test_final_without_text_falls_back_to_what_is_on_screen(self):
        c, seen = self._client()
        await self._feed(c, "delta", "已记下。")
        await self._feed(c, "final")
        assert seen[-1] == ("final", "已记下。")


class TestBadgeIsComputedPerFrame:
    """W6 徽记的**时序**。

    这组用例来自对抗式评审确认的一条 HIGH：徽记曾经是「在 dispatch 里取样一次」，
    而 `agent_is_trusted()` 在"还没握上手"时返回 True（那时对端身份是未知，
    不该冤枉它）。两者一叠加，冷启动或断线重连后的**整整一轮**回答都不打标 ——
    而那一轮恰恰是替身在回答。演示里最该告状的那一次，屏幕反而沉默。

    修法是每帧现算。所以这里断言的不是"某一帧带不带标"，而是**同一轮里徽记会变**：
    握手前诚实地不标，握手后立刻标。
    """

    class _LateClaw(FakeClaw):
        """握手在 `chat_send` 内部才完成 —— 这就是冷启动第一句的真实时序。"""

        async def chat_send(self, key, message, on_event, timeout_ms=180_000):
            self.sent.append((key, message))
            self.connected.set()          # 直到这一刻才知道对面是替身
            await on_event("partial", "下午三点", "")
            await on_event("final", "下午三点。", "")
            return "run_late"

    @staticmethod
    async def _run(production: bool) -> list[dict]:
        cfg = make_config()
        claw = TestBadgeIsComputedPerFrame._LateClaw(
            cfg.openclaw, production=production, connected=False)
        session = DeviceSession("dev_late", cfg, FakeAsr(), claw)
        sink = Sink()
        session.attach(sink)
        await session.voice.dispatch("现在几点")
        for _ in range(4):
            await asyncio.sleep(0)
        session.hud.cancel_timer()
        return sink.frames

    @staticmethod
    def _badges(frames: list[dict], state: str) -> list[str]:
        return [f["containers"]["status"].split(" ")[0]
                for f in frames if f["state"] == state]

    async def test_answer_frames_are_marked_even_when_the_handshake_lands_late(self):
        frames = await self._run(production=False)
        badges = self._badges(frames, "S7")
        assert badges, "应当有一帧 S7 完成帧"
        assert all(b == "工?" for b in badges), \
            f"替身答的这一帧没打标：{badges}"

    async def test_the_pre_handshake_frame_stays_honest(self):
        """握手前不打标是**对的** —— 那时身份未知，而且 S4 的正文是用户自己
        刚说的话，屏幕上还没有 agent 的任何输出。喊早了的狼来了会被学会忽略。"""
        frames = await self._run(production=False)
        assert self._badges(frames, "S4") == ["工"]

    async def test_a_real_agent_never_gets_marked(self):
        frames = await self._run(production=True)
        assert all(b == "工" for b in self._badges(frames, "S7"))

    async def test_probe_failure_falls_back_instead_of_crashing_the_screen(self):
        """探针每帧都跑，它抛异常就不能把画面带崩 —— 回落到上次已知身份。"""
        hud = HudDevice("dev_p", make_config())
        hud.note_agent(False)
        hud.bind_agent_probe(lambda: (_ for _ in ()).throw(RuntimeError("探针炸了")))
        assert hud.status_line("S4").startswith("工? ")


class TestConnectionLifecycle:
    """连接的两种死法 —— 都是对抗式评审确认的 HIGH，且**两个 provider 同病**。

    ① 握手失败不收 socket：`ensure_connected()` 的短路判据是「`_ws` 非空且未关闭」，
       握手抛错时 socket 还开着，于是此后**永远不会再握一次手**。表现是 agent
       明明起来了，网关却在一条没握过手的 socket 上一直发 chat.send，进程不重启
       就好不了；而 `connected` 恒为 False ⇒ `/healthz` 撒谎、W6 徽记被永久压掉。
    ② 断线不清 run 表：`session_busy()` 永久为 True，用户之后每次按 PTT 都被挡在
       「上一条还在跑」那一帧 —— agent 早就重连好了，眼镜却锁死。

    这两条都必须用**真的 aiohttp WebSocket** 验：它们全都发生在 `_reader` 的
    finally 与 `_connect` 的异常路径上，打桩就把要测的东西打没了。
    """

    @staticmethod
    async def _serve(handler):
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        app = web.Application()
        app.router.add_get("/", handler)
        server = TestServer(app)
        await server.start_server()
        return server

    @staticmethod
    def _openclaw_cfg(tmp_path, port):
        (tmp_path / "openclaw.json").write_text(
            '{"gateway": {"auth": {"token": "t"}}}', encoding="utf-8")
        return OpenClawConfig(url=f"ws://127.0.0.1:{port}/",
                              config_path=str(tmp_path / "openclaw.json"))

    # ---------------------------------------------------------------- ①

    @staticmethod
    async def _deaf_server(connects):
        """接受连接，然后对 connect 请求装聋 —— 客户端只能超时。"""
        from aiohttp import web

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            connects.append(1)
            async for _ in ws:
                pass
            return ws
        return handler

    async def test_lens_really_reconnects_after_a_failed_handshake(self):
        connects: list[int] = []
        server = await self._serve(await self._deaf_server(connects))
        try:
            c = LensAgentClient(AgentConfig(provider="lens",
                                            url=f"ws://127.0.0.1:{server.port}/",
                                            connect_timeout=0.2))
            with pytest.raises(Exception):
                await c.ensure_connected()
            assert c._ws is None, "握手失败必须把 socket 收干净"
            assert not c.connected.is_set()
            with pytest.raises(Exception):
                await c.ensure_connected()
            assert len(connects) == 2, "第二次 ensure_connected 必须真的重连，而不是短路返回"
            await c.close()
        finally:
            await server.close()

    async def test_openclaw_really_reconnects_after_a_failed_handshake(self, tmp_path):
        connects: list[int] = []
        server = await self._serve(await self._deaf_server(connects))
        try:
            c = OpenClawClient(self._openclaw_cfg(tmp_path, server.port))
            for _ in range(2):
                with pytest.raises(Exception):
                    await asyncio.wait_for(c.ensure_connected(), 1.0)
            assert c._ws is None
            assert len(connects) == 2
            await c.close()
        finally:
            await server.close()

    # ---------------------------------------------------------------- ②

    @staticmethod
    async def _drops_after_run(hello: dict, run_key: str, method: str):
        """握手正常，收到一次问答请求后回 runId 就立刻断开 —— 模拟 agent 崩了。"""
        import json as _json

        from aiohttp import web

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                f = _json.loads(msg.data)
                if f.get("method") == "connect":
                    await ws.send_json({"type": "res", "id": f["id"], "ok": True,
                                        "payload": hello})
                elif f.get("method") == method:
                    await ws.send_json({"type": "res", "id": f["id"], "ok": True,
                                        "payload": {run_key: "r1"}})
                    break
            await ws.close()
            return ws
        return handler

    @staticmethod
    async def _drive(client, key: str) -> list[str]:
        """跑一轮问答，返回"屏幕收到的失败信号"。

        断线可能落在两个位置，两种都算合格：`chat_send` 还在 await 时断 ⇒ 抛异常
        （pipeline 兜成 S8「无法连接 agent」）；已经登记完才断 ⇒ 回调收到 error
        （pipeline 兜成 S8「与 agent 断开」）。**不合格的只有一种：什么都没发生。**
        """
        seen: list[str] = []

        async def cb(kind: str, text: str, extra: str) -> None:
            seen.append(kind)

        try:
            await client.chat_send(key, "现在几点", cb)
        except Exception:
            seen.append("error")
        for _ in range(20):
            await asyncio.sleep(0.02)
            if not client.session_busy(key):
                break
        return seen

    async def test_lens_frees_the_session_when_the_agent_dies(self):
        server = await self._serve(await self._drops_after_run(
            {"agent": "t", "production": False}, "runId", "chat.send"))
        try:
            c = LensAgentClient(AgentConfig(provider="lens",
                                            url=f"ws://127.0.0.1:{server.port}/",
                                            connect_timeout=1.0))
            seen = await self._drive(c, "lens:dev")
            assert "error" in seen, "断线要通知屏幕，不能静默"
            assert not c.session_busy("lens:dev"), \
                "断线后必须放开会话，否则用户按 PTT 永远被挡在「上一条还在跑」"
            await c.close()
        finally:
            await server.close()

    async def test_openclaw_frees_the_session_when_the_agent_dies(self, tmp_path):
        server = await self._serve(await self._drops_after_run(
            {"protocol": 3, "server": {"name": "t"}}, "runId", "chat.send"))
        try:
            c = OpenClawClient(self._openclaw_cfg(tmp_path, server.port))
            seen = await self._drive(c, "lens:dev")
            assert "error" in seen
            assert not c.session_busy("lens:dev")
            await c.close()
        finally:
            await server.close()

    # ---------------------------------------------------------------- 握手解析

    def test_openclaw_hello_survives_a_string_server_field(self, tmp_path):
        """不是假想形状 —— 本仓库自己的夹具改之前发的就是 `"server": "fake-openclaw/0.1.0"`。

        这里抛 `AttributeError` 会连锁触发上面 ① 的整条死法。
        """
        c = OpenClawClient(self._openclaw_cfg(tmp_path, 1))
        info = c._read_hello({"protocol": 3, "server": "fake-openclaw/0.1.0"})
        assert info.backend == "openclaw" and info.production is True

    def test_openclaw_hello_survives_a_non_dict_payload(self, tmp_path):
        c = OpenClawClient(self._openclaw_cfg(tmp_path, 1))
        assert c._read_hello({"server": ["x"]}).backend == "openclaw"
