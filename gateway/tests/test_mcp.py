"""MCP 表面端到端：**真 MCP 服务器 + 真控制面 + 真设备会话**，中间没有打桩。

链路是完整的：`server.call_tool(...)` → `lens_mcp.client` → HTTP → `lens_gateway.control`
→ `DeviceSession` → `HudDevice` → 帧。这正是厂商模型驱动这副眼镜时走的路。
"""
from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lens_gateway.session import DeviceSession
from lens_mcp import server as mcp_mod
from lens_mcp.client import ControlClient
from tests.test_device import Sink, make_config
from tests.test_session import FakeAsr, FakeClaw
from tests.test_telemetry import GLASSES

LONG = "".join(f"第{i}段，用来把 MCP 写上去的内容撑到多页。" for i in range(30))


def payload(result) -> dict:
    """CallToolResult → 工具实际返回的那个 dict。"""
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    from lens_gateway import server as srv

    monkeypatch.setattr(srv, "STATE_DIR", tmp_path)
    monkeypatch.setattr(srv, "jwt_secret", lambda: b"0" * 32)
    monkeypatch.setattr(srv, "control_secret", lambda: "mcp-test-secret")

    async def _no_warmup(_self) -> None:
        return None

    monkeypatch.setattr(srv.LensServer, "_warmup", _no_warmup)
    gateway = srv.LensServer(make_config())

    code = gateway.auth.new_pair_code()
    dev, _ = gateway.auth.pair(code, "测试眼镜")
    session = DeviceSession(dev.device_id, gateway.cfg, FakeAsr(), FakeClaw(gateway.cfg.openclaw))
    session.attach(Sink())
    gateway.sessions[dev.device_id] = session

    http = TestClient(TestServer(gateway.build_app()))
    await http.start_server()

    control = ControlClient(base_url=str(http.make_url("")).rstrip("/"), secret="mcp-test-secret")
    monkeypatch.setattr(mcp_mod, "_client", control)
    try:
        yield {"id": dev.device_id, "session": session, "gateway": gateway, "mcp": mcp_mod.server}
    finally:
        await control.close()
        await http.close()


class TestSurface:
    async def test_tool_inventory(self, env):
        names = {t.name for t in await env["mcp"].list_tools()}
        assert names == {"textkit_paginate", "glasses_list", "hud_show", "hud_page",
                         "hud_clear", "hud_release", "glasses_telemetry", "glasses_events"}

    async def test_every_read_tool_documents_poll_semantics(self, env):
        """MCP 2026-07-28 无状态且服务器不能主动发请求 ⇒「监控」只能是轮询，
        工具描述里必须写明，否则模型会把十分钟前的数据当成此刻的。"""
        by_name = {t.name: t for t in await env["mcp"].list_tools()}
        for name in ("glasses_list", "glasses_telemetry", "glasses_events"):
            desc = by_name[name].description or ""
            assert ("采样" in desc or "轮询" in desc), f"{name} 的描述没说清轮询语义"

    async def test_prompt_is_derived_from_the_real_layout(self, env):
        from lens_gateway.formatting import DEFAULT_LAYOUT

        got = await env["mcp"].get_prompt("small-screen-style")
        text = got.messages[0].content.text
        assert f"一页 {DEFAULT_LAYOUT.body.max_lines} 行" in text
        assert "不用 markdown" in text
        assert "什么都不显示" in text     # 字库外字符的真实行为


class TestPaginate:
    async def test_pure_tool_needs_no_device(self, env):
        out = payload(await env["mcp"].call_tool("textkit_paginate", {"text": LONG}))
        assert out["ok"] is True
        assert out["total"] > 1
        assert out["lines_per_page"] == 8 and out["line_height_px"] == 27
        assert all(len(p.split("\n")) <= 8 for p in out["pages"])

    async def test_reports_glyphs_that_would_silently_vanish(self, env):
        """真机上字库外字符**什么都不画**（不是豆腐块）。模型必须能提前知道。"""
        out = payload(await env["mcp"].call_tool(
            "textkit_paginate", {"text": "完成 ✓ 电量 ⚡ 齿轮 ⚙"}))
        assert set(out["dropped_glyphs"]) == {"✓", "⚡", "⚙"}
        assert "✓" not in out["pages"][0], "返回的页面必须是真机会显示的样子"

    async def test_markdown_is_stripped_like_the_real_pipeline(self, env):
        out = payload(await env["mcp"].call_tool(
            "textkit_paginate", {"text": "# 标题\n- 一项\n**加粗**\n```\ncode\n```"}))
        page = out["pages"][0]
        assert "#" not in page and "**" not in page and "```" not in page

    async def test_bad_container(self, env):
        out = payload(await env["mcp"].call_tool("textkit_paginate",
                                                 {"text": "x", "container": "屏幕"}))
        assert out["ok"] is False and out["error"]["code"] == "bad_container"


