# Lens 协议 v1 — 插件 ↔ Lens Gateway

> 单一 WebSocket 连接，路径 `/ws`。JSON 文本帧 = 控制/渲染消息；**二进制帧 = 原始 PCM 音频**（16kHz, s16le, mono），仅在 PTT 期间上行。
> 设计原则：服务器持有全部状态；下行渲染帧**幂等**（整屏替换）且带**单调 seq**——客户端丢弃旧 seq，断连重连后服务器重放当前帧即恢复现场。

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
- `containers` 三个 key 恒在（空串=清空该区）；`\n` 为眼镜端换行，折行已由服务器完成（17 汉字/行，CJK 标点禁则）。
- 客户端唯一职责：seq 过滤 → 三个 `textContainerUpgrade`（120ms 防抖）。

## 4. 容器布局契约（插件 createStartUpPageContainer 时使用）

| container | ID | name | x,y | w,h | 字号期望 |
|---|---|---|---|---|---|
| status | 1 | `status` | 0,0 | 576,32 | 24px |
| body | 2 | `body` | 0,32 | 576,220 | 32px（17 汉字/行 × 5 行） |
| foot | 3 | `foot` | 0,252 | 576,36 | 24px |

（G2 实际字号由固件渲染决定；服务器折行宽度可通过 `gateway` 配置 `wrap_width_chars` 在真机实测后校准。）

## 5. 错误码

| code | 含义 | 客户端动作 |
|---|---|---|
| `pair_failed` | 配对码无效 | 提示重新输入 |
| `token_expired` | accessToken 过期 | 用 refresh 换新后重发 hello |
| `auth_failed` | token 无效/设备被吊销 | 回配对页 |
| `busy` | 上一条还在跑 | 提示「说打断或稍等」 |
| `internal` | 服务器内部错误 | 显示错误帧 |

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
