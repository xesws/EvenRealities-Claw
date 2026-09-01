"""控制面：给 MCP 服务器（独立进程）操作设备的 HTTP 接口。

## 为什么是独立进程 + HTTP，而不是把 MCP 塞进网关

网关是 aiohttp；官方 `mcp` SDK 的 Streamable HTTP 传输是 ASGI/Starlette。
两个 web 框架塞进一个进程只会互相别扭，而且 MCP 表面是**面向外部厂商**的攻击面，
让它跟持有麦克风、ASR、设备凭证的网关同进程并不划算。所以 MCP 服务器是独立进程，
经这里的控制面操作设备 —— 边界清晰，也方便单独限流或下线。

## 所有写屏都要租约

屏幕只有一块，而写者有三方：语音链路、本地状态机、MCP。租约（`device/hud.py`）是
唯一的仲裁者。控制面**不绕过它** —— `render` / `page` / `clear` 都必须带 `lease_id`，
被抢占（用户开口了）就返回结构化错误，让 MCP 客户端知道自己已经不在控制中。

## 读接口一律带 `as_of`

MCP 2026-07-28 是无状态的，**服务器不能主动发起请求** —— 做不到推送。
所以"监控"只能是轮询语义，每个读接口都必须把"这是什么时候的数据"一起交出去，
否则模型会把十分钟前的电量当成此刻的。
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

from .device import LeaseError

log = logging.getLogger(__name__)

#: 一次 render 的正文上限。真机单容器上限是 UTF-8 999 字节（docs/HARDWARE-SPEC.md §2.1），
#: 但服务器会分页，所以这里限制的是**整篇**长度 —— 纯粹为了防止一条工具调用打爆内存。
MAX_RENDER_CHARS = 20_000
MAX_TITLE_CHARS = 24
MAX_HOLDER_CHARS = 64


def _err(status: int, code: str, message: str, **extra) -> web.Response:
    return web.json_response({"code": code, "message": message, **extra}, status=status)


async def _body(req: web.Request) -> dict:
    try:
        data = await req.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class ControlPlane:
    """挂在 LensServer 上的一组路由。构造时只拿 server 的引用，不复制状态。"""

    def __init__(self, server) -> None:
        self.server = server

    # ------------------------------------------------------------------ 装配

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/control/devices", self.list_devices)
        app.router.add_post("/control/{device_id}/lease", self.acquire_lease)
        app.router.add_delete("/control/{device_id}/lease/{lease_id}", self.release_lease)
        app.router.add_post("/control/{device_id}/render", self.render)
        app.router.add_post("/control/{device_id}/page", self.page)
        app.router.add_post("/control/{device_id}/clear", self.clear)
        app.router.add_get("/control/{device_id}/state", self.state)
        app.router.add_get("/control/{device_id}/telemetry", self.telemetry)
        app.router.add_get("/control/{device_id}/events", self.events)

    def _session(self, req: web.Request):
        self.server._require_control_auth(req)
        device_id = req.match_info["device_id"]
        session = self.server.sessions.get(device_id)
        if session is None:
            # 区分"没这台设备"与"这台设备本次进程内没连过"——对调用方是两回事
            known = any(d.device_id == device_id for d in self.server.auth.list_devices())
            raise web.HTTPNotFound(
                text=f'{{"code":"{"device_never_connected" if known else "device_unknown"}",'
                     f'"message":"{"设备已配对但本次进程内没有连接过" if known else "未知设备"}"}}',
                content_type="application/json")
        return session

    @staticmethod
    def _as_of() -> dict:
        """每个响应都带的时间戳。MCP 只能轮询，调用方必须知道数据有多旧。"""
        return {"as_of": time.time()}

    # ------------------------------------------------------------------ 只读

    async def list_devices(self, req: web.Request) -> web.Response:
        self.server._require_control_auth(req)
        rows = []
        for d in self.server.auth.list_devices():
            if d.revoked:
                continue
            session = self.server.sessions.get(d.device_id)
            rows.append({
                "device_id": d.device_id,
                "name": d.name,
                "online": bool(session and session.hud.online),
                "state": session.state if session else None,
                "lease": session.hud.lease_info() if session else None,
                "has_telemetry": bool(session and session.telemetry.snapshot()),
            })
        return web.json_response({"devices": rows, **self._as_of()})

    async def state(self, req: web.Request) -> web.Response:
        # W6：屏幕上这段文字是谁生成的，跟着画面一起返回 —— MCP 客户端读画面时
        # 就能知道后面挂的是真 agent 还是替身，不用再去问 /healthz。
        return web.json_response({
            **self._session(req).snapshot(),
            "agent": {"connected": self.server.agent.connected.is_set(),
                      **self.server.agent.info().as_dict()},
            **self._as_of(),
        })

    async def telemetry(self, req: web.Request) -> web.Response:
        session = self._session(req)
        snap = session.telemetry.snapshot()
        # 没有就是没有。返回一个 battery=0 的默认结构就是在编数据。
        return web.json_response({
            "telemetry": snap,
            "available": snap is not None,
            "note": None if snap else "该设备尚未上报过遥测（插件未连接，或宿主不是 Even App）",
            "diagnostics": session.telemetry.diagnostics(),
            **self._as_of(),
        })

    async def events(self, req: web.Request) -> web.Response:
        session = self._session(req)
        try:
            after = int(req.query.get("after", "0"))
        except ValueError:
            return _err(400, "bad_request", "after 必须是整数")
        events = session.hud.drain_events(after)
        return web.json_response({
            "events": events,
            "next": events[-1]["id"] if events else after,
            **self._as_of(),
        })

    # ------------------------------------------------------------------ 租约

    async def acquire_lease(self, req: web.Request) -> web.Response:
        session = self._session(req)
        body = await _body(req)
        holder = str(body.get("holder") or "").strip()[:MAX_HOLDER_CHARS]
        if not holder:
            return _err(400, "bad_request", "必须给 holder（标识是谁在控制这块屏）")
        try:
            ttl_ms = int(body.get("ttl_ms", 30_000))
        except (TypeError, ValueError):
            return _err(400, "bad_request", "ttl_ms 必须是整数毫秒")
        try:
            lease = session.hud.acquire_lease(holder, ttl_ms)
        except LeaseError as exc:
            return web.json_response({**exc.as_dict(), **self._as_of()}, status=409)
        return web.json_response({**lease.as_dict(), "online": session.hud.online, **self._as_of()})

    async def release_lease(self, req: web.Request) -> web.Response:
        session = self._session(req)
        released = session.hud.release_lease(req.match_info["lease_id"])
        return web.json_response({"released": released, **self._as_of()})

    # ------------------------------------------------------------------ 写屏

    async def render(self, req: web.Request) -> web.Response:
        session = self._session(req)
        body = await _body(req)
        lease_id = str(body.get("lease_id") or "")
        text = body.get("text")
        if not isinstance(text, str):
            return _err(400, "bad_request", "text 必须是字符串")
        if len(text) > MAX_RENDER_CHARS:
            return _err(413, "text_too_long",
                        f"正文超过 {MAX_RENDER_CHARS} 字（服务器会分页，但不接受无上限的输入）")
        title = body.get("title")
        title = str(title)[:MAX_TITLE_CHARS] if title else None
        hold_ms = body.get("hold_ms")
        try:
            hold_ms = int(hold_ms) if hold_ms is not None else None
        except (TypeError, ValueError):
            return _err(400, "bad_request", "hold_ms 必须是整数毫秒")
        try:
            frame = session.hud.render_external(lease_id, text, title=title, hold_ms=hold_ms)
        except LeaseError as exc:
            return web.json_response({**exc.as_dict(), **self._as_of()}, status=409)
        return web.json_response(self._render_result(session, frame))

    async def page(self, req: web.Request) -> web.Response:
        session = self._session(req)
        body = await _body(req)
        direction = str(body.get("dir", "next"))
        if direction not in ("next", "prev"):
            return _err(400, "bad_request", 'dir 只能是 "next" 或 "prev"')
        try:
            # 翻页同样要租约：否则第二个 MCP 客户端可以翻走持有者正在展示的内容
            session.hud._check_lease(str(body.get("lease_id") or ""))
        except LeaseError as exc:
            return web.json_response({**exc.as_dict(), **self._as_of()}, status=409)
        turned = session.hud.page(1 if direction == "next" else -1, source="mcp")
        result = self._render_result(session, session.hud.current_frame)
        result["turned"] = turned
        if not turned:
            result["note"] = "已在边界，未翻页（不发冗余帧）"
        return web.json_response(result)

    async def clear(self, req: web.Request) -> web.Response:
        session = self._session(req)
        body = await _body(req)
        try:
            frame = session.hud.clear_external(str(body.get("lease_id") or ""))
        except LeaseError as exc:
            return web.json_response({**exc.as_dict(), **self._as_of()}, status=409)
        return web.json_response(self._render_result(session, frame))

    def _render_result(self, session, frame: dict) -> dict:
        page = session.hud.paginator
        return {
            "ok": True,
            "seq": frame.get("seq"),
            "state": frame.get("state"),
            "containers": frame.get("containers", {}),
            "page": {"cur": page.cur + 1, "total": page.total},
            "online": session.hud.online,
            # 离线不是错误：帧存在服务器上，设备重连时会被重放。但调用方必须知道。
            "note": None if session.hud.online
                    else "设备当前离线；画面已保存，设备重连后会立刻恢复到这一帧",
            **self._as_of(),
        }
