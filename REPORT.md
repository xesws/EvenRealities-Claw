# 交付报告：阶段一（模拟器闭环）+ 阶段二（真机 MVP）

> 2026-06-11 · v0.2.0
> 结论先行：**整条链路已开发完成并在服务器上实弹验证通过**——按住说话 → 真实中文语音 → faster-whisper 转写 → 真实工部 agent 回复 → HUD 帧分页渲染，端到端 11 秒，全状态机 S2→S3→S4→S6→S7 跑通。服务已作为 systemd 服务常驻运行。**你拿到眼镜后只需做 3 件事即可开始使用**（见第 3 节，预计 10 分钟）。

---

## 1. 开发了什么

### 1.1 总体形态

```
G2 眼镜 ◀─BLE(官方桥)─▶ 手机 Even App 内的插件 ◀─WS─▶ Lens Gateway (本服务器:8443) ◀─loopback─▶ 工部
 mic/HUD                  (WebView, 哑终端+看门狗)        ASR·状态机·帧编排·设备JWT
```

三个组件 + 一份协议，全部在本仓库：

| 组件 | 技术栈 | 代码量 | 状态 |
|---|---|---|---|
| `protocol/` Lens 协议 v1 | JSON over WS + 二进制 PCM | PROTOCOL.md | 定稿 |
| `gateway/` Lens Gateway | Python 3.9 / aiohttp / faster-whisper / PyJWT | 8 模块 | **已部署运行**（systemd 用户服务 `lens-gateway`，端口 8443） |
| `plugin/` 眼镜插件 | TypeScript / Vite / 官方 even_hub_sdk@0.0.10 | 8 源文件，bundle 88.7kB | 已构建，由网关托管于 `/plugin/` |
| `plugin/harness/` 浏览器模拟器 | 同上 | mock 宿主桥 | 无需眼镜即可全链路体验 |

### 1.2 Lens Gateway（服务器端核心）

- **WS 接入与认证**（`server.py`/`auth.py`）：一次性 6 位配对码（10 分钟有效）→ 设备注册 → accessToken（JWT 15 分钟）+ refreshToken（仅存哈希）。单设备可吊销。管理接口仅 loopback。**OpenClaw 网关 token 全程不出服务器**（红队 R8 铁律），运行时从 `~/.openclaw/openclaw.json` 读取。
- **ASR 管线**（`asr.py`）：双模型策略——tiny 做聆听态 partial（仅供显示，~670ms/跳），base 做松手后 final（路由与发送只认它，RTF≈0.35）；热词 `initial_prompt`（工部/格物/OpenClaw/链路…）；local-agreement 稳定前缀防止转写跳变；**全局解码串行锁**（两个 ctranslate2 实例并发解码会在 4 核 ARM 上互锁，实测踩坑后加锁根治）。
- **HUD 状态机**（`session.py`）：S0 待机 / S2 聆听 / S3 确认 / S4 思考(计秒) / S6 流式回复 / S7 翻页阅读 / S8 错误，全部按设计文档「一瞥 HUD」实现。帧规则：状态切换帧免节流、内容帧 ≤2Hz 且 coalescing 只留最新；会话跨 WS 重连存活，重连后**1 帧恢复现场**；25 秒说话软上限；>0.8s 无音频自动报「麦克风没有声音」（mic 被抢看门狗）。
- **排版引擎**（`textkit.py`）：CJK 折行 17 汉字/行（半角折半计宽、行首行尾标点禁则、拉丁词不切断）、markdown 强制剥离（表格降级"共n行请在手机查看"、URL 只留域名、代码块截断）、85 字/页分页 + 上页末句锚点。
- **OpenClaw 适配器**（`openclaw.py`）：连接握手（protocol v3）→ `chat.send` 流式 delta/final/error → `chat.abort`；delta 兼容增量/累计两种形态；runId 僵尸标记杜绝打断后的迟到事件污染（R14）。lens 会话独立 sessionKey（`lens:<deviceId>`），首条消息注入小屏输出风格指令（先结论、短句、禁 markdown、≤170 字）。

### 1.3 眼镜插件（手机端）

