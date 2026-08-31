"""控制面 HTTP 客户端。MCP 进程通过它操作设备，不直接碰网关内部状态。

鉴权用状态目录里的共享密钥（`~/.lens-gateway/control.secret`，0600）。
**边界就是文件权限**：能读那个文件的进程 = 能管这台网关的进程。
"""
from __future__ import annotations

import os
from pathlib import Path

import aiohttp

DEFAULT_URL = "http://127.0.0.1:8443"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


class ControlError(RuntimeError):
    """控制面返回了结构化错误（租约冲突、设备未知、参数不合法……）。"""

    def __init__(self, status: int, payload: dict):
        super().__init__(payload.get("message") or f"控制面返回 {status}")
        self.status = status
        self.payload = payload

    def as_result(self) -> dict:
        """给 MCP 工具用的返回体。

        故意**不抛给协议层**：租约冲突、设备离线这些是"结果"而不是"协议错误" ——
        模型需要看到 holder 是谁、还剩多少毫秒，才能决定是等还是放弃。
        直接抛异常只会得到一句干巴巴的失败。
        """
        return {"ok": False, "error": {**self.payload, "http_status": self.status}}


def read_secret() -> str:
    """按优先级取控制面密钥：环境变量 > 状态目录文件。"""
    env = os.environ.get("LENS_CONTROL_SECRET")
    if env:
        return env.strip()
    state = Path(os.environ.get("LENS_STATE_DIR", "~/.lens-gateway")).expanduser()
    f = state / "control.secret"
    if not f.exists():
        raise RuntimeError(
            f"找不到控制面密钥 {f}。先启动一次网关（它会生成），"
            "或用 LENS_CONTROL_SECRET 环境变量提供。")
    return f.read_text().strip()


class ControlClient:
    def __init__(self, base_url: str | None = None, secret: str | None = None):
        self.base = (base_url or os.environ.get("LENS_CONTROL_URL") or DEFAULT_URL).rstrip("/")
        self.secret = secret if secret is not None else read_secret()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=_TIMEOUT, headers={"Authorization": f"Bearer {self.secret}"})
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, json: dict | None = None) -> dict:
        session = await self._ensure()
        async with session.request(method, f"{self.base}{path}", json=json) as resp:
            try:
                payload = await resp.json()
            except Exception:
                payload = {"code": "bad_response", "message": (await resp.text())[:200]}
            if resp.status >= 400:
                raise ControlError(resp.status, payload if isinstance(payload, dict) else {})
            return payload

    # ---- 便捷方法 ----

    async def devices(self) -> dict:
        return await self.request("GET", "/control/devices")

    async def state(self, device_id: str) -> dict:
        return await self.request("GET", f"/control/{device_id}/state")

    async def telemetry(self, device_id: str) -> dict:
        return await self.request("GET", f"/control/{device_id}/telemetry")

    async def events(self, device_id: str, after: int = 0) -> dict:
        return await self.request("GET", f"/control/{device_id}/events?after={after}")

    async def acquire(self, device_id: str, holder: str, ttl_ms: int) -> dict:
        return await self.request("POST", f"/control/{device_id}/lease",
                                  {"holder": holder, "ttl_ms": ttl_ms})

    async def release(self, device_id: str, lease_id: str) -> dict:
        return await self.request("DELETE", f"/control/{device_id}/lease/{lease_id}")

    async def render(self, device_id: str, lease_id: str, text: str,
                     title: str | None = None, hold_ms: int | None = None) -> dict:
        body = {"lease_id": lease_id, "text": text}
        if title:
            body["title"] = title
        if hold_ms is not None:
            body["hold_ms"] = hold_ms
        return await self.request("POST", f"/control/{device_id}/render", body)

    async def page(self, device_id: str, lease_id: str, direction: str) -> dict:
        return await self.request("POST", f"/control/{device_id}/page",
                                  {"lease_id": lease_id, "dir": direction})

    async def clear(self, device_id: str, lease_id: str) -> dict:
        return await self.request("POST", f"/control/{device_id}/clear", {"lease_id": lease_id})
