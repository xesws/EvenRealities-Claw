# Even Realities G2 硬件与 EvenHub 平台规格基准

> **本文件是仓库里所有硬件魔数的唯一真源。** 代码里的每个尺寸/上限常量都应回指本文件的小节号；
> 跨语言共享的部分固化在 `protocol/hud-contract.json`（网关 Python 与插件 TypeScript 读同一份）。

每条规格标注**出处等级**：

| 等级 | 含义 |
|---|---|
| **文档** | 官方文档站 / SDK README 的明文陈述 |
| **实测·SDK** | 在本机把官方 SDK 跑起来观察到的行为（可复现，脚本在 `plugin/tools/`） |
| **实测·模拟器** | 官方 `evenhub-simulator` 自动化接口的观测结果（截图/返回码，`plugin/tools/g2probe.mjs`） |
| **实测·度量库** | 官方 `@evenrealities/pretext` 的返回值 |
| **待真机** | 模拟阶段无法判定，留给真机复验 |

---

## 1. 显示

| 项 | 值 | 出处 |
|---|---|---|
| **开发者可寻址画布** | **576 × 288 px / 眼**，左上原点，X 向右、Y 向下 | 文档：`/docs/build/display` — *"Each eye displays a 576 x 288 pixel canvas. The coordinate origin is the top-left corner."* |
| 灰阶 | **4-bit，16 级绿** | 文档：*"4-bit greyscale - 16 levels of green."* |
| 物理面板 | 640 × 350 px / 眼，micro-LED 波导，27.5° FOV，1200 nit，60 Hz | 硬件规格。**不是可寻址范围**，代码里不要出现这两个数字 |
| 摄像头 / 扬声器 | **都没有** | 文档：`/docs/get-started/overview` |
| 无线 | BLE 5.2 | 同上 |

> ⚠ **576 × 288 的出处曾经被标错。** `even_hub_sdk` 0.0.14 里 576/288 只作为
> `xPosition/yPosition/width/height` 的 **"range: 0-576" / "range: 0-288"** 字段范围出现；
> 混淆后的 `dist/index.js` 里 `576`/`288`/`640` **零命中** —— **SDK 运行时不做任何尺寸校验**
> （判定在 Flutter 宿主与固件侧）。真正的出处是官方文档站。

### 1.1 排版（推翻过三个仓库早期假设）

| 项 | 值 | 出处 |
|---|---|---|
| **行高** | **固定 27 px，不可配** | 文档：官方 text-heavy 模板 `paginate.ts` — `const LINE_HEIGHT = 27`（*"Line height is a fixed 27px in EvenHub's LVGL build."*）；同一数字内嵌在 pretext 的 `line_height` 字段，由 `plugin/tools/extract_metrics.mjs` 断言 |
| **字号** | **根本没有字号控制** | 文档：*"No alignment options, no font-size control, no bold or italic."*；实测·SDK：`TextContainerProperty` 里**没有任何字号字段** |
| **对齐** | 只能左对齐、顶对齐 | 文档：*"Plain text, left-aligned, top-aligned."*、*"'Centering' means padding with spaces."* |
| **字体** | 单一 LVGL 字体，**非等宽** | 文档 + 实测·度量库：CJK advance 320/16 = 20 px，`·` 80/16 = 5 px，`→` 272/16 = 17 px |
| **一屏可见行数** | `floor(容器高 / 27)` 为**完整**行数；余数 ≥ 1px 时还会露出**半行** | 实测·模拟器：288px 容器里第 10 行（y=270..287，只有 18px）仍有墨。本仓库三个容器高度都取 27 的整数倍，不触发这种半行 |

**字形度量的复刻公式**（`gateway/lens_gateway/formatting/metrics.py` 已按此实现，
并与 pretext 在 17 075 个码点 + 1 376 个折行用例上逐条比对零分歧）：

```
kerning  = (kern_value * kern_scale) >> 4
每字宽度 = (adv_w + kerning + 8) >> 4        # 逐字形取整，不是把 1/16px 累加后再取整
```

### 1.2 亮度分层

| 项 | 值 | 出处 |
|---|---|---|
| `textColor` | **整数 0~4**，五级；省略即设备默认 4 | 实测·SDK：`MIN_TEXT_BRIGHTNESS=0`、`MAX_TEXT_BRIGHTNESS=4`，`isValidTextBrightness(5)===false`、`(3.5)===false`；SDK **0.0.14+** 才有 |
| `borderColor` | 0~15，**另一套刻度** | SDK README |

⇒ `DESIGN.md` 早期写的 L15/L12/L10/L6/L3 四档灰阶层级与 L6↔L12 呼吸动画**物理上做不到**。
本仓库的分层：status 4 / body 3 / foot 2（见 `protocol/hud-contract.json`）。**这是 G2 上
唯一真实存在的视觉分层手段** —— 没有字号，也没有对齐。

### 1.3 字形覆盖

见 **[GLYPH-TABLE.md](./GLYPH-TABLE.md)**（度量库 + 模拟器截图双重判定，26/26 一致）。

