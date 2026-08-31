# 硬件 MCP 表面 —— 让任意厂商的模型直接驱动这副眼镜

> 状态：**已实现并端到端验证**（M5）。
> 代码：`gateway/lens_mcp/`（MCP 服务器）+ `gateway/lens_gateway/control.py`（网关控制面）。
> 验证：`gateway/tests/test_mcp.py` 18 项 + `gateway/tests/test_control_plane.py` 22 项
> + `gateway/tests/test_control_auth.py` 22 项 + **`gateway/tests/e2e_mcp.py` 四进程真链路 27 项**。

这一层要回答的问题只有一个：**一个不认识本项目的厂商模型（Claude / GPT / DeepSeek …），
在没有任何私有约定的情况下，能不能把字写到这副眼镜上、并且写对？**

---

## 1. 拓扑：为什么是独立进程

```
  厂商模型 / Claude Code / 任意 MCP 客户端
        │  MCP Streamable HTTP   POST /mcp
        ▼
  ┌───────────────────────────┐   lens_mcp（独立进程，默认 127.0.0.1:8765）
  │ 8 tools · 3 resources     │   —— 面向外部的攻击面在这里，且只有这里
  │ 1 prompt                  │
  └───────────┬───────────────┘
              │  控制面 HTTP + Bearer 共享密钥
              ▼
  ┌───────────────────────────┐   lens_gateway（持有麦克风、ASR、设备 JWT）
  │ /control/*                │
  │   └ DeviceSession → HudDevice（唯一写屏入口，帧租约在这里仲裁）
  └───────────┬───────────────┘
              │  Lens 协议 v1.1 WebSocket
              ▼
        手机插件 → BLE → G2 眼镜
```

两个进程而不是一个，有两条独立的理由，缺一条也仍然要拆：

1. **技术**：官方 `mcp` SDK 的 Streamable HTTP 是 ASGI/Starlette，网关是 aiohttp，同进程跑不了。
2. **安全**（更重要）：MCP 表面是**面向外部厂商的攻击面**。网关持有麦克风、ASR、
   设备 JWT 签名密钥与 OpenClaw 全权 token；MCP 进程一个都不持有，它能做的事
   **等于控制面暴露的那九个路由**，越权在架构上不成立，不靠代码自律。

---

## 2. 三条写进工具描述里的事实

这三条不是注释，是**逐字写在 tool description 里**的 —— 模型不读我们的文档，只读描述。

| # | 事实 | 为什么必须让模型知道 |
|---|---|---|
| 1 | **「监控」只能是轮询** | MCP 2026-07-28 规范下服务器不能主动发起 JSON-RPC 请求，做不到推送。所有读接口返回 `as_of`，描述里明写「这是一次采样，不是订阅」，否则模型会以为自己订阅上了 |
| 2 | **屏幕只有一块，写屏要租约** | 用户按下 PTT 的那一刻屏幕无条件归语音链路，模型的租约被抢占。`glasses_events` 是模型发现这件事的**唯一**途径 |
| 3 | **遥测可能是缓存值** | 官方没说明 `getDeviceInfo()` 是否真的触发 BLE 读取，所以返回体带 `source`（push / poll）与 `stale`。描述里明写「available=false 时不要臆造一个电量数字」 |

---

## 3. 工具、资源、提示

### 8 个工具

| 工具 | 依赖设备 | 说明 |
|---|---|---|
| `textkit_paginate(text, container="body")` | ❌ 纯函数 | 按 G2 **真实像素版式**分页，返回每页整屏文本 + `lines_per_page` / `inner_width_px` / `line_height_px` / **`dropped_glyphs`**（真机上会被静默丢弃的字符）/ `sanitized` |
| `glasses_list()` | ❌ | 已配对眼镜快照，带 `as_of` |
| `hud_show(device_id, text, title?, hold_ms?, holder?, ttl_ms?)` | ✅ 租约 | **先取租约再渲染**，返回 `lease_id` 与页码；冲突返回 `LEASE_HELD` + 持有者 + 剩余毫秒 |
| `hud_page(device_id, lease_id, direction)` | ✅ 租约 | `turned=false` 表示已在首/末页（不是错误，也不发冗余帧） |
| `hud_clear(device_id, lease_id)` | ✅ 租约 | 回待机但**保留**租约 |
| `hud_release(device_id, lease_id)` | ✅ 租约 | 主动交还控制权 |
| `glasses_telemetry(device_id)` | ✅ 遥测通路 | 电量/佩戴/连接，带 `source` / `sampled_at` / `age_ms` / `stale` |
| `glasses_events(device_id, after)` | ✅ | 增量拉取 `lease_acquired` / `lease_released` / **`lease_preempted`**；把 `next` 当作下次的 `after` |

**参数里没有「每行几个字」这类旋钮**，这是刻意的：G2 字体非等宽，分页由容器的真实像素盒决定
（正文区 576×216px、固定 27px 行高 ⇒ 8 行/页），字宽用官方 `@evenrealities/pretext`
的固件度量复刻。给模型一个字符数参数只会让它算错。

### 3 个资源

`glasses://devices` · `glasses://{device_id}/frame` · `glasses://{device_id}/telemetry`

### 1 个提示

