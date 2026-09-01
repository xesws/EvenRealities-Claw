"""手写 agent loop。不用任何框架 —— 整个循环要能一眼读完。

设计要求（AGENT-LAYER §6.1）：每一步的权限检查都在这段代码里**看得见**。
`policy.check()` 是唯一的授权点；它旁边就是审计记录；再旁边是硬轮次上限。
一个人花五分钟读完这一个文件，就能判断这个 agent 能干什么、不能干什么。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import policy, skills, tools
from .audit import Audit
from .llm.base import LLMProvider, LLMReply
from .tools import _t

log = logging.getLogger(__name__)

#: 一轮对话里最多问几次模型。**不是"最多调几个工具"** —— 每一轮工具调用之后
#: 还要再问一次模型，所以 now → calc → 回答就已经吃掉 3 次。
#:
#: 原来写 3，实测「离圣诞还有几天」（先 now 取今天、再 calc 做减法、再组织回答）
#: 稳定撞上「工具轮次用尽」，用户看到的是一句道歉而不是答案。
#: 真正的护栏是 deadline（skill 的 budget），它按墙钟掐；轮次上限只是防死循环，
#: 不该顺带把正常的两步工具链也掐掉。
MAX_TURNS = 4
#: 降级收尾时缀在半截正文后面的标记。开头用「…」是因为正文区寸土寸金，
#: 而这个字形已确认在 G2 字库内（docs/GLYPH-TABLE.md）。
#:
#: 它跟着 locale 走：这一小截字是「屏幕不许撒谎」那条原则的**唯一载体** ——
#: 没有它，一段被预算掐断的半截话会以「√ Done」的状态停在屏幕上，
#: 用户完全看不出这句话没说完。所以它绝不能因为语言不对而变成看不懂的字。
TRUNCATED_MARK = _t("…（未说完）", "… (cut off)")
MAX_HISTORY = 6        # 一副眼镜的对话不需要长记忆；短历史也让缓存前缀更稳定

#: (state, payload) → 发事件。state ∈ delta | tool | final | error
Emit = Callable[[str, dict], Awaitable[None]]


@dataclass
class ChatRequest:
    session_key: str
    message: str
    budget_ms: int = 8000
    run_id: str = field(default_factory=lambda: "run_" + uuid.uuid4().hex[:8])
    #: 网关随这一轮带过来的眼镜遥测快照（协议里的可选字段 `deviceState`）。
    #: 只在本轮有效 —— 电量是会变的，缓存它等于制造一个新的说谎来源。
    device_state: dict | None = None


class AgentLoop:
    def __init__(self, llm: LLMProvider, audit: Audit | None = None) -> None:
        self.llm = llm
        self.audit = audit or Audit()
        self.history: dict[str, list[dict]] = {}

    def reset(self, session_key: str) -> None:
        self.history.pop(session_key, None)

    # ------------------------------------------------------------------

    async def run(self, req: ChatRequest, emit: Emit) -> str:
        """跑一轮对话。返回最终文本。"""
        skill = skills.route(req.message)          # ★ 闸 2：代码选 skill，模型无权参与
        # 本轮的眼镜状态放进 ContextVar，`device` 工具从那里读。
        # 放在这里而不是工具里去拿，是因为工具不该知道请求是怎么来的。
        tools.DEVICE_STATE.set(req.device_state)
        # 提醒工具把「要排哪些程」放进这个队列，工具跑完后由本函数发给网关。
        # **agent 不拥有屏幕**，所以它到点也响不了 —— 它只能请求，由网关去响。
        scheduled: list[dict] = []
        tools.PENDING_REMINDERS.set(scheduled)
        tools.SESSION_KEY.set(req.session_key)
        history = self.history.get(req.session_key, [])
        messages = skill.build_messages(req.message, history)
        schemas = skill.tool_schemas()
        budget = min(req.budget_ms, skill.budget_ms) / 1000
        deadline = time.monotonic() + budget
        log.info("run %s skill=%s budget=%.1fs tools=%s",
                 req.run_id, skill.name, budget, skill.tools or "无")

        #: 当前应该显示在屏幕上的完整正文。
        #:
        #: **协议约定（Lens Agent Protocol v1）：`delta.text` 恒为完整正文，不是增量块。**
        #: 网关侧据此直接赋值，不做任何「是增量还是全文」的猜测 —— 猜错的后果见
        #: `providers/lens.py` 里那段注释（工具轮之后会拼出二次方级重复的乱码）。
        streamed = ""

        async def sink(chunk: str) -> None:
            nonlocal streamed
            streamed += chunk
            await emit("delta", {"text": streamed})

        for turn in range(MAX_TURNS):
            remaining = deadline - time.monotonic()
            if remaining <= 0.2:
                return await self._degrade(messages, streamed, emit, _t("预算耗尽", "out of time"))
            try:
                reply: LLMReply = await self.llm.complete(
                    messages, schemas, sink=sink, timeout=max(1.0, remaining))
            except asyncio.CancelledError:
                raise                       # 打断走的是取消，不能被下面吞掉
            except asyncio.TimeoutError:
                return await self._degrade(messages, streamed, emit, _t("模型超时", "model timed out"))
            except Exception as exc:
                # 只兜超时是不够的：HTTP 429 / 5xx、连接中断、SSE 解析失败都会
                # 从这里抛出来。让它冒到上层的话，用户屏幕上已经流出去的正文
                # 会被一句原始异常串整个替换掉 —— 已经读到一半的答案凭空消失，
                # 换来一行看不懂的英文。降级收尾至少保住已经在屏幕上的东西。
                log.warning("模型调用失败：%s: %s", type(exc).__name__, exc)
                return await self._degrade(
                    messages, streamed, emit,
                    _t(f"模型出错（{type(exc).__name__}）", f"model error: {type(exc).__name__}"))
            if reply.reasoning_chars:
                # 关了 thinking 还收到思维链 = 服务端行为变了，值得报警。
                # 但它**已经被丢弃了**，不会上屏（deepseek.py `_consume`）。
                log.warning("收到 %d 字 reasoning_content（已丢弃，未上屏）",
                            reply.reasoning_chars)

            if not reply.tool_calls:
                text = reply.text or streamed
                self._remember(req.session_key, req.message, text)
                return text

            messages = messages + [reply.as_message()]
            for call in reply.tool_calls:
                result = await self._invoke(req, skill, call, deadline, emit)
                messages.append(result.as_message())
            # 排程请求紧跟着工具走，不等这一轮说完 —— 万一后面降级收尾，
            # 提醒也已经交出去了。用户听到「好，10 分钟后叫你」时它必须是真的。
            while scheduled:
                await emit("schedule", {"reminder": scheduled.pop(0)})
            # 工具跑完后模型会重新组织正文，屏幕从头来。这是**有意的**：
            # 工具前那段散文（「让我查一下…」，还常带 markdown 反引号）不是答案，
            # 不该留在屏幕上。归零之后下一个 delta 就是新的完整正文。
            streamed = ""

        return await self._degrade(messages, streamed, emit,
                                   _t("工具轮次用尽", "ran out of tool turns"))

    async def _invoke(self, req: ChatRequest, skill: skills.Skill, call,
                      deadline: float, emit: Emit):
        """执行一次工具调用。授权 → 上屏 → 执行 → 审计，顺序固定。"""
        try:
            policy.check(skill, call.name)                    # ★ 唯一的授权点
        except policy.PolicyDenied as denied:
            self.audit.denied(req.session_key, skill.name, call.name, denied.reason)
            log.warning("拒绝工具调用：%s", denied)
            # 把拒绝**作为工具结果**回给模型，让它换个说法完成回答，
            # 而不是让整轮对话失败。用户不该为模型的越权尝试买单。
            return tools.ToolResult(call.id, call.name,
                                    f"该操作不被允许：{denied.reason}", False, 0)

        await emit("tool", {"tool": {"name": call.name,
                                     "label": tools.label_of(call.name),
                                     "phase": "start"}})
        # 审计必须覆盖**被打断的那一次**。原来只在执行成功返回之后才写，
        # 于是用户按下"打断"、任务被 cancel 时，这次已经真的跑过的调用
        # 不留任何记录 —— 而闸 4 宣称的是"每次工具调用都留痕"。
        # 审计日志的用处恰恰是回答"到底发生过什么"，漏记比记得晚更糟。
        try:
            result = await tools.invoke(call.name, call.id, call.arguments, deadline=deadline)
        except asyncio.CancelledError:
            self.audit.record(req.session_key, skill.name, call.name, call.arguments,
                              "（执行中被打断）", False, 0)
            raise
        except Exception as exc:
            self.audit.record(req.session_key, skill.name, call.name, call.arguments,
                              f"（执行异常：{type(exc).__name__}）", False, 0)
            raise
        self.audit.record(req.session_key, skill.name, call.name, call.arguments,
                          result.content, result.ok, result.elapsed_ms)
        return result

    async def _degrade(self, messages: list[dict], streamed: str, emit: Emit,
                       why: str) -> str:
        """A3 · 预算耗尽的收尾。

        眼镜场景下"慢"等于"坏"：宁可给一句不完整但立刻可见的话，
        也不要让用户盯着「思考 40s」。已经流出去的正文直接沿用 —— 它已经在屏幕上了，
        再换一段文字反而是二次伤害。
        """
        log.warning("降级收尾：%s", why)
        if streamed.strip():
            # ★ 必须带截断标记。不带的话，一段被预算掐断的半截话会以
            # 「√ 完成」的状态停在屏幕上 —— 用户完全看不出这句话没说完，
            # 而这正是最需要他知道的事。整个项目的前提是"屏幕不许撒谎"。
            return streamed.rstrip() + TRUNCATED_MARK
        return _t(f"这个问题一时答不上来（{why}），换个说法再问一次吧。",
                  f"I could not finish that one ({why}). Try asking again.")

    def _remember(self, session_key: str, question: str, answer: str) -> None:
        hist = self.history.setdefault(session_key, [])
        hist.append({"role": "user", "content": question})
        hist.append({"role": "assistant", "content": answer})
        del hist[:-MAX_HISTORY]
