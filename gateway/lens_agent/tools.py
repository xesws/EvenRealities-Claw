"""工具注册表。

准入标准（AGENT-LAYER §9.1）：**一句话能问、一屏能答、两秒内能返。**
这条标准同时排掉了绝大多数危险工具 —— 需要二次确认的操作天然违反"一屏能答"。

闸 1：能力枚举里**根本没有 exec 这一档**。没有 shell、没有任意文件读写、
没有代码执行、没有任意网络请求。新工具若无法归入 READ / WRITE 两类，
说明它不该出现在一副眼镜的 agent 里。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable


class Capability(str, Enum):
    READ = "read"      # 无副作用
    WRITE = "write"    # 有副作用，且必须绑定到具体资源（闸 3）


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    capability: Capability
    budget_ms: int
    parameters: dict
    handler: Callable[[dict], Awaitable[str]]
    label: str = ""          # 上屏用的短词（S5 工具态），≤4 字
    #: 闸 3：这个工具被允许改动的资源，写死在代码里。
    #:
    #: 这里曾经**只是一句注释** —— 设计文档和交付报告都把「WRITE 必须绑定到具体
    #: 资源」当作四道闸之一在宣称，而代码里一行实现都没有。当时没有任何 WRITE 工具，
    #: 所以没出事；但那意味着第一个 WRITE 工具可以毫无阻力地带着"模型给什么路径就
    #: 写什么路径"进来，而四道闸的说法会继续成立地写在报告里。
    #:
    #: 现在它是构造期强制的：WRITE 工具必须声明非空 `resources`，READ 必须为空。
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.capability is Capability.WRITE and not self.resources:
            raise ValueError(
                f"工具 {self.name!r} 声明了写能力却没有绑定资源（闸 3）。"
                f"写能力必须钉在代码里写死的资源上，不能由模型给。")
        if self.capability is Capability.READ and self.resources:
            raise ValueError(f"工具 {self.name!r} 是只读的，不该绑定可写资源")

    def schema(self) -> dict:
        """OpenAI function-calling 格式。"""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    ok: bool
    elapsed_ms: int

    def as_message(self) -> dict:
        return {"role": "tool", "tool_call_id": self.call_id, "content": self.content}


# ---------------------------------------------------------------- 第一批工具

WEEKDAYS = "一二三四五六日"


async def _now(_args: dict) -> str:
    t = datetime.now()
    return (f"{t.year}年{t.month}月{t.day}日 星期{WEEKDAYS[t.weekday()]} "
            f"{t.hour:02d}:{t.minute:02d}")


NOW = Tool(
    name="now",
    description="获取当前的本地日期、星期与时间。问到「今天几号」「现在几点」「星期几」时用它。",
    capability=Capability.READ,
    budget_ms=50,
    parameters={"type": "object", "properties": {}},
    handler=_now,
    label="查时间",
)

REGISTRY: dict[str, Tool] = {t.name: t for t in (NOW,)}


class ToolError(RuntimeError):
    pass


async def invoke(name: str, call_id: str, arguments: str, *,
                 deadline: float | None = None) -> ToolResult:
    """执行一次工具调用。**不做授权** —— 授权是 `policy.check` 的事，只有那一处。

    参数解析失败不抛给 loop，而是作为工具结果回给模型：让它自己纠正一次参数，
    比让整轮对话直接失败对用户友好得多。
    """
    tool = REGISTRY.get(name)
    if tool is None:
        raise ToolError(f"未注册的工具：{name}")
    started = time.monotonic()
    try:
        args = json.loads(arguments) if arguments.strip() else {}
        if not isinstance(args, dict):
            raise ValueError("参数必须是一个 JSON 对象")
    except Exception as exc:
        return ToolResult(call_id, name, f"参数解析失败：{exc}", False,
                          int((time.monotonic() - started) * 1000))

    budget = tool.budget_ms / 1000
    if deadline is not None:
        budget = min(budget, max(0.05, deadline - time.monotonic()))
    try:
        content = await asyncio.wait_for(tool.handler(args), timeout=budget)
        ok = True
    except asyncio.TimeoutError:
        content, ok = f"{tool.name} 超时（预算 {tool.budget_ms}ms）", False
    except Exception as exc:                       # 工具坏了不该炸掉整轮对话
        content, ok = f"{tool.name} 执行失败：{str(exc)[:120]}", False
    return ToolResult(call_id, name, content, ok,
                      int((time.monotonic() - started) * 1000))


def schemas(names: tuple[str, ...]) -> list[dict]:
    return [REGISTRY[n].schema() for n in names if n in REGISTRY]


def label_of(name: str) -> str:
    tool = REGISTRY.get(name)
    return (tool.label or tool.name) if tool else name


def capability_of(name: str) -> Capability | None:
    tool = REGISTRY.get(name)
    return tool.capability if tool else None


def describe() -> list[dict[str, Any]]:
    """给 /healthz 之类的自证接口用：现在到底装了哪些工具、各是什么能力。"""
    return [{"name": t.name, "capability": t.capability.value,
             "budget_ms": t.budget_ms, "label": t.label} for t in REGISTRY.values()]
