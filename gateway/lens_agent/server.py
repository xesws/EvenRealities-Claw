"""Lens Agent Protocol v1 服务端。**只监听 127.0.0.1，不做认证。**

这个取舍是刻意的（AGENT-LAYER A1）：agent 进程不持有网关的 JWT 密钥、设备库、
配对码，工具能力里没有 shell / 文件读写 / 代码执行，loopback 边界与它能造成的
最大伤害是匹配的。多用户服务器上这个假设不成立，届时需要加 token —— 已如实
记在 §13.2 的待决项里，而不是假装解决了。

握手会回 `model` / `production` / `note`：这是 W6 溯源的上游一半，
网关据此在 `/healthz` 上暴露"这一轮到底是哪个模型答的"。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import WSMsgType, web

from . import tools
from .audit import Audit
from .llm import DeepSeekProvider, MissingApiKey
from .loop import AgentLoop, ChatRequest
from . import reminders
from .reminders import ReminderScheduler

log = logging.getLogger(__name__)

PROTOCOL = 1
VERSION = "0.1.0"
HANDSHAKE_TIMEOUT = 10.0


class LensAgentServer:
    def __init__(self, llm=None, audit: Audit | None = None) -> None:
        self.llm = llm or DeepSeekProvider()
        self.loop = AgentLoop(self.llm, audit)
        #: sessionKey → 正在跑的任务。眼镜一次只说一句话，同一会话不并发。
        self._runs: dict[str, asyncio.Task] = {}
        self._aborted: set[str] = set()
        #: 所有活着的连接，**按连上的先后**。提醒到点时要往其中一条发通知 ——
        #: 那一刻没有任何 run 在跑，所以通知不带 runId，走的是一条**主动**的事件。
        #:
        #: 为什么是一个列表而不是"当前那条连接"：连上来的不止网关一个。
        #: `demo/chat.py` 也说同一套协议，它一 connect 就会把那个单槽覆盖掉，
        #: 退出时再置空 —— 于是**跑一次调试 CLI，提醒就再也送不到眼镜上了**，
        #: 而且没有任何症状，直到某条提醒该响的时候没响。
        self._conns: list[web.WebSocketResponse] = []
        #: sessionKey → 它最后一次是从哪条连接来的。提醒记着自己的 sessionKey，
        #: 所以能精确送回**当初交代这件事的那一头**，而不是猜。
        self._session_ws: dict[str, web.WebSocketResponse] = {}
        self.reminders = ReminderScheduler(self._notify)

    # ---------------- HTTP/WS ----------------

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_ws)
        app.router.add_get("/healthz", self.handle_health)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_cleanup(self, _app: web.Application) -> None:
        self.reminders.cancel_all()
        for task in list(self._runs.values()):
            task.cancel()
        await self.llm.close()

    async def handle_health(self, _req: web.Request) -> web.Response:
        return web.json_response({
            "ok": True, "protocol": PROTOCOL, "version": VERSION,
            "model": self.llm.model, "provider": self.llm.name,
            "tools": tools.describe(),
            "reminders_pending": self.reminders.pending,
        })

    def hello(self) -> dict:
        # 这份自我介绍会原样出现在网关的 `/healthz` 里 —— 它就是 W6 溯源的证据本身，
        # 演示时当场 `curl` 就能自证「对面不是替身」。所以它也要跟着 locale 走，
        # 否则英文演示里蹦出一个中文名字，看的人只会觉得这里有东西没对齐。
        return {
            "protocol": PROTOCOL,
            "agent": tools._t("小龙虾", "Lens"),
            "version": VERSION,
            "model": self.llm.model,
            # 真模型答的就是 production。替身不会走到这个文件里 —— 它是另一个进程。
            "production": True,
            "note": tools._t(
                f"自研 lens agent，模型 {self.llm.model}（{self.llm.name}）",
                f"first-party lens agent, model {self.llm.model} ({self.llm.name})",
            ),
        }

    async def handle_ws(self, req: web.Request) -> web.WebSocketResponse:
        # ★ 只绑回环是**不够**的。浏览器的 WebSocket 不受同源策略约束：
        # 用户随便打开一个网页，那个页面就能 `new WebSocket("ws://127.0.0.1:18790")`
        # 连上这个没有鉴权的 agent，替他烧 API key、读它能读的东西。
        # 判据是 `Origin` 头：浏览器发起的连接**一定**带它，而我们唯一的合法客户端
        # （网关，aiohttp 客户端）不带。所以带 Origin 的一律拒掉。
        origin = req.headers.get("Origin")
        if origin is not None:
            log.warning("拒绝带 Origin 的 WS 连接（疑似来自浏览器页面）：%s", origin[:80])
            raise web.HTTPForbidden(reason="browser origins are not allowed")

        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        shook = False
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if frame.get("type") != "req":
                    continue
                req_id = frame.get("id", "")
                method = frame.get("method", "")
                params = frame.get("params")
                if not isinstance(params, dict):
                    params = {}
                # 单条请求处理失败**不能**掀掉整条连接。否则一个畸形参数就让
                # `async for` 抛出、走到 finally 静默关闭 —— 网关那头看到的是
                # 「连接莫名其妙断了」，收不到任何错误帧，也无从排查。
                try:
                    if method == "connect":
                        shook = True
                        if ws not in self._conns:
                            self._conns.append(ws)
                        await self._res(ws, req_id, self.hello())
                        # 握手完成才恢复提醒：早于这一刻发通知，网关那头
                        # 还没准备好接收。sessionKey 由请求方在 chat.send 里给，
                        # 恢复时只能用磁盘上记着的那一个。
                        for row in tools._load_reminders(reminders.GRACE_SECONDS):
                            self.reminders.schedule(str(row.get("session") or "default"), row)
                    elif not shook:
                        await self._err(ws, req_id, "not_connected", "请先 connect")
                    elif method == "chat.send":
                        await self._on_send(ws, req_id, params)
                    elif method == "chat.abort":
                        await self._on_abort(ws, req_id, params)
                    else:
                        await self._err(ws, req_id, "unknown_method", f"未知方法 {method}")
                except Exception as exc:
                    log.exception("处理 %s 失败", method)
                    await self._err(ws, req_id, "internal", f"{type(exc).__name__}: {exc}"[:200])
        finally:
            # 连接没了，还在跑的那一轮已经没人接收事件了。不取消的话它会继续
            # 烧 token，而且 `_runs[session_key]` 会一直是"未完成"状态 ——
            # 网关重连后的第一句话会被自己的 busy 判据拒掉。
            for key, task in list(self._runs.items()):
                if not task.done():
                    task.cancel()
                self._runs.pop(key, None)
            # 只摘掉自己这一条。别的连接（尤其是网关那条）不受影响 ——
            # 这正是从前那个单槽做不到的事。
            if ws in self._conns:
                self._conns.remove(ws)
            for key in [k for k, w in self._session_ws.items() if w is ws]:
                del self._session_ws[key]
            # 待响的提醒**不取消**：连接会重连，而提醒是用户交代过的事。
            # 断连期间到点的那些由 `restore` 按宽限期补发。
            await ws.close()
        return ws

    async def _notify(self, session_key: str, text: str) -> None:
        """提醒到点。**这是唯一一条不属于任何 run 的事件。**

        网关据此写屏 —— 屏幕是它的，agent 只能请求。
        """
        # 先找**当初交代这件事的那一头**；它不在了，再退回最近连上的那条。
        # 退回是有意的：agent 进程重启后 `restore` 会把磁盘上的提醒重新排上，
        # 而那时还没有任何 chat.send 来建立 sessionKey → 连接的映射。
        ws = self._session_ws.get(session_key)
        if ws is None or ws.closed:
            ws = next((w for w in reversed(self._conns) if not w.closed), None)
        if ws is None:
            # 发不出去就留在磁盘上，等网关重连时 `restore` 在宽限期内补发。
            raise ConnectionError("网关不在线")
        await self._send(ws, {"type": "event", "event": "notify",
                              "payload": {"sessionKey": session_key, "text": text}})

    # ---------------- 方法 ----------------

    async def _on_send(self, ws: web.WebSocketResponse, req_id: str,
                       params: dict) -> None:
        session_key = str(params.get("sessionKey") or "default")
        # 记下这个会话是从哪条连接来的 —— 提醒到点时要原路送回去，
        # 否则「谁交代的」和「送给谁」就对不上了。
        self._session_ws[session_key] = ws
        message = str(params.get("message") or "").strip()
        if not message:
            await self._err(ws, req_id, "empty_message",
                            tools._t("message 不能为空", "message must not be empty"))
            return
        if session_key in self._runs and not self._runs[session_key].done():
            await self._err(ws, req_id, "busy", "该会话上一轮还在跑")
            return

        try:
            budget_ms = int(params.get("budgetMs") or 8000)
        except (TypeError, ValueError):
            await self._err(ws, req_id, "bad_params", "budgetMs 必须是整数毫秒")
            return
        # `deviceState` 是可选字段：老网关不发，这里就是 None，行为与从前一致。
        device_state = params.get("deviceState")
        if not isinstance(device_state, dict):
            device_state = None
        chat = ChatRequest(session_key=session_key, message=message,
                           budget_ms=budget_ms, device_state=device_state)
        # 先回 runId 再开跑：网关要靠它把后续事件对上号（与 v3 同构）
        await self._res(ws, req_id, {"runId": chat.run_id})
        self._aborted.discard(session_key)
        self._runs[session_key] = asyncio.create_task(self._drive(ws, chat))

    async def _on_abort(self, ws: web.WebSocketResponse, req_id: str,
                        params: dict) -> None:
        session_key = str(params.get("sessionKey") or "default")
        self._aborted.add(session_key)
        task = self._runs.get(session_key)
        if task and not task.done():
            task.cancel()
        await self._res(ws, req_id, {"ok": True})

    async def _drive(self, ws: web.WebSocketResponse, chat: ChatRequest) -> None:
        async def emit(state: str, payload: dict) -> None:
            if chat.session_key in self._aborted:
                # 打断之后一个字都不该再上屏（对齐 v3 的 runId 僵尸语义）
                raise asyncio.CancelledError
            await self._event(ws, {"runId": chat.run_id, "state": state, **payload})

        try:
            text = await self.loop.run(chat, self._wrap(chat, emit))
            if chat.session_key not in self._aborted:
                await self._event(ws, {
                    "runId": chat.run_id, "state": "final",
                    "message": {"content": [{"type": "text", "text": text}]}})
        except asyncio.CancelledError:
            log.info("run %s 已被打断", chat.run_id)
        except MissingApiKey as exc:
            await self._event(ws, {"runId": chat.run_id, "state": "error",
                                   "errorMessage": str(exc)})
        except Exception as exc:
            log.exception("run %s 失败", chat.run_id)
            await self._event(ws, {"runId": chat.run_id, "state": "error",
                                   "errorMessage": f"{type(exc).__name__}: {str(exc)[:120]}"})
        finally:
            self._runs.pop(chat.session_key, None)

    def _wrap(self, chat: ChatRequest, emit):
        """把 loop 的 (state, payload) 翻译成协议帧形状。"""
        async def inner(state: str, payload: dict) -> None:
            if state == "delta":
                await emit("delta", {"message": {"content": [
                    {"type": "text", "text": payload["text"]}]}})
            elif state == "schedule":
                # 排程请求**不出网**：它是 agent 内部的事，网关只在到点时
                # 收到一条 notify。往协议里多塞一种网关根本不需要处理的事件，
                # 只会让两头都要维护它。
                self.reminders.schedule(chat.session_key, payload.get("reminder") or {})
            else:
                await emit(state, payload)
        return inner

    # ---------------- 帧 ----------------

    @staticmethod
    async def _send(ws: web.WebSocketResponse, obj: dict[str, Any]) -> None:
        if not ws.closed:
            await ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def _res(self, ws, req_id: str, payload: dict) -> None:
        await self._send(ws, {"type": "res", "id": req_id, "ok": True, "payload": payload})

    async def _err(self, ws, req_id: str, code: str, message: str) -> None:
        await self._send(ws, {"type": "res", "id": req_id, "ok": False,
                              "error": {"code": code, "message": message}})

    async def _event(self, ws, payload: dict) -> None:
        await self._send(ws, {"type": "event", "event": "chat", "payload": payload})


def main() -> None:
    logging.basicConfig(level=os.environ.get("LENS_AGENT_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = os.environ.get("LENS_AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("LENS_AGENT_PORT", "18790"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        # 没有认证的服务绑到外网 = 把一个能烧钱的 LLM 端点白送出去
        raise SystemExit(f"拒绝绑定到 {host}：lens agent 没有认证，只能监听回环地址。")
    server = LensAgentServer()
    log.info("lens agent 监听 %s:%s，模型 %s", host, port, server.llm.model)
    web.run_app(server.build_app(), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
