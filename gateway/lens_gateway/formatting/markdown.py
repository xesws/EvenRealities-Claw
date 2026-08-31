"""Markdown 剥离：模型爱写 markdown，眼镜只能显示纯文本。

G2 的文本容器是**纯文本、左对齐、单一字体、无字号控制**，
所以 `**粗体**`、表格、代码块在真机上只会变成一堆碍眼的原始标记。

相对旧实现的关键修复（F7）：**流式期间的未闭合代码围栏**。
模型吐到一半时文本里常常只有一个 ``` 而没有配对的收尾，旧的正则只匹配成对围栏，
于是裸的 ``` 会直接上屏。这里显式处理落单的围栏。
"""
from __future__ import annotations

import re

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_URL = re.compile(r"https?://([^/\s]+)\S*")
_FENCE = re.compile(r"```")

_MD_PATTERNS: list[tuple[re.Pattern[str], object]] = [
    (re.compile(r"`([^`]*)`"), r"\1"),                        # 行内代码
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),           # 图片 → alt
    (re.compile(r"\[([^\]]*)\]\(([^)]*)\)"), lambda m: _link_text(m)),
    (re.compile(r"^#{1,6}\s*", re.M), ""),                    # 标题井号
    (re.compile(r"^\s*[-*+]\s+", re.M), "• "),               # 列表符 → •（U+2022，已确认在字库）
    (re.compile(r"^\s*\d+\.\s+", re.M), lambda m: m.group(0).strip() + " "),
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),
    (re.compile(r"\*([^*]*)\*"), r"\1"),
    (re.compile(r"__([^_]*)__"), r"\1"),
    (re.compile(r"^>\s*", re.M), ""),                         # 引用
    (re.compile(r"^[-=_]{3,}\s*$", re.M), ""),                # 分割线
]

# 代码块最多显示几行，超出给一句提示
_CODE_PREVIEW_LINES = 3


def _link_text(m: re.Match[str]) -> str:
    text = m.group(1).strip()
    return text if text else _URL.sub(r"\1", m.group(2))


def _summarize_code(inner: str) -> str:
    """把一段代码压成前几行 + 一句提示。"""
    first_nl = inner.find("\n")
    if first_nl > -1:
        inner = inner[first_nl + 1:]        # 丢掉 ```python 那行的语言标注
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    if not lines:
        return "（代码块）"
    if len(lines) > _CODE_PREVIEW_LINES:
        return "\n".join(lines[:_CODE_PREVIEW_LINES]) + f"\n（代码共{len(lines)}行，手机查看全文）"
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    """处理代码围栏，**包括流式期间落单的那一个**（修 F7）。"""
    parts = _FENCE.split(text)
    if len(parts) == 1:
        return text
    # parts 交替为 [外部, 代码, 外部, 代码, ...]
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(part)
        else:
            out.append(_summarize_code(part))
    # 奇数个围栏 = 最后一段代码尚未闭合，上面已按代码块处理，不会留下裸 ```
    return "".join(out)


def strip_markdown(text: str) -> str:
    """剥离 markdown 标记；表格降级为一句话占位；URL 只留域名。"""
    if not text:
        return ""
    text = _strip_fences(text)
    rows = _TABLE_ROW.findall(text)
    if rows:
        n = max(0, len(rows) - 2)          # 去掉表头与分隔行
        text = _TABLE_ROW.sub("", text)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        text += f"\n（含表格共{n}行，请在手机查看）"
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)          # type: ignore[arg-type]
    text = _URL.sub(r"\1", text)            # URL 只留域名
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