- **眼镜渲染**（`glasses.ts`）：按协议布局契约创建 3 个文本容器（状态条 576×32 / 正文 576×220 / 页脚 576×36），120ms 防抖 + 只写变化容器 + 串行写（BLE 队列慢，官方模板同款策略）；单击镜腿=翻页、双击=退出（官方标准模式）；protobuf 零值省略（CLICK_EVENT=0 到达时为 undefined）已按官方模板做归一。
- **连接层**（`ws.ts`）：指数退避+抖动重连（1→30s，单飞锁）；seq 过滤丢弃旧帧；心跳 20s×2 无响应 → **本地看门狗直接向眼镜推「⛓ 连接丢失·重连中」**（消灭"旧帧撒谎"，红队 R1 的关键缓解）；token 过期自动 refresh 重试；PCM 合并成 ≤200ms 块上行。
- **手机 UI**（`ui.ts`）：配对屏（网关地址自动推导 + 6 位码）→ 主屏（按住说话大按钮、●REC、状态行、576×288 眼镜画面实时预览、打断/清屏、电量显示、设置/重新配对）。
- **浏览器模拟器**（`harness/`）：完整 mock 官方宿主桥（实测过真 SDK 的消息信封），假眼镜屏黑底绿字按坐标渲染，真麦克风重采样 16kHz 推流——**在电脑浏览器里就是一台"假眼镜"**，连真网关、问真工部。

### 1.4 验证结果（全部可复跑）

| 验证 | 结果 |
|---|---|
| 单元测试（折行/分页/markdown/认证/JWT） | 26/26 通过（`gateway/tests/`） |
| 插件构建 + strict tsc + jsdom 协议冒烟 | 25/25 通过，心跳看门狗专项通过 |
| **端到端闭环**（真 ASR + 真工部，`tests/e2e_sim.py`） | **14/14 通过**：配对→PTT→中文语音→3-4s 转写上屏→S4→工部回复→分页→seq 单调→每行≤17 字→重连 1 帧恢复→reset |
| **生产部署冒烟**（systemd 服务实例） | S2→S3→S4→S6→S7，工部回复"收到，眼镜链路畅通，随时待命。"，全程 11.0s |

实测性能（t4g.xlarge）：说完→转写上屏 3-4s；说完→回复完成 ~10s（其中工部生成 ~7s，是延迟大头且不可压缩，与设计预算一致）。

### 1.5 开发中发现并修复的三个工程问题（记录备查）

1. **ctranslate2 并发互锁**：warmup 与首个 partial 并发解码时两实例 OMP 线程在 4 核 ARM 上互锁 → 全局解码串行锁。
2. **py3.9 事件循环绑定**：`asyncio.Lock` 在 `web.run_app` 建立自己的 loop 之前构造导致 "attached to a different loop" → 服务对象改为在循环内构造。
3. **ARM 进程级首解码延迟**：ctranslate2 int8 在 ARM 上每进程首次解码需 20-35s（kernel 初始化）→ 启动 warmup 吃掉该成本，`/healthz` 增加 `asr_ready` 字段，就绪后才可用。

---

## 2. 与开发计划的对照（如实说明）

| 计划项 | 状态 |
|---|---|
| 1.1-1.7 阶段一全部 | ✅ 完成（模拟器闭环 = e2e_sim 14/14 + 浏览器 harness） |
| 2.2 真机音频通路 / 2.3 真机渲染 | ✅ 代码与协议层完成，**物理眼镜上的最终确认待你到手实测** |
| 2.4 公网部署 | ⚠️ 服务已 0.0.0.0:8443 常驻；**安全组放行需你在 AWS 控制台操作**（见 3.1）；当前为 http/ws 明文（官方 dev 模式扫码支持 http URL），TLS 升级路径见 5.2 |
| 2.5 插件本地看门狗 | ✅ 完成并专项测试 |
| 2.1 五项真机实测 | ❌ 无眼镜无法执行——后台存活/Even AI 抢 mic/TouchBar 暴露度/audioControl 上限/真实刷新率，留作你拿到眼镜后第一周的事 |
| 阶段三（多 agent 路由/TTS/都察告警） | ❌ 未做（按你的指示只做前两阶段；当前固定路由到工部） |

---

## 3. 【最重要】拿到眼镜后怎么用

### 3.0 前提

- Even Realities G2 已用官方 **Even Realities App**（iOS/Android）完成蓝牙配对，眼镜工作正常；
- 手机能上网（流量或 WiFi 均可）。

### 3.1 一次性准备 A：打开服务器入口（5 分钟，在 AWS 控制台）

服务器已在 8443 端口运行，但 AWS 安全组还没放行（这步只有你能做）：

1. 打开 AWS 控制台 → EC2 → 实例 `i-0774fa15e542c6f1d` → 安全 → 安全组 → **编辑入站规则**；
2. 添加规则：类型 `自定义 TCP`、端口 `8443`、来源 `0.0.0.0/0`（图省事）或你手机运营商网段（更安全）；
3. 验证：**手机浏览器**打开 `http://35.169.46.183:8443/healthz`，看到 `"ok": true, "asr_ready": true` 即通。

（可选但强烈建议，防"越聊越卡"：同页面 → 操作 → 实例设置 → 更改积分规格 → 勾选 **Unlimited**。）

