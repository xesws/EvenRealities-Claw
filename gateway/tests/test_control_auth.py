"""管理面 / 控制面鉴权与配对暴力破解防线（W4）。

这两条都不是 MCP 才引入的新问题，而是**今天就存在的活隐患**：

- `_require_loopback` 按 peername 判断，而本项目推荐的 TLS 方案是
  caddy `reverse_proxy 127.0.0.1:8443` —— 反代之后所有请求的 peername 都是
  127.0.0.1，判据整体失效；`host` 又默认 `0.0.0.0`，等于把 `/admin/*` 敞开。
- 配对码只有 6 位数字、10 分钟有效、**无限重试**。
"""
from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lens_gateway.auth import (
    PAIR_GLOBAL_MAX,
    PAIR_LOCKOUT,
    PAIR_MAX_FAILURES,
    AuthStore,
)
from tests.test_device import make_config


@pytest.fixture()
async def client(tmp_path, monkeypatch):
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
    c = TestClient(TestServer(server.build_app()))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


ADMIN = ("/admin/pair-code", "/admin/devices", "/admin/revoke")


class TestControlAuth:
    @pytest.mark.parametrize("path", ADMIN)
    async def test_rejected_without_token(self, client, path):
        """★ 关键回归：请求来自 loopback（TestClient 就是），但**没有令牌照样拒绝**。

        以前这一条会通过 —— 而反代之后所有人的请求都长这样。
        """
        method = client.post if path != "/admin/devices" else client.get
        resp = await method(path, json={})
        assert resp.status == 401
        assert "Bearer" in resp.headers.get("WWW-Authenticate", "")

    @pytest.mark.parametrize("path", ADMIN)
    async def test_rejected_with_wrong_token(self, client, path):
        method = client.post if path != "/admin/devices" else client.get
        resp = await method(path, json={}, headers={"Authorization": "Bearer 猜的"})
        assert resp.status == 401

    async def test_accepted_with_the_shared_secret(self, client):
        hdr = {"Authorization": "Bearer test-control-secret"}
        resp = await client.post("/admin/pair-code", headers=hdr)
        assert resp.status == 200
        body = await resp.json()
        assert len(body["code"]) == 6 and body["code"].isdigit()

    async def test_malformed_authorization_headers(self, client):
        for bad in ("test-control-secret", "Basic test-control-secret",
                    "Bearer", "Bearer ", "Bearer test-control-secre",
                    "Bearer test-control-secret2"):
            resp = await client.post("/admin/pair-code", headers={"Authorization": bad})
            assert resp.status == 401, f"{bad!r} 不该被接受"

    async def test_wellformed_variants_accepted(self, client):
        """RFC 7235：scheme 大小写不敏感，与凭证之间允许 1 个以上空格。"""
        for ok in ("bearer test-control-secret", "BEARER test-control-secret",
                   "Bearer  test-control-secret"):
            resp = await client.post("/admin/pair-code", headers={"Authorization": ok})
            assert resp.status == 200, f"{ok!r} 是合法的，不该被拒"

    async def test_non_ascii_token_is_401_not_500(self, client):
        """★ 回归：`secrets.compare_digest(str, str)` 遇到非 ASCII 会抛 TypeError，
        handler 里未捕获就变成 500 —— 这本身就是个可供攻击者区分的信号。"""
        resp = await client.post("/admin/pair-code",
                                 headers={"Authorization": "Bearer 猜的令牌"})
        assert resp.status == 401

    async def test_healthz_stays_public(self, client):
        """健康探针是给监控用的，不该要令牌；但也不该泄漏任何设备内容。"""
        resp = await client.get("/healthz")
        assert resp.status == 200
        body = await resp.json()
        assert set(body) == {"ok", "asr_ready", "openclaw", "devices", "sessions"}


