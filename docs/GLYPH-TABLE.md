# G2 字形判定表

> **一句话结论**：仓库早期 HUD 用的 13 个字形里，**10 个在 G2 上根本画不出来，而且不留任何痕迹**。
> 现役的 16 个 symbol 档字形全部经过双重独立验证：官方度量库判定在库，官方模拟器截图判定有墨。

本文件是 `protocol/hud-contract.json` 里 `glyphProfiles` 的证据链。任何新增字形都必须先过这两关。

---

## 1. 为什么这件事非查不可

官方文档站 `/docs/build/display` 原文：

> Characters outside the font are **silently skipped** (no placeholder glyph is shown).

也就是说，下发一个字库外的字符，屏幕上**什么都不会发生** —— 没有豆腐块、没有问号、没有错误码。
本地浏览器里一切正常，真机上状态条最左边那个"0.5 秒瞥视锚点"直接消失，而且**没有任何东西会告诉你**。

`DESIGN.md` 的核心交互承诺是「最左三个字符告诉你系统在干什么」。用错字形，这条承诺在真机上整体失效。

---

## 2. 两条互相独立的判据

| 判据 | 来源 | 机制 | 覆盖面 |
|---|---|---|---|
| **A. 度量库** | `@evenrealities/pretext` 0.1.4（官方） | `getAdvW(cp) === 0` ⇔ 字体回退链（evenroster → evenroster_crylgrek → cn → evenemoji）上没有任何字体覆盖该码点 | 全 Unicode，可在 CI 里跑 |
| **B. 渲染截图** | `@evenrealities/evenhub-simulator` 0.9.4（官方） | 每行一个字形建页 → `GET /api/screenshot/glasses` → 逐 27px 行带统计**不透明像素**；一格墨都没有 = 画不出来 | 只能一次几十个，但直接观测渲染栈 |

判据 B 的采样细节：模拟器导出的 576×288 RGBA PNG 把每个像素的 RGB 固定成纯绿 `(0,255,0)`，
**灰阶强度全在 alpha 通道**。按 RGB 判会把整屏都算成有墨 —— 必须按 alpha 判。

复现命令（自动拉起 vite + 模拟器，跑完自动收摊）：

```bash
cd plugin && node tools/g2probe.mjs
```

产物：`docs/assets/g2probe-*.png`（逐屏截图）与 `docs/assets/g2probe.json`（结构化判定）。

---

## 3. 判定结果

### 3.1 画不出来的（仓库早期全在用）

截图证据：`docs/assets/g2probe-01-glyphs-missing.png` —— **整幅图零个不透明像素**。

| 字形 | 码点 | 曾用作 | 判据 A（advance） | 判据 B（墨迹） |
|---|---|---|---|---|
| `◉` | U+25C9 | 聆听 | 0 | 0 px |
| `◔` | U+25D4 | 思考 | 0 | 0 px |
| `▸` | U+25B8 | 回答 | 0 | 0 px |
| `⚙` | U+2699 | 工具 | 0 | 0 px |
| `⚠` | U+26A0 | 警告 | 0 | 0 px |
| `⛓` | U+26D3 | 断线 | 0 | 0 px |
| `✓` | U+2713 | 完成 | 0 | 0 px |
| `✕` | U+2715 | 错误 | 0 | 0 px |
| `⏸` | U+23F8 | 暂停 | 0 | 0 px |
| `⏹` | U+23F9 | 停止 | 0 | 0 px |

**两条判据 10/10 一致。** 官方文档站把 Misc Technical (U+2300–23FF) 与 Dingbats (U+2700–273F)
整区列为 *"Entirely absent ranges"*，与此吻合。

另一条同样重要的观测：模拟器 v0.7.0 的更新日志说它开了 `LV_USE_FONT_PLACEHOLDER` 并用 lvgl 的
`g2` feature「对齐固件的缺字渲染」。截图证明**对齐的结果就是什么都不画** —— 官方文档站
"no placeholder glyph is shown" 这句话在渲染栈里是字面成立的。

