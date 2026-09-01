"""闸 2 的执行点：**整个系统里唯一的授权判定**。

安全审计因此只需要确认两件事：这个函数被正确调用了，以及它的规则表是对的。
这是"小到能读完"的实际价值。
"""
from __future__ import annotations

from .skills import Skill
from .tools import REGISTRY, Capability


class PolicyDenied(PermissionError):
    def __init__(self, skill: str, tool: str, reason: str) -> None:
        super().__init__(f"skill {skill!r} 不允许调用 {tool!r}：{reason}")
        self.skill, self.tool, self.reason = skill, tool, reason


def check(skill: Skill, tool_name: str) -> None:
    """★ 唯一的授权点。任何工具执行前都必须先过这里。

    模型能"要求"调用任何名字，包括我们没注册的、以及别的 skill 才有的 ——
    所以这里查的是**白名单**而不是黑名单：不在 `skill.tools` 里的一律拒。
    """
    if tool_name not in REGISTRY:
        raise PolicyDenied(skill.name, tool_name, "工具未注册")
    if tool_name not in skill.tools:
        raise PolicyDenied(skill.name, tool_name, "不在该 skill 的白名单内")
    tool = REGISTRY[tool_name]
    if tool.capability is Capability.WRITE and skill.is_default:
        # 冗余的一道 —— 兜底 skill 的白名单本来就是空的。留着是因为白名单将来会被改，
        # 而"兜底 skill 永远不该有写能力"这条不该依赖于别人记得。
        # 判据是 `is_default` 而不是 `name == "ask"`：把安全性质挂在字符串上，
        # 等于让这道闸在某次改名时悄无声息地失效。
        raise PolicyDenied(skill.name, tool_name, "兜底 skill 不得持有写能力")
    if tool.capability is Capability.WRITE and not tool.resources:
        # 闸 3。正常情况下 `Tool.__post_init__` 已经挡住了，这里是运行期的第二遍 ——
        # 授权点必须能独立成立，不依赖"注册的时候有人检查过"。
        raise PolicyDenied(skill.name, tool_name, "写能力没有绑定资源（闸 3）")
