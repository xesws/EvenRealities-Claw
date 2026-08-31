# Lens Gateway

服务器端核心：WSS 接入 + 设备 JWT 认证 + faster-whisper ASR + OpenClaw 适配 + HUD 帧编排
+ 面向厂商模型的 **MCP 表面**（独立进程 `lens_mcp/`）。

## 模块

| 模块 | 职责 |
|---|---|
| `server.py` | aiohttp 服务：`/ws`（插件）、`/plugin/`（托管前端）、`/healthz`、`/admin/*` 与 `/control/*`（共享密钥 Bearer）、离线会话 TTL 回收 |
| `session.py` | 装配层：一台设备 = 一块屏（`device`）+ 一条语音链路（`voice`）。只做装配与消息路由，无业务逻辑 |
| `device/hud.py` | **唯一写屏入口**：状态机 S0-S9、帧构造、节流（状态帧免节流/内容帧 2Hz coalescing）、状态条、分页、计时器、**帧租约**（语音与 MCP 共用一块屏的仲裁，用户开口无条件抢占）、跨重连现场恢复 |
| `voice/pipeline.py` | 语音链路：PTT → PCM → partial 滚动窗 → final 转写 → 确认窗口 → agent → 流式上屏；mic 看门狗 |
| `asr.py` | 双模型管线：tiny partial（显示用）+ base final（路由用）、local-agreement 稳定前缀、全局解码串行锁 |
| `openclaw.py` | OpenClaw 网关 WS 客户端：chat.send 流式事件、abort、runId 僵尸标记 |
| `formatting/` | 排版引擎：G2 字形度量复刻 → 像素盒折行（CJK 标点禁则）→ 8 行/页分页 + 锚点、markdown 剥离、文本净化、字形在库校验 |
| `auth.py` | 配对码 → 设备注册 → accessToken(JWT 15min) + refreshToken（只存哈希），单设备吊销 |
| `control.py` | 控制面：`/control/*` 九个路由，共享密钥 Bearer 鉴权。**MCP 进程能做的事 = 这九个路由**，越权在架构上不成立 |
| `config.py` | 配置（状态目录 `~/.lens-gateway/`，不入库）+ `control_secret()`（首次生成，0600 落盘） |
| `lens_mcp/` | **独立进程**的 MCP 服务器：8 tools / 3 resources / 1 prompt，经控制面驱动网关。不持有麦克风、ASR、设备凭证 —— 外部攻击面被隔离在这里。见 [docs/MCP-SURFACE.md](../docs/MCP-SURFACE.md) |

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m lens_gateway.main serve     # 前台
bash ../scripts/install-service.sh                            # systemd 用户服务
.venv/bin/python -m lens_gateway.main pair-code               # 生成配对码
.venv/bin/python -m lens_gateway.main devices                 # 已配对设备
.venv/bin/python -m lens_gateway.main revoke dev_xxx          # 吊销设备
```

MCP 表面（独立进程，让厂商模型直接驱动眼镜）：

```bash
.venv/bin/pip install -r requirements-mcp.txt
PYTHONPATH=. .venv/bin/python -m lens_mcp                     # 默认 streamable-http 127.0.0.1:8765
claude mcp add --transport http even-glasses http://127.0.0.1:8765/mcp
```

## 测试

```bash
PYTHONPATH=. .venv/bin/pytest tests/ -q          # 单元测试（298）
PYTHONPATH=. .venv/bin/python tests/e2e_sim.py   # 语音端到端闭环（28 项，自带 agent 夹具，无外部依赖）
PYTHONPATH=. .venv/bin/python tests/e2e_mcp.py   # MCP 四进程真链路（27 项，帧从设备 WS 出来）
```

## 实测数据（t4g.xlarge, 4×Neoverse-N1）

- ASR：tiny partial ≈ 670ms；base final RTF ≈ 0.35（4s 语音 ≈ 1.4s）；small RTF ≈ 1.0（弃用）
- **进程首次解码有 20-35s 的 ctranslate2 初始化**（ARM int8 kernel），服务启动 warmup 吃掉，`/healthz` 的 `asr_ready` 为准
- 已知坑：两个 ctranslate2 实例并发解码会在 4 核 ARM 上互锁 → 全局解码串行锁（asr.py）
- 已知坑：`warmup()` 必须幂等。静音输入会让 whisper 退化成重复生成直到 max tokens，
  一次 warmup ≈ 12s 且**全程持解码锁**；重复调用会把用户第一句话的 final 堵在锁上十几秒
- 说完→转写上屏：本机（M 系列 Mac）实测 **0.4s**（3.8s 语音，base 模型解码 0.35s）；
  t4g.xlarge 上 ≈ 3-4s。说完→agent 回复完成 ≈ 10s（含工部 ~7s）
