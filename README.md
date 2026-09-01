# EvenRealities-Claw 🦞👓

通过 Even Realities G2 智能眼镜，以语音随时向私有服务器上的 agent 提问，并在眼镜 HUD 上获得实时状态反馈与分页回复。

```
G2 眼镜 ◀─BLE─▶ 手机 Even App 插件 ◀─WSS─▶ Lens Gateway ◀─loopback─▶ agent
 mic/HUD        (哑终端+看门狗)        ASR·状态机·排版·帧编排        │
                                            ▲                    ├─ lens_agent（自研，直连 DeepSeek）
                          控制面 HTTP + Bearer │                    └─ OpenClaw（第三方，可选）
                                     ┌────────┴────────┐
   厂商模型 / Claude Code ──MCP HTTP─▶│ lens_mcp（独立进程）│  8 tools · 3 resources · 1 prompt
                                     └─────────────────┘
```

**当前状态**：v0.7.0 —— 阶段一（模拟器闭环）+ 阶段二（真机 MVP）+ 模拟阶段全量开发 M0–M7 完成。
pytest **437**、插件 vitest **82**、语音端到端 **31/31**、MCP 四进程真链路 **27/27**、
**真 agent 端到端 21/21**（真 DeepSeek），前四者自足运行、不依赖任何仓库外服务。
CI 在 `.github/workflows/`。**眼镜到手即可按 [REPORT.md](REPORT.md) 第 5 节开始使用。**

本机跑起来（三种模式，差别只在 `chat.send` 的对端是谁）：

```bash
export LENS_LLM_API_KEY=sk-...
./demo/start.sh --lens     # 推荐：拉起自研 agent，直连 DeepSeek
./demo/start.sh --real     # 连本机真的 OpenClaw 网关
./demo/start.sh            # 替身模式，离线调链路用（屏幕徽记会带「?」自证）
```

把这副眼镜接给任意厂商的模型：

```bash
cd gateway && .venv/bin/pip install -r requirements-mcp.txt
PYTHONPATH=. .venv/bin/python -m lens_mcp
claude mcp add --transport http even-glasses http://127.0.0.1:8765/mcp
```

## 文档

| 文档 | 内容 |
|---|---|
| [REPORT.md](REPORT.md) | **交付报告**：开发内容与技术方法 + **拿到眼镜后的完整上手流程** + 排障速查 |
| [protocol/PROTOCOL.md](protocol/PROTOCOL.md) | Lens 协议 v1.1：插件↔网关 WS 协议（认证/渲染帧/遥测上行/时序） |
| [docs/DEVELOPMENT-PLAN.md](docs/DEVELOPMENT-PLAN.md) | 开发计划：MVP 定义 + 四阶段任务分解（含验收标准） |
| [docs/DESIGN.md](docs/DESIGN.md) | 系统设计：调研结论、总体架构、「一瞥 HUD」UI 规范、红队风险清单 R1-R14 |
| **[docs/MCP-SURFACE.md](docs/MCP-SURFACE.md)** | **硬件 MCP 表面**：8 tools / 3 resources / 1 prompt、帧租约语义、鉴权设计、四进程端到端证据 |
| **[docs/AGENT-LAYER.md](docs/AGENT-LAYER.md)** | **自研 agent**：~900 行手写 loop、四道闸（无 exec 档 / 代码选 skill / 写能力绑资源 / 审计）、DeepSeek 接入的**四条实测事实**（§13.1） |
| **[docs/HARDWARE-SPEC.md](docs/HARDWARE-SPEC.md)** | **G2 规格基准**：仓库里所有硬件魔数的唯一真源，每条标注出处等级（文档 / 实测·SDK / 实测·模拟器 / 实测·度量库 / 待真机） |
| [docs/GLYPH-TABLE.md](docs/GLYPH-TABLE.md) | 字形判定表：哪些字形 G2 画得出来、哪些画不出来，及其截图与度量证据 |
| [docs/SIMULATOR-PARITY.md](docs/SIMULATOR-PARITY.md) | 模拟保真度对照：每条结论属于「官方模拟器已判定 / 仅自建夹具 / 真机不可替代」哪一档 |
| [protocol/hud-contract.json](protocol/hud-contract.json) | HUD 契约（机器可读）：画布几何、容器版式、语义字形表 —— 网关与插件读同一份 |

## 仓库结构

```
plugin/     # Even Hub 插件（TypeScript/Vite，跑在官方 App WebView 内）
            #   harness/ 浏览器夹具（可注入故障）· probe/ 官方模拟器探针页 · tools/ 度量导出与模拟器自动化
gateway/    # Lens Gateway（Python/aiohttp：WS 服务、设备 JWT、faster-whisper ASR、排版、HUD 帧编排、控制面）
            #   providers/  agent 抽象（openclaw / lens）+ W6 溯源
            #   lens_mcp/   MCP 表面（独立进程，不持有麦克风/ASR/设备凭证）
            #   lens_agent/ 自研 agent（独立进程，只监听回环，LLM key 只存在于这里）
protocol/   # Lens 协议 v1.1（插件与网关的契约）
demo/       # 本地一键起链路（--lens / --real / 替身）
deploy/     # systemd 服务单元
scripts/    # 安装脚本
docs/       # 设计与计划文档
.github/    # CI：插件 typecheck+vitest+build · 网关 pytest · 端到端
```

## 核心设计原则

1. **0.5 秒瞥视契约** — 任何状态下，状态条最左的状态字形即可读懂系统在干什么（字形全部经官方度量库 + 官方模拟器截图确认在 G2 字库内）
2. **眼镜是哑屏** — 折行、分页、节流全部在服务器端完成，下发幂等整屏帧
3. **凭证永不出服务器** — 手机端只持有可吊销的短时效设备 JWT
4. **按住说话** — 无唤醒词、无 always-on 监听，原始音频不落盘
5. **一块屏幕，一个持有者** — 语音链路与 MCP 客户端共用同一块屏，写屏要过帧租约；
   用户开口说话**无条件抢占**，被抢的一方只能靠轮询发现（MCP 规范做不到推送）
6. **权限由架构而非自律保证** — 面向外部的 MCP 进程能做的事等于控制面暴露的九个路由；
   自研 agent 的能力枚举里**根本没有 exec 这一档**，skill 由确定性代码选（模型说什么都改不了它），
   写能力必须在代码里绑定到具体资源，每次调用与每次拒绝都进审计日志
7. **屏幕不许撒谎** — 对端不是生产 agent 时状态条徽记带「?」（演示可当场 `curl /healthz` 自证）；
   被预算掐断的半截回答带「…（未说完）」而不是伪装成「√ 完成」；
   遥测从没上报过就返回 null 而不是编一个电量数字

> 注：本仓库为 public。文档为脱敏版本，不包含内部端口、路径与凭证信息；如需私有化请在 GitHub Settings 中切换可见性。
