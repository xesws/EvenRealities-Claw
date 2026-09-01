"""网关看到的 agent 全部接口。

抽象的动机不是"将来也许要换"，而是**现在就有两个实现**：不受控的第三方
OpenClaw 网关，与自研的轻量 Lens Agent。两者在职责上有一条真实分界（见 §4.1）：

| provider  | 谁负责小屏输出风格 |
|-----------|--------------------|
| `openclaw`| 网关注入 `STYLE_HEADER`——对面不受控，网关不得不越权管输出 |
| `lens`    | agent 的 system prompt 自带——它是我们自己的，该自己承担契约 |

排版层强制剥 markdown 的兜底两种情况都保留：那防的是模型不听话，是第二道防线。
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Protocol, runtime_checkable

#: (kind, text, extra)，kind ∈ partial | final | error | tool
ChatCallback = Callable[[str, str, str], Awaitable[None]]


@dataclass(frozen=True)
class AgentInfo:
    """W6 · agent 溯源：这一轮回答到底是谁生成的。

    存在的唯一理由是**可自证**。演示时最难反驳的质疑是「你后面挂的是不是一个
    写死剧本的替身」——所以对端身份必须在握手时就被记录下来，并原样暴露在
    `/healthz` 与控制面 `state` 上，让任何人当场 `curl` 一下自己看。

    `production=False` 时（例如 e2e 用的 `demo/fake_openclaw.py`），HUD 状态条
    会带一个可见标记 —— **屏幕上就能看出这不是真 agent**，而不是只写在日志里。
    """

    backend: str = "unknown"        # openclaw | lens | none
    name: str = ""                  # 对端自报的名字
    version: str = ""
    model: str = ""                 # lens provider 才有；openclaw 对面不透露
    endpoint: str = ""              # 连到哪儿了
    production: bool = False        # False = 测试替身/夹具，HUD 会显示标记
    note: str = ""                  # 人话解释，直接给人看的

    def as_dict(self) -> dict:
        return asdict(self)


#: 未连接时的占位，避免 `/healthz` 出现 None 字段
UNKNOWN_AGENT = AgentInfo(backend="none", note="尚未与 agent 建立连接")


def agent_is_trusted(provider: "AgentProvider") -> bool:
    """HUD 状态条要不要打「?」标记 —— 规则只写这一处。

    **未连接时不打标记**：那时对端身份是"未知"而不是"已知是替身"，
    在还没握上手的时候就往屏幕上写「?」等于喊狼来了，用户会学会忽略它。
    只有握过手、且对端自报（或被识别）为非生产 agent 时才标。
    """
    return (not provider.connected.is_set()) or provider.info().production


@runtime_checkable
class AgentProvider(Protocol):
    """任何实现了这 7 项的对象都能插进网关。"""

    connected: asyncio.Event                       # 供 /healthz 探针读取

    async def ensure_connected(self) -> None: ...
    async def close(self) -> None: ...
    def session_busy(self, session_key: str) -> bool: ...
    async def chat_send(self, session_key: str, message: str,
                        callback: ChatCallback, timeout_ms: int = 180_000) -> str: ...
    async def abort(self, session_key: str) -> None: ...
    def info(self) -> AgentInfo: ...
    #: 网关是否需要替对面注入小屏风格指令（自研 agent 自带，不需要）
    injects_style: bool
