"""Agent provider 工厂。

`agent.provider` 决定网关把话说给谁：

- `"openclaw"`（默认）：第三方 OpenClaw 网关，协议 v3，网关替它注入小屏风格；
- `"lens"`：自研轻量 agent，Lens Agent Protocol v1，自带小屏契约与工具权限模型。

两者都实现 `AgentProvider`，网关其余部分对此无感。
"""
from __future__ import annotations

from ..config import Config
from .base import (UNKNOWN_AGENT, AgentInfo, AgentProvider, ChatCallback,
                   agent_is_trusted)
from .lens import LensAgentClient
from .openclaw import OpenClawClient

__all__ = ["AgentInfo", "AgentProvider", "ChatCallback", "UNKNOWN_AGENT", "agent_is_trusted",
           "LensAgentClient", "OpenClawClient", "build_provider"]


def build_provider(cfg: Config) -> AgentProvider:
    kind = (cfg.agent.provider or "openclaw").lower()
    if kind == "lens":
        return LensAgentClient(cfg.agent)
    if kind == "openclaw":
        return OpenClawClient(cfg.openclaw)
    raise ValueError(f'未知的 agent.provider：{kind!r}（只能是 "openclaw" 或 "lens"）')
