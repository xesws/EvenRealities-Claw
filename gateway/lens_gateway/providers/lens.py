"""自研 Lens Agent 的网关侧客户端（Lens Agent Protocol v1）。

与 OpenClaw v3 同构（`req` / `res` / `event` 三种帧），但去掉了我们用不上的
`role` / `scopes` / `operator`，并**加了三样 v3 没有、眼镜真正需要的东西**：

1. `state: "tool"` 事件 —— HUD 的 S5 工具态在 v3 下是死的（适配器根本收不到
   工具事件），眼镜上只能干等在「思考 12s」。有了它就能显示「答 ◆ 查时间」。
2. `budgetMs` —— 单轮延迟预算。眼镜场景下"慢"等于"坏"，超预算要降级收尾
   而不是无限等下去。
3. 握手回 `agent` / `model` —— W6 溯源，让 `/healthz` 能自证接的是哪个模型。

认证：**无**，只监听 127.0.0.1（见 AGENT-LAYER.md A1）。这是刻意的取舍：
agent 进程不持有网关的 JWT 密钥、设备库、配对码，也没有 shell 能力，
loopback 边界与它能造成的最大伤害是匹配的。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field

import aiohttp

from ..config import AgentConfig
from .base import UNKNOWN_AGENT, AgentInfo, ChatCallback

log = logging.getLogger(__name__)

PROTOCOL = 1


@dataclass
class _Run:
    run_id: str
    session_key: str
    callback: ChatCallback
    accumulated: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)
    zombie: bool = False


class LensAgentClient:
    """说 Lens Agent Protocol v1 的客户端。结构刻意与 `OpenClawClient` 保持一致。"""

    #: 自研 agent 的 system prompt 自带小屏契约，网关不再越权注入（AGENT-LAYER §4.1）
    injects_style = False

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http: aiohttp.ClientSession | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._runs: dict[str, _Run] = {}
        self._session_runs: dict[str, str] = {}
        self._connect_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self.connected = asyncio.Event()
        self._info: AgentInfo = UNKNOWN_AGENT
        #: sessionKey → 已发出 chat.send 但还没拿到 runId 的 run（见 chat_send）
        self._pending_runs: dict[str, _Run] = {}

    def info(self) -> AgentInfo:
        return self._info

    # ---------------- 连接管理 ----------------

    async def ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._ws is not None and not self._ws.closed:
                return
            await self._connect()

    async def _connect(self) -> None:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        self._ws = await self._http.ws_connect(self.cfg.url, heartbeat=20)
        self._reader_task = asyncio.create_task(self._reader())
        try:
            hello = await self._request("connect", {
                "protocol": PROTOCOL,
                "client": "lens-gateway/0.7.0",
            }, timeout=self.cfg.connect_timeout)
        except BaseException:
            # `BaseException` 而不是 `Exception`：外部取消（`asyncio.wait_for`、
            # 用户按打断）走的是 `CancelledError`，它不是 `Exception` 的子类，
            # 用 `except Exception` 会**正好漏掉最常见的那种失败**。
            # 握手失败必须把 socket 收干净。否则 `_ws` 留在"未关闭"状态，
            # `ensure_connected()` 的短路判据（ws 非空且未关闭）会认为已经连上了，
            # 于是**再也不会重连** —— 表现是 agent 起来了网关也永远连不上，
            # 而 W6 溯源永久停在 unknown（对端身份未知 ⇒ 不打「?」⇒ 替身也不告状）。
            await self._teardown()
            raise
        self._info = AgentInfo(
            backend="lens",
            name=str(hello.get("agent") or self.cfg.agent_name),
            version=str(hello.get("version") or ""),
            model=str(hello.get("model") or ""),
            endpoint=self.cfg.url,
            production=bool(hello.get("production", False)),
            note=str(hello.get("note") or ""),
        )
        log.info("lens agent connected: model=%s production=%s",
                 self._info.model, self._info.production)
        self.connected.set()

    async def _teardown(self) -> None:
        """收掉当前连接的 socket 与 reader，让 ensure_connected 下次真的重连。"""
        self.connected.clear()
        self._info = UNKNOWN_AGENT
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

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
                            err = frame.get("error") or {}
                            fut.set_exception(RuntimeError(err.get("message") or "lens agent error"))
                elif ftype == "event":
                    await self._on_event(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lens agent reader died")
        finally:
            self.connected.clear()
            self._info = UNKNOWN_AGENT
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("lens agent connection lost"))
            self._pending.clear()
            # ★ 必须连 `_runs` / `_session_runs` 一起清掉，不能只 set(done)。
            # 漏掉这一步的后果是**这台设备被永久锁死**：`session_busy()` 只看
            # `_session_runs` 里有没有这个 key，一次 agent 掉线之后，
            # 用户此后说的每一句话都会被「上一条还在跑，点打断后再说」挡回去，
            # 而"打断"走的 `abort()` 又依赖同一张表，救不回来 —— 只能重启网关。
            for run in list(self._runs.values()):
                if not run.done.is_set():
                    asyncio.ensure_future(run.callback("error", "与 agent 断开", ""))
                    run.done.set()
                self._finish(run)
            self._runs.clear()
            self._session_runs.clear()
            # 还没拿到 runId 的占位 run 同样要通知并清掉 —— 它们卡在 `chat_send`
            # 的 await 上，而那个 await 的 future 刚刚被置了异常。
            for run in list(self._pending_runs.values()):
                if not run.done.is_set():
                    asyncio.ensure_future(run.callback("error", "与 agent 断开", ""))
                    run.done.set()
            self._pending_runs.clear()

    async def _request(self, method: str, params: dict, timeout: float = 30) -> dict:
        assert self._ws is not None
        req_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send_str(json.dumps(
                {"type": "req", "id": req_id, "method": method, "params": params},
                ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout)
        finally:
            # 超时/取消/发送失败都要把坑填上。否则每一次超时都留下一个永不回收的
            # future，`_pending` 无界增长 —— 一个长时间运行的网关会慢慢漏内存，
            # 而且断线时的"把未决请求全部置异常"会越跑越久。
            self._pending.pop(req_id, None)

    # ---------------- 事件分发 ----------------

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
        run_id = str(payload.get("runId", ""))
        run = self._runs.get(run_id)
        if run is None and self._pending_runs:
            # 事件跑在 chat.send 的响应前面了（见 chat_send 的注释）。
            # 眼镜一次只说一句话，同一时刻至多一个占位 run，直接认领。
            session_key, run = next(iter(self._pending_runs.items()))
            run.run_id = run_id
            self._pending_runs.pop(session_key, None)
            self._runs[run_id] = run
            self._session_runs[session_key] = run_id
        if run is None or run.zombie:
            return
        state = payload.get("state")
        if state == "delta":
            text = self._extract_text(payload.get("message") or {})
            if not text:
                return
            # ★ 这里**不能**照抄 openclaw.py 那套「增量块 / 累计全文」的启发式。
            #
            # Lens Agent Protocol v1 是我们自己定的：`delta.text` 恒为**当前应显示的
            # 完整正文**（见 lens_agent/loop.py 的 sink）。两端都归我们，不需要猜。
            #
            # 猜的代价实测过：模型在发 tool_calls 之前会先流一段散文（M6 实测事实 4），
            # 工具跑完后 agent 会把正文清零重新组织 —— 新文本自然不以旧文本开头，
            # 启发式就落到 `+=` 分支，于是每来一个 delta 就把当前全文再拼一遍：
            #   「让我查一下时间」+「现」+「现在」+「现在是」… → 二次方级重复的乱码。
            # 「现在几点」这种最普通的问题就会触发。
            run.accumulated = text
            await run.callback("partial", run.accumulated, "")
        elif state == "tool":
            tool = payload.get("tool") or {}
            if tool.get("phase") == "start":
                # HUD S5：工具名给屏幕，正文保持上一问不动（extra 由 pipeline 决定）
                await run.callback("tool", str(tool.get("label") or tool.get("name") or "工具"), "")
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

    # ---------------- 对外 API ----------------

    def session_busy(self, session_key: str) -> bool:
        return session_key in self._session_runs

    async def chat_send(self, session_key: str, message: str, callback: ChatCallback,
                        timeout_ms: int = 180_000) -> str:
        """发起一轮。返回 runId。

        **登记必须先于 await。** agent 那头是「先回 res 再开跑」，而我们这头
        `_reader` 是一个连续的循环：它 resolve 掉 res 的 future 之后**不会停下来等**
        本协程恢复，而是接着读下一帧。于是首批 delta 完全可能在 `chat_send` 拿到
        runId 之前就到达 —— `_on_event` 查不到这个 run，把它们**静默丢弃**，
        屏幕就卡在「思考 0s」直到 final 才跳一下。
        所以先按 sessionKey 占位登记，拿到真 runId 后再改挂。
        """
        await self.ensure_connected()
        run = _Run(run_id="", session_key=session_key, callback=callback)
        self._pending_runs[session_key] = run
        try:
            res = await self._request("chat.send", {
                "sessionKey": session_key,
                "message": message,
                "budgetMs": self.cfg.budget_ms,
            })
        except Exception:
            self._pending_runs.pop(session_key, None)
            raise
        run_id = res.get("runId") or uuid.uuid4().hex
        self._pending_runs.pop(session_key, None)
        # ★ 登记前的最后一道检查：`_request` 成功返回**不等于**连接还活着。
        # `_reader` 在 resolve 掉 res 的 future 之后不会停下来等本协程恢复，
        # 它接着读下一帧 —— 那一帧完全可能是 close。于是 finally 先跑完
        # （那时 run 还没进表，清了个空），本协程才恢复并把 run 登记到一条
        # **已经死掉的连接**上：回调永远等不到事件，`session_busy()` 永久为 True，
        # 眼镜锁死。这条竞态被 `_drops_after_run` 的用例稳定复现。
        if not self.connected.is_set():
            raise ConnectionError("与 agent 的连接在这一轮发送过程中断开")
        run.run_id = run_id
        self._runs[run_id] = run
        self._session_runs[session_key] = run_id
        return run_id

    async def abort(self, session_key: str) -> None:
        run_id = self._session_runs.get(session_key)
        if run_id and run_id in self._runs:
            self._runs[run_id].zombie = True   # 僵尸标记：迟到事件全部丢弃
            self._finish(self._runs[run_id])
        try:
            await self._request("chat.abort", {"sessionKey": session_key}, timeout=5)
        except Exception:
            log.warning("chat.abort failed (run may have already ended)")
