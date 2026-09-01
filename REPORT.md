# 交付报告：阶段一（模拟器闭环）+ 阶段二（真机 MVP）+ 模拟阶段全量开发（M0–M7）

> 2026-08-31 · v0.7.0
> 仓库：https://github.com/xesws/EvenRealities-Claw （本文件 = 仓库根目录 `REPORT.md`）
> 服务器：AWS EC2，服务端口 `8443`
>
> 本文档里的 `<SERVER_IP>` 与 `<EC2_INSTANCE_ID>` 是占位符 —— 仓库是公开的，
> 把运行中主机的公网地址和实例 ID 写进去等于对外发布攻击面。真实值在部署机上的
> `DEPLOYMENT.local.md`（`.gitignore` 已排除），照着替换即可。

## TL;DR

**整条链路已开发完成、部署为常驻服务、并实弹验证通过**：按住说话 → 中文语音 → faster-whisper 转写 → 真实工部 agent 回复 → 眼镜 HUD 分页渲染。生产冒烟全程 11 秒，状态机 S2→S3→S4→S6→S7 完整走通。

**这副眼镜现在也是一台 MCP 设备**：`claude mcp add --transport http even-glasses http://127.0.0.1:8765/mcp`
之后，任意厂商的模型都能用 8 个标准 MCP 工具驱动它——按 G2 真实像素版式分页、写屏、翻页、读遥测。
屏幕只有一块，写屏要过**帧租约**，用户开口说话无条件抢占。四进程真链路端到端 **27/27**
（帧真的从设备 WebSocket 出来，不是只改了服务器状态）——详见 [docs/MCP-SURFACE.md](docs/MCP-SURFACE.md)。

**回答你的那个 agent 现在是我们自己的**（M6）。`gateway/lens_agent/` 是一个约 900 行、
能一次读完的 agent：直连 DeepSeek 的 OpenAI 兼容端点，**技能路由由确定性代码决定、
不让模型选技能**，能力枚举只有读/写两档、**没有 exec**，每次工具调用都进审计日志。
设计阶段留的四个问号全部实测打靶（`stream` 与 `tool_calls` 能并存、关掉 thinking 省 2.8s
且少付 79 个 token、`reasoning_content` 确实出现在流式 delta 里所以必须显式拦），
结论回填在 [docs/AGENT-LAYER.md](docs/AGENT-LAYER.md) §13.1。

**而且屏幕会自己告状**（W6 agent 溯源）。握手时记下对端身份，`/healthz` 与控制面
`state` 原样暴露；**对端不是生产 agent 时，眼镜状态条的徽记会带一个「?」**。
演示时可以当场 `curl /healthz` 自证「没有替身」—— 这条约束是被测试守住的，
不是靠自觉：`e2e_sim.py` 用夹具跑时会断言每一条状态条都带「?」，
`e2e_agent.py` 用真 DeepSeek 跑时断言一条都不带。
徽记是**每帧现算**而不是一轮取样一次——取样式写法会在冷启动和断线重连时留下一个窗口，
而窗口里的那几帧恰恰是替身在答话却不打标（这条是提交前的对抗式评审找出来的，见 §13.6）。

**你拿到眼镜后要做的全部事情（共 ~10 分钟）：**
1. AWS 控制台放行 8443 端口（→ 第 5.1 节）
2. `npx @evenrealities/evenhub-cli qr --url http://<SERVER_IP>:8443/plugin/` 生成二维码，Even App 扫码（→ 第 5.2 节）
3. 服务器跑一条命令拿 6 位配对码，手机输入（→ 第 5.3 节）

之后：手机亮屏开插件 → 按住说话 → 抬眼看眼镜。没有眼镜时也可以先用浏览器模拟器体验（→ 第 5.6 节）。

---

## 目录

