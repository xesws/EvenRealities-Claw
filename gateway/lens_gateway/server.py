"""Lens Gateway HTTP/WS 服务。

- /ws        插件 WebSocket（pair/hello 认证 → DeviceSession）
- /plugin/   托管插件构建产物（手机 Even App 扫码加载的就是这个地址）
- /healthz   健康探针
- /admin/*   Bearer 控制面密钥：生成配对码、设备管理（供 CLI 调用）
             **不是 loopback 判据** —— 推荐的 TLS 方案是反代，那时所有请求的
             peername 都是 127.0.0.1，按来源判断等于把管理面向全网敞开。见 `_require_control`。
安全（红队 R8）：对外 API 仅 提交语音/收帧/翻页/打断 四类动作，绝不透传 OpenClaw RPC。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path

import jwt as pyjwt
from aiohttp import WSMsgType, web

from .asr import AsrEngine
from .auth import AuthStore
from .config import STATE_DIR, Config, control_secret, jwt_secret
from .control import ControlPlane
from .providers import AgentProvider, build_provider
from .device import LeaseHeld
from .session import DeviceSession

log = logging.getLogger(__name__)


class LensServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.auth = AuthStore(STATE_DIR, jwt_secret())
        self.control_secret = control_secret()
        self.asr = AsrEngine(cfg.asr)
        self.agent: AgentProvider = build_provider(cfg)
        # 提醒到点：agent 那头发一条不属于任何 run 的事件，网关负责把它显示出来。
        # **屏幕归网关**，所以响铃这一步必须落在这里，而不是让 agent 反过来打控制面。
        # 用 setattr 而不是写死在 AgentProvider 协议里：只有自研 agent 有这条通路，
        # 第三方 OpenClaw 网关没有，不该为它加一个恒为 None 的字段。
        if hasattr(self.agent, "on_notify"):
            self.agent.on_notify = self._on_agent_notify
        self.sessions: dict[str, DeviceSession] = {}      # deviceId -> 常驻会话
        self.active_ws: dict[str, web.WebSocketResponse] = {}  # deviceId -> 当前连接
        self._sweeper: asyncio.Task | None = None

    # ---------- app ----------

    async def _on_agent_notify(self, session_key: str, text: str) -> None:
        """一条提醒到点了，把它写到那副眼镜上。

        走**外部渲染租约**（W1），和 MCP 写屏是同一条路：这样它和正在进行的
        对话之间的仲裁规则只有一套 —— 用户正说着话时提醒不该插进来抢屏，
        而租约冲突本来就是这么处理的。
        """
        device_id = session_key.split(":", 1)[-1]
        session = self.sessions.get(device_id)
        if session is None:
            log.info("提醒到点，但设备 %s 不在线，丢弃：%s", device_id, text[:40])
            return
        try:
            lease = session.hud.acquire_lease("reminder", ttl_ms=30_000)
            session.hud.render_external(lease.id, text,
                                        title=session.hud.msg("reminder_title"),
                                        hold_ms=self.cfg.composer.reminder_hold_ms)
        except LeaseHeld as held:
            # 别人正拿着屏幕（MCP 客户端在写）。提醒不抢 —— 抢屏比迟到更糟。
            log.info("提醒到点但屏幕被 %s 占着，跳过：%s", held.holder, text[:40])
        except Exception:
            log.exception("提醒上屏失败：%s", text[:40])

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
        ControlPlane(self).add_routes(app)
        dist = self.cfg.resolve_plugin_dist()
        if dist:
            async def _to_plugin(_req: web.Request) -> web.Response:
                raise web.HTTPFound("/plugin/")

            app.router.add_get("/", _to_plugin)
            app.router.add_get("/plugin", _to_plugin)
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
            await self.agent.ensure_connected()
        except Exception:
            log.exception("agent connect failed (将在首次使用时重试)")

    # ---------- 健康/管理 ----------

    async def handle_health(self, _req: web.Request) -> web.Response:
        # `agent` 是 W6 溯源：演示时当场 curl 一下就能看到接的到底是谁、是不是替身。
        # `openclaw` 这个键保留是为了不破坏既有探针脚本（它现在的语义是"agent 已连接"）。
        return web.json_response({
            "ok": True,
            "asr_ready": self.asr.ready,
            "openclaw": self.agent.connected.is_set(),
            "agent": {"connected": self.agent.connected.is_set(), **self.agent.info().as_dict()},
            "devices": len(self.auth.list_devices()),
            "sessions": len(self.sessions),
        })

    def _require_control_auth(self, req: web.Request) -> None:
        """管理面与控制面的唯一鉴权：状态目录里的共享密钥（W4）。

        取代了原来的 loopback 判据 —— 它按 peername 判断，而推荐的 TLS 方案是
        caddy `reverse_proxy 127.0.0.1:8443`，反代之后所有请求的 peername 都是
        127.0.0.1，判据整体失效；加上 `host` 默认 `0.0.0.0`，等于把 `/admin/*`
        暴露给了任何人。密钥怎么来见 `config.control_secret()`。
        """
        header = req.headers.get("Authorization", "")
        # RFC 7235 允许 scheme 与凭证之间有 1 个以上空格，scheme 本身大小写不敏感
        scheme, _, rest = header.partition(" ")
        token = rest.strip() if scheme.lower() == "bearer" else ""
        # 必须比字节：非 ASCII 的令牌会让 compare_digest(str, str) 抛 TypeError，
        # 结果是 500 而不是 401 —— 那本身就是一个可供攻击者区分的信号。
        if not token or not secrets.compare_digest(token.encode(), self.control_secret.encode()):
            raise web.HTTPUnauthorized(
                text="需要 Authorization: Bearer <控制面密钥>（见 ~/.lens-gateway/control.secret）",
                headers={"WWW-Authenticate": 'Bearer realm="lens-control"'},
            )

    async def handle_admin_pair_code(self, req: web.Request) -> web.Response:
        self._require_control_auth(req)
        return web.json_response({"code": self.auth.new_pair_code(), "ttl": 600})

    async def handle_admin_devices(self, req: web.Request) -> web.Response:
        self._require_control_auth(req)
        rows = []
        for d in self.auth.list_devices():
            row = dict(d.__dict__)
            session = self.sessions.get(getattr(d, "device_id", ""))
            # 没有会话就是"这台设备本次进程内没连过"，遥测如实为 None，不编默认值
            row["live"] = session.snapshot() if session else None
            rows.append(row)
        return web.json_response(rows)

    async def handle_admin_revoke(self, req: web.Request) -> web.Response:
        self._require_control_auth(req)
        body = await req.json()
        ok = self.auth.revoke(body.get("deviceId", ""))
        return web.json_response({"ok": ok})

    # ---------- WS ----------

    async def handle_ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(req)

        device_id = await self._authenticate(ws, req)
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
            session = DeviceSession(device_id, self.cfg, self.asr, self.agent)
            self.sessions[device_id] = session

        async def send_json(obj: dict) -> None:
            if not ws.closed:
                await ws.send_str(json.dumps(obj, ensure_ascii=False))

        resume = session.attach(send_json)
        access, exp = self.auth.issue_access(device_id)
        await send_json({"type": "hello_ok", "deviceId": device_id, "exp": exp,
                         "server": "lens-gateway/0.1.0", "accessToken": access,
                         "resume": resume})
        # 连上就先拉一次遥测，免得在设备下一次状态变化之前网关一直"不知道电量"。
        # 拉回来的值记作 source="poll"（可能是手机端缓存），不冒充新鲜值。
        await session.request_telemetry()

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

    def _client_key(self, req: web.Request) -> str:
        """配对节流的计数键。

        直连时用 peername。跑在反代后面时 peername 恒为 127.0.0.1，必须改看
        `X-Forwarded-For` —— 但这个头**直连时是攻击者可以随手伪造的**，
        所以它只在 `trust_forwarded_for` 显式打开时才被采信。这也正是
        `auth.PAIR_GLOBAL_MAX` 那道全局闸存在的理由：按来源的那一层永远可能被绕过。
        """
        if self.cfg.trust_forwarded_for:
            fwd = req.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        peer = req.transport.get_extra_info("peername") if req.transport else None
        return peer[0] if peer else "unknown"

    async def _authenticate(self, ws: web.WebSocketResponse, req: web.Request) -> str | None:
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
                client = self._client_key(req)
                retry = self.auth.pair_locked(client)
                if retry:
                    await ws.send_str(json.dumps(
                        {"type": "error", "code": "pair_throttled",
                         "message": f"配对尝试过多，请 {retry}s 后再试"}, ensure_ascii=False))
                    log.warning("配对被节流：来源 %s，还需等待 %ds", client, retry)
                    return None
                result = self.auth.pair(str(obj.get("code", "")),
                                        str(obj.get("deviceName", "未命名设备")), client=client)
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