### 3.2 画得出来的（现役 symbol 档 + 三个此前存疑的）

截图证据：`docs/assets/g2probe-02-glyphs-substitutes-1.png`、`-03-glyphs-substitutes-2.png`。

| 字形 | 码点 | 语义 | advance (1/16px) | 墨迹 | 墨迹宽 |
|---|---|---|---|---|---|
| `·` | U+00B7 | idle 待机 | 80 | 4 px | 2 px |
| `●` | U+25CF | listening 聆听 | 320 | 272 px | 18 px |
| `→` | U+2192 | transcribing 转写 | 272 | 23 px | 14 px |
| `◐` | U+25D0 | thinking 思考 | 320 | 169 px | 18 px |
| `◆` | U+25C6 | tool 工具 | 320 | 200 px | 20 px |
| `▶` | U+25B6 | answering 回答 | 320 | 175 px | 17 px |
| `√` | U+221A | done 完成 | 208 | 22 px | 10 px |
| `×` | U+00D7 | error 错误 | 160 | 13 px | 7 px |
| `！` | U+FF01 | warning / disconnected | 320 | 26 px | 2 px |
| `‖` | U+2016 | paused 暂停 | 320 | 42 px | 4 px |
| `■` | U+25A0 | stopped 停止 | 320 | 272 px | 16 px |
| `▌` | U+258C | cursor 流式光标 | 320 | 210 px | 10 px |
| `‹` | U+2039 | page_prev 上一页 | 128 | 9 px | 5 px |
| `›` | U+203A | page_next 下一页 | 128 | 9 px | 5 px |
| `…` | U+2026 | ellipsis 省略号 | 160 | 6 px | 7 px |
| `•` | U+2022 | bullet 项目符号 | 144 | 24 px | 6 px |

**两条判据 16/16 一致。** 其中 `… ‹ ›` 三个此前被计划标为「待截图确认」（General Punctuation
只有部分支持、官方没有逐字列出），现在确认**在库**。

### 3.3 为什么 `·` 在库却不当项目符号

`·` U+00B7 在库（advance 80），但它是**中文间隔号**，属于行首禁排字符
（`gateway/lens_gateway/formatting/wrap.py` 的 `NO_LINE_START`）。
把它同时用作 markdown 项目符号，会让排版引擎在每个列表项前都触发一次禁则追出。
所以项目符号改用 `•` U+2022，`·` 只保留 idle 语义。

---

## 4. 降级档位

`protocol/hud-contract.json` 预置三档，一行配置切换（`ComposerConfig.glyph_profile`）：

| 档位 | 用途 | 例（聆听 / 思考 / 完成 / 错误） |
|---|---|---|
| `symbol` | 默认。信息密度最高 | `●` `◐` `√` `×` |
| `cjk` | 纯汉字兜底，任何字形争议都绕开 | `听` `思` `完` `误` |
| `ascii` | 极端降级 / 日志与终端调试 | `*` `~` `v` `x` |

---

## 5. 这条防线怎么保证不退化

三处强制校验，任何一处失败都会让构建/测试红掉：

1. **网关侧（Python）**：`gateway/lens_gateway/formatting/glyphs.py` 在 **import 时**遍历全部档位，
   任何字形不在度量表内直接 `GlyphError` —— 画不出来的字形连进程都起不来。
2. **插件侧（TypeScript）**：`plugin/tests/hud.test.ts` 用 pretext 逐字扫描
   `GLYPHS`、三个档位、以及插件写死会上屏的 `HUD_TEXT`；并**反向断言**被替换掉的 10 个原字形确实缺失
   （否则这份替换表就是白改的）。
3. **夹具侧**：`plugin/harness/mock.ts` 按 `getAdvW(cp) === 0` 静默丢弃字库外字符并计数 ——
   浏览器模拟器里也再不会出现真机上不存在的字。
