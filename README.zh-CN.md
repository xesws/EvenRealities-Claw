# EvenRealities-Claw 🦞👓

[English →](README.md)

用 [Even Realities G2](https://www.evenrealities.com/) 智能眼镜跟一个私有 agent 说话。
按住镜腿、开口，答案落在 576×288 的 HUD 上 —— 折行、分页、限频全部由你自己的服务器完成。
同一块屏幕还是一个 **MCP 表面**，任意厂商的模型（Claude Code、你的 IDE、任何会说 MCP 的东西）
都能往你正戴着的这副眼镜上写字。

**演示里没有任何一处是假的。** 真麦克风、真 `faster-whisper` 转写、真 DeepSeek、
真工具调用打到真 API、真固件字形度量。当初的约定是「只有数据可以自制」，
最后连数据也没有自制的必要 —— 整个仓库里唯一被替换掉的输入是那几段演示音频，
而音频就是数据。下面这一节讲的是怎么自己去证伪它，而不是让你信这段话。

---

## 架构

![架构](docs/assets/diagrams/architecture.zh.svg)

> 本文档里每一张图都是真的 [draw.io](https://app.diagrams.net/) 文件。你看到的 `.svg`
> 与 `.drawio` 由**同一份 XML** 渲染而来，所以图和源永远对得上。要改就打开
> `docs/assets/diagrams/*.drawio`。见 [图](#图)。

四个进程，三档信任：

| 进程 | 端口 | 手里有什么 | 干什么 |
|---|---|---|---|
| **Lens 插件** | — | 一个可吊销的设备 JWT | 跑在官方 Even App 的 WebView 里。一个带看门狗的哑终端：开麦、推 PCM、来什么帧画什么帧。 |
| **Lens Gateway** | `8443` | 设备密钥、ASR 模型 | WS 服务、配对、流式 ASR、排版、HUD 状态机、帧租约、控制面。 |
| **lens_agent** | `18790` | **LLM key 的唯一一份** | 手写的 agent loop。只监听回环。 |
| **lens_mcp** | `8765` | 什么都没有 | 给外部模型用的 MCP 表面。没有麦克风、没有 ASR、没有设备凭证 —— 它能做的事，恰好等于控制面那九个路由允许的事。 |

这个切法本身就是设计。`lens_mcp` 是第三方模型唯一接触到的进程，所以它是那个什么都不持有的进程。
它读不了麦克风，不是因为被禁止，而是因为它的代码里根本没有能读麦克风的东西。

---

## 哪些是真的，以及怎么验

这个项目最核心的那句话是「演示不是壳」，所以下面给的是怎么去证伪它。

**1. 直接问网关，对端到底是谁。** 溯源（`W6`）是在 agent 握手时记下来的，不是在文档里声称的：

```bash
curl -s http://127.0.0.1:8443/healthz | python3 -m json.tool
```

```json
"agent": { "backend": "lens", "model": "deepseek-v4-flash",
           "production": true, "endpoint": "ws://127.0.0.1:18790" }
```

`production` 为 `false` 时，眼镜状态条的徽记会自己带上一个「?」。
**屏幕自己会告状** —— 你不需要相信终端里打出来的字。

**2. 拿一个 WAV 文件把整条链路跑一遍，不经过浏览器。**
`demo/verify_audio.py` **本身就是一台设备**：它连 `/ws` 配对、按 PTT、按真实时间把 PCM 一块块推上去、
松手，然后把网关下发的每一帧原样打出来。唯一被换掉的是声音的来源，而声音是数据。

```bash
cd gateway && .venv/bin/python ../demo/verify_audio.py ../demo/audio/en-weather.wav
```

本机实测输出：

```
S2  Lens ● Listening 0:02   | What's the weather like today? Do I need a jet?▌
S3  Lens → Heard            | What's the weather like today? Do I need a jacket?
S4  Lens ◐ Thinking 5s      | ...
S5  Lens ◆ Weather          | ← 模型真的调了天气工具
S7  Lens √ Done             | It is 16 degrees and overcast in San Francisco, with a high of 18
```

注意 S2 到 S3 之间那个 `jet` → `jacket`：这是流式 ASR 在最终转写时自己纠了回来。
一段排练好的演示不会特意先错一次。

**3. 实测延迟**（同一个脚本，真语音 → 真 DeepSeek，各跑一次）：

| 素材 | 音频 | 整轮 | 路径 |
|---|---|---|---|
| `en-navigation.wav` | 2.73 秒 | **6.1 秒** | 无工具 |
| `en-park.wav` | 2.94 秒 | **6.7 秒** | 无工具 |
| `en-weather.wav` | 2.78 秒 | **11.5 秒** | 一次工具调用 |

一次工具调用差不多让整轮翻倍，因为**这里的预算是按「模型往返次数」算的，不是按工具耗时算的** ——
工具本身微秒级就返回了，贵的是回到模型的第二趟。

**4. 连数据最后也没自制。** 当初允许数据是编的，实际做下来一样没编：
`weather` 打 Open-Meteo，`currency` 打 Frankfurter，`now` / `days_until` / `calc` 是算出来的，
`list_*` 和 `remind_*` 真的在读写磁盘上的文件，而 `device` 在从没收到过遥测时**返回 `null`**，
不是返回一个看起来很合理的电量。整个仓库里唯一被替换掉的输入是 `demo/audio/` 里那几段音频，
用 macOS `say` 合成、16kHz 单声道 —— 跟眼镜四麦阵列真正上行的格式一致。

---

## 一轮语音，从头到尾

![语音一轮](docs/assets/diagrams/voice-turn.zh.svg)

这条路径上有三件事不像看上去那么显然：

**麦克风是真的先开了。** 插件等 `audioControl(true)` 返回之后才发 `ptt start`，
所以网关那个「没有声音」的看门狗量的是一件真事。旧设计只给麦克风 **1.4 秒**吐出第一块 PCM ——
而这 1.4 秒要塞下 WS 往返 + BLE 下发 + 固件启麦 + 首块回传 + 插件攒 200ms。
真机上这几乎每一轮都会误报。现在拆成两个计时器，开麦宽限 2.5 秒。

**屏幕是故意做哑的。** 折行、分页、限频全在服务器端，下发的是带单调 `seq` 的幂等整屏帧。
眼镜不对版式做任何决定，所以丢一帧或者重复一帧都不可能把画面搞坏。

**排版用的是固件自己的字形宽度。** `@evenrealities/pretext` 是官方的度量库，
复刻的就是设备上那个 LVGL 构建。Python 的折行引擎复刻了它的算术，
而测试拿这个 JavaScript 库当**外部 oracle** —— 逐条断言两边在整个语料上的折行位置完全一致。
服务器认为的一行，就是设备认为的一行；这是构造出来的，不是靠留安全余量猜出来的。

---

## agent，以及它为什么这么小

![四道闸](docs/assets/diagrams/gates.zh.svg)

`lens_agent` 是一个约 900 行的手写 loop，跑在 OpenAI 兼容端点上。它没有建在任何框架上，
而这是个安全决定，不是审美决定：在这个体量下，「这个 agent 到底能做哪些事」
是一份人真的读得完的清单。

它有 **12 个工具**（`now`、`days_until`、`device`、`weather`、`calc`、`currency`、
`list_show`、`list_add`、`list_remove`、`remind_set`、`remind_list`、`remind_cancel`）
和 **7 个 skill**（`ask`、`daily`、`weather`、`math`、`list`、`device`、`remind`），后面挡着四道闸：

| 闸 | 规则 | 为什么它挡得住 |
|---|---|---|
| **1** | 能力枚举只有 `READ \| WRITE`，**没有 exec 这一档**。 | 不是「告诉模型不许执行命令」，是根本没有一条能执行命令的代码路径。 |
| **2** | `route()` 用纯正则选 skill，连同它的工具白名单一起选定。 | 模型从来不挑自己的权限。等它看到系统提示的时候，工具集已经定死了。 |
| **3** | 写工具在 **import 期**就被绑定到具体文件上。 | 没有任何一个工具带路径参数，所以不存在一个模型能填的参数让它够到别的文件。 |
| **4** | **每次调用和每次拒绝**各写一行 JSON 到 `~/.lens-agent/audit.jsonl`。 | 一次不留痕迹的拒绝，和一次从没发生过的攻击，是分不出来的。 |

这就是为什么提示注入在这里结构上不成立。注入的那句话落在 **user 轮**里 —— 而 `route()` 早就跑完了。
skill、工具白名单、延迟预算在那之前就定死了，这句话没有任何东西可改。
这就是「护栏」和「请你不要」的区别。

有一条设计规则是踩了坑之后才写进 [docs/AGENT-LAYER.md](docs/AGENT-LAYER.md) 的：
**路由判意图，skill 判可行性。** 之前路由顺手判了可行性，结果 agent 对着它明明能办的请求
回了一句「我还不能设提醒」。**说自己做不到一件其实做得到的事，和编一个答案是同一类错误。**

---

## 跑起来

```bash
python3 -m venv gateway/.venv && gateway/.venv/bin/pip install -r gateway/requirements.txt
export LENS_LLM_API_KEY=sk-...          # DeepSeek，或任意 OpenAI 兼容端点

./demo/start.sh --lens --en             # 推荐：真 agent + 英文 HUD
./demo/start.sh --lens                  # 同上，中文 HUD
./demo/start.sh --real                  # 改连本机真的 OpenClaw 网关
./demo/start.sh                         # 离线替身（屏幕徽记会带「?」自证）
```

脚本会打印一个配对码和一个地址。打开、允许麦克风、输入配对码、按住页面上的 PTT 按钮。
没有眼镜的话你看到的是**浏览器夹具** —— 它按官方 `pretext` 的字形宽度，
在真实的 576×288 上渲染 HUD，折行位置与真机一致。

key 只从环境变量读，绝不落盘、绝不进仓库。

## 接上任意厂商的模型

```bash
cd gateway && .venv/bin/pip install -r requirements-mcp.txt
PYTHONPATH=. .venv/bin/python -m lens_mcp
claude mcp add --transport http even-glasses http://127.0.0.1:8765/mcp
```

**8 tools · 3 resources · 1 prompt。** `textkit_paginate` 根本不需要设备 ——
它把排版引擎当纯函数暴露出去，这是证明「一个外部模型真的调到了你的代码」最便宜的办法。
`hud_show` / `hud_page` / `hud_clear` / `hud_release` 要写屏，所以必须先拿**帧租约**。

租约存在的理由是：**一块屏幕只能有一个持有者**。两个 MCP 客户端不会悄悄互相覆盖 ——
输的那个拿到的是一个结构化的 `LEASE_HELD`，里面带当前持有者和 TTL，而不是「最后写的赢」。
而且**用户开口说话无条件抢占**：你的声音永远压过一个机器人的渲染。
被抢的一方只能靠轮询发现，因为 MCP 规范里服务器没有办法主动推。

---

## 决定了整个设计的那些硬件约束

下面每一个数字在 [docs/HARDWARE-SPEC.md](docs/HARDWARE-SPEC.md) 里都有出处，
并标了证据等级（厂商文档 / 实测·SDK / 实测·官方模拟器 / 实测·度量库 / 待真机）。

| 约束 | 后果 |
|---|---|
| 每只眼 576 × 288 px，4 位灰阶（16 级绿） | 整个版式预算的来源。 |
| **行高固定 27px** | 正文框 216px = **正好 8 行**。早先的版本按 5 行排，每一页白白浪费 37% 的屏幕。 |
| **没有字号、没有粗体、不能设对齐** | 旧协议文档里那三列「字号」描述的是硬件根本不存在的东西，已删。 |
| 文本亮度是 `textColor` 0–4，不是 16 级 | 唯一能用的视觉分层：状态条 4、正文 3、页脚 2。 |
| 字体**不是等宽的** | 「字符预算」式的排版模型在原理上就是错的。排版必须由逐字形的真实宽度驱动。 |
| **字库外的字符会被静默丢弃** —— 不留豆腐块 | HUD 最早用的 13 个字形里有 10 个 G2 画不出来。`⛓ 连接丢失` 会渲染成 ` 连接丢失`：最需要被一眼看见的那条告警，反而失去了它的视觉锚。见 [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md)。 |
| `createStartUpPageContainer` 一个页面生命周期**只能调一次** | 自建夹具原来允许无限次调用并恒返回成功，所以这一类 bug 在接上官方模拟器之前是完全看不见的。 |

---

## 仓库结构

```
plugin/     Even Hub 插件（TypeScript / Vite，跑在官方 App 的 WebView 里）
            harness/  可注入故障的浏览器夹具 · probe/  官方模拟器探针页
            tools/    度量导出、pretext oracle、模拟器自动化
gateway/    Lens Gateway（Python / aiohttp）
            formatting/  pretext 度量 → 折行 → 分页 → 净化
            device/      HUD 状态机、帧租约、遥测缓存
            voice/       PTT / ASR / agent 编排
            providers/   agent 抽象（openclaw | lens）+ 溯源
            lens_agent/  手写 agent（独立进程，LLM key 在这里）
            lens_mcp/    MCP 表面（独立进程，什么都不持有）
protocol/   Lens 协议 v1.1 + 机器可读的 HUD 契约
demo/       一条命令起链路 · verify_audio.py · chat.py
tools/      diagrams.py —— 本文档里所有图的生成脚本
docs/       设计、硬件规格、字形表、agent 层、MCP 表面
```

| 组件 | 行数 |
|---|---|
| 网关核心 | 4,403 |
| `lens_agent` | 2,299 |
| `lens_mcp` | 401 |
| 插件与工具链（TS） | 4,510 |
| **测试** | **6,571** |

---

## 验证

| 套件 | 结果 | 覆盖什么 |
|---|---|---|
| `pytest` | **590 通过** | 排版不变量、HUD 状态机、租约语义、鉴权、遥测、agent 四道闸、提醒 |
| `vitest` | **82 通过** | 插件桥接、PCM 三种载荷形态、WS 协议、夹具故障注入 |
| `tsc --noEmit` | 干净 | |
| `e2e_sim.py` | **32/32** | 对着模拟器跑完整语音链路，自足运行 |
| `e2e_mcp.py` | **27/27** | 四个真进程：MCP 客户端 → `lens_mcp` → 控制面 → 网关 → 设备 |
| `e2e_agent.py` | **23/23** | 真 DeepSeek，端到端 |
| `test_asr_quality.py` | CER **0.0085**（阈值 0.05） | 自建 10 条带 ground truth 的数据集、3 个音色，跑的是**生产** ASR 路径 |
| `test_metrics_oracle.py` | — | Python 折行引擎 vs 官方 `pretext`，逐个折行位置比对 |

前四个套件不依赖本仓库以外的任何服务。CI 在 `.github/workflows/ci.yml`。

测试上有两条约定，因为它们正是这些数字有意义的原因：

- **每条新测试都要做变异检验。** 故意把代码改坏，如果测试还过，那它就是装饰。
  这里有好几条测试是没通过这一关之后重写的。
- **一条量的是夹具而不是代码的测试，比没有测试更糟。** 这里有一组回归测试
  在任务还没开始跑的时候就把它取消了，于是它声称要验的那条清理路径**一次都没执行过** ——
  它恰恰因为这个才是绿的。

---

## 核心设计原则

1. **0.5 秒瞥视契约。** 任何状态下，状态条最左那个字形就足以读懂系统在干什么。
   这一组字形全部经官方度量库与官方模拟器截图确认在 G2 字库内。
2. **眼镜是哑屏。** 折行、分页、限频都在服务器端；下发的是幂等整屏帧。
3. **凭证永不出服务器。** 手机端只持有短时效、可吊销的设备 JWT。
   LLM key 只存在于一个进程里，而那个进程只监听回环。
4. **按住说话。** 无唤醒词、无 always-on 监听、原始音频不落盘。
5. **一块屏幕，一个持有者。** 语音与 MCP 共用同一块屏，靠租约仲裁；用户开口无条件抢占。
6. **权限由架构保证，不靠自律。** 面向外部的 MCP 进程能做的事恰好等于控制面九个路由；
   agent 的能力枚举里根本没有 exec 这一档。
7. **屏幕不许撒谎。** 对端不是生产 agent 时徽记带「?」；被预算掐断的半截回答结尾是
   `…（未说完）` 而不是伪装成 `√ 完成`；从没上报过的遥测返回 `null`，不返回一个编出来的电量。

---

## 图

由 `tools/diagrams.py` 生成，一套几何同时出两种格式：

```bash
python3 tools/diagrams.py --gen
```

| 文件 | |
|---|---|
| `docs/assets/diagrams/*.drawio` | 真的 draw.io 文件。用 [app.diagrams.net](https://app.diagrams.net/) 或 VS Code 插件打开即可编辑。 |
| `docs/assets/diagrams/*.svg` | 由同一份 XML 渲染；每个 SVG 的 `content` 属性里还嵌着 `.drawio` 源，所以 **SVG 本身也能直接用 draw.io 打开编辑**。 |

中英文两版**共用一套几何**，只有标签不同，所以两个语言版本展示的是同一个系统。

---

## 文档

| 文档 | 内容 |
|---|---|
| [REPORT.md](REPORT.md) | **交付报告**：开发内容与技术方法 + 拿到眼镜后的完整上手流程 + 排障速查 |
| [protocol/PROTOCOL.md](protocol/PROTOCOL.md) | Lens 协议 v1.1：插件 ↔ 网关 WS（认证 / 渲染帧 / 遥测上行 / 时序） |
| [docs/HARDWARE-SPEC.md](docs/HARDWARE-SPEC.md) | **G2 规格基准**：仓库里所有硬件魔数的唯一真源，每条标注证据等级 |
| [docs/AGENT-LAYER.md](docs/AGENT-LAYER.md) | **自研 agent**：手写 loop、四道闸、DeepSeek 接入的四条实测事实（§13.1） |
| [docs/MCP-SURFACE.md](docs/MCP-SURFACE.md) | **硬件 MCP 表面**：工具清单、帧租约语义、鉴权设计、四进程端到端证据 |
| [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md) | 字形判定表：哪些 G2 画得出来、哪些画不出来，附截图与度量证据 |
| [docs/SIMULATOR-PARITY.md](docs/SIMULATOR-PARITY.md) | 模拟保真度对照：每条结论属于「官方模拟器已判定 / 仅自建夹具 / 真机不可替代」哪一档 |
| [docs/DESIGN.md](docs/DESIGN.md) | 系统设计、「一瞥 HUD」UI 规范、红队清单 R1–R14 |
| [docs/DEVELOPMENT-PLAN.md](docs/DEVELOPMENT-PLAN.md) | 里程碑 M0–M7 与验收标准 |
| [protocol/hud-contract.json](protocol/hud-contract.json) | 机器可读的 HUD 契约 —— 网关与插件读同一份 |

---

## 状态

**v0.7.0。** 模拟器闭环、真机 MVP、M0–M7 全部完成。

有六件事在拿到真眼镜之前**原理上无法验证**，它们被明确列为未验证，而不是悄悄当成没问题：
BLE 渲染时序与闪烁、镜腿事件是否真的能到达 WebView、麦克风仲裁与启麦延迟、
`audioControl` 的时长上限、后台/锁屏存活、满画布版式的 `oversize` 真实阈值。
除此之外的每一条，都已经用官方模拟器、官方度量库或真实端到端跑通验证过。

眼镜到手后，按 [REPORT.md](REPORT.md) 第 5 节开始。

> 本仓库为 public。文档为脱敏版本，不包含内部端口、路径与凭证信息。
