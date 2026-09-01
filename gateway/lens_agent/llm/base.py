"""LLM 层接口。刻意做窄：换模型不该引起 loop 的改写。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

#: 流式 token 回调。只喂**可以上屏的正文** —— 思维链绝不走这条路。
DeltaSink = Callable[[str], Awaitable[None]]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""      # 原始 JSON 串（模型是分片吐的，拼完才解析）

    def as_message_part(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments or "{}"}}


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    #: 只用于诊断与断言。**永远不会**被下发到眼镜，见 `deepseek.py` 的说明。
    reasoning_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [c.as_message_part() for c in self.tool_calls]
        return msg


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, messages: list[dict], tools: list[dict], *,
                       sink: DeltaSink | None = None,
                       timeout: float = 30.0) -> LLMReply: ...
    async def close(self) -> None: ...