1. [系统形态与仓库地图](#1-系统形态与仓库地图)
2. [各组件详细说明](#2-各组件详细说明)
3. [一次问答的完整数据流（含实测耗时）](#3-一次问答的完整数据流)
4. [验证结果汇总（pytest 590 + vitest 82 + 语音 e2e 32 + MCP e2e 27 + 真 agent e2e 23）](#4-验证结果汇总)
5. [【最重要】拿到眼镜后的完整上手流程](#5-拿到眼镜后的完整上手流程)
6. [运维手册：服务、配置、日志、更新、**MCP 接入**](#6-运维手册)
7. [排障速查表](#7-排障速查表)
8. [安全模型](#8-安全模型)
9. [如实声明：未完成项与已知限制](#9-未完成项与已知限制)
10. [眼镜到手后的五项实测清单（附操作方法）](#10-五项真机实测清单)
11. [开发过程中发现并修复的工程问题](#11-工程问题记录)
12. [阶段三预告](#12-阶段三预告)
13. [未验证项清单 · 含**提交前的对抗式评审记录**](#13-未验证项清单)

---

## 1. 系统形态与仓库地图

```
┌─────────┐ BLE(官方桥) ┌────────────────────┐  WS:8443   ┌──────────────────────────────────┐
│ G2 眼镜  │◀──────────▶│ 手机 Even App       │◀──────────▶│ EC2 服务器 (<SERVER_IP>)          │
│ mic/HUD │             │  └ OpenClaw Lens    │  设备 JWT   │  lens-gateway (systemd 用户服务)   │
└─────────┘             │    插件(WebView)     │            │   ├ /ws       插件接入             │
     ↑ 单击翻页/双击退出  │    按住说话+预览屏    │            │   ├ /plugin/  托管插件本体          │
                        └────────────────────┘            │   ├ /healthz  健康探针             │
                                                          │   ├ /admin/*  配对码等(Bearer)     │
                                                          │   └ /control/* 控制面(Bearer)  ◀──┼─┐
                                                          │      ↓ loopback (token 不出服务器) │ │
                                                          │  OpenClaw 网关 → 工部 agent        │ │
                                                          └──────────────────────────────────┘ │
                                            ┌───────────────────────────────────────────┐      │
   厂商模型 / Claude Code ──MCP Streamable──▶│ lens_mcp（独立进程 :8765）                  │──────┘
   任意 MCP 客户端            HTTP  /mcp     │ 8 tools · 3 resources · 1 prompt          │ 控制面 HTTP
                                            │ 不持有 mic / ASR / 设备凭证                 │
                                            └───────────────────────────────────────────┘
```

仓库文件地图（你需要关心的部分）：

```
EvenRealities-Claw/
├── REPORT.md                  ← 本报告
├── README.md                  ← 项目入口与文档索引
├── protocol/PROTOCOL.md       ← 插件↔网关 WS 协议 v1.1（认证/渲染帧/遥测上行/时序图）
├── gateway/                   ← 服务器端（Python 3.9 / aiohttp）
│   ├── lens_gateway/
│   │   ├── server.py          ← HTTP/WS 服务与路由 + 会话 TTL 回收
│   │   ├── session.py         ← 装配层：一块屏 + 一条语音链路（只做路由，无业务逻辑）
│   │   ├── device/hud.py      ← 设备抽象：帧构造/节流/状态条/分页/计时器/**帧租约**
│   │   ├── voice/pipeline.py  ← 语音链路：PTT → PCM → ASR → 确认窗口 → agent → 流式上屏
│   │   ├── asr.py             ← faster-whisper 双模型管线 + 热词回声守卫
│   │   ├── providers/         ← agent 抽象：网关对"对面是谁"只认这 6 个方法
│   │   │   ├── base.py        ←   AgentProvider 协议 + AgentInfo（W6 溯源）
│   │   │   ├── openclaw.py    ←   OpenClaw 适配器
│   │   │   └── lens.py        ←   自研 agent 适配器
│   │   ├── formatting/        ← 排版引擎（像素盒分页，与官方 pretext 度量逐条对齐）
│   │   │   ├── metrics.py     ←   G2 字形度量的 Python 复刻（advance+kerning+逐字取整）
│   │   │   ├── wrap.py        ←   折行 + 中文禁则（行首/行尾禁排，追出与悬挂）
│   │   │   ├── paginate.py    ←   像素盒分页（8 行/页）+ 锚点 + 页脚
│   │   │   ├── layout.py      ←   容器版式（读 protocol/hud-contract.json）
│   │   │   ├── glyphs.py      ←   语义字形表 + import 时在库校验
│   │   │   ├── sanitize.py    ←   控制字符/双向覆盖/伪状态条剔除
│   │   │   └── markdown.py    ←   markdown 降级
│   │   ├── auth.py            ← 配对码/设备 JWT/吊销 + 配对失败节流
│   │   ├── control.py         ← 控制面：/control/* 九个路由（MCP 进程的全部权限边界）
│   │   ├── config.py          ← 配置定义（运行时配置在 ~/.lens-gateway/）
│   │   └── main.py            ← CLI：serve / pair-code / devices / revoke
│   ├── lens_mcp/              ← **MCP 表面（独立进程）**：8 tools / 3 resources / 1 prompt
│   │   ├── server.py          ←   工具定义与描述（三条事实逐字写进 description）
│   │   └── client.py          ←   控制面 HTTP 客户端（Bearer 共享密钥）
│   ├── lens_agent/            ← **自研 agent（独立进程）**：~900 行，可整体搬走
│   │   ├── server.py          ←   Lens Agent Protocol v1（WS，只监听回环）
│   │   ├── loop.py            ←   手写 agent loop（轮次上限 + 预算 + 降级收尾）
│   │   ├── policy.py          ←   **全系统唯一的授权点**（白名单，不是黑名单）
│   │   ├── skills.py          ←   skill = 系统提示 + 工具子集 + 预算；**路由由代码决定**
│   │   ├── tools.py           ←   能力枚举只有 READ/WRITE，**没有 exec 档**
│   │   ├── audit.py           ←   每次工具调用一行 JSON
│   │   └── llm/deepseek.py    ←   DeepSeek OpenAI 兼容端点（aiohttp 直发，理由见 AGENT-LAYER §6.2）
│   ├── tests/                 ← 590 单测 + e2e_sim.py（语音 32 项）+ e2e_mcp.py（MCP 27 项）
│   │                            + e2e_agent.py（真 agent 23 项）+ data/（语音数据集 / HUD golden）
│   ├── requirements.txt
│   ├── requirements-mcp.txt   ← MCP 表面的依赖（网关本身不需要）
│   └── README.md              ← 网关模块说明与实测数据
├── plugin/                    ← 手机端插件（TypeScript / Vite / 官方 SDK ^0.0.14）
│   ├── app.json               ← Even Hub 清单（含 g2-microphone 权限）
│   ├── src/                   ← glasses.ts / ws.ts / ui.ts / store.ts / main.ts / types.ts
│   ├── harness/               ← 浏览器模拟器（mock 官方桥 + 假眼镜屏）
│   └── README.md              ← 插件开发/构建/扫码说明
├── deploy/lens-gateway.service ← systemd 用户服务单元
├── scripts/install-service.sh  ← 一键安装服务
└── docs/                      ← DESIGN.md（系统设计）/ MCP-SURFACE.md（MCP 表面）/
                               HARDWARE-SPEC.md（G2 权威规格）/ GLYPH-TABLE.md（字形判定）/
                               SIMULATOR-PARITY.md（三方对照）/ DEVELOPMENT-PLAN.md
```

服务器上的运行位置（不在仓库里）：

| 路径 | 内容 |
|---|---|
| `~/EvenRealities-Claw/` | 仓库工作副本（gateway/.venv 虚拟环境、plugin/dist 构建产物在此，均不入 git） |
| `~/.lens-gateway/` | 网关状态目录：`config.json`（可调参数）、`devices.json`（已配对设备）、`jwt.secret`、`control.secret`（控制面共享密钥，0600） |
| `~/.config/systemd/user/lens-gateway.service` | 服务单元（已 enable，开机自启、崩溃自拉起） |
| `~/.cache/huggingface/` | whisper 模型缓存（tiny + base，已下载） |

---

## 2. 各组件详细说明

### 2.1 Lens Gateway（服务器端，已部署运行）

**接入与认证（`server.py` + `auth.py`）**
- WS 连接首帧必须是 `pair`（带 6 位配对码）或 `hello`（带 accessToken）或 `refresh`（带 refreshToken），10 秒内未认证即断开；
- 配对码由 `main.py pair-code` 生成：一次性、10 分钟有效，经仅监听 loopback 的 `/admin/pair-code` 写入运行中的服务；
- 配对成功发 `pair_ok`：`deviceId` + `accessToken`（HS256 JWT，15 分钟）+ `refreshToken`（一次性下发，服务端只存 SHA-256 哈希）；
- 设备库 `~/.lens-gateway/devices.json`（0600 权限），`revoke` 子命令单设备吊销（access 与 refresh 同时失效）；
- 单设备单连接：同 deviceId 新连接会挤掉旧连接（code 4000），杜绝双连接竞态；
- 认证后服务器立即下发 `hello_ok`，其中 `resume` 字段是**当前 HUD 帧的原样重放**——断线重连后客户端渲染这一帧即恢复现场，agent 在断线期间继续干活。

**ASR 管线（`asr.py`）**
- 双模型：`tiny`(int8) 做聆听态 partial（只供 HUD 显示，平均 ~670ms 一跳）；`base`(int8) 做松手后的 final 重解码（**路由与发送只认 final**），实测 RTF≈0.35（4 秒语音 ≈1.4 秒出文本）。`small` 实测 RTF≈1.0，弃用（质量增益不值一倍延迟）；
- 热词：`initial_prompt` 注入「工部、格物、都察、Hermes、OpenClaw、小龙虾、眼镜、链路、网关」——实测把"眼睛练路"修正为"眼镜链路"、"OpenClaw"拼写全对；
- local-agreement：连续两次 partial 的公共前缀视为稳定文本，聆听屏不回改、不跳变；
- partial 只解码最近 12 秒尾部（控制 CPU）；说话软上限 25 秒自动截停（规避 G2 audioControl 未知时长上限，红队 R10）；
- **全局解码串行锁**：两个 ctranslate2 实例并发解码会在 4 核 ARM 上互锁（实测踩坑，见第 11 节），全部解码（含启动 warmup）严格串行。

**HUD 状态机（`device/hud.py`）——按设计文档「一瞥 HUD」实现**

| 状态 | 状态条 | 行为 |
|---|---|---|
| S0 待机 | `·` | 全黑屏，只留一个在线点 |
| S2 聆听 | `工 ◉ 聆听 0:07` | partial 滚动窗实时上屏（带 `▌` 光标）；>0.8s 无音频帧报「麦克风没有声音」（mic 被抢看门狗，R2） |
| S3 确认 | `→ 工部` | final 转写全文停留 1.2s（低置信 3s + 提示「请核对文字…」）后自动发送；期间重按 PTT = 重说 |
| S4 思考 | `工 ◔ 思考 6s` | 1Hz 计秒；>30s 加提示「仍在思考·点打断可停止」（长任务合法，不自动放弃） |
| S6 流式 | `工 ▸ 回答` | 回复实时分页上屏，自动跟最新页；手动回翻则暂停跟随（页脚 `⏸`），翻回末页恢复 |
| S7 阅读 | `工 ✓ 完成` | 页脚 `2/3 ›`；短回复 15s / 长回复 60s 无操作渐回待机 |
| S8 错误 | `工 ✕ …` | 未听清/占用/agent 错误，各带下一步动作提示，自动回落 |

- 帧规则：**状态切换帧免节流立即下发**（感知延迟优先，R9）；同状态内容帧 ≥500ms 间隔（≤2Hz，BLE 预算内），发送队列只留最新一帧（coalescing）；seq 单调递增，客户端丢弃旧帧；
- 会话对象跨 WS 重连存活（服务器持有全部状态，R1/R6）；
- 与工部的会话用独立 sessionKey `lens:<deviceId>`（与 Discord 会话完全隔离，R7），首条消息注入小屏风格指令：先结论后细节、短句、禁 markdown/表格、非必要 ≤170 字。

**排版引擎（`lens_gateway/formatting/`）**

不是「按字符数猜一个安全宽度」，而是**像素盒 + 真实字形度量**——与固件同一套算法：

- **度量**：`metrics.py` 复刻 LVGL 的 `kerning=(kern*scale)>>4`、`每字宽=(adv+kern+8)>>4`
  （逐字形取整，不是把 1/16px 累加后再取整）。度量表由 `plugin/tools/extract_metrics.mjs`
  从官方 `@evenrealities/pretext` 原样导出，**运行时零 Node 依赖**；
- **外部 oracle**：`tests/test_metrics_oracle.py` 拿官方 pretext 当独立判据，
  在 **17 075 个码点 + 1 376 个折行用例**上逐条比对，**零分歧**；
- **折行**：真实像素宽度 + 中文禁则（行首禁排闭合标点、行尾禁排开放标点，用「追出」解决，
  连续标点退化为悬挂），拉丁长词硬切但不跨行断词；
- **分页**：`maxLines = floor(容器高 / 27)` ⇒ 正文 216px = **8 行/页 ≈ 224 汉字**
  （旧实现按 5 行 85 字排，只用掉 38% 的屏幕）；非首页首行携带上页末句的像素截断锚点；
- **字形**：语义字形表在 **import 时**逐个校验在库，画不出来的字形连进程都起不来
  （见 [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md)）；
- **净化**：C0/C1 控制字符、双向覆盖字符（U+202A–202E、U+2066–2069）一律剔除，
  行首的状态字形也会被剥掉（否则模型在正文第一行写「√ 完成」就伪造出第二条状态条）。

**OpenClaw 适配器（`openclaw.py`）**
- 连 `ws://127.0.0.1:18789`（工部网关，loopback），protocol v3 connect 握手，token 运行时从 `~/.openclaw/openclaw.json` 读取——**手机端永远拿不到这个 token**；
- `chat.send` → 立即返回 runId → 流式 `chat` 事件（delta/final/error）；delta 同时兼容"累计全文"与"增量块"两种形态（自动识别）；
- `chat.abort` 打断 + runId 僵尸标记：被打断的 run 后续一切迟到事件直接丢弃（R14），新 run 不会被旧事件污染；
- 上一条还在跑时新提问不发送，提示「上一条还在跑，点打断后再说」（避免误杀有写操作的任务）。

### 2.2 眼镜插件（`plugin/`，已构建并由网关托管）

**眼镜渲染（`glasses.ts`）**
- 启动时按协议布局契约（`protocol/hud-contract.json`，网关与插件读同一份）调 `createStartUpPageContainer` 建 3 个文本容器：状态条(0,0,576×36, `textColor` 4) / 正文(0,36,576×216 = **27px × 8 行**, 3) / 页脚(0,252,576×36, 2)，失败码（oversize/outOfMemory）直接显示在手机页；
  **只调一次**：官方规定一个页面生命周期内 `createStartUpPageContainer` 不可重复，之后一律 `rebuildPageContainer`；
- `textContainerUpgrade` 写入：120ms 防抖 + 只写内容变化的容器 + 串行写（BLE 渲染队列慢——官方 asr 模板同款策略）；空内容用单个空格兜底（防 protobuf 零值省略吃掉清屏指令）；
- 输入事件：**5 种官方手势 × 4 个事件来源**（左镜腿 / 右镜腿 / R1 戒指 / 未知）都被解析并分开映射——单击 / 下滑=下一页、上滑=上一页、镜腿双击=`shutDownPageContainer(1)` 退出插件、**戒指双击=上一页**（旧版戒指双击直接把插件退掉）、长按与长按释放独立成事件且**故意不绑动作**（SDK 0.0.10 上长按被降级成 CLICK，绑了就是一次长按两次误翻页）。CLICK_EVENT=0 在 protobuf 零值省略下会变 undefined——已按官方模板做归一处理，但**缺 `eventType` 的未知系统事件不再一律归成单击**（否则任何未知事件都变成幽灵翻页）；
- `onDeviceStatusChanged`：眼镜电量/佩戴状态显示在手机页，**并按协议 v1.1 上报给网关**。
  上报前先用 `getDeviceInfo()` 的 `model` + `sn` 判定这是不是眼镜 —— `DeviceStatus` 里
  只有 sn 没有 model，而 R1 戒指与眼镜走同一套推送，不判定就会把戒指电量报成眼镜电量。

**连接层（`ws.ts`）**
- 重连：指数退避 1/2/4/8/16/30s + 抖动，单飞锁（不会重连风暴）；
- seq 过滤：丢弃 ≤ 已渲染 seq 的帧；新连接重置基线（保证 resume 帧能盖掉本地看门狗帧）；
- **本地看门狗**：心跳 20s，两次无 pong → 直接向眼镜推「⛓ 连接丢失·重连中」——这是哑终端唯一自带的逻辑，消灭"链路死了但眼镜还显示旧状态"的撒谎问题（R1 关键缓解），已做专项定时测试（恰在 60s 触发）；
- 凭证：accessToken 只存内存，重启后用 refreshToken（持久存储）换新——收到 `token_expired` 自动刷新重试一次，`auth_failed` 清凭证回配对页；
- PCM 上行合并成 ≤200ms/块，`ptt stop` 前冲洗尾块（不丢最后半个字）。

**手机 UI（`ui.ts`）**
- 配对屏：网关地址自动推导（插件从哪个服务器加载就连哪个 `ws://同源/ws`，一般不用改）+ 6 位配对码输入；
- 主屏：按住说话大按钮（pointerdown/up/cancel 全处理）、●REC 指示、状态行（镜像眼镜状态条）、**576×288 眼镜画面等比实时预览**（眼镜上显示什么手机上就能看到什么，既是调试也是阳光下看不清的兜底）、打断/清屏按钮、眼镜连接状态与电量、设置（改地址/重新配对）。

**浏览器模拟器（`harness/`）**
- 完整 mock 官方宿主桥（信封格式经真 SDK 实测确认：`callHandler('evenAppMessage', …)` 上行 / `window._listenEvenAppMessage` 下行）；
- 假眼镜屏：576×288×1.5 倍，黑底绿字，按容器坐标绝对定位渲染；
- `audioControl(true)` → 浏览器 getUserMedia 真麦克风 → 流式重采样 16kHz s16le → 100ms 块推回插件——**与真眼镜走的代码路径完全一致**；
- 附加按钮：模拟单击/双击镜腿、模拟断网（验证看门狗）。

### 2.3 MCP 表面（`gateway/lens_mcp/`，独立进程）

把一副物理眼镜的能力抽象成标准 MCP Tools，让**任何厂商的模型**都能驱动它。
完整说明见 [docs/MCP-SURFACE.md](docs/MCP-SURFACE.md)，这里只讲三件事：

**为什么是独立进程。** 技术上，官方 `mcp` SDK 的 Streamable HTTP 是 ASGI/Starlette 而网关是
aiohttp，同进程跑不了；更重要的是安全——MCP 表面是面向外部厂商的攻击面，不该与持有麦克风、
ASR、设备 JWT 签名密钥、OpenClaw 全权 token 的网关待在一起。两个进程之间只有一条控制面 HTTP，
**MCP 进程能做的事等于控制面暴露的九个路由**，越权在架构上不成立。

**三条被逐字写进 tool description 的事实**（模型不读我们的文档，只读描述）：

| 事实 | 为什么必须写给模型 |
|---|---|
| 「监控」只能是轮询 | MCP 2026-07-28 规范下服务器不能主动发起 JSON-RPC 请求，做不到推送。所有读接口返回 `as_of`，否则模型会以为自己订阅上了 |
| 屏幕只有一块，写屏要租约 | 用户按下 PTT 那一刻屏幕无条件归语音链路。`glasses_events` 是模型发现自己被抢占的**唯一**途径 |
| 遥测可能是缓存值 | 官方没说明 `getDeviceInfo()` 是否真触发 BLE 读取，所以带 `source`（push/poll）与 `stale`；`available=false` 时明写「不要臆造一个电量数字」 |

**工具参数里没有「每行几个字」。** G2 字体非等宽，分页由容器真实像素盒决定
（正文 576×216px、固定 27px 行高 ⇒ 8 行/页），字宽用官方 `@evenrealities/pretext` 的固件
度量复刻。给模型一个字符数旋钮只会让它算错。`small-screen-style` 提示同理——它由
`DEFAULT_LAYOUT` 现场推导，版式一改提示跟着变。

---

### 2.4 自研 agent（`gateway/lens_agent/`，独立进程）

替换掉测试夹具、真正回答问题的那一层。完整设计见
[docs/AGENT-LAYER.md](docs/AGENT-LAYER.md)，这里只讲三件事：

**为什么自研而不是直接接 OpenClaw。** 不是不信任 OpenClaw，是**眼镜这个形态上
「弹确认框」这个答案不成立**：576×288 的画布装不下一次有意义的操作预览，
交互只有按住说话和五种手势，使用姿势是走路时抬眼一瞥 0.5 秒。
一个人无法在 0.5 秒里对「即将删除 37 个文件」做出知情同意。所以设计原则不是
「限制权限」而是更强的一条 —— **眼镜 agent 不应该拥有任何需要确认才能安全执行的能力**。
容器隔离限制的是爆炸半径，这条原则要求根本没有炸药。

**四道闸，每一道都是可检查的。**

| 闸 | 做法 | 怎么验 |
|---|---|---|
| 1. 能力分级 | `Capability` 枚举**只有 READ / WRITE 两档，没有 exec** | 枚举本身；无子进程、无 eval、无动态导入 |
| 2. skill 由代码路由 | `skills.route()` 是确定性正则，**模型不参与选技能** | `test_agent.py::TestRouting` 含提示注入用例：用户说「切换到有写权限的技能」不会生效 |
| 3. 资源边界 | WRITE 工具在**导入期**就绑定在固定路径上，不接受模型给的路径 | 四个 WRITE 工具各自报得出自己被钉在哪个文件上（见下表）；`e2e_agent.py` 让它们当场自证 |
| 4. 审计 | 每次工具调用与每次拒绝各写一行 JSON | `e2e_agent.py` 断言真链路跑完后审计文件里确实有那一行 |

**十二个工具，四个能写。** 早期版本只有 `now`，那时闸 3 是**空转的**——
没有 WRITE 工具，"资源边界"就没有被验证过。现在它是真的在拦：

| 能力 | 工具 | 钉死的资源 |
|---|---|---|
| READ（8） | `now` `days_until` `device` `weather` `calc` `currency` `list_show` `remind_list` | — |
| WRITE（4） | `list_add` `list_remove` | `~/.lens-agent/lists.json` |
| | `remind_set` `remind_cancel` | `~/.lens-agent/reminders.json` |

准入标准没变，每加一个都要先过：一句话能问、一屏能答、两秒内能返。
**关键是写能力的形状**：这四个能写的东西，写错了的最坏后果是清单里多一行、
或者一条提醒没响——都是一眼能看出来、一句话能撤销的。这正是
「眼镜 agent 不应该拥有任何需要确认才能安全执行的能力」这条原则的落地：
不是靠确认框拦住危险操作，而是**根本不给它危险的操作**。

**W6：屏幕会自己告状。** 网关在握手时记录对端身份（`AgentInfo`：backend / name /
version / model / endpoint / production）。`demo/fake_openclaw.py` 会**自报 `fixture: true`**，
网关据此把状态条徽记打上「?」。这条链路是被两头夹住的：`e2e_sim.py`（用夹具）断言
每一条状态条都带「?」，`e2e_agent.py`（用真 DeepSeek）断言一条都不带。
所以"演示时接的是不是真 agent"这件事不靠自觉 —— 屏幕上看得见，`curl /healthz` 查得到。

---

## 3. 一次问答的完整数据流

以生产冒烟实测为例（合成语音"请只回复一句话，眼镜链路畅通。"，4.2 秒）：

| # | 环节 | 实测耗时 |
|---|---|---|
| 1 | 手机按下「按住说话」→ 插件 `audioControl(true)` + 发 `ptt start` | — |
| 2 | 网关回 S2 聆听帧（免节流，立即） | <100ms |
| 3 | 眼镜 mic PCM → 官方桥 → 插件合并 200ms 块 → WS 上行 → 网关缓冲 | 持续 |
| 4 | 每 700ms：tiny partial 解码 → local-agreement → S2 帧（转写滚动窗+▌） | ~670ms/跳 |
| 5 | 松手 → `ptt stop` → base final 重解码全段 | 转写上屏 3-4s |
| 6 | S3 确认帧停留 1.2s（此刻可重按重说）→ 注入小屏风格指令 → `chat.send` | — |
| 7 | S4 思考帧 1Hz 计秒；工部生成（**延迟大头，~7s，不可压缩**） | |
| 8 | delta 流式 → 剥 markdown → 折行 → 分页 → S6 帧（≤2Hz coalescing） | |
| 9 | final → S7 完成帧，页脚页码，单击镜腿翻页 | **全程 11.0s** |

---

## 4. 验证结果汇总

| 验证 | 范围 | 结果 |
|---|---|---|
| `gateway/tests/`（pytest，全量） | 下列各专项之和：排版引擎/配对/JWT/设备抽象/遥测/控制面鉴权/控制面路由/MCP 工具/agent 层/agent 工具与提醒/ASR 质量/mic 看门狗/HUD golden/provider 连接生命周期 | **596/596** |
| 排版与认证 | 字形度量/折行禁则/像素盒分页/净化/markdown 降级/版式契约/配对/JWT/吊销/过期/持久化，含 3 宽度 × 31 语料的参数化不变量与 600 例随机模糊 | **170/170** |
| **设备抽象层**（`tests/test_device.py`） | 帧节流与 coalescing、seq 单调、状态迁移、翻页四触发源等价与边界、租约冲突/续租/过期/抢占、外部渲染走同一排版引擎、事件缓冲增量拉取、快照结构 | **30/30** |
| **会话装配与回收**（`tests/test_session.py`） | S5 工具态接活、错误分支、`reset` 重新注入小屏风格、消息路由、会话 TTL 只回收「离线且静默」、启动钩子只注册一次、ASR warmup 幂等 | **19/19** |
| **provider 选择与 W6 溯源**（`tests/test_providers.py`） | provider 工厂与配置期拒绝未知值、徽记/名字跟着 provider 走、握手自报 `fixture` 的三种嵌套形状、**未连接 = unknown 而不是「假」**（不知道对端是谁时不冤枉它）、连上真 agent 才算可信 | **34/34** |
| **排版引擎 vs 官方 pretext** | 17 075 码点的 advance + 1 376 个折行用例逐条比对（外部 oracle） | **零分歧** |
| 插件构建链 | `npm install && tsc --noEmit && vite build`（strict 模式） | 全绿 |
| 插件桥接冒烟（vitest + jsdom） | 真 SDK + 真 `GlassesController` + 保真夹具：建页只能一次/rebuild 接力、写失败不毒化去重缓存、BLE 卡死 5s 超时、缺字静默丢弃、折行与 pretext 一致、溢出裁行、前台进出 vs 真退出、5 手势 × 4 来源、未知 eventType 不变幽灵翻页、**遥测组装与 R1 戒指过滤**、麦被抢 | **30/30** |
| **★ 真 agent 端到端** `tests/e2e_agent.py` | 三进程：真 `lens_agent` → 真 DeepSeek → 真网关 → 真设备 WS。灌真实语音走完 ASR，断言答案里有真的当前时间（工具真的被调用了）、审计日志真的落了、状态条徽记**没有**「?」、全程没收到 `reasoning_content`、帧满足全部约束，以及**闸 1/闸 3 的现场自证**（能力枚举里没有 exec 档、每个写工具都报得出自己被钉在哪个文件上） | **23/23** |
| **遥测上行通路**（`tests/test_telemetry.py`） | 无数据返回 None 而非零值、SN 出网关只留后 4 位、戒指整条拒收并计数、未确认型号同样拒收、字段白名单、poll 标注"可能是缓存"、过期仍返回最后已知值、cmd/cmd_result 一次性与重连作废、失败回执不覆盖已知值、低电量页脚只出现一次 | **28/28** |
| **控制面鉴权**（`tests/test_control_auth.py`） | Bearer 共享密钥（多空格/大小写/非 ASCII token 不再 500）、反代后 loopback 判据失效的回归、配对码失败节流（按来源锁定 + 全局上限）、`trust_forwarded_for` 关闭时不认 XFF | **22/22** |
| **控制面路由**（`tests/test_control_plane.py`） | 九个路由的正常与错误分支：未知设备 vs 从未连过、租约冲突 409 结构化体、正文超限 413、所有读接口带 `as_of`、事件游标增量 | **22/22** |
| **MCP 工具层**（`tests/test_mcp.py`） | `server.call_tool(...)` → `lens_mcp.client` → HTTP → `control.py` → `DeviceSession` → `HudDevice` → 帧的真实链路，无打桩：8 个工具 + 3 个资源 + 1 个提示 + 控制面不可达时的降级返回 | **18/18** |
| **★ MCP 四进程真链路** `tests/e2e_mcp.py` | 真 MCP 客户端 → 真 `lens_mcp` 进程 → 真网关进程 → 真设备 WebSocket。断言落在最远端：调完工具后**帧必须真的从设备 WS 出来**；并发写屏返回 `LEASE_HELD` 而非最后写入者赢；`ptt start` 抢占后原租约 `LEASE_INVALID` 且事件可轮询到 | **27/27** |
| 插件字形与契约 | 用官方 pretext 逐字校验所有会上屏的字符；反向断言被替换的 10 个旧字形确实缺失；版式自洽 | **10/10** |
| **官方模拟器实测** `tools/g2probe.mjs` | 8 屏自动化：满画布建页返回码、缺字渲染、26 个字形逐格墨迹判定、内容上限字节/字符口径 | 见 [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md)、[docs/HARDWARE-SPEC.md](docs/HARDWARE-SPEC.md) |
| **自研 agent 层**（`tests/test_agent.py`） | 思维链不上屏（3 条路径）、`tool_calls` 分片装配、模型 id 与 key 读取、技能路由的提示注入抗性、policy 白名单、工具能力档、loop 的降级与轮次上限、系统提示前缀字节稳定 | **67/67** |
| **agent 工具与四道闸**（`tests/test_agent_tools.py`） | 12 个工具的真实执行，无打桩：`calc` 的注入表（`__import__` / 属性访问 / `9**9**9` 全被 AST 白名单挡住）、`days_until` 跨月跨年、`device` 没遥测时只说不知道、清单增删的全局查找与歧义拒绝、四道闸各自的构造期与运行期断言、路由表逐条、提示注入改不了 skill | **87/87** |
| **提醒排程**（`tests/test_agent_reminders.py`） | 排程/取消/列出、钟点换算（`at=09:00` 取下一次出现）、进程重启后按宽限期补发、id 不碰撞、会话之间互不可见、24 小时上限、一条响完不抹掉别的还在宽限期里等着的、**重连恢复与进程退出都不许清空磁盘**、**送不出去不许当成响过了**、**一条调试 CLI 连上来不许挤掉网关的通知通道** | **42/42** |
| **ASR 质量**（`tests/test_asr_quality.py`） | 自建 10 条语音数据集（edge-tts，3 个音色，带 ground truth）跑**生产** `AsrEngine.final()`：CER 均值 0.0085（阈值 0.05）、最差 0.50 上限、弃转不计入错误但有上限、**热词回声零容忍** | **15/15** |
| **mic 看门狗**（`tests/test_voice.py`） | 启麦慢与链路断用两个判据（此前混用一个硬编码 1.0s）；1.4s 处不误报的回归；预算非法值在加载期拒绝 | **11/11** |
| **HUD 帧序列 golden**（`tests/test_hud_golden.py`） | 13 个场景 68 帧快照 + 每帧硬性不变量：**所有字形都在 G2 字库内**、行宽不超容器、行数不超 `floor(h/27)`、无裸 markdown、seq 单调、页脚与 `meta.page` 同源、W6 徽记全程一致 | **25/25** |
| **插件 WS 协议冒烟（存根网关）** | 配对→resume→翻页→PTT 上行→PCM 合并→心跳看门狗→退避重连→自动 refresh→旧 seq 丢弃→未知消息容忍。打桩只到传输层，被测的是真的 `LensClient`；**5 个变异测试确认这套断言会咬** | **28/28** |
| **插件 PCM 载荷契约** | 实测钉住 SDK 对 `number[]` / base64 / `Uint8Array` 三种载荷 × 三种信封的归一行为；解不出来的形状不崩、不把坏数据交给上行、并留下可排障的日志 | **10/10** |
| **按住说话状态机**（`tests/ptt.test.ts`） | 手机按钮与镜腿长按共用一个状态机：先开麦确认成功才发 `ptt start`（B2/B3 回归）、开麦失败不发 start、**松手事件丢了由看门狗兜底关麦**、看门狗默认 30s 刻意晚于网关 25s 软上限、stop/cancel 两条路都清定时器、两条入口混用不双开双关；另有 7 条**接线**用例把 `LONG_PRESS(9)`/`LONG_PRESS_RELEASE(10)` 从宿主 SDK 事件一路走到 UI，并回归四条既有收尾路径。**5 个变异（掏空看门狗 / 调回 start 顺序 / 去掉防双开 / 不清定时器 / 长按不接线）逐个打红** | **17/17** |
| **端到端闭环** `tests/e2e_sim.py` | 真服务进程 + 真 ASR + agent 测试夹具（`demo/fake_openclaw.py`，protocol v3 同一套，仅回复内容来自剧本）：配对→PTT→灌真实语音→转写→回复→帧约束（seq 单调/行宽/容器结构）→翻页→重连恢复→reset。**自足运行，不依赖任何仓库外服务**；打真 agent 见 §6.6 | 见下方运行输出 |
| **生产部署冒烟** | systemd 正式实例（非测试实例）整轮问答 | S2→S3→S4→S6→S7，11.0s |

**CI 已建**（`.github/workflows/ci.yml`，M7）：插件 typecheck + vitest + build、
网关 pytest、端到端（模拟链路 + MCP 四进程）、`systemd-analyze verify deploy/*.service` 四个 job。
最后那个是新加的，理由与它守的东西同源 —— unit 写错的表现是**开机时静默失败**，
人只会在「重启之后眼镜连不上」时才发现，跟改坏的那一行隔着几天。其中有一道刻意加的闸门 ——
排版引擎的外部 oracle 需要 node 与 `@evenrealities/pretext`，缺了会整模块 skip，
那样 CI 会绿而「我们的折行与官方一致」这个最关键的结论根本没被验证过；
所以 CI 跑完会解析 junit xml，**oracle 只要有一条 skip 就判失败**。
真 agent 端到端（`e2e_agent.py`）**故意不进 CI**：它会产生真实付费调用，
放进去等于每个 PR 都花钱，且外部服务抖动会把构建结果变成噪音 —— 它是发版前手动跑的验收项。

当前服务状态（交付时刻）：`{"ok": true, "asr_ready": true, "openclaw": true}`，服务 enabled（开机自启）+ active。

---

## 5. 拿到眼镜后的完整上手流程

### 5.0 前提
- G2 已用官方 **Even Realities App** 完成蓝牙配对（官方 App 自身功能正常，能在眼镜上看到时间等）；
- 手机有网（4G/5G/WiFi 均可）。

### 5.1 一次性准备 A：放行服务器端口（~5 分钟，只有你能做）

服务已在 8443 监听，但 AWS 安全组未放行：

1. AWS 控制台 → EC2 → 实例 **`<EC2_INSTANCE_ID>`** → 「安全」标签 → 点安全组 → **编辑入站规则** → 添加规则：
   - 类型 `自定义 TCP`，端口 `8443`，来源 `0.0.0.0/0`（最简单；想更稳妥就填手机运营商网段）
2. **验证**：手机浏览器打开 `http://<SERVER_IP>:8443/healthz`
   - 期望看到：`{"ok": true, "asr_ready": true, "openclaw": true, ...}`
   - `asr_ready: false` 说明服务刚重启还在热身，等 1 分钟刷新。

**强烈建议顺手做**（防长对话越聊越卡）：同实例页 → 操作 → 实例设置 → **更改积分规格 → Unlimited**。这台是 t4g 突发型实例，ASR 是持续 CPU 负载，积分烧光后所有转写延迟会恶化 3-5 倍（红队 R4，开发期间已摸到边）。

> 有域名之后这一步会变：改成放行 **80 + 443**（Let's Encrypt 要从公网访问进来），
> 并把 8443 的公网入站**关掉** —— 那时网关只监听 `127.0.0.1`，那条规则留着只会让人以为它还通。
> 装机脚本见 6.4。

### 5.2 一次性准备 B：把插件装进 Even App（~3 分钟）

官方开发者模式 = 扫码加载插件 URL：

1. 在任何装了 Node.js 的电脑上（或 SSH 到服务器上）执行：
   ```bash
   npx @evenrealities/evenhub-cli qr --url http://<SERVER_IP>:8443/plugin/
   ```
   终端会打印一个二维码（加 `--external` 可生成图片文件）；
2. 手机打开 **Even Realities App** → 找到扫码/开发者入口 → 扫这个码；
3. App 在内置 WebView 里加载插件 → 看到深色「OpenClaw Lens」配对页 = 成功。

> **备选路径**（若你的 App 版本找不到扫码入口）：官方正式分发走打包上传——
> ```bash
> cd plugin && npx @evenrealities/evenhub-cli login        # 用你 Even App 的账号
> npx @evenrealities/evenhub-cli pack app.json dist -o lens.ehpk
> ```
> 然后把 `lens.ehpk` 上传到 https://hub.evenrealities.com 开发者门户的 private build。官方文档原话："Sideload via QR, or upload a private build to the dev portal"。

### 5.3 一次性准备 C：配对（~1 分钟）

1. SSH 到服务器：
   ```bash
   ~/EvenRealities-Claw/gateway/.venv/bin/python -m lens_gateway.main pair-code
   ```
   输出示例：`配对码：847291（10 分钟内有效，一次性）`
2. 手机插件配对页：网关地址**保持默认**（已自动填好），输入 6 位码 → 确认；
3. 进入主屏（大按钮 + 眼镜预览）= 完成。凭证已存手机，之后打开即用。

### 5.4 日常使用

| 想做什么 | 怎么做 |
|---|---|
| **提问** | 手机亮屏停在插件页 → **长按镜腿**说话（≤25 秒）→ 松手 → 抬眼看眼镜 |
| 提问（不想抬手） | 同上，改成**按住手机上的大按钮**。两条路共用一个状态机，混着用不会开两次麦 |
| 重说 | 转写确认的 1.2 秒内重新按住说话 |
| 翻页 | **单击镜腿**；或看手机上的眼镜预览 |
| 打断长回答 | 手机「打断」按钮（已收到的内容保留可翻页） |
| 清屏 | 手机「清屏」按钮 |
| 退出插件 | **双击镜腿**（官方标准手势，会弹确认） |
| 换服务器/重新配对 | 主屏设置入口 |

读屏说明：状态条最左的符号是状态字形（`●`聆听 `◐`思考 `▶`回答 `√`完成 `×`错误 `！`警告）——**瞥一眼就知道系统在干什么**。这些字形全部经官方度量库与官方模拟器截图双重确认在 G2 字库内（早期用的 `◉◔▸✓✕⚠` 在真机上一个都画不出来，见 [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md)）。回复一页 8 行 ≈ 224 汉字，页脚 `‹ 2/3 ›` 是页码。

### 5.5 注意事项（设计如此，不是 bug）

- **手机必须亮屏且停在插件页**。锁屏/切后台后 mic 和连接大概率被系统挂起（Even App 的 WebView 限制，我们控制不了）——此时眼镜会显示「⛓ 连接丢失」而不是假装正常。回到插件页自动重连，断线期间工部仍在干活，回来直接看结果。
- 当前是 http/ws 明文传输（官方 dev 模式支持）。升级 TLS 只差一个域名，模板与装机脚本已经就位：见 6.4。
  **在那之前只在你信得过的网络里用** —— 配对码与 refresh token 都走明文。

### 5.6 没有眼镜也能先玩（现在就行）

安全组放行后，电脑 Chrome 打开：
```
http://<SERVER_IP>:8443/plugin/harness/harness.html
```
允许麦克风 → 页面里有块"假眼镜屏" → 走 5.3 配对 → 按住说话。与真机代码路径完全一致，
还能模拟镜腿点击和断网。

本机一条命令拉起完整链路（三种模式，差别只在 `chat.send` 的对端是谁）：

```bash
export LENS_LLM_API_KEY=sk-...
./demo/start.sh --lens     # 推荐：拉起自研 agent，直连 DeepSeek
./demo/start.sh --lens --en  # 同上，全英文演示
./demo/start.sh --real     # 连本机真的 OpenClaw 网关
./demo/start.sh            # 替身模式，离线调链路用
```

`--en` 一次切三处，缺一处画面就中英混杂：眼镜 HUD 的状态词与 agent 的作答语言
（`composer.locale` + `LENS_AGENT_LOCALE`）、whisper 的解码语言与热词
（`asr.language` / `asr.hotwords` —— 默认是 zh，**拿中文热词当英文语音的
`initial_prompt` 是错的偏置**）、手机端 UI 文案（`?lang=en`，脚本会把它拼进打印的地址）。
眼镜屏上的字**不归插件管**——它由网关整帧下发，插件一个字都不该改，
否则同一帧在手机预览屏和眼镜上会长得不一样。

不用浏览器也能验，两条路：

```bash
cd gateway
.venv/bin/python ../demo/chat.py                      # 直接跟 agent 对话，随便问
.venv/bin/python ../demo/chat.py -f cases.txt         # 一行一题，批量跑
.venv/bin/python ../demo/verify_audio.py ../demo/audio/en-weather.wav
```

- **`demo/chat.py`** 说的就是 agent 自己的协议（§5.1），和网关说的是同一套 ——
  你在这里问什么、怎么问都行，agent 的行为和戴着眼镜说话时完全一致。
  每轮都把过程摊开：代码选中了哪个 skill、拿到哪些工具（闸 2）、**真实发生的工具调用**
  与耗时、以及答案按 G2 的 576×288 真实版式排出来是几页、每页长什么样。
  它不注入任何脚本、不开浏览器 —— 这是判断「这个 agent 到底能干什么」最快的入口。
- **`demo/verify_audio.py`** 自己就是一台设备，拿一段 WAV 当麦克风把**整条**链路
  （whisper → 网关 → agent → 排版 → 帧下发）跑一遍并打印帧序列。
  `demo/audio/` 下有四段现成的英文提问，分别演示真工具调用、长回答、分页，
  以及**写能力**（`en-remind.wav` 配 `--linger=35`：真的排一条提醒，
  20 秒后 `S9 Lens ◆ Reminder` 自己出现在屏幕上）。

⚠️ **替身模式不是可以拿去演示的东西**：`demo/fake_openclaw.py` 在握手里自报
`fixture: true`，网关会据此在状态条徽记上打「?」（W6）。**屏幕自己会告状，这是有意的。**
演示请用 `--lens`，并当场 `curl /healthz` 看 `agent.backend` / `agent.model` /
`agent.production` 自证对端是谁。

---

## 6. 运维手册

### 6.1 服务管理（systemd 用户服务）

两个服务，**互相独立**：

| unit | 是什么 | 什么时候需要 |
|---|---|---|
| `lens-gateway` | 网关（ASR + HUD + 设备） | 总是 |
| `lens-agent` | 自研 agent | 只有 `config.json` 里 `agent.provider = "lens"` 时 |

```bash
systemctl --user status  lens-gateway     # 状态
systemctl --user restart lens-gateway     # 重启（warmup ~1 分钟，healthz 的 asr_ready 为准）
journalctl --user -u lens-gateway -n 100 --no-pager

systemctl --user status  lens-agent       # agent 同上一套
journalctl --user -u lens-agent -f

curl -s http://127.0.0.1:8443/healthz     # 网关健康（本机）
curl -s http://127.0.0.1:18790/healthz    # agent 自报模型与工具清单
```

两个都 enable、崩溃 3 秒自动拉起；内存上限网关 3G、agent 512M。

几件反直觉的事，写在这里省得下次现查：

- **网关不会拉起 agent。** 它对 agent 是纯客户端（懒连接，socket 没了下次说话自己重连），
  所以两个 unit 之间只有 `After=` 排序，没有 `Requires=`。**重启任何一个都不会带走另一个** ——
  这是有意的：绑上依赖的话，重启 agent 会顺手把所有已连眼镜的 WebSocket 一起踢掉。
- 刚重启完 agent，网关 `/healthz` 里的 `agent.connected` 是 `false` **属于正常**，
  第一次说话时会自己连上。想立刻变 `true` 就重启网关（代价见上一条）。
- 用户服务默认只在你登录期间活着。装机脚本会打开 `loginctl enable-linger`，
  没打开的话服务器一重启这两个都不会自己回来。

装（在服务器上，各跑一次即可）：

```bash
bash scripts/install-service.sh          # 网关
bash scripts/install-agent-service.sh    # agent（provider=lens 才需要）
bash scripts/install-agent-service.sh --check   # 只体检，不碰 systemd
```

`install-agent-service.sh` 的 preflight 占了脚本一半篇幅，因为 agent **缺 key 时是起来又立刻挂**，
而 `Restart=always` 会让它每 3 秒重来一次 —— 表现是网关一切正常、只有说话没反应。
它会当场点名缺哪个变量、`.env` 里的 `export` 行 systemd 读不了、以及
**`.env` 里的 `MODEL_NAME` 不被 agent 读取**（要写 `LENS_LLM_MODEL`）。

### 6.2 设备管理

```bash
VENV=~/EvenRealities-Claw/gateway/.venv/bin/python
$VENV -m lens_gateway.main pair-code     # 生成配对码（需服务运行中）
$VENV -m lens_gateway.main devices       # 列出已配对设备
$VENV -m lens_gateway.main revoke dev_xxx  # 吊销某台手机（怀疑凭证泄漏时）
```

### 6.3 配置参考（`~/.lens-gateway/config.json`，改完 restart 生效）

文件默认不存在（全用默认值）；要改就创建，只写要覆盖的键：

```jsonc
{
  "port": 8443,
  "trust_forwarded_for": false,   // 默认不认 X-Forwarded-For。只有确认自己在
                                  // 受信反代之后才打开——否则配对失败节流的
                                  // "来源"可被任意客户端伪造，锁定形同虚设
  "asr": {
    "partial_model": "tiny",      // 聆听态模型
    "final_model": "base",        // 松手后模型（升 small 质量更好但延迟×3）
    "cpu_threads": 4,
    "hotwords": "工部、格物、都察、Hermes、OpenClaw、小龙虾、眼镜、链路、网关。",
    "max_utterance_seconds": 25.0
  },
  "composer": {
    // 注意：**没有** wrap_chars / lines_per_page 这类"每行几个字"的旋钮了。
    // 每行宽度与每页行数由 protocol/hud-contract.json 的像素版式和固件
    // 27px 固定行高唯一决定（官方：分页由容器真实像素盒驱动，不是字符预算）。
    // 旧键仍写在配置里不会让网关起不来——Config.load 会告警并忽略。
    "glyph_profile": "symbol",    // 字形档位：symbol / cjk / ascii
    "glyph_overrides": {},        // 按语义名覆盖单个字形，如 {"listening": "★"}
    "body_safety_px": 0,          // 折行额外退让像素（0 = 与固件度量逐位一致）
    "throttle_ms": 500,           // 内容帧最小间隔 ← 真机刷新实测后调
    "confirm_seconds": 1.2,       // 转写确认停留
    "reading_idle_seconds": 60.0, // 阅读态无操作回待机
    "session_ttl_seconds": 86400, // 离线且静默超过此时长的会话被回收（0 = 永不）
    "telemetry_stale_seconds": 60,// 遥测超过此时长未更新即标 stale（协议 v1.1）
    "battery_warn_percent": 15    // 低于此电量在页脚提示一次（0 = 关闭）
  },
  "openclaw": {
    "url": "ws://127.0.0.1:18789",            // 工部网关
    "config_path": "~/.openclaw/openclaw.json" // token 来源（运行时读）
  }
}
```

### 6.4 升级 TLS（有域名后）

**网关侧一行代码都不用改**：`run_app` 本来就没有 `ssl_context`，监听地址由 `config.json` 决定；
`_client_key()` 早就写好了 `X-Forwarded-For` 分支；控制面鉴权也早从 loopback 判据换成了 Bearer。
所以这一步全是配置。

```bash
# 1. 域名 A 记录 → 本机公网 IP，等 DNS 生效
# 2. 安全组 / ufw 放行 80 与 443（Let's Encrypt 要从公网访问进来）
LENS_DOMAIN=lens.example.com bash scripts/install-tls.sh --check   # 先体检
LENS_DOMAIN=lens.example.com bash scripts/install-tls.sh           # 再装
# 3. 扫码 URL 换成 https://lens.example.com/plugin/
```

脚本做四件事：装 caddy、把 `deploy/Caddyfile.example` 渲染到 `/etc/caddy/Caddyfile`（**先 validate 再落盘**）、
把 `config.json` 的 `host` 收回 `127.0.0.1` 且 `trust_forwarded_for` 打开（**改前备份并打印 diff**）、
重启后自检 `https://域名/healthz`。

两个容易漏的点，脚本会拦：

- **DNS 必须先指对。** 解析到别人家 IP 时它直接拒绝 —— Let's Encrypt 的失败次数本身有配额
  （同域名每小时 5 次），连试几轮就得等一小时。
- **`host` 必须收回回环。** 不收的话 `0.0.0.0:8443` 明文口还开着，任何人都能绕过刚装好的 TLS。
  装完记得把安全组里 8443 的公网入站一并关掉。

插件那头不用动：`ui.ts` 的 `defaultGatewayUrl` 按 `location.protocol` 推导，页面是 https 打开的，
WebSocket 就自动是 `wss://`。

**还没有域名**：先别装。直接跑脚本会打印三条路（买个便宜域名 ✅ / `tls internal` + 手机装根证书 ⚠️ /
`sslip.io` 旁路 ⚠️）以及各自的代价。在此之前明文只在你信得过的网络里用。

### 6.5 更新代码

```bash
cd ~/EvenRealities-Claw && git pull
cd plugin && npm install && npx vite build      # 插件有改动时
systemctl --user restart lens-gateway
```

### 6.6 跑测试

```bash
cd ~/EvenRealities-Claw/gateway
.venv/bin/pip install -r requirements-dev.txt      # 测试依赖（pytest / pytest-asyncio / mcp）
PYTHONPATH=. .venv/bin/pytest tests/ -q            # 596 单测，秒级
PYTHONPATH=. .venv/bin/python tests/e2e_sim.py     # 语音端到端，自足运行（~2 分钟）
PYTHONPATH=. .venv/bin/python tests/e2e_mcp.py     # MCP 四进程真链路（~30 秒）

# 真 agent 端到端：三进程 + 真 DeepSeek。会产生真实付费调用，故不在 CI 里。
LENS_LLM_API_KEY=sk-... PYTHONPATH=. .venv/bin/python tests/e2e_agent.py

# 插件侧
cd ../plugin && npm ci && npm run typecheck && npm test    # 99 个 vitest 用例
```

再生成两套 golden（**只有确认排版/画面改动是预期的**才做，diff 要有人看）：

```bash
PYTHONPATH=. .venv/bin/python -m tests.data.formatting.corpus --regen   # 排版
PYTHONPATH=. .venv/bin/python -m tests.data.hud.scenes --regen          # HUD 帧序列
```

`e2e_sim.py` 默认自己拉起 `demo/fake_openclaw.py` 作为 **agent 测试夹具**——它跑的是与真
网关完全相同的 protocol v3，唯一区别是回复文本来自剧本而非模型。这是测试里的 test
double，**不是演示链路的替身**：演示必须接真 agent。要拿真 agent 跑同一套断言：

```bash
LENS_E2E_AGENT_URL=ws://127.0.0.1:18789 \
LENS_E2E_AGENT_CONFIG=~/.openclaw/openclaw.json \
PYTHONPATH=. .venv/bin/python tests/e2e_sim.py
```

### 6.7 MCP 表面：让厂商模型直接驱动这副眼镜

MCP 服务器是**独立进程**，不与网关同进程（技术原因：官方 `mcp` SDK 是 ASGI/Starlette，
网关是 aiohttp；安全原因：它是面向外部厂商的攻击面，不该与持有麦克风、ASR、
设备凭证的网关待在一起）。它能做的事**等于控制面暴露的九个路由**。

```bash
cd ~/EvenRealities-Claw/gateway
.venv/bin/pip install -r requirements-mcp.txt
PYTHONPATH=. .venv/bin/python -m lens_mcp          # 默认 streamable-http 127.0.0.1:8765

# 注册给 Claude Code（或任何 MCP 客户端）
claude mcp add --transport http even-glasses http://127.0.0.1:8765/mcp
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LENS_MCP_TRANSPORT` | `streamable-http` | 可设 `stdio` |
| `LENS_MCP_HOST` / `LENS_MCP_PORT` | `127.0.0.1` / `8765` | **不要对外暴露**：MCP 层没有鉴权 |
| `LENS_CONTROL_URL` | `http://127.0.0.1:8443` | 网关地址 |
| `LENS_CONTROL_SECRET` | 读 `~/.lens-gateway/control.secret` | 控制面共享密钥 |

工具清单、租约语义、鉴权设计与端到端证据见 **[docs/MCP-SURFACE.md](docs/MCP-SURFACE.md)**。
一句话版本：8 个工具（1 个纯排版 + 1 个列设备 + 4 个写屏 + 2 个读状态）、3 个资源、
1 个由真实版式推导的写作提示；**屏幕只有一块**，写屏要租约，用户按下 PTT 无条件抢占。

---

## 7. 排障速查表

| 现象 | 原因与处理 |
|---|---|
| 手机打不开 healthz | 安全组没放行 8443（5.1）；或服务没起（6.1） |
| healthz `asr_ready: false` | 刚重启在热身（ARM 上模型初始化 20-35s/个），等 1 分钟 |
| healthz `openclaw: false` | 工部网关没起：`systemctl --user status openclaw-gateway*`；lens-gateway 会在下次使用时自动重连 |
| 插件页一直「连接丢失」 | 服务重启中/断网；查 6.1 日志；插件自动退避重连无需操作 |
| 配对码无效 | 一次性+10 分钟时效，重新生成；注意服务重启后旧码全失效 |
| 按住说话但眼镜显示「麦克风没有声音」 | mic 被抢（比如长按了镜腿触发官方 Even AI）或蓝牙断了——松手重试；避免在使用本插件时长按镜腿 |
| 转写明显变慢、越聊越卡 | CPU 积分耗尽（红队 R4）：开 Unlimited（5.1）；`vmstat 1 3` 看 st 列确认 |
| 眼镜黑屏但手机预览正常 | 眼镜 BLE 问题：Even App 里重连眼镜；插件双击退出重进 |
| 回复乱码/格式怪 | 不应发生（markdown 已强制剥离）——把 `journalctl` 日志和提问发给下一个会话排查 |
| 手机丢了/凭证泄漏 | `main.py revoke dev_xxx` 立即吊销，泄漏面仅此一台设备 |

---

## 8. 安全模型

1. **OpenClaw 全权 token 永不出服务器**（它等于服务器 shell 权限）：运行时从 `~/.openclaw/openclaw.json` 读取，只在 loopback 内使用；
2. 手机端只持有：15 分钟 accessToken（内存）+ refreshToken（持久，服务端只存哈希，单设备可吊销）；
3. 对外 WS API 只有 4 类动作：提交语音 / 翻页 / 打断 / 清屏——没有任何 OpenClaw RPC 透传，拿到设备凭证最多只能"跟工部说话"，不能改配置不能执行命令；
4. **管理接口与控制面用共享密钥 Bearer**（`~/.lens-gateway/control.secret`，首次生成、0600）。
   上一版按 peername 判 loopback，而 §6.4 推荐的 TLS 方案正是 caddy 反向代理——**反代之后
   所有请求的 peername 都变成 127.0.0.1，这条守卫整体失效**，任何人都能 `POST /admin/pair-code`
   把自己的手机配上来。这是当时就存在的活隐患，已改掉；配对码另加两层节流
   （按来源 5 次/10 分钟锁 15 分钟 + 全局 30 次上限，全局阈值故意更宽松，
   免得一个攻击者把所有人一起 DoS 掉）；
5. 隐私：原始 PCM 不落盘（转写即丢）；按住说话 = 物理收音边界，无任何 always-on 监听；聆听态手机有 ●REC、眼镜状态条有 ◉；
6. **MCP 表面是独立进程**：它不持有麦克风、ASR、设备 JWT 签名密钥、OpenClaw token 中的任何一个，
   能做的事**等于控制面暴露的九个路由**（列设备/取还租约/渲染/翻页/清屏/读状态/读遥测/读事件）——
   越权在架构上不成立，不靠代码自律。厂商模型即使完全失控，也只能在这块屏幕上乱写字，
   而写屏还要过租约、且用户开口会无条件夺回；
7. 已知薄弱点：明文传输（6.4 升级路径）；**MCP 层本身无鉴权**，只能绑 `127.0.0.1`
   （对外暴露前必须自己加一层）；`gh` CLI 的 GitHub token 以明文存于本机（与本系统无关，建议换 fine-grained token）。

---

## 9. 未完成项与已知限制

| 项 | 状态 | 说明 |
|---|---|---|
| 五项真机实测 | ❌ 需物理眼镜 | 见第 10 节清单 |
| MCP 层鉴权 | ⚠️ 无 | MCP 进程本身不校验调用方，只能绑 `127.0.0.1`。控制面（网关侧）有 Bearer，所以**越权拿不到设备凭证**，但同机上任何进程都能写这块屏 |
| MCP 多客户端身份 | ⚠️ 靠自报 | MCP 规范没有 session 概念，服务器分不清谁在调用；仲裁由租约的 `holder` 字符串承担，客户端自报。恶意客户端可以冒用别人的 holder 名 —— 但仍然抢不到别人手上的租约 |
| 控制面限流 | ⚠️ 仅正文上限 | `MAX_RENDER_CHARS=20000`（超出 413），无 QPS 限流 |
| TLS | ⚠️ 差一个域名 | 反代模板与装机脚本已在仓库里（`deploy/Caddyfile.example` + `scripts/install-tls.sh`），网关侧**不需要改代码**。把 A 记录指过来就能跑，见 6.4 |
| 锁屏可用 | ❌ 设计内放弃 | 产品定位"亮屏按住说话"；锁屏存活时长属于真机实测项 |
| 多 agent 路由 | ❌ 阶段三 | 当前固定单 agent；"问格物…"/"切到…"文法在设计文档已定稿 |
| 自研 agent 的容器隔离 | ❌ AGENT-LAYER P3 | 进程边界已经有了（agent 单独进程、只监听回环、LLM key 只存在于 agent 进程），容器化演练未做 |
| 自研 agent 的工具集 | ✅ 12 个 | 8 读（`now` `days_until` `device` `weather` `calc` `currency` `list_show` `remind_list`）+ 4 写（`list_add`/`list_remove` → `lists.json`，`remind_set`/`remind_cancel` → `reminders.json`）。准入标准不变：一句话能问、一屏能答、两秒内能返 |
| lens agent 协议层鉴权 | ⚠️ 无 | 与 MCP 同理：只绑 `127.0.0.1`，`server.py` 会拒绝绑到非回环地址。同机进程仍可调用 |
| TTS 语音回放 | ❌ 阶段三 | 你已确认 MVP 纯 HUD 文本 |
| 都察告警上屏 | ❌ 阶段三 | 告警管道（去重/限流/熔断）设计已定稿 |
| R1 戒指支持 | ⚠️ 部分 | SDK 事件已监听（EventSourceType.RING 与镜腿同路），未单独测试 |
| ASR 独立实例 | ❌ 中期 | 当前与 agent 同机（4 线程 + 全局串行锁缓解）；重负载并行时是已知瓶颈（R5） |
| `evenhub qr` 扫码入口在 App 内的具体位置 | ⚠️ 未验证 | CLI 与官方文档确认存在；App 内入口位置等你拿到真机确认，备选 ehpk 上传路径已写明（5.2） |

## 10. 五项真机实测清单

拿到眼镜后第一周建议完成（每项 10-20 分钟），结果用于校准 `config.json`：

1. **后台存活**：插件工作中锁屏/切后台，计时到眼镜出现「⛓ 连接丢失」——得出真实可用窗口（iOS/Android 分别测）；
2. **mic 仲裁**（本轮把长按镜腿绑成了我们自己的按住说话，所以这条拆成两半）：
   - **长按能不能用**：长按镜腿说一句，看录音起不起得来 —— 这等价于验 §13.7-2「事件到不到 WebView」。收不到的话镜腿说不了话，手机按钮不受影响；
   - **抢麦**：从别处唤起官方 Even AI，插件聆听中是否出现「麦克风没有声音」告警（看门狗应在 ~1s 内报）；反向：Even AI 用完后插件能否恢复收音；
3. **镜腿事件**：单击/双击在插件页是否如期翻页/退出（验证 TouchBar 事件对 WebView 的暴露）；若不行，翻页退化为手机按钮（已可用）；
4. **audioControl 时长**：连续按住说到 25s 自动截停是否正常；再把 `max_utterance_seconds` 临时调到 60 试探固件上限；
5. **真实刷新与字宽**：盯着流式回复看是否闪烁/撕裂（调 `throttle_ms`）。
   **字宽不用再调了** —— 折行已按官方 `pretext` 的固件度量逐位复刻，与官方 JS oracle
   17 075 码点零分歧；真机若出现半字溢出，是版本差，退让阀是 `body_safety_px`（默认 0），
   不是"每行几个字"（那个旋钮已经删掉了，见 §6.3）。

## 11. 工程问题记录

开发中真实踩中并修复的坑（都有提交记录可查）：

1. **ctranslate2 并发互锁**：服务启动 warmup 与首个用户 partial 并发解码 → 两实例 OMP 线程在 4 核 ARM 上互锁，解码无限挂起。修复：全局解码串行锁（含 warmup）。
2. **py3.9 事件循环绑定**：`asyncio.Lock`/`Event` 在 `web.run_app` 创建自己的 loop 之前构造 → `got Future attached to a different loop`。修复：服务对象在循环内构造。
3. **ARM 进程级首解码延迟**：ctranslate2 int8 每进程首次解码需 20-35s（kernel 初始化），且每个模型一次。修复：启动 warmup 吃掉成本 + healthz 增加 `asr_ready` 字段。
4. ASR 误听"眼镜链路"→"眼睛练路"：热词表补充域词后修正——**以后发现误听就往 `config.json` 的 hotwords 里加词**。
5. **aiohttp 启动钩子撞名 → 说完话十秒字才上屏**：给 `server.py` 加会话回收时新写了一个
   `_on_startup`，与文件后面已有的同名方法冲突；Python 类体里后定义的覆盖先定义的，
   于是 `build_app` 里两处 `on_startup.append` 变成"同一个 ASR warmup 注册两遍"。
   静音输入会让 whisper 退化成重复生成直到 max tokens，**一次 warmup 要 ~12s 且全程持解码锁**，
   第二遍正好卡住用户第一句话的 `final`。表征是"ASR 好慢"（松手→上屏 7.6s），
   实测拆开是**等锁 9.5s + 真正解码 0.35s**。修复：钩子合并成一个 + `warmup()` 幂等，
   松手→上屏回到 **0.4s**；两条都有回归单测（`tests/test_session.py`）。
   教训是**测总耗时不够，要能拆出"等待"与"计算"各占多少**——否则这类问题会被归咎于模型太慢。
6. **两页的回答根本读不完**：`Paginator` 的流式重排原本「跟随末页」——终端里 tail 输出的
   习惯做法。8 行的屏幕上它是有害的：回答一旦超过一页，读者正读着第一页，最后一个 token
   落地的瞬间画面就跳到 `2/2`，只剩半句结尾，开头再也看不见。

   这个缺陷**单测和 golden 全绿的情况下存在了很久**，因为快照忠实记录的正是错的行为，
   而 `test_follow_tracks_last_page_while_streaming` 把它写成了断言。是拿真语音跑英文
   演示、看导航那一问的收尾帧时才发现的——**没有哪个测试会告诉你「这个屏幕没法读」**。

   修复：重排永远不移动读者，只把 `cur` 夹进合法区间。判据是 `Paginator` 的三个使用者
   （S6/S7 回答、MCP 写屏、打断回顾）没有一个需要跟随；真正该像实时字幕跟着最新的是
   S2 的部分转写，而它走 `tail_window()`，根本不经过分页器。顺带删掉了 `_follow` 字段，
   语义并入 `at_last`。

   收尾帧从 `‹ 2/2` 的半句结尾变成 `1/2 ›` 的答案开头，页脚告诉读者还有下一页。
   两条新回归测试都做过变异测试（把逻辑改回跟随末页，测试必须失败）——其中
   `test_streaming_never_moves_the_reader` 第一版**没能咬住**，因为它复用的流式语料
   全都只有一页，`cur` 恒为 0，跟随与否都测不出来；换成真会跨页的语料才生效。
   golden 快照的 3 处 diff 逐帧核对过再重生成的。

7. **它会假装自己什么都能做**。第一版工具表只有 `now` 和 `weather`，拿 15 个日常问题
   跑一遍（`demo/chat.py -f`），发现问题不是「工具少」而是**没有工具时它会撒谎**：
   「Set a timer for 10 minutes」答「Timer set for 10 minutes.」；
   「Add milk to my shopping list」答「Milk and eggs are now on your shopping list.」；
   问今天日程，它编出了客户电话和 Q3 roadmap review；问上一场比赛，它编出了
   「Lakers 112-108 Celtics，LeBron 34 分」；问眼镜电量，它编了个 82% ——
   而**那份遥测就在网关的缓存里**，只是 agent 拿不到。

   对照组：问心率，它老实说看不到。所以模型不是不会拒绝 —— 是没人告诉它边界在哪：
   小屏契约当时 6 条规则**全是排版**，没有一条讲能力。

   修复分两层，顺序不能反。先给契约加了第 1 条（没有工具的动作直接说做不到、
   没有工具的实时事实直接说不知道、绝不能说「已经帮你办好了」），
   这一条就修掉了上面除日历外的全部编造；然后才是补工具，优先补
   「系统里已经有数据、agent 却拿不到」的那些。

   数字类问题**单靠契约修不掉** —— 模型说「我算一下」然后算错，它并不认为自己在编。
   「离圣诞还有几天」两次跑给出 48 天和 116 天（正确 116）。所以 `calc`（AST 白名单
   求值，不是 eval）和 `days_until` 是必须的。

   顺带发现两件事：一是 `MAX_TURNS=3` 装不下 `now → calc → 回答` 这条两步工具链，
   用户看到的是「工具轮次用尽」的道歉；二是 `list_remove` 在用户不报清单名时
   落到默认清单，**「删成功了但其实什么都没删」**。前者提到 4 并把预算按
   「模型往返次数」重定，后者改成找不到就在所有清单里找。
   这一章新增 123 条单测（`test_agent_tools.py` 87 + `test_agent_reminders.py` 36），
   16 个关键变异全部被咬住：把 `_eval_node` 换成真 `eval`、摘掉闸 3 的构造期检查、
   去掉清单全局查找、让 `device` 没数据时编 82%、去掉 stale 标注、
   提醒 id 退回会撞的时间戳、会话隔离被摘掉、宽限期失效、24 小时上限被摘掉、
   取消有歧义时不再拒绝、钟点解析放宽到什么都收、省略 `day` 时不再跨天、
   `minutes` 与 `at` 同时给出时不再拒绝、把「取消」从路由里摘掉、
   写档预算压回 6s、以及**同时**摘掉提醒收尾的两层防护。
   另有 10 条落在别处：折行的数字断行 6 条（`test_formatting.py`）、
   契约两份规则条数一致与「跟随提问语言」4 条（`test_agent.py`）。

   补到「提醒」的时候撞上一个归属问题：**agent 有状态、网关有屏幕**，而到点响铃
   两样都要。放网关则 `remind_list`/`remind_cancel` 拿不到自己排过的东西；放 agent
   则它根本没有屏幕。最后切成「状态和计时在 agent，响铃在网关」，中间只加了
   协议里唯一一条**不属于任何 run** 的事件 `notify`；网关收到后照样走 W1 租约去写屏，
   抢不到就不写 —— 抢掉用户正在读的那一屏比提醒迟到更糟。细节见
   [docs/AGENT-LAYER.md](docs/AGENT-LAYER.md) 的「提醒：状态在 agent，响铃在网关」。

   工具补齐之后又拿真链路一句一句问，逼出了三处只有跑起来才看得见的问题：
   一句中文提问被英文答了（契约里当时没有「跟随提问语言」这条，补成第 8 条）；
   「下个月提醒我换护照」回了句「我还不会设提醒」——**说自己做不到一件做得到的事**，
   和编一个答案一样糟（路由的判据从「有没有说时间」改成只判意图，
   可行性交给 skill：24 小时内就排程，超出就记进待办并如实说明）；
   以及汇率答案里的 `75.52` 被断成「75.」和「52」两行，读者看到的是两个数
   （折行时数字中间的小数点/千分位/冒号/斜杠不再算断行机会）。

   **提醒这一档有两个不会报错的坑，都是变异测试挖出来的**，而它们的共同点是
   「症状要等到出事那一刻才出现」：一是提醒 id 用毫秒时间戳取模，同一秒里连排两条
   会撞，而排程器看到相同 id 会把前一条**取消**再排新的 ⇒ `remind_list` 说有两条、
   实际只响一条；二是一条响完之后从磁盘划掉时用了默认读法，而默认读法会丢掉所有
   已到点的条目 ⇒ 这次保存把别的、还在宽限期里等着补发的提醒**一起抹掉了**。
   两条都不抛异常、不打日志，用户唯一能察觉的是「说好要提醒我，结果没响」。

   第三个坑是真链路问出来的，而且更狠：**取消被当成了「响过了」**。
   「响完从磁盘划掉」那段收尾写在 `finally` 里，于是任务被 `cancel()` 时也跑一遍，
   而取消的两个来源都是无辜的 —— 网关重连时的幂等恢复（重排同一条 ⇒ 先取消再排）、
   进程退出时的 `cancel_all()`。前者的症状是**设完提醒、下一句问「有什么提醒」
   答「一条都没有」**，而内存里那条其实还会响；后者意味着每次重启都可能把所有
   待响的提醒清空，且是竞态的：收尾跑得赢就清空，跑不赢就留着。
   修成了两层（取消分支不碰磁盘 + 恢复扫描幂等），回归测试把两层同时摘掉才会红。

   顺带暴露了一件更该记下来的事：**两条 e2e 里的翻页断言早就过期了，而我没重跑。**
   「说完停在末页」这个行为在修 F7 时就改成了「重排不移动读者」，可 `e2e_sim.py`
   和 `e2e_mcp.py` 的翻页仍然从末页往回翻 —— 于是 `prev` 翻不动、三条断言一起红，
   而单测全绿。**单测不会告诉你端到端的前提变了。**两处都按新前提重写了，
   并加了一条正面断言：说完那一帧的页脚必须是「1/N ›」。

## 12. 阶段三预告（设计已定稿，未开发）

- 双层路由：「问格物，…」单次借调 / 「切到格物」改粘性默认 + 拼音兜底（hé mǐ sī→Hermes）；
- Hermes 通路 + 「读」控制词（手机 TTS 朗读当前页）；
- 都察告警管道：severity 门限/指纹去重/5 分钟合并/风暴熔断/P1-P3 三级插入（绝不抢活跃流式）；
- 语音控制词全集（停/继续/重说/详细/清屏）+ 防误触文法。

——以上全部细节见 `docs/DESIGN.md` 与 `docs/DEVELOPMENT-PLAN.md`。


## 13. 未验证项清单

本节存在的理由：本报告本身是演示材料的一部分，因此必须明确区分「已验证」与「已实现但未验证」
与「未实现」，并且**每条已验证的结论都要标注它是被什么验证的**（三档归属见
[docs/SIMULATOR-PARITY.md](docs/SIMULATOR-PARITY.md)）。

> ⚠️ **本节上一版有一条实质性错误，在此更正。**
> 上一版把「字形可用性」和「真实字宽 / 折行」列为*原理上无真机不可验证*。**这是错的。**
> 官方 `@evenrealities/pretext` 是复刻固件 LVGL 度量的字形库，官方
> `evenhub-simulator` v0.7.0+ 带 `--automation-port`，`/api/screenshot/glasses` 直出
> 576×288 RGBA PNG，且其缺字渲染已用 `LV_USE_FONT_PLACEHOLDER` + lvgl `g2` feature
> 与固件对齐。两者都在本轮被接进来，这两项**已经判定完毕**（§13.4）。
> 把可判定的事情记成「等硬件」，代价是几个月里所有排版决策都建立在猜测上 —— 记在这里以免重犯。

### 13.1 曾被声称、但当时仓库内无任何产物

| 声称位置 | 当时状态 | 现在 |
|---|---|---|
| §4「插件协议冒烟（jsdom + 存根网关）25/25」 | **不存在** | ✅ **已补齐**（M7）：`plugin/tests/` 现有 **82** 个 vitest 用例。WS 协议层由 `ws-protocol.test.ts` 覆盖 **28** 条 —— 配对/resume/seq 过滤/PTT 与 PCM 合并/心跳/退避重连/自动 refresh/命令回执/未知消息容忍。打桩只到传输层（`tests/stub-gateway.ts`），被测的是真的 `LensClient` |
| §4「心跳看门狗专项 通过」 | **不存在** | ✅ **已补齐**（M7）：4 条专项 —— 20s 一次 ping、两次无 pong 判定断线且**先通知看门狗再撕 socket**（顺序是关键：眼镜上必须先盖掉旧帧）、pong 按时不误判、断线只通知一次且重连后重新武装 |

这两条当初是**被声称但不存在**的，所以补齐之后必须给出可复核的证据，而不是再声称一次。
证据是：把 `src/ws.ts` 做 5 处变异（seq 过滤改成严格小于 / 新连接不重置 seq 基线 /
看门狗阈值调到永不触发 / stop 前不 flush 尾块 / refresh 不设已重试标志），
**每一处都恰好打红一条用例**。一套全绿但杀不死任何变异的测试等于没有测试。

### 13.2 从未在物理 G2 上执行过的代码路径

`plugin/src/glasses.ts` 的全部宿主调用**仍然一次都没有在真机上跑过**。
但证据基础已经从「对 SDK 类型声明的静态核对」升级为三条可复现的实测：

1. **官方 SDK 实跑**：在 DOM shim 下加载 SDK，实测出全部 `EvenAppMethod` 名、信封形状、
   `getDeviceInfo()→getGlassesInfo` 的映射、`validateEvenHubPageContainer` 的实际覆盖面、
   `OsEventTypeList`/`EventSourceType` 全表、以及「未知 eventType 会被解析层吞掉但
   `jsonData` 原样透传」这个判别依据（见 [docs/HARDWARE-SPEC.md](docs/HARDWARE-SPEC.md) §3.1、§5）。
2. **官方模拟器实跑**：`plugin/tools/g2probe.mjs` 驱动 8 屏建页/重建，逐屏截图判定。
3. **保真夹具实跑**：真 SDK + 真 `GlassesController` + 可注入故障的宿主夹具，26 个 vitest 用例。

### 13.3 自建夹具与真机的差距（已大幅收窄）

上一版这里列了 9 条「模拟器上必然通过、检出率为 0」的风险。本轮把其中 7 条修掉了：

| 上一版的风险 | 现状 |
|---|---|
| `createStartUpPageContainer` 恒返回 0 | ✅ 已修：只允许调一次，且 invalid/oversize/outOfMemory 三条错误路径都可注入并有用例 |
| `textContainerUpgrade` 恒返回 true | ✅ 已修：可注入 false，并有「写失败不毒化去重缓存」的回归 |
| 忽略 `isEventCapture` | ✅ 已修：夹具校验「恰好一个」，官方模拟器侧靠 `/api/input` 点击验证了捕获容器确实收事件 |
| 所有调用同步返回，无 BLE 延迟 | ✅ 已修：可注入往返延迟与「永不 settle」，后者用于验证 5s 超时保护 |
| 假屏字号是启发式猜测（`h<=48?24px:32px`） | ✅ 已修：G2 **没有字号控制**，两档字号是虚构的；夹具改为单一字号 + 27px 固定行高，横向位置由 pretext 的累计 advance 定位 |
| 用桌面字体渲染，12 个特殊字形是否存在「完全未知」 | ✅ 已判定：见 §13.4 与 [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md) |
| 无 mic 被抢 | ✅ 已修：`micDenied` 可注入 |
| **PCM 载荷推 `number[]`** | ✅ **已判定，而且结论和计划里写的不一样**：原计划是在插件里写一个「兼容三种载荷」的归一函数。实测发现**SDK 自己已经归一了** —— `number[]`、base64 字符串、`Uint8Array` 三种载荷 × `{type,jsonData}` / `{type,data}` / 数组三种信封，九种组合全部出来的是 `Uint8Array`（`plugin/tests/audio-pcm.test.ts` 把这个契约钉住了）。所以那段归一函数没有写：它会是重复实现，而且会掩盖真正的失败模式 —— SDK **认不出**的形状（如 Node 的 `{type:'Buffer',data:[…]}`）会让 `audioEvent` 整个消失，音频被**静默丢弃**，用户说了一整句话一个字节没上行，而网关那头只报「麦克风没有声音」。改动是给这条路径加了可排障的日志（带载荷形状、且有刷屏防护），不是加一层归一 |
| **麦克风是本地 getUserMedia（毫秒级）** | ⚠️ **真值仍待真机，但误报的成因已经修掉**：旧看门狗把两件事混成一个硬编码 1.0s 判据 —— 而 partial 循环 700ms 才检查一次，真实宽限只有 1.4s。现在拆成两个：`mic_warmup_seconds`（默认 **2.5**，等第一块 PCM，要塞下 WS RTT + BLE 下发 + 固件启麦 + 首帧回传 + 插件攒 200ms + 上行）与 `mic_gap_seconds`（默认 0.8，音频已在流又断了）。两者的合理等待时间差好几倍，混在一起必然错判一边。`tests/test_voice.py` 里有一条专门守着「1.4s 处不报警」的回归 |

### 13.4 上一版第 6、7 项：**已判定，从真机清单里移除**

- **第 6 项「字形可用性」——已判定。** 两条互相独立的判据（官方度量库 `getAdvW===0`
  与官方模拟器截图的逐行墨迹统计）在 **26 个字形上 26/26 一致**：
  仓库早期用的 10 个字形（`◉◔▸⚙⚠⛓✓✕⏸⏹`）**全部画不出来且不留占位框**
  （截图 `docs/assets/g2probe-01-glyphs-missing.png` 整幅零个不透明像素）；
  现役 16 个字形全部有墨。明细见 [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md)。
- **第 7 项「`\n` 换行语义」——已解除。** 官方文档站明文：*"'\n' is a line break."*，
  并在模拟器上按行渲染验证。
- **附带解决的两项**：满画布 576×288 建页**不触发 `oversize`**
  （`createStartUpPageContainer → 0`）；单容器内容上限是 **UTF-8 999 字节**而非 1000 字符
  （999B ✅ / 1002B ❌ / 1000 个 ASCII 字符 ❌ —— 最后一行是决定性的）。

### 13.5 单元测试覆盖

| 范围 | 行数 | 单测 |
|---|---|---|
| `lens_gateway/formatting/`（排版引擎） | 971 | **168**（含 3 宽度 × 31 语料参数化 + 600 例随机模糊 + 官方 oracle 比对） |
| `auth.py` | 106 | 含在上述 168 内 |
| `device/hud.py`（设备抽象 + 帧租约 + 低电量提示） | 380 | **24** |
| `device/telemetry.py`（遥测缓存） | 130 | **28**（与上行通路合计） |
| `session.py`（装配 + 遥测路由）+ `voice/pipeline.py` + `server.py` 会话回收 | 620 | **16** |
| `control.py`（控制面九路由）+ `server.py` 鉴权与节流 | 200 + | **44**（路由 22 + 鉴权 22） |
| `lens_mcp/`（MCP 表面：server + client） | 400 | **18**（走真实链路到帧，无打桩） |
| `lens_agent/`（自研 agent：loop/policy/tools/skills/audit/llm） | ~900 | **51** |
| `asr.py`（含热词回声守卫） | 183 | **15**（自建语音数据集 + CER 阈值，走生产解码路径） |
| `voice/pipeline.py`（mic 看门狗） | ~230 | **11** + 端到端 |
| HUD 帧序列（跨 `device/` + `voice/` + `formatting/` 的集成快照） | — | **25**（13 场景 68 帧 + 每帧不变量） |
| `providers/`（AgentProvider 抽象 + 两个实现 + W6 溯源 + 连接生命周期） | ~450 | **34** |
| `plugin/src`（TypeScript） | ~1500 | **82**（桥接层 30 + 字形契约 10 + 夹具接线 4 + **WS 协议层 28** + **PCM 载荷契约 10**） |

端到端：语音链路 `tests/e2e_sim.py` **31/31**、MCP 链路 `tests/e2e_mcp.py` **27/27**
（两者自足运行、不依赖任何仓库外服务），真 agent 链路 `tests/e2e_agent.py` **23/23**
（需 `LENS_LLM_API_KEY`，会产生真实付费调用，故不进 CI）。
**CI 已建**：`.github/workflows/ci.yml`，三个 job + 一道「外部 oracle 不许静默 skip」的闸门。

### 13.6 对抗式评审：M6 + M7 在提交前被 105 个 agent 找过一遍

M6（自研 agent）与 M7（桥接硬化）写完之后没有直接提交，先过了一轮**对抗式评审**：
6 个维度（流式契约 / 安全 / 协议 / 并发 / 测试质量 / 桥接）各自独立找问题，
每一条发现再交给 **3 个互相看不见的视角去「把它驳倒」**——复核的任务不是确认"对不对"，
而是"请证伪它"，默认立场是驳回。共 **105 个 agent**，提出 **33 条**，**存活 6 条**。

被驳回的 27 条不是"看着不像问题"就放过的。驳回方给的是复现脚本或反证，例如：
有一条说 golden 里的「无裸 markdown」是恒真断言，驳回方直接按它描述的变异改了
`strip_markdown` 去跑，结果是 8 条用例打红而不是它声称的"全绿"；另一条说
`abort_midway` 场景其实是在 final 之后才打断（等于没测僵尸 run），驳回方数了
golden 里那个场景的 7 帧、指出最后一帧正文停在「没有字号控」这个词中间，
证明屏幕确实冻在半截流式文本上。**驳回的质量本身就是这轮评审的产出之一。**

#### 一个必须讲清楚的时序问题

这轮评审跑在一个**仍在演进的工作区**上。6 条"确认"里有 5 条，在评审还在运行期间
就已经被独立发现并修掉了——所以同一个问题的另一份提交，在复核阶段读到的是修好后的代码，
于是进了"驳回"列。两个列表因此互相矛盾（比如 `_read_hello` 那条同时出现在确认与驳回里）。

这不是评审出错，是**对着移动靶做评审的固有局限**。处理办法只有一个：把 6 条确认逐条
拿回代码里对，而不是照单全收。对完的结果是——真正需要新动手的只有第 3 条（W6 徽记时序）。

#### 6 条确认 + 3 条顺藤摸出来的

| 位置 | 缺陷 | 演示时会怎样 |
|---|---|---|
| `providers/lens.py` delta 归并 | 照抄 openclaw 的「增量 / 全文」启发式，而 agent 在工具跑完后会把正文清零重写 | 「现在几点」这种最普通的问题，屏幕上拼出二次方级重复的乱码 |
| `providers/openclaw.py` `_read_hello` | 对端把 `server` 报成字符串就抛 `AttributeError` | 连锁触发下一条 |
| `providers/lens.py` `_connect` | 握手失败不收 socket，`ensure_connected` 此后永远短路 | agent 起来了也再连不上，且 W6 溯源永久停在 unknown ⇒ **替身不再告状** |
| `providers/lens.py` `_reader` | 断线不清 run 表 | `session_busy` 永久为 True，眼镜锁死在「上一条还在跑」 |
| `voice/pipeline.py` W6 徽记 | 一轮取样一次，而"没连上"时按可信处理 | **冷启动第一句**：替身在答，屏幕上却一个「?」都没有 |

修 W6 那条时没有按建议"挪个位置再取样一次"，而是把徽记改成**每帧现算**
（`HudDevice.agent_production` 绑一个溯源探针）。理由是取样式写法必然留下时序窗口，
挪位置只是把窗口移小；现算把窗口整个消掉——握手什么时候完成，下一帧的徽记就什么时候变。

顺着补回归测试，又摸出 **3 条评审没提的同类缺陷**：

1. **openclaw 三处没跟 lens 同步**：握手失败不 teardown、断线不清 run 表、`_request`
   超时泄漏 future。`--fake` 演示模式走的正是这条 provider。
2. **两个 provider 的 `chat_send` 都可能把 run 登记到一条已经死掉的连接上**：
   `_reader` resolve 掉 res 之后不会停下来等 `chat_send` 恢复，它接着读下一帧——
   那一帧可能就是 close。于是 finally 先跑完（那时 run 还没进表，清了个空），
   `chat_send` 才恢复并登记。症状与上表第 4 条一样（会话永久占用）但成因完全不同，
   评审的 6 条里没有覆盖。
3. **`except Exception` 抓不到 `CancelledError`**：握手被外部取消（`asyncio.wait_for`、
   用户按打断）时 socket 同样不回收——正是上表第 3 条的另一半，而且是更常见的那一半。

这 3 条现在都由 `TestConnectionLifecycle` 守着，用的是**真的 aiohttp WebSocket 服务器**
（这些缺陷全都发生在 `_reader` 的 finally 与 `_connect` 的异常路径上，打桩就把要测的东西打没了）。

#### 这一节想说明的

评审的产出不是"33 条里存活 6 条"这个数字，而是**每一条存活的都留下了一个会咬的回归测试**。
逐条做过变异验证：把修复挨个改回去（现算退回取样、`BaseException` 退回 `Exception`、
去掉连接存活检查、去掉 `isinstance` 守卫），每一处都恰好打红对应的用例。
`providers/` 的用例数因此从 21 涨到 **34**。

### 13.7 真正只能靠真机判定的（6 项）

上一版列了 8 条，其中「真实字宽」「特殊字形可用性」「`\n` 换行语义」三条已在 §13.4 判定并移除。
剩下的是：

1. BLE 渲染时序与闪烁（`textContainerUpgrade` 的真实往返延迟与合并窗口）
2. 镜腿 / 戒指事件是否真的能到达 WebView（**官方模拟器把 `eventSource` 硬编码成 1**，测不了左右与戒指）。
   本轮把**长按**绑成了按住说话，这条的分量随之变重：以前收不到只是少个没绑动作的手势，现在收不到就是镜腿说不了话
3. 麦克风仲裁与 BLE 启麦延迟（`mic_warmup_seconds` 待回填）
4. `audioControl` 的单次连续时长上限
5. 插件在后台 / 锁屏下的存活时长
6. `FOREGROUND_ENTER/EXIT` 在真机上的实际触发时机

**真机第一天只做标定与复验，不做设计变更。**
