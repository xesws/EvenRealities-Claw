# 交付报告：阶段一（模拟器闭环）+ 阶段二（真机 MVP）

> 2026-06-11 · v0.2.0
> 仓库：https://github.com/xesws/EvenRealities-Claw （本文件 = 仓库根目录 `REPORT.md`）
> 服务器：EC2 `i-0774fa15e542c6f1d`，公网 IP `35.169.46.183`，服务端口 `8443`

## TL;DR

**整条链路已开发完成、部署为常驻服务、并实弹验证通过**：按住说话 → 中文语音 → faster-whisper 转写 → 真实工部 agent 回复 → 眼镜 HUD 分页渲染。生产冒烟全程 11 秒，状态机 S2→S3→S4→S6→S7 完整走通。

**你拿到眼镜后要做的全部事情（共 ~10 分钟）：**
1. AWS 控制台放行 8443 端口（→ 第 5.1 节）
2. `npx @evenrealities/evenhub-cli qr --url http://35.169.46.183:8443/plugin/` 生成二维码，Even App 扫码（→ 第 5.2 节）
3. 服务器跑一条命令拿 6 位配对码，手机输入（→ 第 5.3 节）

之后：手机亮屏开插件 → 按住说话 → 抬眼看眼镜。没有眼镜时也可以先用浏览器模拟器体验（→ 第 5.6 节）。

---

## 目录

