"""控制面 HTTP 接口：MCP 服务器（独立进程）操作设备的唯一通道。

两条贯穿全文的规矩：
1. **所有写屏都要租约**——屏幕只有一块，写者有三方（语音 / 本地状态机 / MCP）。
2. **所有读接口都带 `as_of`**——MCP 无状态、服务器不能主动推送，"监控"只能是轮询，
   调用方必须知道手里的数据有多旧。
"""
from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lens_gateway.session import DeviceSession
from tests.test_device import Sink, make_config
from tests.test_session import FakeAsr, FakeClaw
from tests.test_telemetry import GLASSES

AUTH = {"Authorization": "Bearer test-control-secret"}
LONG = "".join(f"第{i}段，用来把外部渲染撑到多页，好让翻页有东西可翻。" for i in range(30))


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    from lens_gateway import server as srv

    monkeypatch.setattr(srv, "STATE_DIR", tmp_path)
    monkeypatch.setattr(srv, "jwt_secret", lambda: b"0" * 32)
    monkeypatch.setattr(srv, "control_secret", lambda: "test-control-secret")
    # 单测不真的加载 whisper 模型、也不拨 OpenClaw：TestServer 会真跑 on_startup，
    # 而 _warmup() 里是 WhisperModel(...) + claw.ensure_connected()，
    # 与被测行为无关，只会让测试变慢变脆并留下没关的 aiohttp session。
    async def _no_warmup(_self) -> None:
        return None

    monkeypatch.setattr(srv.LensServer, "_warmup", _no_warmup)
    server = srv.LensServer(make_config())

    # 造一台"已配对且连接过"的设备
    code = server.auth.new_pair_code()
    dev, _ = server.auth.pair(code, "测试眼镜")
    session = DeviceSession(dev.device_id, server.cfg, FakeAsr(), FakeClaw(server.cfg.openclaw))
    sink = Sink()
    session.attach(sink)
    server.sessions[dev.device_id] = session

    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        yield {"client": client, "server": server, "id": dev.device_id,
               "session": session, "sink": sink}
    finally:
        await client.close()


async def lease(env, holder="mcp-a", ttl_ms=60_000) -> str:
    r = await env["client"].post(f'/control/{env["id"]}/lease',
                                 json={"holder": holder, "ttl_ms": ttl_ms}, headers=AUTH)
    assert r.status == 200, await r.text()
    return (await r.json())["lease_id"]


class TestAuth:
    async def test_every_route_requires_the_bearer(self, env):
        did = env["id"]
        routes = [("get", "/control/devices"), ("get", f"/control/{did}/state"),
                  ("get", f"/control/{did}/telemetry"), ("get", f"/control/{did}/events"),
                  ("post", f"/control/{did}/lease"), ("post", f"/control/{did}/render"),
                  ("post", f"/control/{did}/page"), ("post", f"/control/{did}/clear"),
                  ("delete", f"/control/{did}/lease/x")]
        for method, path in routes:
            r = await getattr(env["client"], method)(path, json={})
            assert r.status == 401, f"{method.upper()} {path} 没要令牌"


class TestReads:
    async def test_list_devices(self, env):
        r = await env["client"].get("/control/devices", headers=AUTH)
        body = await r.json()
        assert body["devices"][0]["device_id"] == env["id"]
        assert body["devices"][0]["online"] is True
        assert "as_of" in body

    async def test_revoked_devices_are_hidden(self, env):
        env["server"].auth.revoke(env["id"])
        body = await (await env["client"].get("/control/devices", headers=AUTH)).json()
        assert body["devices"] == []

    async def test_state_carries_as_of(self, env):
        body = await (await env["client"].get(f'/control/{env["id"]}/state', headers=AUTH)).json()
        assert body["device_id"] == env["id"]
        assert body["state"] == "S0"
        assert "as_of" in body and "containers" in body

    async def test_telemetry_says_unavailable_instead_of_faking_zeros(self, env):
        body = await (await env["client"].get(f'/control/{env["id"]}/telemetry', headers=AUTH)).json()
        assert body["available"] is False
        assert body["telemetry"] is None
        assert "尚未上报" in body["note"]

    async def test_telemetry_after_report(self, env):
        await env["session"].handle_text({"type": "telemetry", "data": GLASSES})
        body = await (await env["client"].get(f'/control/{env["id"]}/telemetry', headers=AUTH)).json()
        assert body["available"] is True
        assert body["telemetry"]["batteryLevel"] == 86
        assert body["telemetry"]["stale"] is False

    async def test_events_are_incremental(self, env):
        lid = await lease(env)
        body = await (await env["client"].get(f'/control/{env["id"]}/events', headers=AUTH)).json()
        assert [e["kind"] for e in body["events"]] == ["lease_acquired"]
        nxt = body["next"]
        again = await (await env["client"].get(
            f'/control/{env["id"]}/events?after={nxt}', headers=AUTH)).json()
        assert again["events"] == [] and again["next"] == nxt
        assert lid

    async def test_events_reject_bad_cursor(self, env):
        r = await env["client"].get(f'/control/{env["id"]}/events?after=abc', headers=AUTH)
        assert r.status == 400

    async def test_unknown_device_404(self, env):
        r = await env["client"].get("/control/dev_nope/state", headers=AUTH)
        assert r.status == 404
        assert (await r.json())["code"] == "device_unknown"

    async def test_paired_but_never_connected_is_distinguishable(self, env):
        """"没这台设备"和"这台设备本次进程内没连过"对调用方是两回事。"""
        code = env["server"].auth.new_pair_code()
        dev, _ = env["server"].auth.pair(code, "另一台")
        r = await env["client"].get(f"/control/{dev.device_id}/state", headers=AUTH)
        assert r.status == 404
        assert (await r.json())["code"] == "device_never_connected"