### 3.2 一次性准备 B：把插件装进 Even App（3 分钟）

官方 dev 模式 = 扫二维码加载插件 URL：

1. 在任何装了 Node 的电脑（或直接在这台服务器上）执行：
   ```bash
   npx @evenrealities/evenhub-cli qr --url http://35.169.46.183:8443/plugin/
   ```
   终端会打出一个二维码；
2. 打开手机 **Even Realities App** → 扫码入口（App 内开发者/扫码功能）→ 扫上面的二维码；
3. App 会在内置 WebView 里加载 OpenClaw Lens 插件，看到深色的配对页即成功。

> 备选路径：若你的 App 版本找不到扫码入口，用 `evenhub login` + `evenhub pack plugin/app.json plugin/dist -o lens.ehpk` 打包后上传 hub.evenrealities.com 开发者门户的 private build（官方文档：Sideload via QR, or upload a private build to the dev portal）。

### 3.3 一次性准备 C：配对（1 分钟）

1. SSH 到服务器执行：
   ```bash
   ~/EvenRealities-Claw/gateway/.venv/bin/python -m lens_gateway.main pair-code
   # 输出例：配对码：847291（10 分钟内有效，一次性）
   ```
2. 手机插件配对页：网关地址保持默认（自动填好），输入 6 位配对码 → 确认；
3. 看到主屏（按住说话按钮 + 眼镜画面预览）即配对完成。凭证已存手机，以后打开即用，无需重复。

### 3.4 日常使用

| 动作 | 操作 |
|---|---|
| **提问** | 手机亮屏开着插件页 → **按住「按住说话」说话 → 松手** → 抬眼看眼镜：聆听转写 → 「工 ◔ 思考」→ 回复分页显示 |
| 翻页 | **单击镜腿**（或看手机预览） |
| 打断 | 手机「打断」按钮 |
| 清屏 | 手机「清屏」按钮 |
| 退出插件 | **双击镜腿**（官方标准手势） |
| 换默认服务器/重新配对 | 主屏右上设置 |

说话上限 25 秒（自动截断收音）；回复一页 85 字，眼镜右下角 `2/3 ›` 表示页码。

### 3.5 没有眼镜也能先玩（现在就可以）

电脑浏览器打开 `http://35.169.46.183:8443/plugin/harness/harness.html`（Chrome，需允许麦克风）——页面里有一块 576×288 的"假眼镜屏"，按住说话→真转写→真工部回复，与真机体验完全一致（安全组放行后即可访问；在服务器本机可用 `curl` + 端口转发先验）。

### 3.6 排障速查

| 现象 | 处理 |
|---|---|
| 手机打不开 healthz | 安全组 8443 没放行（3.1） |
| 插件页"连接丢失" | `systemctl --user status lens-gateway`；重启 `systemctl --user restart lens-gateway`（启动后 warmup ~1 分钟） |
| 配对码无效 | 码是一次性+10 分钟，重新生成 |
| 眼镜黑屏无反应 | 看手机预览是否正常：预览正常→眼镜 BLE 问题（Even App 重连眼镜）；预览也没有→看服务日志 `journalctl --user -u lens-gateway -n 50` |
| 转写慢/越聊越卡 | 开 Unlimited 积分（3.1 可选项）；查 `vmstat 1 3` 的 st 列 |
| 怀疑手机凭证泄漏 | `… main.py revoke dev_xxx` 吊销后重新配对 |

---

## 4. 安全边界（已实现）

- OpenClaw 全权 token 永不出服务器；手机只持 15 分钟 JWT + 可吊销 refreshToken（仅存哈希）；
- 对外 API 仅 4 类动作（语音/翻页/打断/清屏），无任何 OpenClaw RPC 透传；管理接口仅 loopback；
- 原始 PCM 不落盘，转写即丢；按住说话=物理收音边界，无 always-on 监听；
- 眼镜会话与 Discord 会话完全隔离（独立 sessionKey）。

## 5. 已知限制与下一步

1. **明文传输**：当前 http/ws（dev 模式可用）。升级：买个域名解析到 35.169.46.183 → caddy 反代自动 Let's Encrypt → 插件地址改 `wss://`。
2. **锁屏即断**（设计内）：产品定位"亮屏按住说话"；锁屏存活时长是你拿到眼镜后五项实测之一。
3. **单 agent**：固定工部。多 agent 路由（"问格物…"/"切到…"）、Hermes TTS、都察告警 = 阶段三。
4. **五项真机实测**（DEVELOPMENT-PLAN 2.1）后即可校准：折行宽度 `wrap_chars`、刷新节流 `throttle_ms` 等均为 `~/.lens-gateway/config.json` 可调项。
