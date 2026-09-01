# Lens 协议 v1.1 — 插件 ↔ Lens Gateway

> 单一 WebSocket 连接，路径 `/ws`。JSON 文本帧 = 控制/渲染消息；**二进制帧 = 原始 PCM 音频**（16kHz, s16le, mono），仅在 PTT 期间上行。
> 设计原则：服务器持有全部状态；下行渲染帧**幂等**（整屏替换）且带**单调 seq**——客户端丢弃旧 seq，断连重连后服务器重放当前帧即恢复现场。
>
> **版本兼容规则：两端对未知消息类型一律静默忽略。** 因此加消息是**加法安全**的 ——
> v1.0 的插件遇到 v1.1 的 `cmd` 什么也不做，v1.0 的网关收到 `telemetry` 同样直接丢。
> 不存在"协议版本协商"，也不需要。
>
> **v1.1 变更**：新增遥测上行（`telemetry`）与命令/回执（`cmd` / `cmd_result`）。
> 动机是 `glasses_telemetry` 这类 MCP 工具必须有真实数据源 —— 在此之前遥测从未离开过手机，
> 网关对电量/佩戴/连接的知识为零，工具只能编。

## 1. 连接与认证

### 1.1 配对（首次）
```jsonc
// C→S（WS 打开后第一帧）
{ "type": "pair", "code": "847291", "deviceName": "iPhone 15 / Even App" }
// S→C 成功
{ "type": "pair_ok", "deviceId": "dev_a1b2c3", "accessToken": "<jwt 15min>",
  "refreshToken": "<只此一次下发>", "exp": 1760000000 }
// S→C 失败（配对码错误/过期）
{ "type": "error", "code": "pair_failed", "message": "配对码无效或已过期" }
```
配对码由服务器端命令行生成（`lens-gateway pair-code`），10 分钟有效，一次性。

### 1.2 认证（每次连接）
```jsonc
// C→S
{ "type": "hello", "token": "<accessToken>", "client": "plugin", "version": "0.1.0" }
// S→C —— resume 携带当前帧，客户端直接渲染即恢复现场
{ "type": "hello_ok", "deviceId": "dev_a1b2c3", "exp": 1760000000,
  "server": "lens-gateway/0.1.0", "resume": { /* frame 消息原样 */ } }
```
accessToken 过期 → `{"type":"error","code":"token_expired"}`，客户端用 refreshToken 换新：
```jsonc
{ "type": "refresh", "refreshToken": "..." }          // C→S
{ "type": "refresh_ok", "accessToken": "...", "exp": 1760000000 }  // S→C
```

## 2. 上行消息（插件 → 网关）

| 消息 | 含义 |
|---|---|
| `{"type":"ptt","action":"start"}` | 按下说话：服务器进入聆听态，准备接收 PCM |
| 二进制帧（PCM s16le 16k mono） | 音频块，建议 ≤100ms/块 |
| `{"type":"ptt","action":"stop"}` | 松手：final 转写 → 路由 → 发给 agent |
| `{"type":"ptt","action":"cancel"}` | 取消本次说话，丢弃音频 |
| `{"type":"page","dir":"next"\|"prev"}` | 翻页（阅读态） |
| `{"type":"abort"}` | 打断当前 agent run |
| `{"type":"reset"}` | 清屏回待机 |
| `{"type":"ping","t":123}` | 心跳（建议 20s），服务器回 `pong` |
| `{"type":"telemetry","data":{…}}` | **v1.1** 主动上报一次遥测，见 §2.1 |
| `{"type":"cmd_result","id":"c1","ok":true,"data":{…}}` | **v1.1** 命令回执，见 §2.2 |

### 2.1 遥测上报（v1.1）

插件在 `onDeviceStatusChanged` 触发时**主动上报**——这是唯一能保证「新鲜」的来源。

```jsonc
{"type": "telemetry", "data": {
  "model": "g2",              // 来自 getDeviceInfo()；未知为 null
  "sn": "G2SN-ABCD1234",      // 完整 SN 只到网关为止，出网关只留后 4 位
  "isGlasses": true,          // ★ 见下方"戒指问题"
  "connectType": "connected",
  "connected": true,
  "batteryLevel": 86,         // 每个字段都可能是 null
  "isCharging": false,
  "isWearing": true,
  "isInCase": false
}}
```