class TestLease:
    async def test_acquire_and_conflict(self, env):
        await lease(env, "mcp-a")
        r = await env["client"].post(f'/control/{env["id"]}/lease',
                                     json={"holder": "mcp-b"}, headers=AUTH)
        assert r.status == 409
        body = await r.json()
        assert body["code"] == "LEASE_HELD" and body["holder"] == "mcp-a"
        assert body["expires_in_ms"] > 0

    async def test_holder_is_required(self, env):
        r = await env["client"].post(f'/control/{env["id"]}/lease', json={}, headers=AUTH)
        assert r.status == 400

    async def test_release(self, env):
        lid = await lease(env)
        r = await env["client"].delete(f'/control/{env["id"]}/lease/{lid}', headers=AUTH)
        assert (await r.json())["released"] is True


class TestWrites:
    async def test_render_needs_a_lease(self, env):
        r = await env["client"].post(f'/control/{env["id"]}/render',
                                     json={"text": "没有租约", "lease_id": "假的"}, headers=AUTH)
        assert r.status == 409
        assert (await r.json())["code"] == "LEASE_INVALID"

    async def test_render_goes_through_the_formatting_engine(self, env):
        lid = await lease(env)
        r = await env["client"].post(f'/control/{env["id"]}/render',
                                     json={"lease_id": lid, "text": LONG, "title": "检索结果"},
                                     headers=AUTH)
        body = await r.json()
        assert body["ok"] is True and body["state"] == "S9"
        assert body["page"]["total"] > 1, "长文必须走同一个分页器"
        assert "检索结果" in body["containers"]["status"]
        assert body["online"] is True and body["note"] is None
        assert env["sink"].frames[-1]["seq"] == body["seq"]

    async def test_render_rejects_non_string_and_oversized_text(self, env):
        lid = await lease(env)
        bad = await env["client"].post(f'/control/{env["id"]}/render',
                                       json={"lease_id": lid, "text": 42}, headers=AUTH)
        assert bad.status == 400
        big = await env["client"].post(f'/control/{env["id"]}/render',
                                       json={"lease_id": lid, "text": "字" * 20_001}, headers=AUTH)
        assert big.status == 413

    async def test_page_needs_the_lease_too(self, env):
        """否则第二个 MCP 客户端可以翻走持有者正在展示的内容。"""
        lid = await lease(env)
        await env["client"].post(f'/control/{env["id"]}/render',
                                 json={"lease_id": lid, "text": LONG}, headers=AUTH)
        r = await env["client"].post(f'/control/{env["id"]}/page',
                                     json={"dir": "prev", "lease_id": "别人的"}, headers=AUTH)
        assert r.status == 409

    async def test_page_and_boundary(self, env):
        lid = await lease(env)
        await env["client"].post(f'/control/{env["id"]}/render',
                                 json={"lease_id": lid, "text": LONG}, headers=AUTH)
        back = await (await env["client"].post(f'/control/{env["id"]}/page',
                                               json={"dir": "prev", "lease_id": lid},
                                               headers=AUTH)).json()
        assert back["turned"] is True
        fwd = await (await env["client"].post(f'/control/{env["id"]}/page',
                                              json={"dir": "next", "lease_id": lid},
                                              headers=AUTH)).json()
        assert fwd["turned"] is True
        edge = await (await env["client"].post(f'/control/{env["id"]}/page',
                                               json={"dir": "next", "lease_id": lid},
                                               headers=AUTH)).json()
        assert edge["turned"] is False and "边界" in edge["note"]

    async def test_page_rejects_bad_direction(self, env):
        lid = await lease(env)
        r = await env["client"].post(f'/control/{env["id"]}/page',
                                     json={"dir": "上", "lease_id": lid}, headers=AUTH)
        assert r.status == 400

    async def test_clear(self, env):
        lid = await lease(env)
        await env["client"].post(f'/control/{env["id"]}/render',
                                 json={"lease_id": lid, "text": LONG}, headers=AUTH)
        body = await (await env["client"].post(f'/control/{env["id"]}/clear',
                                               json={"lease_id": lid}, headers=AUTH)).json()
        assert body["state"] == "S0" and body["page"] == {"cur": 1, "total": 1}

    async def test_user_speaking_preempts_and_the_client_can_see_it(self, env):
        """★ 用户开口 ⇒ 屏幕无条件归语音链路。MCP 只能轮询，所以抢占必须进事件缓冲区。"""
        lid = await lease(env, "mcp-a")
        await env["client"].post(f'/control/{env["id"]}/render',
                                 json={"lease_id": lid, "text": "MCP 写的"}, headers=AUTH)

        env["session"].hud.emit("S2", "聆听", "", urgent=True)     # 本地写屏 = 抢占

        r = await env["client"].post(f'/control/{env["id"]}/render',
                                     json={"lease_id": lid, "text": "还想写"}, headers=AUTH)
        assert r.status == 409 and (await r.json())["code"] == "LEASE_INVALID"

        ev = await (await env["client"].get(f'/control/{env["id"]}/events', headers=AUTH)).json()
        kinds = [e["kind"] for e in ev["events"]]
        assert "lease_preempted" in kinds
        assert ev["events"][-1]["reason"] == "local_render"

    async def test_offline_write_is_allowed_but_labelled(self, env):
        """离线不是错误：帧存在服务器上，重连即恢复。但调用方必须知道。"""
        lid = await lease(env)
        env["session"].detach()
        body = await (await env["client"].post(f'/control/{env["id"]}/render',
                                               json={"lease_id": lid, "text": "离线时写的"},
                                               headers=AUTH)).json()
        assert body["ok"] is True and body["online"] is False
        assert "离线" in body["note"]
