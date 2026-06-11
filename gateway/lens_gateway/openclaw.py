"""OpenClaw 网关适配器：loopback WS，token 只在服务器内部使用。

协议（OpenClaw gateway protocol v3）：
- 连接后发 {type:"req", method:"connect", params:{auth:{token}, ...}}，收 hello-ok；
- chat.send → 立即 res {runId}，随后 event:"chat" 流（state: delta/final/error）；
- chat.abort 打断。
delta 语义兼容两种形态（增量块 / 累计全文），自动识别。
runId 僵尸标记（红队 R14）：abort 后该 runId 的一切后续事件被丢弃。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import aiohttp

from .config import OpenClawConfig

log = logging.getLogger(__name__)

ChatCallback = Callable[[str, str, str], Awaitable[None]]
# 参数: (kind, text, extra)  kind ∈ partial|final|error|tool


@dataclass
class _Run:
    run_id: str
    session_key: str
    callback: ChatCallback
    accumulated: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)
    zombie: bool = False


class OpenClawClient:
    def __init__(self, cfg: OpenClawConfig):
        self.cfg = cfg
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http: aiohttp.ClientSession | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._runs: dict[str, _Run] = {}
        self._session_runs: dict[str, str] = {}  # sessionKey -> active runId
        self._connect_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self.connected = asyncio.Event()

    # ---------- 连接管理 ----------

    async def ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._ws is not None and not self._ws.closed:
                return
            await self._connect()

    async def _connect(self) -> None:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        token = self.cfg.read_token()
        self._ws = await self._http.ws_connect(self.cfg.url, heartbeat=20)
        self._reader_task = asyncio.create_task(self._reader())
        hello = await self._request("connect", {
            "minProtocol": 3,
            "maxProtocol": 3,
            "client": {
                "id": "gateway-client",
                "version": "0.1.0",
                "platform": "linux",
                "mode": "backend",
            },
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
            "auth": {"token": token},
            "locale": "zh-CN",
            "userAgent": "lens-gateway/0.1.0",
        }, timeout=10)
        log.info("openclaw connected: protocol=%s", hello.get("protocol"))
        self.connected.set()

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._http is not None:
            await self._http.close()

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                ftype = frame.get("type")
                if ftype == "res":
                    fut = self._pending.pop(frame.get("id", ""), None)
                    if fut and not fut.done():
                        if frame.get("ok", False):
                            fut.set_result(frame.get("payload") or {})
                        else:
                            err = (frame.get("error") or {})
                            fut.set_exception(RuntimeError(err.get("message") or "openclaw error"))
                elif ftype == "event":
                    await self._on_event(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("openclaw reader died")
        finally:
            self.connected.clear()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("openclaw connection lost"))
            self._pending.clear()
            for run in self._runs.values():
                if not run.done.is_set():
                    asyncio.ensure_future(run.callback("error", "与 agent 网关断开", ""))
                    run.done.set()

    async def _request(self, method: str, params: dict, timeout: float = 30) -> dict:
        assert self._ws is not None
        req_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._ws.send_str(json.dumps(
            {"type": "req", "id": req_id, "method": method, "params": params}, ensure_ascii=False))
        return await asyncio.wait_for(fut, timeout)

    # ---------- 事件分发 ----------

    @staticmethod
    def _extract_text(message: dict) -> str:
        parts = []
        for item in message.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)

    async def _on_event(self, frame: dict) -> None:
        if frame.get("event") != "chat":
            return
        payload = frame.get("payload") or {}
        run_id = payload.get("runId", "")
        run = self._runs.get(run_id)
        if run is None or run.zombie:
            return
        state = payload.get("state")
        if state == "delta":
            text = self._extract_text(payload.get("message") or {})
            if not text:
                return
            # 兼容累计全文 / 增量块两种 delta 形态
            if text.startswith(run.accumulated) and len(text) >= len(run.accumulated):
                run.accumulated = text
            else:
                run.accumulated += text
            await run.callback("partial", run.accumulated, "")
        elif state == "final":
            text = self._extract_text(payload.get("message") or {}) or run.accumulated
            run.accumulated = text
            await run.callback("final", text, "")
            run.done.set()
            self._finish(run)
        elif state == "error":
            await run.callback("error", payload.get("errorMessage") or "agent 出错", "")
            run.done.set()
            self._finish(run)

    def _finish(self, run: _Run) -> None:
        self._runs.pop(run.run_id, None)
        if self._session_runs.get(run.session_key) == run.run_id:
            del self._session_runs[run.session_key]

    # ---------- 对外 API ----------

    def session_busy(self, session_key: str) -> bool:
        return session_key in self._session_runs

    async def chat_send(self, session_key: str, message: str, callback: ChatCallback,
                        timeout_ms: int = 180_000) -> str:
        await self.ensure_connected()
        res = await self._request("chat.send", {
            "sessionKey": session_key,
            "message": message,
            "idempotencyKey": uuid.uuid4().hex,
            "timeoutMs": timeout_ms,
        })
        run_id = res.get("runId") or uuid.uuid4().hex
        run = _Run(run_id=run_id, session_key=session_key, callback=callback)
        self._runs[run_id] = run
        self._session_runs[session_key] = run_id
        return run_id

    async def abort(self, session_key: str) -> None:
        run_id = self._session_runs.get(session_key)
        if run_id and run_id in self._runs:
            self._runs[run_id].zombie = True  # 僵尸标记：迟到事件全部丢弃
            self._finish(self._runs.get(run_id) or _Run(run_id, session_key, _noop))
        try:
            await self._request("chat.abort", {"sessionKey": session_key}, timeout=5)
        except Exception:
            log.warning("chat.abort failed (run may have already ended)")


async def _noop(kind: str, text: str, extra: str) -> None:
    return None
