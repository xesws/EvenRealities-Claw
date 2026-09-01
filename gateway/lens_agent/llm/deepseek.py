"""DeepSeek（OpenAI 兼容端点）的流式实现。

本文件里的每一条约束都来自**实测**，不是文档推测。探测脚本与结果见
`docs/AGENT-LAYER.md` §13 与 `REPORT.md` §11。四条结论：

1. **`.env` 里的 `deepseek-v4-flash-0731` 调不通**。服务端 400 并列出可用 id：
   `deepseek-v4-pro` / `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp`。
   `0731` 是 checkpoint 后缀，不是调用串。默认值因此是 `deepseek-v4-flash`。
2. **必须显式关思考**。默认 `thinking` 开启且 effort=high：实测同一个问题
   首字 5.75s（开）vs 2.94s（关），且 `prompt_tokens` 94 → 15
   （开启时服务端注入约 79 token 的思考前缀）。对眼镜是纯延迟。
3. **`reasoning_content` 与 `content` 同级，流式下也混在 delta 里**（实测 114 字）。
   它**绝不能上屏** —— 本实现只把 `delta.content` 喂给 `sink`，
   `reasoning_content` 只累加长度用于诊断，正文一个字都不带。
4. **`stream=true` 与 `tool_calls` 可以共存**（实测 `finish_reason=tool_calls`，
   13 个分片增量拼出完整 arguments）。工具在场对纯文本流的代价只有 0.17s。
   ⇒ 带工具的 skill 不必退化成"等工具跑完再一次性出文"。

另一条实测细节决定了 loop 的写法：**模型会在发 tool_calls 之前先流一段正文**
（"我来帮你查询设备 dev_abc 的电量信息。"），而且带 markdown 反引号。
这段文字是真的会先上屏的，所以排版层的 markdown 剥离不是可选项。
"""
from __future__ import annotations

import json
import logging
import os

import aiohttp

from .base import DeltaSink, LLMReply, ToolCall

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"          # ← 不带日期后缀，实测

#: 关思考。写成常量是为了让"它必须被发出去"这件事有一个可断言的名字。
THINKING_DISABLED = {"type": "disabled"}


class MissingApiKey(RuntimeError):
    pass


def read_api_key() -> str:
    """key 只从环境读，**绝不进仓库**。

    `LENS_LLM_API_KEY` 优先于 `OPENAI_API_KEY`：后者在很多机器上被别的项目占着，
    而这套眼镜链路指向的是 DeepSeek 端点，混用会得到一个难查的 401。
    """
    key = os.environ.get("LENS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise MissingApiKey(
            "没有 LLM API key。设置 LENS_LLM_API_KEY（或 OPENAI_API_KEY）后重试。")
    return key.strip()


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, *, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None) -> None:
        self.model = model or os.environ.get("LENS_LLM_MODEL") or DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get("LENS_LLM_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self._key = api_key or read_api_key()
        self._http: aiohttp.ClientSession | None = None

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._key}",
                         "Content-Type": "application/json"})
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()

    async def list_models(self) -> list[str]:
        """`GET /models`。开工时用它确认 id，别猜。"""
        http = await self._session()
        async with http.get(f"{self.base_url}/models") as r:
            body = await r.json()
        return [m["id"] for m in body.get("data", [])]

    # ------------------------------------------------------------------

    async def complete(self, messages: list[dict], tools: list[dict], *,
                       sink: DeltaSink | None = None,
                       timeout: float = 30.0) -> LLMReply:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "thinking": THINKING_DISABLED,   # ← 见模块头第 2 条，不可省
        }
        if tools:
            body["tools"] = tools

        reply = LLMReply()
        frags: dict[int, ToolCall] = {}      # index → 累积中的工具调用
        http = await self._session()
        async with http.post(f"{self.base_url}/chat/completions", json=body,
                             timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"LLM {resp.status}: {(await resp.text())[:200]}")
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if usage := chunk.get("usage"):
                    reply.prompt_tokens = usage.get("prompt_tokens", 0)
                    reply.completion_tokens = usage.get("completion_tokens", 0)
                for choice in chunk.get("choices") or []:
                    await self._consume(choice, reply, frags, sink)

        reply.tool_calls = [frags[i] for i in sorted(frags)]
        return reply

    @staticmethod
    async def _consume(choice: dict, reply: LLMReply, frags: dict[int, ToolCall],
                       sink: DeltaSink | None) -> None:
        delta = choice.get("delta") or {}

        # ★ 思维链**只数长度、不进正文、不进 sink**。这是 reasoning_content
        #   泄漏到眼镜上的唯一防线（另有单测直接断言 sink 一个字都没收到）。
        if rc := delta.get("reasoning_content"):
            reply.reasoning_chars += len(rc)

        if text := delta.get("content"):
            reply.text += text
            if sink is not None:
                await sink(text)

        for frag in delta.get("tool_calls") or []:
            idx = int(frag.get("index", 0))
            cur = frags.setdefault(idx, ToolCall(id="", name=""))
            if frag.get("id"):
                cur.id = frag["id"]
            fn = frag.get("function") or {}
            if fn.get("name"):
                # ★ 名字是**赋值**不是累加。实测 DeepSeek 只在首片给 name，
                # 但 OpenAI 兼容端点并不都这样 —— 有的会在每一片里重复带上它。
                # 那时累加会拼出 "nownownow"，policy 查白名单查不到，
                # 于是整轮被静默拒掉，而日志里只有一个看不懂的工具名。
                # arguments 才是必须累加的（实测被拆成 13 片）。
                cur.name = fn["name"]          # 名字理论上也可能分片，累加更安全
            if fn.get("arguments"):
                cur.arguments += fn["arguments"]

        if reason := choice.get("finish_reason"):
            reply.finish_reason = reason
