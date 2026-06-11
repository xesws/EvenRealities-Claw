# OpenClaw Lens — Even Hub 插件

Even Realities G2 眼镜的 Even Hub 插件（Vite + TypeScript），把眼镜变成你私有 Agent
网关（Lens Gateway，协议见 `../protocol/PROTOCOL.md`）的终端：按住说话上行 PCM、
网关下行渲染帧整屏替换、断线看门狗保证眼镜上永远不显示"撒谎的旧帧"。

## 目录结构

```
plugin/
├── app.json                 # Even Hub 清单（构建时自动拷入 dist/）
├── index.html               # 插件主页面入口
├── vite.config.ts           # base:'./'（托管在 /plugin/ 子路径）；多页构建（main + harness）
├── src/
│   ├── main.ts              # 装配：UI 先行 → bridge 等待(3s) → 容器创建 → WS 连接 → resume 渲染
│   ├── glasses.ts           # 容器布局契约、120ms 防抖渲染、镜腿事件归一(CLICK=0 零值)、看门狗帧
│   ├── ws.ts                # 网关 WS：退避+抖动重连、seq 过滤、心跳/看门狗、pair/hello/refresh、PCM 合并
│   ├── store.ts             # 配置存储（bridge LocalStorage 优先，降级 window.localStorage）
│   ├── ui.ts                # 手机端页面：配对屏 / 主屏（按住说话、预览、●REC、打断/清屏、设置）
│   ├── types.ts             # Lens 协议 v1 消息类型
│   └── style.css            # 深色 + 绿色点缀，移动优先
└── harness/
    ├── harness.html         # 浏览器模拟器页面（576×288 假眼镜屏 ×1.5 + 手机框 + 控制按钮）
    ├── mock.ts              # 宿主 mock：flutter_inappwebview.callHandler / _listenEvenAppMessage
    └── harness.ts           # 先注入 mock，再动态加载插件主模块（顺序与真机一致）
```

## 本地开发

```bash
npm install
npm run dev
```

- 插件主页面：`http://localhost:5173/`（纯浏览器里没有 Even App 宿主，会提示
  "未在 Even App 内"，手机端 UI 仍可用）
- **浏览器模拟器（推荐）**：`http://localhost:5173/harness/harness.html`
  - 完整插件 + 假眼镜：mock 在插件 bundle 之前注入，行为与真实桥一致
    （`createStartUpPageContainer` 渲染到黑底绿字假屏；`audioControl(true)` 走真麦克风，
    重采样到 16kHz s16le mono 后切 ~100ms 块以 `audioEvent` 推回插件）
  - 按钮：模拟单击镜腿（翻页；故意不带 eventType，复现 protobuf 零值省略）、
    双击镜腿（退出）、断网模拟（关掉页面里所有 WebSocket，观察看门狗与退避重连）
  - 配对屏里填入真实网关地址（如 `ws://localhost:8080/ws`）+ 配对码，即可全链路验证

类型检查与构建：

```bash
npm run typecheck     # tsc --noEmit（strict）
npm run build         # 产出 dist/（index.html + harness/harness.html + app.json）
```

## 部署与真机 sideload

1. 构建后把 `dist/` 整目录托管到网关的 `/plugin/` 子路径（`base:'./'` 已适配子路径）。
2. 手机与眼镜连好 Even App，生成 sideload 二维码：

   ```bash
   evenhub qr --url https://<你的域名>/plugin/
   ```

3. 用 Even App 扫码加载插件（清单 `app.json` 已随构建产出在 dist/ 根部，
   package_id `com.openclaw.lens`，需要 `g2-microphone` 权限）。
4. 首次进入是配对屏：网关地址默认按当前页面 origin 推导为 `wss://<host>/ws`，
   配对码在网关服务器上用 `lens-gateway pair-code` 生成（10 分钟有效，一次性）。
5. 交互约定：手机"按住说话"上行语音；单击镜腿翻页；**双击镜腿退出插件**
   （官方标准退出 `shutDownPageContainer(1)`）。

## 实现要点（与协议/SDK 的对应关系）

- **容器布局契约**（PROTOCOL.md 第 4 节）：status(ID1, 0,0,576×32) / body(ID2, 0,32,576×220)
  / foot(ID3, 0,252,576×36)，仅 foot 置 `isEventCapture=1`（一页只能有一个）。
- **渲染**：客户端唯一职责 = seq 过滤 → 三个 `textContainerUpgrade`（120ms 防抖、
  只写有变化的容器、串行写避免 BLE 队列拥堵）。空串用单个空格下发，规避
  protobuf 零值字段省略导致的"清不掉旧字"。
- **零值归一**：`CLICK_EVENT=0` 在真机上以 `undefined` 到达，凡带 sys/text/list
  事件体的统一 `eventType ?? CLICK_EVENT` 再比较。
- **看门狗**：心跳 ping 20s，两次无 pong 即判死，先向眼镜 status 容器直推
  「⛓ 连接丢失·重连中」（绕过防抖），再撕连接走 1/2/4/8/16/30s（封顶）+ 抖动重连；
  重连成功后 `hello_ok.resume` 直接渲染恢复现场（每条新连接重置 seq 基线）。
- **认证**：`pair → pair_ok`（持久化 deviceId/refreshToken）→ 同连接补 `hello`；
  `token_expired` 自动 `refresh` 重试一次；`auth_failed` 清凭证回配对屏。
  accessToken 只存内存，重启后用 refreshToken 先换新再 hello。
- **PCM 上行**：眼镜 PCM（`audioEvent.audioPcm`，16kHz s16le mono）按 ≤200ms
  （≤6400 字节）合并成二进制帧上行；`ptt stop` 前先冲洗尾块。
