# Lens Gateway

服务器端核心：WSS 接入 + 设备 JWT 认证 + faster-whisper ASR + OpenClaw 适配 + HUD 帧编排。

## 模块

| 模块 | 职责 |
|---|---|
| `server.py` | aiohttp 服务：`/ws`（插件）、`/plugin/`（托管前端）、`/healthz`、`/admin/*`（仅 loopback） |
| `session.py` | 设备会话状态机 S0-S8、帧节流（状态帧免节流/内容帧 2Hz coalescing）、跨重连现场恢复 |
| `asr.py` | 双模型管线：tiny partial（显示用）+ base final（路由用）、local-agreement 稳定前缀、全局解码串行锁 |
| `openclaw.py` | OpenClaw 网关 WS 客户端：chat.send 流式事件、abort、runId 僵尸标记 |
| `formatting/` | 排版引擎：G2 字形度量复刻 → 像素盒折行（CJK 标点禁则）→ 8 行/页分页 + 锚点、markdown 剥离、文本净化、字形在库校验 |
| `auth.py` | 配对码 → 设备注册 → accessToken(JWT 15min) + refreshToken（只存哈希），单设备吊销 |
| `config.py` | 配置（状态目录 `~/.lens-gateway/`，不入库） |

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m lens_gateway.main serve     # 前台
bash ../scripts/install-service.sh                            # systemd 用户服务
.venv/bin/python -m lens_gateway.main pair-code               # 生成配对码
.venv/bin/python -m lens_gateway.main devices                 # 已配对设备
.venv/bin/python -m lens_gateway.main revoke dev_xxx          # 吊销设备
```

## 测试

```bash
PYTHONPATH=. .venv/bin/pytest tests/ -q          # 单元测试（26）
PYTHONPATH=. .venv/bin/python tests/e2e_sim.py   # 端到端闭环（14 项，需工部网关在线）
```

## 实测数据（t4g.xlarge, 4×Neoverse-N1）

- ASR：tiny partial ≈ 670ms；base final RTF ≈ 0.35（4s 语音 ≈ 1.4s）；small RTF ≈ 1.0（弃用）
- **进程首次解码有 20-35s 的 ctranslate2 初始化**（ARM int8 kernel），服务启动 warmup 吃掉，`/healthz` 的 `asr_ready` 为准
- 已知坑：两个 ctranslate2 实例并发解码会在 4 核 ARM 上互锁 → 全局解码串行锁（asr.py）
- 说完→转写上屏 ≈ 3-4s；说完→agent 回复完成 ≈ 10s（含工部 ~7s）