要点：字库外字符**静默跳过，不留占位框**（文档 + 实测·模拟器：整幅截图零个不透明像素）。

---

## 2. 容器

| 项 | 约束 | 出处 |
|---|---|---|
| 每页容器总数 | `containerTotalNum` **1~12** | 文档 |
| 文本容器数 | ≤ **8** | 文档 |
| 图片容器数 | ≤ **4**，单张 ≤ **288 × 144**，4-bit 灰阶 | 文档 |
| 事件捕获 | **恰好一个**容器 `isEventCapture=1` | 文档 |
| `containerName` | ≤ **16** 字符 | 文档 |
| **内容长度上限** | **UTF-8 999 字节**（**不是** 1000 字符） | **实测·模拟器**，见 §2.1 |
| 固件折行 | *"Text wraps at the container width."* | 文档 |
| 换行符 | `\n` **是**换行符 | 文档 |
| 溢出滚动 | 只有 `isEventCapture=1` 的容器会被固件滚动 | 文档 |
| 满屏容量参考 | *"roughly 400-500 characters"* | 文档（拉丁文口径；CJK 按 §2.1 的字节上限先卡住） |
| `zOrderIndex` | SDK 0.0.12+，**全有或全无**、同页唯一 | 实测·SDK：`validateEvenHubPageContainer` 会返回 `MISSING_Z_ORDER_INDEX` / `DUPLICATE_Z_ORDER_INDEX` |
| 返回码 | 0 success / 1 invalid / 2 oversize / 3 outOfMemory | SDK `StartUpPageCreateResult`（枚举拼写为 `INVAILD`，官方笔误） |
| list 容器 | ≤ 20 项，每项 ≤ 63 字节 | 模拟器 v0.7.3 更新日志 |

### 2.1 内容上限的口径：**字节，999**

SDK README 写「≤1000 **字符**」，模拟器 v0.7.1 更新日志写「text container bytes limit **999**」——
差三倍（CJK 3 字节/字）。用 `g2probe.mjs` 实测拍板：

| 内容 | 字符数 | UTF-8 字节 | `rebuildPageContainer` |
|---|---|---|---|
| `'中' × 333` | 333 | **999** | **true** ✅ |
| `'中' × 334` | 334 | **1002** | **false** ❌ |
| `'a' × 1000` | **1000** | 1000 | **false** ❌ |

最后一行是决定性的：1000 个 ASCII 字符**没有超过任何字符口径**，却仍然失败 ⇒ **判据是字节，上限 999**。
`protocol/PROTOCOL.md` 里「≤1000 字」的说法按此更正；`plugin/harness/mock.ts` 已按 999 字节拦截。

### 2.2 铺满整个画布是安全的

仓库 LAYOUT 正好用满 576×288（36 + 216 + 36 = 288），而官方示例最大只到 x+w=420、y+h=270，
且 SDK 完全没有说明 `oversize` 的触发条件 —— 这一直是个未验证风险。

**实测·模拟器**：按仓库真实 LAYOUT 建页，`createStartUpPageContainer → 0 (success)`，
截图 `docs/assets/g2probe-00-layout.png` 三个容器全部正常渲染。风险解除（真机复验仍在 §6 清单里）。

### 2.3 SDK 自带校验只覆盖三类

实测·SDK：`validateEvenHubPageContainer()` 只检查 **zOrderIndex / textColor / menu**。
以下**一律放行**，要靠宿主或固件拦，本地必须自己兜：

- 内容长度（字符或字节）
- `isEventCapture=1` 的数量（0 个或 2 个都放行）
- `containerName` 长度
- 几何越界（y+h=300 > 288 照样返回 valid）

⇒ `plugin/harness/mock.ts` 的 `validatePage()` 补齐了这四类，`plugin/src/glasses.ts` 在
建页前会先跑一遍 SDK 校验器，省掉一次注定失败的 BLE 往返。

---

## 3. 输入

| 项 | 值 | 出处 |
|---|---|---|
| 手势 | press / double press / swipe up / swipe down / tap-then-long-press-and-release，**两侧镜腿都有** | 文档：`/docs/get-started/overview` |
| R1 戒指 | 同样的手势 | 文档 |
| 事件码 | `CLICK=0 / SCROLL_TOP=1 / SCROLL_BOTTOM=2 / DOUBLE_CLICK=3 / FOREGROUND_ENTER=4 / FOREGROUND_EXIT=5 / ABNORMAL_EXIT=6 / SYSTEM_EXIT=7 / IMU_DATA_REPORT=8 / LONG_PRESS=9 / LONG_PRESS_RELEASE=10` | 实测·SDK：`OsEventTypeList` |
| 事件来源 | `DUMMY_NULL=0 / FROM_GLASSES_R=1 / FROM_RING=2 / FROM_GLASSES_L=3` | 实测·SDK：`EventSourceType` |
| 长按事件码 | **SDK 0.0.14 才有独立的 9/10**；0.0.10 上长按被降级成 CLICK | SDK 版本差异 |

