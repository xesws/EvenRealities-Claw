# Agent Layer 设计方案 v1

> 目标：用一个**小到能读完、默认只读、可整体搬走**的自研 agent，替代直接接入 OpenClaw。
> 参考对象：[NanoClaw](https://github.com/nanocoai/nanoclaw)（~700 行、容器优先隔离）与
> [nanobot](https://github.com/HKUDS/nanobot)（~4000 行覆盖 OpenClaw 90% 能力）。
> 状态：**P0–P2 已实现并接入**（`gateway/lens_agent/`，M6）。P3（容器隔离演练）未做。
> 实现过程中有一处偏离设计，理由记在 §6.2；设计阶段的四个问号已实测，结果在 §13.1。
> 本文描述的是现状，未落地的部分都已就地标注。

---

## 1. 为什么不直接接 OpenClaw

`docs/DESIGN.md` 的红队清单 R8 已经写明：**OpenClaw 全权 token 等于服务器 shell**。
当前 `gateway/lens_gateway/openclaw.py` 的缓解手段是「token 永不出服务器」——这挡住了
凭证泄漏，但没有回答另一个问题：*agent 自己被诱导做出破坏性操作时，谁来拦？*

在桌面或手机上，答案是「弹确认框」。**在眼镜上这个答案不成立**：

- 576×288 画布、4-bit 16 级绿灰阶（文本只有 `textColor` 0~4 五级亮度），
  一页 8 行 ≈ 224 汉字，装不下一次有意义的操作预览；
- 交互只有「按住说话」和五种镜腿/戒指手势，没有可靠的「拒绝」手势；
- 使用姿势是走路时抬眼一瞥 0.5 秒，注意力是碎片的。

一个人无法在 0.5 秒的一瞥里对「即将删除 37 个文件」做出知情同意。因此：

> **设计原则 0：眼镜 agent 不应该拥有任何需要确认才能安全执行的能力。**

这比「限制权限」更强。容器隔离限制的是**爆炸半径**，而这条原则要求**根本没有炸药**。
容器仍然要做（见 P3），但它是第二道防线，不是第一道。

---

## 2. 设计目标

| 目标 | 可检验的判据 |
|---|---|
| 可审计 | 核心 loop + policy 合计 < 400 行；任何人能在一次通勤里读完 |
| 可替换 | agent 与网关之间只有一条 WS 线协议；整个 `lens_agent/` 目录可原样搬到独立仓库 |
| 默认安全 | 默认 skill 无任何写能力；写能力的授予路径是代码，不是模型判断 |
| 眼镜特化 | 每个工具都满足「一句话能问、一屏能答、两秒内能返」 |
| 延迟优先 | 首字延迟进入 HUD 的 S4→S6 切换预算（见 §10） |

**非目标**（明确不做，避免范围蔓延）：通用编码 agent、文件系统读写、shell、
浏览器自动化、多轮复杂任务编排、跨设备同步。这些是 OpenClaw 的领域，
需要时通过 `openclaw` provider 接回去即可。

---

## 3. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│ lens_gateway/  传输层 —— 不含任何 agent 逻辑              │
│                                                          │
│   AgentProvider（抽象，6 个方法）                         │
│      ├── providers/openclaw.py   现有适配器，保留          │
│      └── providers/lens.py       连自研 agent              │
└───────────────────────┬─────────────────────────────────┘
                        │  Lens Agent Protocol v1
                        │  WS，仅监听 127.0.0.1
                        ▼
┌─────────────────────────────────────────────────────────┐
│ lens_agent/  agent 层 —— 从第一天起就可整体搬走            │
│                                                          │
│   server.py    协议端点，把 chat.send 翻译成一次 run       │
│   loop.py      手写 agent loop（~100 行）                 │
│   policy.py    工具白名单 + 能力闸 + 审计日志              │
│   llm/         LLM provider 抽象（DeepSeek / 其他）        │
│   tools/       每个工具声明能力等级与延迟预算              │
│   skills/      skill = 系统提示 + 工具子集 + 预算          │
└─────────────────────────────────────────────────────────┘
```

### 3.1 为什么边界必须落在进程上

「未来可以搬到独立仓库」这个要求，只有在边界是**进程 + 线协议**时才成立。
如果 agent 是网关直接 import 的 Python 模块，那次搬移就是一次重写。

进程边界另外买到三样东西：

1. **故障隔离**——agent 崩了网关还活着，眼镜显示「agent 无响应」而不是整条链路死掉；
2. **可容器化**——agent 单独进容器，网关留在宿主（NanoClaw 式隔离，见 P3）；
3. **凭证隔离**——LLM API key 只存在于 agent 进程，网关和手机都拿不到。

`demo/fake_openclaw.py` 已经实证了这个形状可行：网关对「对端是谁」完全无感。

---

## 4. 网关侧改造：AgentProvider 抽象

现状的耦合面极小，抽象几乎零成本。`session.py` 只用了三个方法，
`server.py` 再用三个：

```python
# gateway/lens_gateway/providers/base.py
from typing import Awaitable, Callable, Protocol

# (kind, text, extra)，kind ∈ partial | final | error | tool
ChatCallback = Callable[[str, str, str], Awaitable[None]]


class AgentProvider(Protocol):
    """网关看到的 agent 全部接口。任何实现了这 6 项的对象都能插进来。"""

    connected: "asyncio.Event"                      # 供 /healthz 探针读取

    async def ensure_connected(self) -> None: ...
    async def close(self) -> None: ...
    def session_busy(self, session_key: str) -> bool: ...
    async def chat_send(self, session_key: str, message: str,
                        callback: ChatCallback, timeout_ms: int = 180_000) -> str: ...
    async def abort(self, session_key: str) -> None: ...
```

配置新增一个开关，默认保持现状不变：

```json
{ "agent": { "provider": "openclaw" } }   // 或 "lens"
```

**P0 的验收标准是「行为零变化」**：`provider: "openclaw"` 时，
现有 26 个单测与 `e2e_sim.py` 的 14 项端到端断言必须全部照常通过。

### 4.1 顺带修正一处职责错位

当前 `session.py:27` 的 `STYLE_HEADER`（小屏输出风格指令）由**网关**注入。
那是因为对面是不受控的第三方 agent，网关不得不越权管输出风格。

自研 agent 应当自己承担这个契约：

| provider | 谁负责小屏风格 |
|---|---|
| `openclaw` | 网关注入 `STYLE_HEADER`（对面不受控，维持现状） |
| `lens` | agent 的 system prompt 自带（网关不注入） |

网关侧 `formatting/` 强制剥离 markdown / 截断代码块的兜底**两种情况都保留**——
它防的是模型不听话，属于第二道防线。

---

## 5. Lens Agent Protocol v1

传输：WebSocket，**仅监听 `127.0.0.1`**。JSON 文本帧。
形状沿用 `req` / `res` / `event` 三种（与 OpenClaw v3 同构，便于两个 provider 共享心智），
但去掉了我们用不上的 `role` / `scopes` / `operator` 概念。

### 5.1 请求

```jsonc
// C→S 握手（连接后首帧，10 秒内未握手即断开）
{ "type": "req", "id": "a1b2", "method": "connect",
  "params": { "protocol": 1, "client": "lens-gateway/0.3.0" } }

// C→S 发起一轮
{ "type": "req", "id": "c3d4", "method": "chat.send",
  "params": {
    "sessionKey": "lens:dev_a1b2c3",
    "message": "帮我记一下：周四要交报销单",
    "skill": "capture",          // 由网关或 agent 侧路由决定，见 §9.1
    "budgetMs": 8000             // 本轮总延迟预算，超时即降级收尾
  } }

// C→S 打断
{ "type": "req", "id": "e5f6", "method": "chat.abort",
  "params": { "sessionKey": "lens:dev_a1b2c3" } }
```

### 5.2 事件流

```jsonc
{ "type": "event", "event": "chat", "payload": {
    "runId": "run_7a8b", "state": "delta",
    "message": { "content": [ { "type": "text", "text": "已记下。" } ] } } }

// ★ v3 没有、我们需要的：工具调用可见性
{ "type": "event", "event": "chat", "payload": {
    "runId": "run_7a8b", "state": "tool",
    "tool": { "name": "note_append", "label": "记备忘", "phase": "start" } } }

{ "type": "event", "event": "chat", "payload": {
    "runId": "run_7a8b", "state": "final",
    "message": { "content": [ { "type": "text", "text": "已记下：周四交报销单。" } ] } } }

{ "type": "event", "event": "chat", "payload": {
    "runId": "run_7a8b", "state": "error",
    "errorMessage": "天气服务超时" } }
```

`state: "tool"` 是这套协议存在的主要理由之一：**HUD 的 S5 工具态目前是死的**
（`session.py:34` 定义了 `"S5": "⚙ 工具"`，但没有任何代码路径会进入它，
因为 OpenClaw 适配器不上报工具事件）。有了这个事件，眼镜上就能显示
「工 ⚙ 查天气」而不是干等在「◔ 思考 6s」。

### 5.3 与 v3 的差异小结

| 能力 | OpenClaw v3 | Lens Agent v1 |
|---|---|---|
| 工具调用可见性 | 无 | `state: "tool"` |
| 延迟预算 | 无 | `budgetMs` |
| skill 选择 | 无（靠 sessionKey 命名空间） | `skill` 字段 |
| 认证 | token（等于 shell 权限） | 无（仅 loopback；见 A1） |
| role / scopes | 有 | 去掉 |

---

## 6. Agent 内部架构

### 6.1 手写 loop

不使用任何框架的 agent loop。整个循环是可读的一段代码：

```python
# lens_agent/loop.py（示意，非最终实现）
async def run(self, req: ChatRequest, emit: EventEmitter) -> str:
    """跑一轮对话。返回最终文本。每一步的权限检查都在这段代码里可见。"""
    skill: Skill = self.skills.resolve(req.skill)          # Skill 实例，见 §9
    messages: list[dict] = skill.build_messages(req.message, self.history[req.session_key])
    tools: list[dict] = skill.tool_schemas()               # 只有该 skill 允许的工具
    deadline: float = time.monotonic() + req.budget_ms / 1000

    for turn in range(self.max_turns):                     # A5：硬上限，防无限循环
        reply = await self.llm.complete(messages, tools, stream=True, emit=emit)
        if not reply.tool_calls:
            return reply.text
        results: list[dict] = []
        for call in reply.tool_calls:
            self.policy.check(skill, call.name)             # ★ 唯一的授权点
            await emit.tool(call.name, phase="start")
            results.append(await self.tools.invoke(call, deadline))
            self.audit.record(req.session_key, skill.name, call)
        messages += [reply.as_message(), *results]
        if time.monotonic() > deadline:                     # A3：预算耗尽即收尾
            return await self._degrade(messages, emit)
    return await self._degrade(messages, emit)
```

`policy.check()` 是**整个系统里唯一的授权点**。这是「小到能读完」的实际价值：
安全审计只需要确认这一个函数被正确调用，以及它的规则表是对的。

### 6.2 LLM provider 抽象

选定 DeepSeek 之后，agent 的模型层也需要可替换——否则换模型又是一次改写。
接口刻意做窄：

```python
# lens_agent/llm/base.py
class LLMProvider(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict],
                       stream: bool, emit: EventEmitter) -> LLMReply: ...
```

首个实现 `llm/deepseek.py`。

**实现时偏离了这里的设计：没有用 OpenAI 官方 Python SDK，而是用 aiohttp 直接发 HTTP。**
理由有三条，按重要性排：

1. **不引入第二个 HTTP 栈。** 网关与 lens_agent 本身都是 aiohttp，装 `openai` 会把
   httpx 一并拖进来。两套连接池、两套超时语义、两套代理配置，在一台 4 核 ARM 的
   小机器上是纯粹的负担 —— 而我们只用到 `/chat/completions` 一个端点。
2. **SSE 解析必须由我们自己掌握。** §7 表里的 `reasoning_content` 是与 `content`
   同级的字段，SDK 的 `ChoiceDelta` 模型不认识它（它是 DeepSeek 的扩展）。
   走 SDK 就得靠 `model_extra` 去捞，反而比直接读 JSON 更绕、更容易漏。
   而"思维链绝不上屏"是这一层最硬的约束，解析路径必须是看得见的。
3. **依赖面 = 攻击面。** 这是接外部大模型的进程，能少一个传递依赖就少一个。

代价是我们自己维护 SSE 分帧与 `tool_calls` 的分片拼接。这两处都有直接的回归测试
（`tests/test_agent.py` 的 `TestReasoningNeverReachesTheScreen` 与 `TestToolCallAssembly`），
用假的 SSE 服务器喂真实抓到的分片序列。

---

## 7. LLM 层：DeepSeek V4-Flash 接入事实

以下均来自官方文档核实（2026-08），不是推测。**这些约束直接决定了配置默认值。**

| 项 | 值 | 出处/影响 |
|---|---|---|
| 模型 ID | `deepseek-v4-flash` | 当前 checkpoint 为 V4-Flash-0731，**调用时不带日期后缀** |
| 规模 | 284B 总参 / 13B 激活 | — |
| base_url | `https://api.deepseek.com` | strict 模式需改用 `https://api.deepseek.com/beta` |
| SDK | OpenAI 兼容（亦兼容 Anthropic 格式） | 用 `openai` 包 + 自定义 base_url |
| 上下文 / 输出 | 1M / 384K | 本场景用不到千分之一，**无需上下文管理** |
| 工具调用 | 标准 OpenAI `tools` / `tool_calls` 格式 | 工具层设计成立 |
| **thinking 默认** | **默认开启，effort 默认 high** | ⚠ 见下方 §7.1 |
| 关闭 thinking | `"thinking": {"type": "disabled"}` | OpenAI 格式下的参数名 |
| effort 实际档位 | low / high / max（medium、xhigh 均映射为 high） | 调优空间只有三档 |
| CoT 字段 | `reasoning_content`，与 `content` 同级 | **绝不可下发到眼镜** |
| thinking 模式限制 | `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` 全部失效 | 别指望用温度控制风格，只能靠 prompt |
| 定价（每 1M） | 输入 cache miss $0.22–0.44、cache hit $0.007–0.014、输出 $0.66–1.32 | 峰谷两档，峰值为 UTC 01–04 与 06–10（周一至周五） |

### 7.1 默认必须关掉 thinking

这是本节最重要的一条。DeepSeek **默认开启 thinking 且 effort 为 high**，
而眼镜场景的问题（「现在几点」「今天天气」「记一下这件事」）绝大多数不需要推理。
若照默认值跑，S4 思考态会显著变长，直接破坏 §10 的延迟预算。

因此配置默认值定为：

```jsonc
{
  "llm": {
    "model": "deepseek-v4-flash",
    "thinking": { "type": "disabled" },   // 默认关，由 skill 按需打开
    "stream": true
  }
}
```

`thinking` 由 **skill 决定**（§9）：`ask` / `daily` / `capture` 全部关闭；
未来若出现需要推理的 skill，在该 skill 上单独开启并放宽 `budgetMs`。

### 7.2 缓存前缀必须字节稳定

cache hit 与 miss 相差约 **30 倍**（$0.007 vs $0.22 每 1M）。我们的请求前缀
（system prompt + 工具 JSON Schema）在同一 skill 内是固定的，天然适合命中缓存。

由此产生一条硬约束：

> **system prompt 与工具定义中不得包含任何每次请求都变化的内容。**
> 当前时间、deviceId、随机 ID 一律放到 user 消息里，不进前缀。
> 工具列表的序列化顺序必须稳定（不能用无序 dict 遍历）。

这条约束容易被无意破坏（例如某天有人在 system prompt 里加了「当前时间：…」），
所以 P2 应加一个测试：连续两次相同请求，断言第二次的 cache hit token 数 > 0。

---

## 8. 权限模型：四道闸

### 闸 1 — 能力分级，且没有 exec 这一档

每个工具在注册时声明能力等级，**枚举里根本不存在 `exec`**：

```python
class Capability(str, Enum):
    READ  = "read"    # 无副作用
    WRITE = "write"   # 有副作用，且必须绑定到具体资源
```

没有 shell、没有任意文件读写、没有代码执行、没有网络任意请求。
新增工具若无法归入这两类，说明它不该出现在眼镜 agent 里。

### 闸 2 — 写能力绑定在 skill 上，且 skill 路由由代码决定

**这是最关键的一条安全设计。**

写能力不是全局开关，而是挂在 skill 上。默认 skill（`ask`）的工具集为空，
`daily` 全部只读，只有 `capture` 持有 `WRITE` 工具。

更重要的是：**skill 的选择必须由确定性代码完成，绝不能让模型自己选**。
如果允许模型选 skill，一次提示注入就能让它自己升进 `capture` 拿到写权限。

路由规则（确定性，可测试）：

```
用户话语以「记一下 / 记录 / 提醒我 / 加个待办」开头  → capture
用户话语命中 时间/天气/日程 关键词                    → daily
其余一切                                            → ask（无工具）
```

### 闸 3 — 资源边界

`WRITE` 工具不接受「路径」这类参数。`note_append` 只能追加到配置里写死的
那一个文件，`todo_add` 只能写那一个待办文件。工具签名里没有可以指向别处的参数，
因此不存在「诱导它写到 `~/.ssh/authorized_keys`」这种攻击面。

### 闸 4 — 审计日志

每次工具调用记录：时间、sessionKey、skill、工具名、参数、结果摘要、耗时。
落地为单行 JSON，追加写。这是「小项目」才负担得起也才有意义的审计——
700 行的代码 + 完整调用日志，一个人可以真正复核。

### 进程级补充

- agent 只监听 `127.0.0.1`，不做认证（见 A1 的讨论与代价）；
- LLM API key 只存在于 agent 进程环境，网关与手机端均不可见；
- agent 不持有网关的 JWT 密钥、设备库、配对码。

---

## 9. 工具与 Skill

### 9.1 工具准入标准

> **一句话能问、一屏能答、两秒内能返。**

三条全过才进第一批。这条标准同时排除了绝大多数危险工具——
需要确认的操作天然违反「一屏能答」。

第一批工具：

| 工具 | 能力 | 预算 | 状态 | 说明 |
|---|---|---|---|---|
| `now` | READ | 50ms | **已实现** | 本地时间/日期/星期 |
| `weather` | READ | 2000ms | **已实现** | Open-Meteo，仅传城市名 |
| `notes_search` | READ | 800ms | 未实现 | 检索配置目录下的笔记，只读 |
| `note_append` | WRITE | 200ms | 未实现 | 追加到**单个**固定文件 |
| `todo_add` | WRITE | 200ms | 未实现 | 追加到**单个**固定文件 |
| `todo_list` | READ | 200ms | 未实现 | 读那一个文件 |

明确不做：shell、文件读写、发消息/邮件、浏览器、代码执行、日历写入。

#### `weather` 的预算是怎么定到 2000ms 的

设计稿原写 1500ms，实测之后改成 2000ms —— 记在这里是因为**过程比数字重要**：

| 阶段 | 耗时 | 原因 |
|---|---|---|
| 最初 | 2180ms | 每次调用现建 `aiohttp.ClientSession`：DNS + TLS 握手全部重来 |
| 复用会话后 | 1283ms → 315ms | 模块级单例，连接池热起来 |
| 预置地理编码缓存后 | **≈220ms** | Open-Meteo 的 geocoding 端点本身就慢（冷启 2.2s），而 forecast 端点只要 240ms |

`_GEO_CACHE` 里 20 个城市的经纬度**取自该 API 自己的返回**，不是我编的。
缓存不命中时仍然照常去查，只是慢一点。

**内层超时必须小于外层预算**：工具自己的 HTTP 超时设成 1.8s，比 `budget_ms`
的 2000ms 小 —— 否则预算先到，工具那句「天气查不到，稍后再试」永远没机会返回，
用户看到的是一个干巴巴的超时。

写文档时曾把它定成 2500ms，被
`test_every_registered_tool_declares_a_capability_and_budget` 当场拦下 ——
那条测试断言的正是 §9.1 的「两秒内能返」。**标准是我自己写的，先违反的也是我自己**，
测试比人可靠。

### 9.2 Skill 定义

skill 是「系统提示 + 工具子集 + 预算 + thinking 开关」的打包：

```python
@dataclass(frozen=True)
class Skill:
    name: str                    # "ask" | "daily" | "capture"
    system_prompt: str           # 含小屏输出契约（§4.1）
    tools: tuple[str, ...]       # 白名单，policy.check 的依据
    budget_ms: int               # 本 skill 的默认延迟预算
    thinking: bool = False       # §7.1：默认关闭
```

第一批：

| skill | 工具 | 预算 | 写能力 | 状态 |
|---|---|---|---|---|
| `ask` | 无 | 4000ms | 无 | **已实现** |
| `daily` | `now` | 6000ms | 无 | **已实现** |
| `weather` | `weather` | 9000ms | 无 | **已实现** |
| `capture` | `note_append` `todo_add` | 5000ms | **有** | 未实现 |

`weather` 从 `daily` 里独立出来了：它是唯一一个要走公网的 skill，
预算（9000ms）比纯本地的 `daily`（6000ms）宽，把两者混在一个 skill 里
等于让「现在几点」也背上外网往返的预算。路由上 `weather` 的正则**先于**
`daily` 匹配，否则「明天天气怎么样」会被 `daily` 抢走。

`ask` 无工具是刻意的——它是最高频路径，跳过工具编排能拿到最低的首字延迟。

### 9.3 写操作必须回显

`capture` 完成后，最终回复必须包含**实际写入的内容**，例如
「已记下：周四交报销单」。用户在眼镜上一瞥就能发现记错了，
然后用「打断」或重说来纠正。这是在无法事前确认的前提下，
能提供的最好的事后可验证性。

---

## 10. 延迟预算

现有实测：一轮 13.2 秒中，ASR final 约占 1.5 秒，其余是 agent。
HUD 的 S4（思考态）停留时长直接由 agent 首字延迟决定。

目标分配（`ask` 路径）：

| 阶段 | 预算 | 依据 |
|---|---|---|
| 松手 → S3 转写确认 | ≤1.5s | 现状实测，不变 |
| S3 停留 | 1.2s | `confirm_seconds`，现有配置 |
| S4 思考 → 首字 | **≤1.5s** | 关闭 thinking + 无工具 + 流式 |
| S6 流式 → S7 完成 | 随回复长度 | 受 170 字上限约束 |

若 `budgetMs` 耗尽仍未收敛（例如工具连续超时），`_degrade()` 负责用已有信息
给一个诚实的短回复（「天气服务没响应，其他的我知道…」），而不是继续等或抛错。

---

## 11. 红队清单（A1–A10）

沿用 `DESIGN.md` 的红队方法，编号另起以区分。

| # | 级别 | 风险 | 缓解 |
|---|---|---|---|
| A1 | HIGH | agent 无认证，本机任意进程可连 18790 发指令 | 仅 loopback + 能力最小化（最坏情况 = 写一条备忘）。**若宿主是多用户机器，此缓解不成立**，需加 token，见 §13 |
| A2 | HIGH | 提示注入：`notes_search` 读回的笔记内容里藏指令 | 工具结果不提升权限；skill 由代码路由不由模型选（闸 2）；写工具无路径参数（闸 3） |
| A3 | MEDIUM | 工具超时拖死 S4，眼镜长时间空转 | 每工具独立预算 + 本轮总 `budgetMs` + `_degrade()` 收尾 |
| A4 | MEDIUM | 模型不听话，吐 markdown 或超长文本 | agent 侧 system prompt 约束 + 网关 `formatting/` 强制剥离（双层） |
| A5 | MEDIUM | 工具调用无限循环烧 token | `max_turns` 硬上限 |
| A6 | MEDIUM | 误触发写操作（随口一句被记成备忘） | 确定性前缀路由（闸 2）+ 写入内容强制回显（§9.3） |
| A7 | MEDIUM | agent 进程崩溃或挂起 | 独立进程不拖垮网关；网关侧超时 → HUD S8「agent 无响应」 |
| A8 | HIGH | LLM API key 泄漏 | key 只在 agent 进程环境；网关、手机、眼镜三处均不可见 |
| A9 | MEDIUM | 隐私：`notes_search` 把私人笔记送进第三方模型 | 文档明示；该工具默认**不启用**，需用户显式在配置中打开并指定目录 |
| A10 | LOW | 缓存前缀被无意破坏，成本涨 30 倍 | P2 加断言测试（§7.2） |

---

## 12. 实施阶段

### P0 — 抽出 AgentProvider（行为零变化）

- 新建 `gateway/lens_gateway/providers/{base,openclaw}.py`，现有 `openclaw.py` 迁入；
- `session.py` / `server.py` 改依赖 `AgentProvider` 抽象；
- 配置新增 `agent.provider`，默认 `"openclaw"`。

**验收**：26 个单测 + `e2e_sim.py` 的 14 项断言全部照常通过；
`demo/start.sh`（替身模式）行为不变。

### P1 — 能说话的骨架

- `lens_agent/` 骨架：`server.py`（协议）、`loop.py`、`llm/deepseek.py`；
- 只实现一个工具 `now`，只实现 `ask` + `daily` 两个 skill；
- 网关新增 `providers/lens.py`。

**验收**：`provider: "lens"` 下，对着模拟器真说话，跑通一轮真实问答；
HUD 出现 S5 工具态；**并实测 `stream=true` 与 `tool_calls` 能否并存**（见 §13）。

### P2 — 工具集、policy、审计

- 补齐第一批 6 个工具与 3 个 skill；
- `policy.py` + 审计日志 + 确定性 skill 路由；
- 缓存前缀稳定性断言测试。

**验收**：闸 1–4 各有对应单测；提示注入用例（A2）有回归测试。

### P3 — 隔离与拆分演练

- agent 容器化（Dockerfile + 只挂载它需要的那两个文件）；
- 演练一次「把 `lens_agent/` 搬到独立仓库」，确认除配置外无需改动。

---

## 13. 实测结论与仍然存在的空白

### 13.1 四条实测（M6 / P1，全部由直接调用真实端点得出）

设计阶段留的四个问号，实现时逐条打靶。**这些不是读文档得来的，是跑出来的**，
每条都在代码里有对应的常量与回归测试。

| # | 问题 | 实测结果 | 落在哪 |
|---|---|---|---|
| 1 | `.env` 写的 `deepseek-v4-flash-0731` 能不能直接调？ | **不能，HTTP 400。** `0731` 是 checkpoint 名，不是调用串。`GET /models` 实际返回三个：`deepseek-v4-flash` / `deepseek-v4-pro` / `deepseek-v4-flash-vision-exp` | `DEFAULT_MODEL = "deepseek-v4-flash"`；§7 表里那一行是对的，**`.env` 是错的** |
| 2 | `thinking:{"type":"disabled"}` 真的被接受吗？省多少？ | **接受。** 首 token 从 **5.75s 降到 2.94s**；更意外的是 `prompt_tokens` 从 94 降到 **15** —— 开着 thinking 时服务器会往前缀里注入约 79 个 token，这笔钱和这段延迟一直在偷偷付 | `THINKING_DISABLED`，每次请求都带 |
| 3 | `reasoning_content` 长什么样、会不会混进正文？ | **是与 `content` 平级的独立字段，且确实出现在流式 delta 里**（实测抓到 114 字符）。它不会混进 `content`，但只要解析时"把 delta 里所有文本都当正文"就会泄漏 | `_consume()` 里只累加它的**长度**，不进正文、不进 sink；`TestReasoningNeverReachesTheScreen` 三条用例守着 |
| 4 | `stream=true` 与 `tool_calls` 能不能并存？（§13 原第 1 条） | **能并存。** `finish_reason=tool_calls`，参数由 **13 个分片**拼出；带上工具定义只多花 0.17s。**但模型会先流一段散文再吐 tool_calls**，而且那段散文里带 markdown 反引号 | 分片按 `index` 归并；排版层强制剥 markdown 的兜底因此**不能去掉** |

第 4 条是本节最重要的：它意味着 §10 的延迟预算成立，`daily` 这类带工具的 skill
不必退化成「等工具跑完再一次性出文」。

### 13.2 仍然存在的空白

1. **A1 的认证缺口**——单用户笔记本上「仅 loopback」够用；多用户服务器上不够。
   是否加 token 取决于部署形态，未决。
3. **skill 路由的关键词表**——第一版是手写规则，中文口语的覆盖率需要真实语料检验。
   已知风险：口音与 ASR 误转写会让前缀匹配失效（例如「记一下」被转成「几一下」）。
4. **`weather` 的数据源**——未选型。需要一个不要求注册、响应稳定 <1.5s 的接口。
5. **多 agent 路由（工部/格物/都察）** 在本设计中**被 skill 取代**。
   `DESIGN.md` §100 设想的 `lens:<agent>:<deviceId>` 命名空间不再需要——
   若未来仍要接多个 OpenClaw agent，那属于 `openclaw` provider 的范畴。
6. **成本模型未估算**——需要真实使用量样本才能给出每日成本区间。

---

## 参考

- [NanoClaw](https://github.com/nanocoai/nanoclaw) —— 容器优先的轻量 agent，本设计的主要参考
- [nanobot](https://github.com/HKUDS/nanobot) —— 小 agent loop + 按需拉取 memory/skills 的思路
- [DeepSeek API 文档](https://api-docs.deepseek.com/) —— §7 的全部事实来源
- `docs/DESIGN.md` —— 系统总体设计与红队清单 R1–R14
- `protocol/PROTOCOL.md` —— 插件↔网关协议 v1.1（与本文的 agent 协议是两回事）
