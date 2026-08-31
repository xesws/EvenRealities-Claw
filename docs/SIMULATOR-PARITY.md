# 模拟保真度对照表

> **任何「模拟器通过」的结论都必须标注它属于哪一档。** 这份表就是那份归属清单。

演示的可信度取决于一件事：我们说「已验证」的时候，读者要能立刻知道**是被什么验证的**。

---

## 三档归属

| 档 | 含义 | 可信度 |
|---|---|---|
| **(a) 官方模拟器已判定** | `@evenrealities/evenhub-simulator` 用真字体、真 4-bit 灰阶、固件对齐的缺字渲染跑出来的结果 | 高。可复现、有截图 |
| **(b) 仅自建夹具覆盖** | `plugin/harness/mock.ts` 的可编程夹具跑出来的结果 | 中。逻辑正确，但渲染与时序不保真 |
| **(c) 真机不可替代** | 模拟阶段原理上判定不了 | 未验证。写清楚待办 |

---

## 两个模拟器的分工

| | `@evenrealities/evenhub-simulator` 0.9.4（官方） | `plugin/harness/mock.ts`（自建） |
|---|---|---|
| **定位** | **保真基准** | **自动化夹具** |
| 字体 | 真 G2 字库（LVGL + `g2` feature） | 桌面字体，但**字符集与横向位置**都由官方 pretext 约束 |
| 灰阶 | 真 4-bit（v0.5.2+） | CSS `opacity` 近似 `textColor` 五级 |
| 缺字 | 固件对齐（`LV_USE_FONT_PLACEHOLDER`，实测＝什么都不画） | 按 `getAdvW(cp)===0` 静默丢弃并计数 |
| 折行 | 固件自己折 | 用 pretext 度量复刻，并与 `measureTextWrap` 的行数交叉校验 |
| 自动化 | HTTP：`/api/screenshot/glasses`、`/api/console`、`/api/input` | 直接函数调用 |
| **`eventSource`** | **硬编码为 1（GLASSES_R）**，测不了左镜腿与戒指 | 四个值都能推 |
| 故障注入 | 无 | 返回码 / BLE 延迟 / 桥卡死 / 麦被抢 / 写失败 |
| 生命周期事件 | 无（README：*"Status Events: Not emitted"*） | FOREGROUND_ENTER/EXIT、SYSTEM/ABNORMAL_EXIT 都能推 |
| 跑在 CI 里 | 需要 GUI，本地跑 | ✅ vitest + jsdom |

**两者是互补而非替代**：官方模拟器回答「屏幕上到底长什么样」，自建夹具回答「错误路径下代码怎么走」。

---

## 逐项归属

### 排版引擎

| 结论 | 档 | 证据 |
|---|---|---|
| 行高 = 27 px | **(a)** | `docs/assets/g2probe-00-layout.png` 逐行墨迹带间距 27 px；pretext `line_height` 字段 |
| 字形度量（advance + kerning + 逐字取整） | **(a)** | Python 引擎与官方 pretext 在 17 075 码点 + 1 376 折行用例上零分歧（`gateway/tests/test_metrics_oracle.py`） |
| 折行位置 | **(a)** | 同上；夹具侧每次渲染都与 `measureTextWrap` 的行数交叉校验 |
| body 每页 8 行 | **(a)** | 216 / 27 = 8，截图确认 |
| 中文禁则（行首/行尾禁排） | **(b)** | 这是**我们自己加的排版策略**，固件没有这个概念。168 条单测 + 600 例随机模糊 |
| 分页 / 锚点 / 页脚箭头 | **(b)** | 同上，纯服务器侧逻辑 |

### 字形

| 结论 | 档 | 证据 |
|---|---|---|
| 10 个旧字形画不出来 | **(a)** | `docs/assets/g2probe-01-glyphs-missing.png` 整幅零墨迹 |
| 16 个现役字形画得出来 | **(a)** | `g2probe-02/03-*.png` 逐行墨迹统计 |
| 缺字是静默跳过、不留占位框 | **(a)** | 同上 |
| 字形档位切换（symbol/cjk/ascii） | **(b)** | 契约驱动，两端 import 时校验 |

详见 [GLYPH-TABLE.md](./GLYPH-TABLE.md)。

### 容器

| 结论 | 档 | 证据 |
|---|---|---|
| 内容上限 = UTF-8 999 字节 | **(a)** | 999B ✅ / 1002B ❌ / 1000 ASCII 字符 ❌，见 HARDWARE-SPEC §2.1 |
| 铺满 576×288 不触发 oversize | **(a)** | `createStartUpPageContainer → 0` |
| `createStartUpPageContainer` 只能调一次 | **(b)** | 夹具强制；官方模拟器未验证该约束 |
| 容器数 / name 长度 / isEventCapture 数量 | **(b)** | 夹具的 `validatePage()`；SDK 自带校验器**不管**这几项（HARDWARE-SPEC §2.3） |
| zOrderIndex / textColor 校验 | **(a)** | 直接调 SDK 的 `validateEvenHubPageContainer` |

### 事件与生命周期

| 结论 | 档 | 证据 |
|---|---|---|
| 事件码表（0~10） | **(a)** | 直接读 SDK 的 `OsEventTypeList` |
| `/api/input` 的 click 能被 `isEventCapture=1` 的容器收到 | **(a)** | `g2probe.mjs` 靠点击步进了 8 屏 |
| 「缺字段 vs 认不出」的判别 | **(b)** | 实测 SDK 透传 `jsonData`；`plugin/tests/bridge.test.ts` 回归 |
| 5 手势 × 4 来源的映射 | **(b)** | 官方模拟器把 `eventSource` 硬编码成 1，只有夹具能测 |
| `FOREGROUND_EXIT` 不断 WS | **(b)** | 官方模拟器不发生命周期事件 |
| 镜腿事件在**真机**上是否真能到 WebView | **(c)** | 待真机 |

### 桥接

| 结论 | 档 | 证据 |
|---|---|---|
| 信封格式与 method 名 | **(a)** | 实测 SDK（HARDWARE-SPEC §5） |
| `getDeviceInfo()` → `getGlassesInfo` | **(a)** | 同上 |
| 5s 超时保护 | **(b)** | 夹具的 `bridgeHang` 注入 |
| 写失败不毒化去重缓存 | **(b)** | 夹具的 `upgradeOk=false` 注入 |
| `audioControl` 返回值 = 麦是否真开 | **(b)** | 夹具的 `micDenied` 注入 |
| BLE 真实往返延迟与闪烁 | **(c)** | 待真机 |
| 启麦延迟 / 麦克风仲裁 | **(c)** | 待真机 |
| 后台与锁屏存活 | **(c)** | 待真机 |

---

## 复现

```bash
# (a) 官方模拟器：自动拉起 vite + 模拟器，逐屏截图与判定
cd plugin && node tools/g2probe.mjs          # 加 --keep 保留窗口肉眼看

# (a) 排版引擎 vs 官方 pretext 的逐条比对
cd gateway && python -m pytest tests/test_metrics_oracle.py -v

# (b) 自建夹具：vitest + jsdom
cd plugin && npm test

# (b) 浏览器里手动跑全链路（含故障注入面板）
cd plugin && npm run dev                     # 打开 /harness/harness.html
```
