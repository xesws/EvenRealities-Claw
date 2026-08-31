"""Lens Gateway HTTP/WS 服务。

- /ws        插件 WebSocket（pair/hello 认证 → DeviceSession）
- /plugin/   托管插件构建产物（手机 Even App 扫码加载的就是这个地址）
- /healthz   健康探针
- /admin/*   仅 loopback：生成配对码、设备管理（供 CLI 调用）
安全（红队 R8）：对外 API 仅 提交语音/收帧/翻页/打断 四类动作，绝不透传 OpenClaw RPC。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import jwt as pyjwt
from aiohttp import WSMsgType, web

from .asr import AsrEngine
from .auth import AuthStore
from .config import STATE_DIR, Config, jwt_secret
from .openclaw import OpenClawClient
from .session import DeviceSession

log = logging.getLogger(__name__)


class LensServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.auth = AuthStore(STATE_DIR, jwt_secret())
        self.asr = AsrEngine(cfg.asr)
        self.claw = OpenClawClient(cfg.openclaw)
        self.sessions: dict[str, DeviceSession] = {}      # deviceId -> 常驻会话
        self.active_ws: dict[str, web.WebSocketResponse] = {}  # deviceId -> 当前连接
        self._sweeper: asyncio.Task | None = None

    # ---------- app ----------

    def sweep_sessions(self, now: float | None = None) -> list[str]:
        """回收离线且静默超过 TTL 的会话（修 S4）。

        `self.sessions` 以前**只增不减**：每个配对过的设备都会永久占着一个
        DeviceSession（含 PCM 缓冲、分页器、计时器任务）。设备再也不上线也不会释放，
        长跑的网关内存单调增长。这里按「已离线 + 静默超时」两个条件一起判定 ——
        在线会话无论多久没动都不回收，否则会把用户正看着的画面清掉。
        """
        ttl = self.cfg.composer.session_ttl_seconds
        if ttl <= 0:
            return []
        now = now if now is not None else time.monotonic()
        dead = [did for did, s in self.sessions.items()
                if not s.hud.online and now - s.last_active > ttl]
        for did in dead:
            session = self.sessions.pop(did)
            session.hud.cancel_timer()
            log.info("回收静默会话 %s（离线 %.0fs）", did, now - session.last_active)
        return dead

    async def _sweep_loop(self) -> None:
        ttl = self.cfg.composer.session_ttl_seconds
        interval = max(60.0, min(ttl / 10, 3600.0)) if ttl > 0 else 3600.0
        try:
            while True:
                await asyncio.sleep(interval)
                self.sweep_sessions()
        except asyncio.CancelledError:
            pass

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/healthz", self.handle_health)
        app.router.add_post("/admin/pair-code", self.handle_admin_pair_code)
        app.router.add_get("/admin/devices", self.handle_admin_devices)
        app.router.add_post("/admin/revoke", self.handle_admin_revoke)
        dist = self.cfg.resolve_plugin_dist()
        if dist:
            app.router.add_get("/", lambda r: web.HTTPFound("/plugin/"))
            app.router.add_get("/plugin", lambda r: web.HTTPFound("/plugin/"))
            app.router.add_get("/plugin/", self._index(dist))
            app.router.add_static("/plugin", dist, show_index=False)
            log.info("serving plugin from %s", dist)
        else:
            log.warning("plugin dist not found — /plugin/ disabled (先构建 plugin)")
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    @staticmethod
    def _index(dist: Path):
        async def handler(_req: web.Request) -> web.FileResponse:
            return web.FileResponse(dist / "index.html")
        return handler

    async def _on_startup(self, _app) -> None:
        asyncio.ensure_future(self._warmup())
        self._sweeper = asyncio.ensure_future(self._sweep_loop())

    async def _on_cleanup(self, _app) -> None:
        if self._sweeper and not self._sweeper.done():
            self._sweeper.cancel()

    async def _warmup(self) -> None:
        try:
            await self.asr.warmup()
        except Exception:
            log.exception("asr warmup failed")
        try:
            await self.claw.ensure_connected()
        except Exception:
            log.exception("openclaw connect failed (将在首次使用时重试)")

    # ---------- 健康/管理 ----------

    async def handle_health(self, _req: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "asr_ready": self.asr.ready,
            "openclaw": self.claw.connected.is_set(),
            "devices": len(self.auth.list_devices()),
            "sessions": len(self.sessions),
        })

    @staticmethod
    def _require_loopback(req: web.Request) -> None:
        peer = req.transport.get_extra_info("peername") if req.transport else None
        if not peer or peer[0] not in ("127.0.0.1", "::1"):
            raise web.HTTPForbidden(text="admin API is loopback-only")

    async def handle_admin_pair_code(self, req: web.Request) -> web.Response:
        self._require_loopback(req)
        return web.json_response({"code": self.auth.new_pair_code(), "ttl": 600})

    async def handle_admin_devices(self, req: web.Request) -> web.Response:
        self._require_loopback(req)
        return web.json_response([d.__dict__ for d in self.auth.list_devices()])

    async def handle_admin_revoke(self, req: web.Request) -> web.Response:
        self._require_loopback(req)
        body = await req.json()
        ok = self.auth.revoke(body.get("deviceId", ""))
        return web.json_response({"ok": ok})

    # ---------- WS ----------

    async def handle_ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(req)

        device_id = await self._authenticate(ws)
        if device_id is None:
            await ws.close()
            return ws

        # 单设备单连接（R6）：新连接挤掉旧连接
        old = self.active_ws.pop(device_id, None)
        if old is not None and not old.closed:
            await old.close(code=4000, message=b"superseded")
        self.active_ws[device_id] = ws

        session = self.sessions.get(device_id)
        if session is None:
            session = DeviceSession(device_id, self.cfg, self.asr, self.claw)
            self.sessions[device_id] = session

        async def send_json(obj: dict) -> None:
            if not ws.closed:
                await ws.send_str(json.dumps(obj, ensure_ascii=False))

        resume = session.attach(send_json)
        access, exp = self.auth.issue_access(device_id)
        await send_json({"type": "hello_ok", "deviceId": device_id, "exp": exp,
                         "server": "lens-gateway/0.1.0", "accessToken": access,
                         "resume": resume})

        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await session.handle_binary(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        obj = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "ping":
                        await send_json({"type": "pong", "t": obj.get("t", 0)})
                    elif obj.get("type") == "refresh":
                        await self._handle_refresh(send_json, obj)
                    else:
                        await session.handle_text(obj)
        finally:
            if self.active_ws.get(device_id) is ws:
                del self.active_ws[device_id]
                session.detach()
        return ws

    async def _authenticate(self, ws: web.WebSocketResponse) -> str | None:
        """第一帧必须是 pair / hello / refresh+hello。10s 超时。"""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                msg = await ws.receive(timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                return None
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    return None
                continue
            try:
                obj = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            mtype = obj.get("type")
            if mtype == "pair":
                result = self.auth.pair(str(obj.get("code", "")), str(obj.get("deviceName", "未命名设备")))
                if result is None:
                    await ws.send_str(json.dumps(
                        {"type": "error", "code": "pair_failed", "message": "配对码无效或已过期"},
                        ensure_ascii=False))
                    continue  # 允许重输
                dev, refresh = result
                access, exp = self.auth.issue_access(dev.device_id)
                await ws.send_str(json.dumps({
                    "type": "pair_ok", "deviceId": dev.device_id,
                    "accessToken": access, "refreshToken": refresh, "exp": exp,
                }, ensure_ascii=False))
                return dev.device_id
            if mtype == "refresh":
                got = self.auth.refresh(str(obj.get("refreshToken", "")))
                if got is None:
                    await ws.send_str(json.dumps(
                        {"type": "error", "code": "auth_failed", "message": "设备凭证无效，请重新配对"},
                        ensure_ascii=False))
                    return None
                device_id, access, exp = got
                await ws.send_str(json.dumps(
                    {"type": "refresh_ok", "accessToken": access, "exp": exp}, ensure_ascii=False))
                continue  # 等 hello
            if mtype == "hello":
                try:
                    device_id = self.auth.verify_access(str(obj.get("token", "")))
                except pyjwt.ExpiredSignatureError:
                    await ws.send_str(json.dumps(
                        {"type": "error", "code": "token_expired", "message": "请刷新凭证"},
                        ensure_ascii=False))
                    continue
                except pyjwt.InvalidTokenError:
                    device_id = None
                if device_id is None:
                    await ws.send_str(json.dumps(
                        {"type": "error", "code": "auth_failed", "message": "设备凭证无效，请重新配对"},
                        ensure_ascii=False))
                    return None
                return device_id
        return None

    async def _handle_refresh(self, send_json, obj: dict) -> None:
        got = self.auth.refresh(str(obj.get("refreshToken", "")))
        if got is None:
            await send_json({"type": "error", "code": "auth_failed", "message": "设备凭证无效，请重新配对"})
        else:
            _device_id, access, exp = got
            await send_json({"type": "refresh_ok", "accessToken": access, "exp": exp})


def run(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    async def _make_app() -> web.Application:
        # 必须在 run_app 自己的事件循环内构造：asyncio.Lock/Event 在 py3.9
        # 绑定创建时的循环，提前构造会导致 "attached to a different loop"。
        return LensServer(cfg).build_app()

    web.run_app(_make_app(), host=cfg.host, port=cfg.port)