**★ 戒指问题（型号判定必须在插件侧做）**：SDK 的 `DeviceStatus` 里**只有 `sn`、没有 `model`**
（`even_hub_sdk/dist/index.d.ts:143`），而 Even 生态里 R1 戒指与眼镜走的是**同一套**
`onDeviceStatusChanged` 推送。只看状态是分不出来的 —— 一不留神就会把戒指的电量报成眼镜的。
插件必须拿 `getDeviceInfo()`（宿主方法名 `getGlassesInfo`）返回的 `model` + `sn` 去比对，
把结论放进 `isGlasses`。**网关只接受 `isGlasses === true` 的记录**，其余计数后丢弃。

**字段白名单**：网关只保留上表列出的字段，多送的一律丢弃 —— 遥测会经 MCP 流向第三方
LLM 厂商，字段集必须是审过的。

**缺失字段给 `null`，绝不补 0**：网关分不清「电量 0%」和「没读到电量」。

### 2.2 命令与回执（v1.1）

网关下发 `cmd`，插件必须回一条同 `id` 的 `cmd_result`——**失败也要回**，否则网关会一直挂着这个 id。

```jsonc
// S→C
{"type": "cmd", "cmd": "telemetry", "id": "c1"}
// C→S（成功）
{"type": "cmd_result", "id": "c1", "ok": true, "data": { /* 同 §2.1 的 data */ }}
// C→S（失败：不支持该命令 / 没有 bridge / 超时）
{"type": "cmd_result", "id": "c1", "ok": false, "error": "no_bridge"}
```

规则：

- **`id` 一次性**。网关认领后即从待回执表里移除，重放同一个 `id` 不会再被接受。
- **重连即作废**。新连接会清空待回执表，旧连接的回执不被新连接认领。
- **拉取回来的值记作 `source:"poll"`，不是 `push`**。官方**没有说明** `getDeviceInfo()`
  是否真的触发一次 BLE 读取，手机端很可能直接返回缓存值。把 poll 无条件标成「新鲜」就是在编数据。
- 失败的回执**不覆盖**已有的已知值。

## 3. 下行渲染帧（网关 → 插件）

```jsonc
{
  "type": "frame",
  "seq": 42,                  // 单调递增；客户端丢弃 seq ≤ 已渲染值的帧
  "state": "S6",              // S0 待机 S2 聆听 S3 确认 S4 思考 S5 工具 S6 流式 S7 阅读 S8 错误
  "containers": {             // 已排版纯文本，插件原样写入对应 container，不做任何加工
    "status": "工 ▸ 回答",     // containerID=1, y0 h32, 字号24
    "body":   "明天下午去杭州的高铁还有余票，\n推荐两班：……",  // containerID=2, y32 h220, 字号32, ≤5行×17字
    "foot":   "2/3 ›"          // containerID=3, y252 h36, 字号24
  },
  "meta": {                   // 给手机侧 UI 的辅助信息（眼镜端不用）
    "rec": false,             // true=聆听中，手机页显示 ●REC
    "page": { "cur": 2, "total": 3 },
    "agent": "gongbu"
  }
}
```

规则：
- **状态切换帧免节流**（立即下发）；同状态内容帧 ≥500ms 间隔（≤2Hz），发送队列只保留最新一帧（coalescing）。
- `containers` 三个 key 恒在（空串=清空该区）；`\n` 是眼镜端换行（官方文档站明文：*"'\n' is a line break."*）；
  折行已由服务器按**真实字形度量**完成（像素盒，非字符预算；含 CJK 标点禁则），正文一页 8 行。
- 客户端唯一职责：seq 过滤 → 三个 `textContainerUpgrade`（120ms 防抖）。

## 4. 容器布局契约（插件 createStartUpPageContainer 时使用）

**机器可读真源是 [`hud-contract.json`](./hud-contract.json)** —— 网关（Python）与插件（TypeScript）
都读它，下表只是它的人类可读投影，改动请改 JSON。