`small-screen-style` —— **由 `DEFAULT_LAYOUT` 现场推导**，不是手写常量：版式一改，
提示里的「一页 8 行、每行约 28 字」跟着变，不会出现文档与代码对不上的经典问题。

---

## 4. 租约：一块屏幕怎么给两个主人用

| 规则 | 行为 |
|---|---|
| 单持有者 | 同一时刻只有一个 `lease_id` 有效 |
| 冲突 | 返回结构化 `{"code":"LEASE_HELD","holder":…,"expires_in_ms":…}`，**不是最后写入者赢** |
| 抢占 | 用户按下 PTT ⇒ 语音链路无条件夺屏，原租约立即失效，后续调用返回 `LEASE_INVALID` |
| 发现抢占 | 写进事件缓冲，由 `glasses_events` 轮询取回（`reason: "local_render"`） |
| 过期 | `ttl_ms`（默认 60s）到点自动失效，不需要客户端善后 |

`e2e_mcp.py` 里有一条专门的用例：甲持有租约时乙调 `hud_show`，断言**乙被拒且屏幕内容没被污染**。

---

## 5. 鉴权

- 控制面用**共享密钥 Bearer**，不是 loopback 判断。原因写在 `REPORT.md §8`：
  推荐的 TLS 方案是 caddy 反向代理，反代之后所有请求的 peername 都变成 127.0.0.1，
  按 peername 判断的守卫**整体失效** —— 这对 `/admin/*` 是今天就存在的隐患，已一并改掉。
- 密钥：首次启动生成，`0600` 落盘到 `~/.lens-gateway/control.secret`。
- MCP 进程读取顺序：`LENS_CONTROL_SECRET` 环境变量 > `$LENS_STATE_DIR/control.secret`。
- 比较用 `secrets.compare_digest` 对 **UTF-8 字节**（直接比字符串在非 ASCII token 上会
  抛 `TypeError` → 500，把「密钥格式不对」这个信息泄露给攻击者）。

---

## 6. 接进 Claude Code

```bash
# 1) 网关（已在跑就跳过）
cd gateway && PYTHONPATH=. .venv/bin/python -m lens_gateway.main serve

# 2) MCP 服务器（另一个终端）
cd gateway && .venv/bin/pip install -r requirements-mcp.txt
PYTHONPATH=. .venv/bin/python -m lens_mcp          # 默认 streamable-http 127.0.0.1:8765

# 3) 注册给 Claude Code
claude mcp add --transport http even-glasses http://127.0.0.1:8765/mcp
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LENS_MCP_TRANSPORT` | `streamable-http` | 可设 `stdio`（给不支持 HTTP 的客户端） |
| `LENS_MCP_HOST` / `LENS_MCP_PORT` | `127.0.0.1` / `8765` | |
| `LENS_CONTROL_URL` | `http://127.0.0.1:8443` | 网关地址 |
| `LENS_CONTROL_SECRET` | 读 `control.secret` | |
| `LENS_MCP_LOG` | `INFO` | |

**stdio 传输**（如需）：

```bash
claude mcp add even-glasses -- /path/to/gateway/.venv/bin/python -m lens_mcp
# 需要 env: LENS_MCP_TRANSPORT=stdio, PYTHONPATH=/path/to/gateway
```

---

## 7. 验证：四进程真链路

`gateway/tests/e2e_mcp.py` 起**四个真进程**，中间没有任何打桩：

```
MCP 客户端（官方 mcp SDK ClientSession）
  → Streamable HTTP → lens_mcp 进程
  → 控制面 HTTP     → lens_gateway 进程
  → 协议 v1.1 WS    → 本脚本扮演的插件（收真实渲染帧）
```

断言落在**最远端** —— 调完 MCP 工具之后，帧必须真的从设备 WebSocket 出来，内容与分页都对。
27 项覆盖：

- initialize / 工具清单 / instructions 内容
- `textkit_paginate` 用真实版式（3 页 × 8 行 / 576px）、字库外字符如实报出
- **★ `hud_show` 之后设备 WS 收到 S9 帧，正文与 MCP 返回逐字一致，页脚是真实页码**
- **★ 并发写屏返回 `LEASE_HELD` 且被拒的写入没有污染屏幕**
- 翻页帧同样送达设备
- **★ `ptt start` 抢占后原租约 `LEASE_INVALID`，抢占事件可被 `glasses_events` 轮询到，游标增量**
- 遥测三件套（`source` / `age_ms` / `stale`）、SN 出网关只留后 4 位
- 未知设备返回结构化错误、资源可读、提示由真实版式推导、抢占后可重新取租约、clear / release

```
=== MCP 端到端结果：27/27 通过 ===
```

运行：`cd gateway && PYTHONPATH=. .venv/bin/python tests/e2e_mcp.py`

---

## 8. 已知边界

| 项 | 状态 |
|---|---|
| 推送 / 订阅 | ❌ 规范不支持，只能轮询。已在工具描述里说明 |
| MCP 侧多客户端身份 | ⚠️ MCP 无 session 概念，服务器分不清谁在调用；仲裁完全由**租约的 `holder` 字符串**承担，靠客户端自报 |
| 控制面限流 | ⚠️ 只有 `MAX_RENDER_CHARS = 20000` 的正文上限（超出 413），没有 QPS 限流 |
| 真机 | ❌ 全链路在模拟阶段验证；BLE 渲染时序见 `REPORT.md §13.6` |