class TestDeviceTools:
    async def test_list(self, env):
        out = payload(await env["mcp"].call_tool("glasses_list", {}))
        assert out["ok"] is True and "as_of" in out
        assert out["devices"][0]["device_id"] == env["id"]
        assert out["devices"][0]["online"] is True

    async def test_show_page_clear_release(self, env):
        show = payload(await env["mcp"].call_tool(
            "hud_show", {"device_id": env["id"], "text": LONG, "title": "检索结果"}))
        assert show["ok"] is True and show["page"]["total"] > 1
        assert "检索结果" in show["containers"]["status"]
        lid = show["lease_id"]
        assert show["lease_expires_in_ms"] > 0

        turned = payload(await env["mcp"].call_tool(
            "hud_page", {"device_id": env["id"], "lease_id": lid, "direction": "prev"}))
        assert turned["turned"] is True
        assert turned["page"]["cur"] == show["page"]["cur"] - 1

        cleared = payload(await env["mcp"].call_tool(
            "hud_clear", {"device_id": env["id"], "lease_id": lid}))
        assert cleared["state"] == "S0"

        released = payload(await env["mcp"].call_tool(
            "hud_release", {"device_id": env["id"], "lease_id": lid}))
        assert released["released"] is True

    async def test_two_clients_conflict_instead_of_last_write_wins(self, env):
        """★ MCP 层没有 session 概念，服务器分不清谁在控制 —— 靠租约仲裁。"""
        first = payload(await env["mcp"].call_tool(
            "hud_show", {"device_id": env["id"], "text": "甲写的", "holder": "客户端甲"}))
        assert first["ok"] is True

        second = payload(await env["mcp"].call_tool(
            "hud_show", {"device_id": env["id"], "text": "乙写的", "holder": "客户端乙"}))
        assert second["ok"] is False
        assert second["error"]["code"] == "LEASE_HELD"
        assert second["error"]["holder"] == "客户端甲"
        assert second["error"]["expires_in_ms"] > 0

        state = payload(await env["mcp"].call_tool("glasses_list", {}))
        assert state["devices"][0]["lease"]["holder"] == "客户端甲"
        assert env["session"].current_frame["containers"]["body"].startswith("甲写的")

    async def test_user_speaking_preempts_and_events_reveal_it(self, env):
        """★ 用户开口 ⇒ 屏幕无条件归语音链路。MCP 收不到推送，只能靠轮询发现。"""
        show = payload(await env["mcp"].call_tool(
            "hud_show", {"device_id": env["id"], "text": "MCP 写的", "holder": "客户端甲"}))
        lid = show["lease_id"]

        env["session"].hud.emit("S2", "聆听", "", urgent=True)      # 用户按下 PTT

        again = payload(await env["mcp"].call_tool(
            "hud_page", {"device_id": env["id"], "lease_id": lid, "direction": "next"}))
        assert again["ok"] is False and again["error"]["code"] == "LEASE_INVALID"

        ev = payload(await env["mcp"].call_tool("glasses_events", {"device_id": env["id"]}))
        kinds = [e["kind"] for e in ev["events"]]
        assert "lease_preempted" in kinds
        assert ev["events"][-1]["reason"] == "local_render"
        assert ev["next"] > 0

        # 轮询语义：拿 next 当游标，第二次应当为空
        again2 = payload(await env["mcp"].call_tool(
            "glasses_events", {"device_id": env["id"], "after": ev["next"]}))
        assert again2["events"] == []

    async def test_telemetry_says_unavailable_before_any_report(self, env):
        out = payload(await env["mcp"].call_tool("glasses_telemetry", {"device_id": env["id"]}))
        assert out["available"] is False and out["telemetry"] is None
        assert "尚未上报" in out["note"], "必须明说没有数据，不能让模型臆造一个电量"

    async def test_telemetry_after_report_carries_provenance(self, env):
        await env["session"].handle_text({"type": "telemetry", "data": GLASSES})
        out = payload(await env["mcp"].call_tool("glasses_telemetry", {"device_id": env["id"]}))
        tel = out["telemetry"]
        assert tel["batteryLevel"] == 86
        assert tel["source"] == "push" and tel["stale"] is False
        assert tel["sn"] == "…1234", "SN 出网关只留后 4 位"
        assert "age_ms" in tel and "as_of" in out

    async def test_unknown_device_is_a_structured_result_not_a_crash(self, env):
        out = payload(await env["mcp"].call_tool("glasses_telemetry", {"device_id": "dev_nope"}))
        assert out["ok"] is False
        assert out["error"]["code"] == "device_unknown"
        assert out["error"]["http_status"] == 404

    async def test_offline_write_is_allowed_but_labelled(self, env):
        env["session"].detach()
        out = payload(await env["mcp"].call_tool(
            "hud_show", {"device_id": env["id"], "text": "离线时写的"}))
        assert out["ok"] is True and out["online"] is False
        assert "离线" in out["note"]


class TestResources:
    async def test_devices_resource(self, env):
        contents = await env["mcp"].read_resource("glasses://devices")
        data = json.loads(list(contents)[0].content)
        assert data["devices"][0]["device_id"] == env["id"]

    async def test_frame_and_telemetry_resources(self, env):
        did = env["id"]
        await env["mcp"].call_tool("hud_show", {"device_id": did, "text": "资源测试"})
        frame = json.loads(list(await env["mcp"].read_resource(f"glasses://{did}/frame"))[0].content)
        assert frame["state"] == "S9" and "资源测试" in frame["containers"]["body"]

        tel = json.loads(list(await env["mcp"].read_resource(f"glasses://{did}/telemetry"))[0].content)
        assert tel["available"] is False


class TestControlPlaneDown:
    async def test_unreachable_gateway_is_reported_not_swallowed(self, env, monkeypatch):
        broken = ControlClient(base_url="http://127.0.0.1:1", secret="x")
        monkeypatch.setattr(mcp_mod, "_client", broken)
        try:
            out = payload(await env["mcp"].call_tool("glasses_list", {}))
            assert out["ok"] is False
            assert out["error"]["code"] == "control_plane_unreachable"
        finally:
            await broken.close()