1. [系统形态与仓库地图](#1-系统形态与仓库地图)
2. [各组件详细说明](#2-各组件详细说明)
3. [一次问答的完整数据流（含实测耗时）](#3-一次问答的完整数据流)
4. [验证结果汇总](#4-验证结果汇总)
5. [【最重要】拿到眼镜后的完整上手流程](#5-拿到眼镜后的完整上手流程)
6. [运维手册：服务、配置、日志、更新](#6-运维手册)
7. [排障速查表](#7-排障速查表)
8. [安全模型](#8-安全模型)
9. [如实声明：未完成项与已知限制](#9-未完成项与已知限制)
10. [眼镜到手后的五项实测清单（附操作方法）](#10-五项真机实测清单)
11. [开发过程中发现并修复的工程问题](#11-工程问题记录)
12. [阶段三预告](#12-阶段三预告)

---

## 1. 系统形态与仓库地图

```
┌─────────┐ BLE(官方桥) ┌────────────────────┐  WS:8443   ┌──────────────────────────────────┐
│ G2 眼镜  │◀──────────▶│ 手机 Even App       │◀──────────▶│ EC2 服务器 (35.169.46.183)        │
│ mic/HUD │             │  └ OpenClaw Lens    │  设备 JWT   │  lens-gateway (systemd 用户服务)   │
└─────────┘             │    插件(WebView)     │            │   ├ /ws       插件接入             │
     ↑ 单击翻页/双击退出  │    按住说话+预览屏    │            │   ├ /plugin/  托管插件本体          │
                        └────────────────────┘            │   ├ /healthz  健康探针             │
                                                          │   └ /admin/*  仅本机(配对码等)      │
                                                          │      ↓ loopback (token 不出服务器) │
                                                          │  OpenClaw 网关 → 工部 agent        │
                                                          └──────────────────────────────────┘
```

仓库文件地图（你需要关心的部分）：

```
EvenRealities-Claw/
├── REPORT.md                  ← 本报告
├── README.md                  ← 项目入口与文档索引
├── protocol/PROTOCOL.md       ← 插件↔网关 WS 协议 v1（认证/渲染帧/时序图）
├── gateway/                   ← 服务器端（Python 3.9 / aiohttp）
│   ├── lens_gateway/
│   │   ├── server.py          ← HTTP/WS 服务与路由 + 会话 TTL 回收
│   │   ├── session.py         ← 装配层：一块屏 + 一条语音链路（只做路由，无业务逻辑）
│   │   ├── device/hud.py      ← 设备抽象：帧构造/节流/状态条/分页/计时器/**帧租约**
│   │   ├── voice/pipeline.py  ← 语音链路：PTT → PCM → ASR → 确认窗口 → agent → 流式上屏
│   │   ├── asr.py             ← faster-whisper 双模型管线
│   │   ├── openclaw.py        ← 工部网关适配器
│   │   ├── formatting/        ← 排版引擎（像素盒分页，与官方 pretext 度量逐条对齐）
│   │   │   ├── metrics.py     ←   G2 字形度量的 Python 复刻（advance+kerning+逐字取整）
│   │   │   ├── wrap.py        ←   折行 + 中文禁则（行首/行尾禁排，追出与悬挂）
│   │   │   ├── paginate.py    ←   像素盒分页（8 行/页）+ 锚点 + 页脚
│   │   │   ├── layout.py      ←   容器版式（读 protocol/hud-contract.json）
│   │   │   ├── glyphs.py      ←   语义字形表 + import 时在库校验
│   │   │   ├── sanitize.py    ←   控制字符/双向覆盖/伪状态条剔除
│   │   │   └── markdown.py    ←   markdown 降级
│   │   ├── auth.py            ← 配对码/设备 JWT/吊销
│   │   ├── config.py          ← 配置定义（运行时配置在 ~/.lens-gateway/）
│   │   └── main.py            ← CLI：serve / pair-code / devices / revoke
│   ├── tests/                 ← 208 单测 + e2e_sim.py（22 项端到端）+ fixtures 语音
│   ├── requirements.txt
│   └── README.md              ← 网关模块说明与实测数据
├── plugin/                    ← 手机端插件（TypeScript / Vite / 官方 SDK ^0.0.14）
│   ├── app.json               ← Even Hub 清单（含 g2-microphone 权限）
│   ├── src/                   ← glasses.ts / ws.ts / ui.ts / store.ts / main.ts / types.ts
│   ├── harness/               ← 浏览器模拟器（mock 官方桥 + 假眼镜屏）
│   └── README.md              ← 插件开发/构建/扫码说明
├── deploy/lens-gateway.service ← systemd 用户服务单元
├── scripts/install-service.sh  ← 一键安装服务
└── docs/                      ← DESIGN.md（系统设计）/ DEVELOPMENT-PLAN.md（四阶段计划）
```

服务器上的运行位置（不在仓库里）：

| 路径 | 内容 |
|---|---|
| `~/EvenRealities-Claw/` | 仓库工作副本（gateway/.venv 虚拟环境、plugin/dist 构建产物在此，均不入 git） |
| `~/.lens-gateway/` | 网关状态目录：`config.json`（可调参数）、`devices.json`（已配对设备）、`jwt.secret` |
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
- 启动时按协议布局契约调 `createStartUpPageContainer` 建 3 个文本容器：状态条(0,0,576×32) / 正文(0,32,576×220) / 页脚(0,252,576×36)，失败码（oversize/outOfMemory）直接显示在手机页；
- `textContainerUpgrade` 写入：120ms 防抖 + 只写内容变化的容器 + 串行写（BLE 渲染队列慢——官方 asr 模板同款策略）；空内容用单个空格兜底（防 protobuf 零值省略吃掉清屏指令）；
- 镜腿事件：单击=翻页（发 `page next`）、双击=`shutDownPageContainer(1)` 退出插件（官方标准手势）；CLICK_EVENT=0 在 protobuf 零值省略下会变 undefined——已按官方模板做归一处理；
- `onDeviceStatusChanged`：眼镜电量/佩戴状态显示在手机页。

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
| `gateway/tests/`（pytest） | 字形度量/折行禁则/像素盒分页/净化/markdown 降级/版式契约/配对/JWT/吊销/过期/持久化，含 3 宽度 × 31 语料的参数化不变量与 600 例随机模糊 | **208/208** |
| **设备抽象层**（`tests/test_device.py`） | 帧节流与 coalescing、seq 单调、状态迁移、翻页四触发源等价与边界、租约冲突/续租/过期/抢占、外部渲染走同一排版引擎、事件缓冲增量拉取、快照结构 | **24/24** |
| **会话装配与回收**（`tests/test_session.py`） | S5 工具态接活、错误分支、`reset` 重新注入小屏风格、消息路由、会话 TTL 只回收「离线且静默」、启动钩子只注册一次、ASR warmup 幂等 | **16/16** |
| **排版引擎 vs 官方 pretext** | 17 075 码点的 advance + 1 376 个折行用例逐条比对（外部 oracle） | **零分歧** |
| 插件构建链 | `npm install && tsc --noEmit && vite build`（strict 模式） | 全绿 |
| 插件桥接冒烟（vitest + jsdom） | 真 SDK + 真 `GlassesController` + 保真夹具：建页只能一次/rebuild 接力、写失败不毒化去重缓存、BLE 卡死 5s 超时、缺字静默丢弃、折行与 pretext 一致、溢出裁行、前台进出 vs 真退出、5 手势 × 4 来源、未知 eventType 不变幽灵翻页、遥测读回、麦被抢 | **26/26** |
| 插件字形与契约 | 用官方 pretext 逐字校验所有会上屏的字符；反向断言被替换的 10 个旧字形确实缺失；版式自洽 | **10/10** |
| **官方模拟器实测** `tools/g2probe.mjs` | 8 屏自动化：满画布建页返回码、缺字渲染、26 个字形逐格墨迹判定、内容上限字节/字符口径 | 见 [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md)、[docs/HARDWARE-SPEC.md](docs/HARDWARE-SPEC.md) |
| 插件 WS 协议冒烟（存根网关） | 配对→resume→翻页→PTT 上行→看门狗→退避重连→自动 refresh→旧 seq 丢弃 | ⚠️ **仍未实现**（桥接层已覆盖，WS 层尚未，见 §13） |
| **端到端闭环** `tests/e2e_sim.py` | 真服务进程 + 真 ASR + agent 测试夹具（`demo/fake_openclaw.py`，protocol v3 同一套，仅回复内容来自剧本）：配对→PTT→灌真实语音→转写→回复→帧约束（seq 单调/行宽/容器结构）→翻页→重连恢复→reset。**自足运行，不依赖任何仓库外服务**；打真 agent 见 §6.6 | 见下方运行输出 |
| **生产部署冒烟** | systemd 正式实例（非测试实例）整轮问答 | S2→S3→S4→S6→S7，11.0s |

当前服务状态（交付时刻）：`{"ok": true, "asr_ready": true, "openclaw": true}`，服务 enabled（开机自启）+ active。

---

## 5. 拿到眼镜后的完整上手流程

### 5.0 前提
- G2 已用官方 **Even Realities App** 完成蓝牙配对（官方 App 自身功能正常，能在眼镜上看到时间等）；
- 手机有网（4G/5G/WiFi 均可）。

### 5.1 一次性准备 A：放行服务器端口（~5 分钟，只有你能做）

服务已在 8443 监听，但 AWS 安全组未放行：

1. AWS 控制台 → EC2 → 实例 **`i-0774fa15e542c6f1d`** → 「安全」标签 → 点安全组 → **编辑入站规则** → 添加规则：
   - 类型 `自定义 TCP`，端口 `8443`，来源 `0.0.0.0/0`（最简单；想更稳妥就填手机运营商网段）
2. **验证**：手机浏览器打开 `http://35.169.46.183:8443/healthz`
   - 期望看到：`{"ok": true, "asr_ready": true, "openclaw": true, ...}`
   - `asr_ready: false` 说明服务刚重启还在热身，等 1 分钟刷新。

**强烈建议顺手做**（防长对话越聊越卡）：同实例页 → 操作 → 实例设置 → **更改积分规格 → Unlimited**。这台是 t4g 突发型实例，ASR 是持续 CPU 负载，积分烧光后所有转写延迟会恶化 3-5 倍（红队 R4，开发期间已摸到边）。

### 5.2 一次性准备 B：把插件装进 Even App（~3 分钟）

官方开发者模式 = 扫码加载插件 URL：

1. 在任何装了 Node.js 的电脑上（或 SSH 到服务器上）执行：
   ```bash
   npx @evenrealities/evenhub-cli qr --url http://35.169.46.183:8443/plugin/
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
| **提问** | 手机亮屏停在插件页 → **按住大按钮说话（≤25 秒）→ 松手** → 抬眼看眼镜 |
| 重说 | 转写确认的 1.2 秒内重新按住说话 |
| 翻页 | **单击镜腿**；或看手机上的眼镜预览 |
| 打断长回答 | 手机「打断」按钮（已收到的内容保留可翻页） |
| 清屏 | 手机「清屏」按钮 |
| 退出插件 | **双击镜腿**（官方标准手势，会弹确认） |
| 换服务器/重新配对 | 主屏设置入口 |

读屏说明：状态条最左的符号是状态字形（`●`聆听 `◐`思考 `▶`回答 `√`完成 `×`错误 `！`警告）——**瞥一眼就知道系统在干什么**。这些字形全部经官方度量库与官方模拟器截图双重确认在 G2 字库内（早期用的 `◉◔▸✓✕⚠` 在真机上一个都画不出来，见 [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md)）。回复一页 8 行 ≈ 224 汉字，页脚 `‹ 2/3 ›` 是页码。

### 5.5 注意事项（设计如此，不是 bug）

- **手机必须亮屏且停在插件页**。锁屏/切后台后 mic 和连接大概率被系统挂起（Even App 的 WebView 限制，我们控制不了）——此时眼镜会显示「⛓ 连接丢失」而不是假装正常。回到插件页自动重连，断线期间工部仍在干活，回来直接看结果。
- 当前是 http/ws 明文传输（官方 dev 模式支持）。升级 TLS 只差一个域名：见 6.4。

### 5.6 没有眼镜也能先玩（现在就行）

安全组放行后，电脑 Chrome 打开：
```
http://35.169.46.183:8443/plugin/harness/harness.html
```
允许麦克风 → 页面里有块"假眼镜屏" → 走 5.3 配对 → 按住说话问真工部。与真机代码路径完全一致，还能模拟镜腿点击和断网。

---

## 6. 运维手册

### 6.1 服务管理（systemd 用户服务）

```bash
systemctl --user status lens-gateway     # 状态
systemctl --user restart lens-gateway    # 重启（warmup ~1 分钟，healthz 的 asr_ready 为准）
systemctl --user stop/start lens-gateway
journalctl --user -u lens-gateway -n 100 --no-pager   # 日志
curl -s http://127.0.0.1:8443/healthz                  # 健康（本机）
```
服务已 enable（跟随用户会话自启；崩溃 3 秒自动拉起；内存上限 3G）。

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
    "session_ttl_seconds": 86400  // 离线且静默超过此时长的会话被回收（0 = 永不）
  },
  "openclaw": {
    "url": "ws://127.0.0.1:18789",            // 工部网关
    "config_path": "~/.openclaw/openclaw.json" // token 来源（运行时读）
  }
}
```

### 6.4 升级 TLS（有域名后）

1. 域名 A 记录 → `35.169.46.183`；
2. 装 caddy，Caddyfile 两行：`你的域名 { reverse_proxy 127.0.0.1:8443 }`（自动 Let's Encrypt）；
3. 安全组放行 443，扫码 URL 换 `https://你的域名/plugin/`。插件地址自动推导会跟着用 `wss://`。

### 6.5 更新代码

```bash
cd ~/EvenRealities-Claw && git pull
cd plugin && npm install && npx vite build      # 插件有改动时
systemctl --user restart lens-gateway
```

### 6.6 跑测试

```bash
cd ~/EvenRealities-Claw/gateway
.venv/bin/pip install -r requirements-dev.txt      # 测试依赖（pytest / pytest-asyncio）
PYTHONPATH=. .venv/bin/pytest tests/ -q            # 208 单测，秒级
PYTHONPATH=. .venv/bin/python tests/e2e_sim.py     # 端到端，自足运行（~2 分钟）
```

`e2e_sim.py` 默认自己拉起 `demo/fake_openclaw.py` 作为 **agent 测试夹具**——它跑的是与真
网关完全相同的 protocol v3，唯一区别是回复文本来自剧本而非模型。这是测试里的 test
double，**不是演示链路的替身**：演示必须接真 agent。要拿真 agent 跑同一套断言：

```bash
LENS_E2E_AGENT_URL=ws://127.0.0.1:18789 \
LENS_E2E_AGENT_CONFIG=~/.openclaw/openclaw.json \
PYTHONPATH=. .venv/bin/python tests/e2e_sim.py
```

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
4. 管理接口（配对码/设备列表/吊销）只监听 loopback，必须先 SSH 进服务器；
5. 隐私：原始 PCM 不落盘（转写即丢）；按住说话 = 物理收音边界，无任何 always-on 监听；聆听态手机有 ●REC、眼镜状态条有 ◉；
6. 已知薄弱点：明文传输（6.4 升级路径）；`gh` CLI 的 GitHub token 以明文存于本机（与本系统无关，建议换 fine-grained token）。

---

## 9. 未完成项与已知限制

| 项 | 状态 | 说明 |
|---|---|---|
| 五项真机实测 | ❌ 需物理眼镜 | 见第 10 节清单 |
| TLS | ⚠️ | 当前 http/ws；差一个域名（6.4） |
| 锁屏可用 | ❌ 设计内放弃 | 产品定位"亮屏按住说话"；锁屏存活时长属于真机实测项 |
| 多 agent 路由 | ❌ 阶段三 | 当前固定工部；"问格物…"/"切到…"文法在设计文档已定稿 |
| TTS 语音回放 | ❌ 阶段三 | 你已确认 MVP 纯 HUD 文本 |
| 都察告警上屏 | ❌ 阶段三 | 告警管道（去重/限流/熔断）设计已定稿 |
| R1 戒指支持 | ⚠️ 部分 | SDK 事件已监听（EventSourceType.RING 与镜腿同路），未单独测试 |
| ASR 独立实例 | ❌ 中期 | 当前与 agent 同机（4 线程 + 全局串行锁缓解）；重负载并行时是已知瓶颈（R5） |
| `evenhub qr` 扫码入口在 App 内的具体位置 | ⚠️ 未验证 | CLI 与官方文档确认存在；App 内入口位置等你拿到真机确认，备选 ehpk 上传路径已写明（5.2） |

## 10. 五项真机实测清单

拿到眼镜后第一周建议完成（每项 10-20 分钟），结果用于校准 `config.json`：

1. **后台存活**：插件工作中锁屏/切后台，计时到眼镜出现「⛓ 连接丢失」——得出真实可用窗口（iOS/Android 分别测）；
2. **mic 仲裁**：插件聆听中长按镜腿触发官方 Even AI，看是否出现「麦克风没有声音」告警（看门狗应在 ~1s 内报）；反向：Even AI 用完后插件能否恢复收音；
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
| §4「插件协议冒烟（jsdom + 存根网关）25/25」 | **不存在** | **部分补上**：`plugin/tests/` 已有 36 个 vitest 用例（桥接层 26 + 字形契约 10），但覆盖的是 **bridge 层**；WS 协议层（配对/resume/退避重连/refresh）的冒烟**仍未实现** |
| §4「心跳看门狗专项 通过」 | **不存在** | **仍未实现** |

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
| **PCM 载荷推 `number[]`** | ❌ **仍未覆盖**：真机若为 base64 字符串或 `Uint8Array`，归一路径未测（列入 M7） |
| **麦克风是本地 getUserMedia（毫秒级）** | ❌ **仍未覆盖**：真机 BLE 启麦冷启动延迟无法模拟。已把插件改为「先开麦、确认成功、再发 ptt start」，但 `mic_warmup_seconds` 的真值待真机回填 |

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
| `device/hud.py`（设备抽象 + 帧租约） | 330 | **24** |
| `session.py`（装配）+ `voice/pipeline.py` + `server.py` 会话回收 | 560 | **16** |
| `asr.py` + `openclaw.py` + `server.py` 其余部分 | ~640 | 端到端覆盖，无独立单测（排在 M7） |
| `plugin/src/`（TypeScript） | ~1400 | **36**（桥接层 26 + 字形契约 10；WS 层仍为 0） |

端到端：`tests/e2e_sim.py` **22/22**，自足运行、不依赖任何仓库外服务。
仓库**仍无 CI**（无 `.github/`），排在 M7。

### 13.6 真正只能靠真机判定的（6 项）

上一版列了 8 条，其中「真实字宽」「特殊字形可用性」「`\n` 换行语义」三条已在 §13.4 判定并移除。
剩下的是：

1. BLE 渲染时序与闪烁（`textContainerUpgrade` 的真实往返延迟与合并窗口）
2. 镜腿 / 戒指事件是否真的能到达 WebView（**官方模拟器把 `eventSource` 硬编码成 1**，测不了左右与戒指）
3. 麦克风仲裁与 BLE 启麦延迟（`mic_warmup_seconds` 待回填）
4. `audioControl` 的单次连续时长上限
5. 插件在后台 / 锁屏下的存活时长
6. `FOREGROUND_ENTER/EXIT` 在真机上的实际触发时机

**真机第一天只做标定与复验，不做设计变更。**