class TestClientKey:
    """`X-Forwarded-For` 只有显式打开才采信 —— 直连时它是可伪造的。"""

    def _server(self, tmp_path, monkeypatch, **over):
        from lens_gateway import server as srv

        monkeypatch.setattr(srv, "STATE_DIR", tmp_path)
        monkeypatch.setattr(srv, "jwt_secret", lambda: b"0" * 32)
        monkeypatch.setattr(srv, "control_secret", lambda: "s")
        cfg = make_config()
        for k, v in over.items():
            setattr(cfg, k, v)
        return srv.LensServer(cfg)

    class _Req:
        def __init__(self, headers, peer):
            self.headers = headers
            self.transport = type("T", (), {"get_extra_info": lambda _s, _k: peer})()

    def test_forwarded_for_ignored_by_default(self, tmp_path, monkeypatch):
        s = self._server(tmp_path, monkeypatch)
        req = self._Req({"X-Forwarded-For": "1.2.3.4"}, ("127.0.0.1", 5))
        assert s._client_key(req) == "127.0.0.1", "默认不信任 XFF，否则节流可被随手绕过"

    def test_forwarded_for_used_when_trusted(self, tmp_path, monkeypatch):
        s = self._server(tmp_path, monkeypatch, trust_forwarded_for=True)
        req = self._Req({"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}, ("127.0.0.1", 5))
        assert s._client_key(req) == "1.2.3.4"

    def test_falls_back_when_header_absent(self, tmp_path, monkeypatch):
        s = self._server(tmp_path, monkeypatch, trust_forwarded_for=True)
        assert s._client_key(self._Req({}, ("9.9.9.9", 5))) == "9.9.9.9"


class TestPairThrottle:
    @pytest.fixture()
    def store(self, tmp_path):
        return AuthStore(tmp_path, b"test-secret-32bytes-test-secret!")

    def test_locks_after_repeated_failures(self, store):
        for _ in range(PAIR_MAX_FAILURES):
            assert store.pair("000000", "攻击者", client="1.2.3.4") is None
        retry = store.pair_locked("1.2.3.4")
        assert 0 < retry <= PAIR_LOCKOUT
        # 注意：拦截发生在**服务器层**（先查 pair_locked 再决定要不要调 pair），
        # store.pair 本身不自锁。"猜对了也不放行"由下面的 WS 级用例保证。

    def test_lock_is_per_source(self, store):
        for _ in range(PAIR_MAX_FAILURES):
            store.pair("000000", "攻击者", client="1.2.3.4")
        assert store.pair_locked("1.2.3.4") > 0
        assert store.pair_locked("5.6.7.8") == 0, "别人的手机不该被连坐"

    def test_success_clears_the_count(self, store):
        for _ in range(PAIR_MAX_FAILURES - 1):
            store.pair("000000", "手滑", client="1.2.3.4")
        assert store.pair_locked("1.2.3.4") == 0
        code = store.new_pair_code()
        assert store.pair(code, "我的手机", client="1.2.3.4") is not None
        for _ in range(PAIR_MAX_FAILURES - 1):
            store.pair("000000", "手滑", client="1.2.3.4")
        assert store.pair_locked("1.2.3.4") == 0, "成功一次应当把失败账清零"

    def test_global_cap_survives_source_rotation(self, store):
        """按来源那一层可以用大量 IP 摊薄，全局闸是第二道防线。"""
        for i in range(PAIR_GLOBAL_MAX):
            store.pair("000000", "僵尸网络", client=f"10.0.0.{i}")
        assert store.pair_locked("10.0.1.99") > 0, "换个新 IP 也该被全局闸挡住"

    def test_global_cap_is_shorter_than_source_lockout(self, store):
        """全局闸只撑住窗口，不能变成"一个攻击者锁死所有人"的拒绝服务。"""
        for i in range(PAIR_GLOBAL_MAX):
            store.pair("000000", "僵尸网络", client=f"10.0.0.{i}")
        assert store.pair_locked("10.0.1.99") <= PAIR_LOCKOUT

    def test_window_expires(self, store):
        for _ in range(PAIR_MAX_FAILURES):
            store.pair("000000", "攻击者", client="1.2.3.4")
        assert store.pair_locked("1.2.3.4") > 0
        # 把失败时刻推到窗口之外
        store._pair_failures["1.2.3.4"] = [time.time() - 10_000] * PAIR_MAX_FAILURES
        store._pair_failures["*"] = [time.time() - 10_000] * PAIR_GLOBAL_MAX
        assert store.pair_locked("1.2.3.4") == 0

    def test_expired_code_counts_as_a_failure(self, store):
        code = store.new_pair_code()
        store._pair_codes[code] = time.time() - 1     # 已过期
        assert store.pair(code, "迟到的手机", client="1.2.3.4") is None
        assert len(store._pair_failures["1.2.3.4"]) == 1


class TestPairThrottleOverWs:
    """锁定必须在**真实的 WS 配对路径**上生效——猜对了也不放行。"""

    @pytest.fixture()
    async def client(self, tmp_path, monkeypatch):
        from lens_gateway import server as srv

        monkeypatch.setattr(srv, "STATE_DIR", tmp_path)
        monkeypatch.setattr(srv, "jwt_secret", lambda: b"0" * 32)
        monkeypatch.setattr(srv, "control_secret", lambda: "s")
        self.server = srv.LensServer(make_config())
        c = TestClient(TestServer(self.server.build_app()))
        await c.start_server()
        try:
            yield c
        finally:
            await c.close()

    async def _try_pair(self, client, code: str) -> dict:
        ws = await client.ws_connect("/ws")
        try:
            await ws.send_json({"type": "pair", "code": code, "deviceName": "测试"})
            return await ws.receive_json(timeout=5)
        finally:
            await ws.close()

    async def test_throttled_client_is_refused_even_with_a_valid_code(self, client):
        for _ in range(PAIR_MAX_FAILURES):
            msg = await self._try_pair(client, "000000")
            assert msg["code"] == "pair_failed"

        good = self.server.auth.new_pair_code()
        msg = await self._try_pair(client, good)
        assert msg["type"] == "error"
        assert msg["code"] == "pair_throttled", "锁定期间猜对了也必须拒绝"
        assert "后再试" in msg["message"]
        # 码没被消耗：解锁后仍然可用
        self.server.auth._pair_failures.clear()
        ok = await self._try_pair(client, good)
        assert ok["type"] == "pair_ok"
