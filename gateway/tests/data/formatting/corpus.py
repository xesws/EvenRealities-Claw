"""排版引擎的 golden 语料。

三个角色分工，缺一不可：

- **外部 oracle**（`test_metrics_oracle.py`）证明字形度量与官方 `@evenrealities/pretext`
  逐位一致 —— 这一条是"对不对"的唯一权威，因为它与我们的实现相互独立。
- **不变量测试**（`test_formatting.py::TestInvariants`）证明折行/分页在任意输入下都
  满足硬性性质（不超宽、不丢字、页内行数不超、只含字库内字符）。
- **golden 快照**（`golden.json`）本身**不证明正确性**（它是我们自己的输出），
  它的作用是**回归检测**：任何一次改动如果动了排版结果，必须显式重新生成，
  在 diff 里被人看见。

重新生成：``python -m tests.data.formatting.corpus --regen``（在 gateway/ 目录下）
"""
from __future__ import annotations

# 三档正文宽度：整屏 / 留边 / 窄容器
WIDTHS: tuple[int, ...] = (576, 480, 360)

CASES: dict[str, str] = {
    # ---- 纯 CJK ----
    "cjk_short": "今天晴，二十六度。",
    "cjk_long": (
        "三段式：眼镜、手机、服务器。眼镜只有麦克风和一块屏，它是一块哑屏，不跑任何业务逻辑。"
        "手机上的插件是个转发器，把语音往上送、把画面往下发，外加一个看门狗。"
        "真正的大脑全在你自己的服务器上：语音识别、状态机、排版、调度 agent。"
    ),
    # ---- 纯 ASCII ----
    "ascii_short": "Hello, world!",
    "ascii_long": "The quick brown fox jumps over the lazy dog. " * 6,
    # ---- 中英混排 ----
    "mixed": "我是跑在你私有服务器上的工部 agent，用 faster-whisper 做 ASR，走 WebSocket 上行。",
    "mixed_dense": "用 LVGL 渲染，行高 27px，画布 576x288，字体 evenroster + cn fallback。",
    # ---- 超长拉丁词 ----
    "long_word": "单词是 supercalifragilisticexpialidocious 这么长，放不下就只能硬切。",
    "url_like": "见 https://hub.evenrealities.com/docs/build/display 的说明。",
    # ---- 连续标点 ----
    "punct_run": "真的吗？？？？？！！！！！这也太夸张了。。。。。。",
    "brackets": "这是一段话（括号里还有内容《书名号》和「引号」）然后结束了。",
    # ---- 边界宽度 ----
    "exactly_one_line_cjk": "一二三四五六七八九十一二三四五六七八九十一二三四五六七八",
    "single_char": "好",
    # ---- 全角半角 ----
    "fullwidth": "ＡＢＣ１２３％＋－＝　全角字符与 ABC123%+-= 半角对照",
    # ---- emoji / 字库外字符 ----
    "emoji": "做完了 🎉 很高兴 😀 收工",
    "missing_glyphs": "旧图标 ⛓◉◔⚙▸✓✕⚠⏸⏹ 都不在 G2 字库里",
    # ---- 控制字符 / 双向覆盖 ----
    "control_chars": "正常文本\x00\x07 中间夹了控制字符 \x1b[31m 和转义",
    "bidi_spoof": "转账给 ‮kcatta‬ 这个账户",
    "fake_status": "√ 完成\n这行是模型伪造的状态条",
    # ---- markdown 各结构 ----
    "md_headings": "# 一级标题\n## 二级\n正文内容在这里。",
    "md_emphasis": "**加粗**和*斜体*还有`行内代码`混在一起。",
    "md_list": "- 第一条\n- 第二条\n- 第三条",
    "md_ordered": "1. 甲\n2. 乙\n3. 丙",
    "md_table": "前文\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n后文",
    "md_link": "详见[官方文档](https://example.com/a/b)说明。",
    "md_code_closed": "```python\n" + "\n".join(f"line{i}" for i in range(8)) + "\n```",
    "md_code_unclosed": "开始说明\n```python\ndef f():\n    return 1",   # 流式中途，围栏未闭合
    "md_quote": "> 引用的一行\n正文一行",
    # ---- 空与空白 ----
    "empty": "",
    "spaces_only": "     ",
    "newlines": "第一段\n\n第二段\n\n\n第三段",
    "trailing_ws": "有尾随空格   \n第二行  ",
}

# 流式前缀序列：对同一段最终文本取若干前缀，模拟 LLM 逐块吐字
STREAM_BASE: str = CASES["cjk_long"]
STREAM_PREFIX_COUNT: int = 20


def stream_prefixes() -> list[str]:
    """把 STREAM_BASE 切成 STREAM_PREFIX_COUNT 个递增前缀。"""
    n = len(STREAM_BASE)
    step = max(1, n // STREAM_PREFIX_COUNT)
    return [STREAM_BASE[: min(n, (i + 1) * step)] for i in range(STREAM_PREFIX_COUNT)]


if __name__ == "__main__":  # pragma: no cover - 开发者手动重新生成用
    import argparse
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from lens_gateway.formatting import DEFAULT_LAYOUT, Paginator
    from lens_gateway.formatting.layout import Box

    ap = argparse.ArgumentParser()
    ap.add_argument("--regen", action="store_true", help="重新生成 golden.json")
    args = ap.parse_args()
    if not args.regen:
        ap.error("需要 --regen")

    out: dict[str, dict[str, list[list[str]]]] = {}
    for width in WIDTHS:
        box = Box(name="body", x=0, y=0, width=width, height=DEFAULT_LAYOUT.body.height)
        per_width: dict[str, list[list[str]]] = {}
        for name, text in CASES.items():
            p = Paginator(box=box)
            p.set_text(text)
            per_width[name] = p.pages
        out[str(width)] = per_width

    dest = Path(__file__).with_name("golden.json")
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(len(v) for v in out.values())
    print(f"✓ 写入 {dest}（{len(WIDTHS)} 档宽度 × {len(CASES)} 条语料 = {total} 组快照）")