| container | ID | name | x,y | w,h | 行数 | textColor | isEventCapture |
|---|---|---|---|---|---|---|---|
| status | 1 | `status` | 0,0 | 576,36 | 1 | 4 | 0 |
| body | 2 | `body` | 0,36 | 576,216 | **8** | 3 | 0 |
| foot | 3 | `foot` | 0,252 | 576,36 | 1 | 2 | **1** |

- 行数 = `floor(h / 27)`。**27px 是 G2 固件 LVGL 的固定行高，不可配**；契约里有断言保证
  每个容器声明的行数与高度自洽。
- **G2 没有字号控制**，也没有对齐控制 —— 早期表格里的「字号期望」列是虚构的，已删。
  `textColor`（0~4 五级亮度）是这块屏上唯一真实存在的视觉分层手段。
- `isEventCapture=1` 挂在 `foot` 而不是 `body` 是有意的，理由与代价都在这里（B5）：

  一页**有且仅能有一个**捕获容器，违反则整页建立失败 —— 所以这是个非此即彼的选择。
  挂 `body` 的话，固件会对溢出的正文做滚动；但服务器已经按真实字形度量做完了分页，
  固件再滚一次等于两套分页打架，用户会看到画面自己动。挂 `foot` 则正文严格按我们
  算好的每页 8 行呈现，翻页完全由服务器的分页器控制。

  代价是**输入焦点落在页脚上**。已验证的部分：官方模拟器里 `/api/input` 的点击
  确实能被 `isEventCapture=1` 的容器收到（`plugin/tools/g2probe.mjs` 靠它连点步进了 8 屏，
  归属 (a)，见 [../docs/SIMULATOR-PARITY.md](../docs/SIMULATOR-PARITY.md)）。
  **未验证的部分**：真机上镜腿触控事件是否真的能到达 WebView —— 这条归属 (c)，
  只能等真机。万一到不了，损失的只是"从眼镜上翻页"这一个触发源，
  手机按钮 / MCP / 语音三个触发源不受影响（`hud.page()` 对触发源是等价的）。
- 折行由服务器按**真实字形度量**完成（`gateway/lens_gateway/formatting/`，与官方
  `@evenrealities/pretext` 逐条比对零分歧），不再需要「按字符数猜一个安全宽度」。
  配置项是 `body_safety_px`（默认 0），只作为万一度量库与固件有版本差时的退让阀。
- 单容器内容上限是 **UTF-8 999 字节**，不是 1000 字符（官方模拟器实测，见
  [../docs/HARDWARE-SPEC.md](../docs/HARDWARE-SPEC.md) §2.1）。

## 5. 错误码

| code | 含义 | 客户端动作 |
|---|---|---|
| `pair_failed` | 配对码无效 | 提示重新输入 |
| `token_expired` | accessToken 过期 | 用 refresh 换新后重发 hello |
| `auth_failed` | token 无效/设备被吊销 | 回配对页 |
| `busy` | 上一条还在跑 | 提示「说打断或稍等」 |
| `internal` | 服务器内部错误 | 显示错误帧 |

命令回执里的 `error`（v1.1，不是上表的连接级错误码）：

| error | 含义 |
|---|---|
| `unsupported` | 插件不认识这条命令（旧版插件遇到新命令即如此） |
| `no_bridge` | 插件没跑在 Even App 里，拿不到设备信息 |
| 其它 | 插件侧异常的 message，原样透传 |

## 6. 时序示例（一次完整问答）

```
C: pair/hello … S: hello_ok(resume S0帧)
C: ptt start            S: frame S2「工 ◉ 聆听 0:00」（免节流）
C: [PCM]×N              S: frame S2 body=partial转写（≤2Hz）
C: ptt stop             S: frame S3「→ 工部」body=final 转写（短暂）
                        S: frame S4「工 ◔ 思考 2s」（1Hz 计秒）
                        S: frame S5「工 ⚙ 工具」（如有 tool 事件）
                        S: frame S6「工 ▸ 回答」body=流式分页（≤2Hz）
                        S: frame S7「工 ✓ 完成」foot=「1/3 ›」
C: page next            S: frame S7 foot=「2/3 ›」（免节流）
（60s 无操作）           S: frame S0（渐隐回待机）
```
