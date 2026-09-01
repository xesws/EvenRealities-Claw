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

    # ---------------- HTTP/WS ----------------

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_ws)
        app.router.add_get("/healthz", self.handle_health)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_cleanup(self, _app: web.Application) -> None:
        for task in list(self._runs.values()):
            task.cancel()
        await self.llm.close()

    async def handle_health(self, _req: web.Request) -> web.Response:
        return web.json_response({
            "ok": True, "protocol": PROTOCOL, "version": VERSION,
            "model": self.llm.model, "provider": self.llm.name,
            "tools": tools.describe(),
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
                        await self._res(ws, req_id, self.hello())
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
            await ws.close()
        return ws

    # ---------------- 方法 ----------------

    async def _on_send(self, ws: web.WebSocketResponse, req_id: str,
                       params: dict) -> None:
        session_key = str(params.get("sessionKey") or "default")
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
            text = await self.loop.run(chat, self._wrap(emit))
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

    @staticmethod
    def _wrap(emit):
        """把 loop 的 (state, payload) 翻译成协议帧形状。"""
        async def inner(state: str, payload: dict) -> None:
            if state == "delta":
                await emit("delta", {"message": {"content": [
                    {"type": "text", "text": payload["text"]}]}})
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
