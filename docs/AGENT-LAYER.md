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
    "budgetMs": 8000,            // 本轮总延迟预算，超时即降级收尾
    // 可选。眼镜此刻的遥测快照，`device` 工具的唯一数据源。
    // 未知字段两端都忽略 ⇒ 这是**加法安全**的：老网关不发，agent 收到 None，
    // 行为与从前完全一致。
    "deviceState": {
      "battery": 41, "worn": true, "charging": false,
      "age_ms": 1200, "stale": false, "source": "push"
    }
  } }

// C→S 打断
{ "type": "req", "id": "e5f6", "method": "chat.abort",
  "params": { "sessionKey": "lens:dev_a1b2c3" } }
```

**`deviceState` 为什么随请求走，而不是开一条反向 RPC。**
眼镜的电量/佩戴状态本来就在网关的遥测缓存里（M4），而 agent 完全看不见 ——
实测问它「我眼镜还有多少电」，它编了个 82%。让 agent 反过来查网关需要
新方向的 RPC、新的鉴权、以及一份「什么时候查」的策略；而这一轮真正需要的
就那么几个字段，随请求带过去即可：没有新连接、没有新方向、没有轮询。
代价是它只在有人说话的那一刻是新鲜的 —— 对一副眼镜来说，这正好够用。

agent 侧用 `ContextVar` 承接（`tools.DEVICE_STATE`）而不是模块级全局：
一个 agent 进程服多副眼镜，每轮 `chat.send` 跑在自己的 task 里，
全局变量会让 A 的电量串到 B 的回答里。

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

// ★ 唯一一条**不属于任何 run** 的事件：一条提醒到点了。
// 那一刻那一轮早就结束了，所以它没有 runId 可以挂靠，只能自报是哪副眼镜。
// 网关据此走外部渲染租约写屏（见 §9.3 的「提醒」）。
{ "type": "event", "event": "notify", "payload": {
    "sessionKey": "lens:dev_a1b2c3", "text": "面条好了" } }
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
| `days_until` | READ | 50ms | **已实现** | 到某日还有几天；只给月日按「下一次」算 |
| `device` | READ | 50ms | **已实现** | 眼镜自身：电量/充电/佩戴。数据来自网关遥测 |
| `weather` | READ | 2000ms | **已实现** | Open-Meteo，仅传城市名 |
| `calc` | READ | 50ms | **已实现** | AST 白名单求值，**不是 eval** |
| `currency` | READ | 2000ms | **已实现** | 欧洲央行参考汇率，仅传币种与金额 |
| `list_show` | READ | 100ms | **已实现** | 读清单 |
| `list_add` | WRITE | 200ms | **已实现** | 追加到**单个**固定文件 |
| `list_remove` | WRITE | 200ms | **已实现** | 从那一个文件里删 |
| `remind_set` | WRITE | 200ms | **已实现** | 24 小时以内；`minutes` 给延时、`at` 给钟点（HH:MM），到点由**网关**写屏 |
| `remind_list` | READ | 100ms | **已实现** | 只看本会话的 |
| `remind_cancel` | WRITE | 200ms | **已实现** | 有歧义时拒绝并回问 |

明确不做：shell、文件读写、发消息/邮件、浏览器、代码执行、日历写入。

#### 为什么工具表长成这样：一次「假装自己什么都能做」的实测

第一版只有 `now` 和 `weather`。拿 15 个日常问题跑一遍（`demo/chat.py -f`），
结果不是「工具太少」，而是**没有工具时它会假装有**：

| 问 | 答 | 真相 |
|---|---|---|
| Set a timer for 10 minutes | "Timer set for 10 minutes." | 没有定时器工具 |
| Add milk and eggs to my shopping list | "Milk and eggs are now on your shopping list." | 没有清单工具 |
| What's on my calendar today? | 编了一整天日程（客户电话、Q3 roadmap review） | 没有日历工具 |
| Who won the game last night? | "Lakers beat Celtics 112-108, LeBron 34 分" | 编造比赛 |
| How much battery do my glasses have? | "82 percent" | **遥测就在网关里，agent 拿不到** |
| How many days until Christmas? | 两次跑给出 48 天 / 116 天 | 正确是 116；同一问不同答 |

对照组：问心率，它老实说看不到。所以模型不是不会拒绝 ——
**是没人告诉它边界在哪**：小屏契约当时 6 条规则全是排版，没有一条讲能力。

于是做了两件事，顺序不能反：

1. **契约加了第 1 条**（`skills.py` 的 `CONTRACT_ZH/EN`）：没有对应工具的动作直接说做不到，
   没有对应工具的实时事实直接说不知道，绝不能说「已经帮你办好了」。
   这一条修掉了上表里除日历外的**全部**编造。
2. **补工具**，优先补「系统里已经有数据、agent 却拿不到」的那些（`device` 就是白送的）。

数字类问题单靠契约修不掉 —— 模型说「我算一下」然后算错，它并不认为自己在编。
所以 `calc` / `days_until` 是必须的，且 skill 的系统提示里写死「不要心算」。

后来又补了**第 8 条：用户用什么语言问，就用什么语言答**。起因是一句中文提问
被英文答了 —— 屏上语言（`LENS_AGENT_LOCALE`）决定的是状态条上那几个词和契约
本身的语言，不该连用户说什么话都替他定了。

新规则一律**追加在契约末尾**，不往中间插：DeepSeek 的缓存前缀按字节匹配，
插在第 N 条会让第 N 条之后的所有字节都对不上，追加则前面那一大段仍然命中。
所以规则的重要性顺序和编号顺序不完全一致。两份契约是**手写的两份常量**
（刻意不做拼接，拼出来的前缀迟早会飘），因此有一条扫描测试盯着两边条数一致 ——
漏在一份里的规则，只在那个 locale 下出事。

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
| `daily` | `now` `days_until` `calc` | 8000ms | 无 | **已实现** |
| `weather` | `weather` | 9000ms | 无 | **已实现** |
| `math` | `calc` `currency` | 9000ms | 无 | **已实现** |
| `list` | `list_show` `list_add` `list_remove` | 8000ms | **有** | **已实现** |
| `device` | `device` | 6000ms | 无 | **已实现** |
| `remind` | `remind_set` `remind_list` `remind_cancel` `list_add` | 8000ms | **有** | **已实现** |

两个写档的预算和 `daily` 齐平（8000ms），比只读档宽：形状是一样的两次模型往返，
但**超时的代价不一样** —— 只读档超时是没答上，写档超时是用户交代的事没办成，
而屏幕上只有一句「一时答不上来」。实测撞到过一次 DeepSeek 长尾，
「明天九点提醒我看牙医」整条掉在 6s 上。

路由顺序即优先级：`remind` → `list` → `device` → `weather` → `math` → `daily` → `ask`。

**路由只判意图，可行性交给 skill。** 这一条是改过的：`remind` 的正则一开始带了
「有没有说时间」的判据，于是「下个月提醒我换护照」落到 `list` 档 —— 而 `list` 档
没有提醒工具，模型照着契约第 1 条老老实实回了句「我还不会设提醒」。**它会**，
只是这一条超出了 24 小时。说自己做不到一件做得到的事，和编一个答案一样糟：
用户不会再问第二次。

所以现在「提醒我 / remind me / wake me」一律进 `remind` 档，由 skill 自己决定是
`remind_set`（24 小时内、说得出钟点）还是 `list_add`（记进待办，并**如实说明**
记下来的不是提醒）—— 这是它的工具能不能兑现的问题，正则判不了，也不该判。
`remind` 档因此多了 `list_add`：没有这条退路，它只剩「做不到」可说。
「叫我 / tell me / ping me」这些说法太泛，仍然要跟一个时间表达式才算数。

「取消」也一律进这一档：这个 agent 只有**一样**东西是它自己能取消的。
取消别的（会议、订阅）它本来就该说做不到，而只有 `remind` 档说得出为什么。
`list` 排第一是因为它是唯一会**真的改状态**的一档 —— 用户说「帮我记一下明天买牛奶」
时那句话同时沾「明天」（daily），被 daily 抢走的后果是「模型嘴上说记住了、
其实什么都没发生」，而用户会以为记住了。

`weather` 从 `daily` 里独立出来了：它是唯一一个要走公网的 skill，把两者混在
一个 skill 里等于让「现在几点」也背上外网往返的预算。

`ask` 无工具是刻意的——它是最高频路径，跳过工具编排能拿到最低的首字延迟。
代价是路由漏判时它只能心算；这是有意选择的方向：**漏判退回无工具档（更安全），
而不是误升权限**。

#### 预算是按「模型往返次数」定的，不是按工具耗时

一次工具调用之后还要再问一次模型，所以 `now → calc → 组织回答` 就是**三次**
`llm.complete`。实测 DeepSeek 单次首字延迟中位数 0.84s、长尾 3.5s，于是：

- `MAX_TURNS` 从 3 提到 4 —— 3 时「离圣诞还有几天」稳定撞「工具轮次用尽」，
  用户看到的是一句道歉而不是答案。真正的护栏是 deadline（按墙钟掐），
  轮次上限只该防死循环。
- `daily` 6000 → 8000ms，`math` 7000 → 9000ms。
- 更重要的是**减少往返**：加 `days_until` 之后「离圣诞还有几天」从
  now→calc→答（3 次）变成 days_until→答（2 次），2.17s 就答完了。
  工具设计的目标不只是「能做到」，而是**常见问题一次调用就够**。

#### 提醒：状态在 agent，响铃在网关

一条提醒要活在两个地方之间：**谁记着它**，和**谁到点把它显示出来**。
这两件事分给了不同的进程，理由是各自的所有权：

- **状态与计时在 agent**。它才知道有哪些提醒，`remind_list` / `remind_cancel`
  也只有它能兑现。落盘在 `~/.lens-agent/reminders.json`（闸 3 的第二个固定资源），
  进程重启后从磁盘恢复。
- **响铃在网关**。屏幕是它的。到点时 agent 通过**已有的那条 WS** 发一条
  `event: "notify"` —— 这是协议里唯一一条不属于任何 run 的事件，因为那一刻
  那一轮早就结束了，没有 runId 可以挂靠。网关收到后走**外部渲染租约**（W1）写屏，
  和 MCP 写屏是同一条路：这样「提醒该不该抢正在进行的对话」这个问题，
  和「两个 MCP 客户端抢屏」用的是同一套仲裁规则。屏幕被别人拿着时提醒**不抢**
  —— 抢屏比迟到更糟。

反过来（网关计时、agent 只解析意图）也能跑，但状态会变成两份：取消要两头同步、
重启要两头恢复。两份状态迟早分叉，而分叉的症状是**「它说取消了，结果还是响了」**。

**宽限期**：断连或重启期间到点的提醒，晚 5 分钟以内仍然补发，再晚就不发
（`reminders.GRACE_SECONDS`）—— 一台关了一夜的机器不该在早上把昨晚所有提醒
一次性糊到屏幕上，但断连三十秒里错过的那条，补发才是对的。

**钟点换算在工具里，不在模型里。**「明天早上九点提醒我」走的是 `at="09:00"`，
`day` 省略时取**下一次**出现的那个钟点（现在 21:09 说「九点」指明早）。
让模型自己把钟点换算成分钟数是行不通的：算错的症状**要到几小时后才出现**，
而它设的那一刻回的仍然是「好的」。同理 `minutes` 和 `at` 同时给出时直接拒绝，
不猜哪个是用户的意思。

这里踩过三个都不会报错的坑，前两个是变异测试挖出来的，第三个是拿真链路
一句一句问出来的：

1. 提醒 id 原来用毫秒时间戳取模，同一秒里连排两条会撞；而排程器看到相同 id
   会把前一条**取消**再排新的 ⇒ `remind_list` 说有两条，实际只响一条。
2. 一条响完之后要从磁盘划掉，但那次写盘**用默认读法**读回列表 ——
   默认读法会丢掉所有已到点的条目，于是这次保存把别的、还在宽限期里等着补发的
   提醒一起抹掉了。它们再也不会响，磁盘上连痕迹都不剩。
3. **取消被当成了「响过了」。** 上面那段划掉磁盘的收尾原来写在 `finally` 里，
   于是任务被 `cancel()` 时也会跑一遍。而取消的来源有两个都是无辜的：网关重连时
   的幂等恢复（重排同一条 ⇒ 先取消再排），和进程退出时的 `cancel_all()`。
   前者的症状是**设完提醒、下一句问「有什么提醒」答「一条都没有」**，而内存里
   那条其实还会响 —— 两个互相矛盾的症状，都不报错；后者更狠：每次重启都可能
   把所有待响的提醒清空，而且是竞态的（收尾跑得赢就清空，跑不赢就留着）。

   修法是两层：`_fire` 的取消分支不再碰磁盘（真正该划掉的只有「响过了」，
   而「用户取消了」由 `remind_cancel` 自己写盘），`schedule()` 对已经排着的
   同一个 id 直接跳过（恢复扫描本来就该是幂等的，不该产生取消）。
   两层各自都能挡住这个 bug —— 回归测试把两层同时摘掉才会红。

### 9.3 写操作必须回显

`list` 档完成后，最终回复必须包含**实际写入的内容**，例如
「已记下：周四交报销单」。工具的返回值本身就是这么写的
（「已把「牛奶」加到清单「shopping」，现在共 2 条」），模型照着复述即可。用户在眼镜上一瞥就能发现记错了，
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