### 3.1 「字段缺省」与「字段无法识别」必须分开

protobuf **零值字段不上线**：`CLICK_EVENT=0` 和 `DUMMY_NULL=0` 到达 WebView 时是 `undefined`。
天真的写法是「`eventType` 缺省 ⇒ 当作单击」，但这样任何**未知**事件也会变成一次幽灵翻页。

实测·SDK 给出了判别方法 —— SDK 在解析后的 `sysEvent` 里丢掉认不出的枚举值，
**但会原样透传 `jsonData`**：

| 宿主推送 | `event.sysEvent` | `event.jsonData` |
|---|---|---|
| `{eventType: 99}` | `{}` | `{eventType: 99}` |
| `{}` | `{}` | `{}` |
| `{eventType: 3}` | `{eventType: 3}` | `{eventType: 3}` |

⇒ 判据：**两处都没有该字段**才是零值省略；`jsonData` 里有、`fromJson` 认不出 ⇒ 未知事件，忽略。
`plugin/src/glasses.ts` 已按此实现。

### 3.2 生命周期语义

| 事件 | 语义 | 正确处理 |
|---|---|---|
| `FOREGROUND_EXIT(5)` | **overlay 关闭、页面仍挂载** | 只暂停（停录音），**绝不能断 WS** |
| `FOREGROUND_ENTER(4)` | 重新进入前台 | 重绘最近一帧（overlay 可能盖过画面） |
| `ABNORMAL_EXIT(6)` / `SYSTEM_EXIT(7)` | 应用真的被销毁 | teardown |
| `createStartUpPageContainer` | **一个页面生命周期只能调一次** | 之后一律走 `rebuildPageContainer` |

把 5 和 6/7 混为一谈是演示中最容易触发的一种死法：用户在眼镜上瞥一眼别的再回来，
WS 永久断开、画面冻在最后一帧，而看门狗还认为一切正常。

---

## 4. 音频

| 项 | 值 | 出处 |
|---|---|---|
| 麦克风 | 4 麦阵列，单路输出 | 文档 |
| 格式 | **16 kHz、有符号 16-bit 小端、单声道 PCM** | 文档 + 模拟器 README |
| 分块 | 每个 `audioEvent` 100 ms = 1600 样本 = 3200 字节 | 模拟器 README |
| `audioControl` | 返回 `Promise<boolean>` —— **这是判断麦是否真的打开的唯一依据** | 实测·SDK |
| 启麦延迟 | **待真机** | 网关当前只给 1.4s 等第一块 PCM，真机上要塞下 WS RTT + BLE 下发 + 固件启麦 + 首块回传 + 插件攒包 + 上行 |

---

## 5. 桥接协议（实测·SDK）

出站：`window.flutter_inappwebview.callHandler('evenAppMessage', <JSON 字符串>)`，
信封 `{type: 'call_even_app_method', method, data}`，handler 的返回值即 Promise 结果。

入站：`window._listenEvenAppMessage({type: 'listen_even_app_data', method, data})`。

实测到的 `method` 全集（`EvenAppMethod`）：

```
getUserInfo  getGlassesInfo  setLocalStorage  getLocalStorage
getAppLocation  startAppLocationUpdates  stopAppLocationUpdates
pickImageFromAlbum  captureImageFromCamera
createStartUpPageContainer  rebuildPageContainer  updateImageRawData
textContainerUpgrade  audioControl  imuControl  shutDownPageContainer
```

注意 **`bridge.getDeviceInfo()` 走的是 `getGlassesInfo`**（名字对不上，实测确认）。
返回的 `DeviceInfo` 带 `isGlasses()` / `isRing()` —— Even 生态含 R1 戒指，
**遥测必须按 model 过滤，否则可能上报戒指的电量**。

几个实测到的信封样例：

```json
{"type":"call_even_app_method","method":"getGlassesInfo"}
{"type":"call_even_app_method","method":"audioControl","data":{"isOpen":true,"source":"glasses"}}
{"type":"call_even_app_method","method":"shutDownPageContainer","data":{"exitMode":1}}
{"type":"call_even_app_method","method":"textContainerUpgrade","data":{"containerID":2,"containerName":"body","content":"hi"}}
```

---

## 6. 仍然只能靠真机判定的（待真机）

模拟阶段已经把这个清单从 7 条压到 6 条（字形可用性与真实字宽已由 §1.3 解决，
`\n` 语义已由官方文档解除，满画布 `oversize` 已由 §2.2 解决）：

1. BLE 渲染时序与闪烁（`textContainerUpgrade` 的真实往返延迟与合并窗口）
2. 镜腿/戒指事件是否真的能到达 WebView（模拟器把 `eventSource` 硬编码成 1，测不了左右与戒指）
3. 麦克风仲裁与启麦延迟（`mic_warmup_seconds` 待回填）
4. `audioControl` 的单次时长上限
5. 后台 / 锁屏下的插件存活
6. `FOREGROUND_ENTER/EXIT` 在真机上的实际触发时机

**真机第一天只做标定与复验，不做设计变更。**
