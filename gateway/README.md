# Lens Gateway

服务器端核心：WSS 接入 + 设备 JWT 认证 + faster-whisper ASR + HUD 帧编排
+ 面向厂商模型的 **MCP 表面**（独立进程 `lens_mcp/`）
+ **自研轻量 agent**（独立进程 `lens_agent/`，直连 DeepSeek）。

## 模块

| 模块 | 职责 |
|---|---|
| `server.py` | aiohttp 服务：`/ws`（插件）、`/plugin/`（托管前端）、`/healthz`、`/admin/*` 与 `/control/*`（共享密钥 Bearer）、离线会话 TTL 回收 |
| `session.py` | 装配层：一台设备 = 一块屏（`device`）+ 一条语音链路（`voice`）。只做装配与消息路由，无业务逻辑 |
| `device/hud.py` | **唯一写屏入口**：状态机 S0-S9、帧构造、节流（状态帧免节流/内容帧 2Hz coalescing）、状态条、分页、计时器、**帧租约**（语音与 MCP 共用一块屏的仲裁，用户开口无条件抢占）、跨重连现场恢复 |
| `voice/pipeline.py` | 语音链路：PTT → PCM → partial 滚动窗 → final 转写 → 确认窗口 → agent → 流式上屏；mic 看门狗（启麦慢与链路断**两个判据**） |
| `asr.py` | 双模型管线：tiny partial（显示用）+ base final（路由用）、local-agreement 稳定前缀、全局解码串行锁、**热词回声守卫** |
| `providers/` | agent 抽象：网关对"对面是谁"只认 6 个方法。`base.py` 含 `AgentInfo`（W6 溯源）、`openclaw.py`、`lens.py` |
| `formatting/` | 排版引擎：G2 字形度量复刻 → 像素盒折行（CJK 标点禁则）→ 8 行/页分页 + 锚点、markdown 剥离、文本净化、字形在库校验 |
| `auth.py` | 配对码 → 设备注册 → accessToken(JWT 15min) + refreshToken（只存哈希），单设备吊销 |
| `control.py` | 控制面：`/control/*` 九个路由，共享密钥 Bearer 鉴权。**MCP 进程能做的事 = 这九个路由**，越权在架构上不成立 |
| `config.py` | 配置（状态目录 `~/.lens-gateway/`，不入库）+ `control_secret()`（首次生成，0600 落盘） |
| `lens_mcp/` | **独立进程**的 MCP 服务器：8 tools / 3 resources / 1 prompt，经控制面驱动网关。不持有麦克风、ASR、设备凭证 —— 外部攻击面被隔离在这里。见 [docs/MCP-SURFACE.md](../docs/MCP-SURFACE.md) |
| `lens_agent/` | **独立进程**的自研 agent（~900 行，可整体搬走）：手写 loop、确定性 skill 路由、四道闸（无 exec 档 / 代码选 skill / 写能力绑资源 / 审计）、DeepSeek 直连。只监听回环且**拒绝带 Origin 的连接**（浏览器 WS 不受同源策略约束）。见 [docs/AGENT-LAYER.md](../docs/AGENT-LAYER.md) |

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

自研 agent（独立进程，回答问题的那一层）：

```bash
export LENS_LLM_API_KEY=sk-...                                # key 只从环境读，绝不落盘
PYTHONPATH=. .venv/bin/python -m lens_agent.server            # 默认 ws://127.0.0.1:18790
curl -s http://127.0.0.1:18790/healthz                        # 自报模型与工具清单
```

网关侧在 `~/.lens-gateway/config.json` 里切过去：

```json
{ "agent": { "provider": "lens", "url": "ws://127.0.0.1:18790" } }
```

或者一条命令拉起全链路：`../demo/start.sh --lens`。

## 测试

```bash
PYTHONPATH=. .venv/bin/pytest tests/ -q          # 单元测试（437）
PYTHONPATH=. .venv/bin/python tests/e2e_sim.py   # 语音端到端闭环（31 项，自带 agent 夹具，无外部依赖）
PYTHONPATH=. .venv/bin/python tests/e2e_mcp.py   # MCP 四进程真链路（27 项，帧从设备 WS 出来）

# 真 agent 端到端（三进程 + 真 DeepSeek）。会产生真实付费调用，故不进 CI。
LENS_LLM_API_KEY=sk-... PYTHONPATH=. .venv/bin/python tests/e2e_agent.py   # 21 项
```

两套 golden 的再生成（**确认改动是预期的**再做，diff 要有人看）：

```bash
PYTHONPATH=. .venv/bin/python -m tests.data.formatting.corpus --regen   # 排版折行
PYTHONPATH=. .venv/bin/python -m tests.data.hud.scenes --regen          # HUD 帧序列（13 场景）
```

## 实测数据（t4g.xlarge, 4×Neoverse-N1）

- ASR：tiny partial ≈ 670ms；base final RTF ≈ 0.35（4s 语音 ≈ 1.4s）；small RTF ≈ 1.0（弃用）
- **进程首次解码有 20-35s 的 ctranslate2 初始化**（ARM int8 kernel），服务启动 warmup 吃掉，`/healthz` 的 `asr_ready` 为准
- 已知坑：两个 ctranslate2 实例并发解码会在 4 核 ARM 上互锁 → 全局解码串行锁（asr.py）
- 已知坑：`warmup()` 必须幂等。静音输入会让 whisper 退化成重复生成直到 max tokens，
  一次 warmup ≈ 12s 且**全程持解码锁**；重复调用会把用户第一句话的 final 堵在锁上十几秒
- 说完→转写上屏：本机（M 系列 Mac）实测 **0.4s**（3.8s 语音，base 模型解码 0.35s）；
  t4g.xlarge 上 ≈ 3-4s。说完→agent 回复完成 ≈ 10s

## DeepSeek 接入实测（M6，见 docs/AGENT-LAYER.md §13.1）

- 模型 id 是 `deepseek-v4-flash`；`.env` 里那个带 `-0731` 后缀的**调不通**（HTTP 400），
  `0731` 是 checkpoint 名不是调用串
- `thinking:{"type":"disabled"}` 必须显式带上：首 token **5.75s → 2.94s**，
  而且 `prompt_tokens` 从 94 降到 **15** —— 开着思考时服务端会往前缀注入约 79 个 token
- `reasoning_content` 与 `content` 平级、**确实出现在流式 delta 里**（实测 114 字）。
  解析时只累计它的长度，不进正文、不进 sink
- `stream=true` 与 `tool_calls` **能并存**（`finish_reason=tool_calls`，参数由 13 个分片拼出，
  带工具只多 0.17s）。但模型会**先流一段散文再吐 tool_calls**，那段散文里带 markdown 反引号 ——
  所以排版层强制剥 markdown 的兜底不能去掉
- 两问（一问走工具、一问不走）端到端共 **10.6s**
