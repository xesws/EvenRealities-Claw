# EvenRealities-Claw 🦞👓

通过 Even Realities G2 智能眼镜，以语音随时调用私有服务器上的 OpenClaw 多 Agent 系统（工部 / 格物 / 都察）与 Hermes Agent，并在眼镜 HUD 上获得实时状态反馈与分页回复。

```
G2 眼镜 ◀─BLE─▶ 手机 Even App 插件 ◀─WSS─▶ Lens Gateway ◀─loopback─▶ OpenClaw Agents + Hermes
 mic/HUD        (哑终端+看门狗)        ASR·路由·HUD帧编排        工部·格物·都察
```

**当前状态**：规划阶段（v0.1）。设计与计划已定稿，尚未开始编码。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/DEVELOPMENT-PLAN.md](docs/DEVELOPMENT-PLAN.md) | **开发计划**：MVP 定义 + 四阶段任务分解（含验收标准） |
| [docs/DESIGN.md](docs/DESIGN.md) | **系统设计**：调研结论、总体架构、「一瞥 HUD」UI 规范（状态机/Layout/更新节奏）、红队风险清单 R1-R14 |

## 规划中的仓库结构

```
plugin/     # Even Hub 插件（TypeScript，跑在官方 App WebView 内）
gateway/    # Lens Gateway（Python：WSS 服务、faster-whisper ASR、Agent 路由、HUD 帧编排）
protocol/   # HUD 帧协议 JSON Schema（TS/Python 双端类型的单一事实源）
docs/       # 设计与计划文档
```

## 核心设计原则

1. **0.5 秒瞥视契约** — 任何状态下，状态条最左 3 字符（agent 徽记 + 状态符）即可读懂系统在干什么
2. **眼镜是哑屏** — 折行、分页、节流全部在服务器端完成，下发幂等整屏帧
3. **凭证永不出服务器** — 手机端只持有可吊销的短时效设备 JWT
4. **按住说话** — 无唤醒词、无 always-on 监听，原始音频不落盘

> 注：本仓库为 public。文档为脱敏版本，不包含内部端口、路径与凭证信息；如需私有化请在 GitHub Settings 中切换可见性。
