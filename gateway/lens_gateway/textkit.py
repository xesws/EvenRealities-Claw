"""文本排版引擎：markdown 剥离、CJK 折行（标点禁则）、分页。

眼镜是远端哑屏，所有排版在服务器端完成。本模块为纯函数，无 IO。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 行首禁排：闭合类标点不能出现在行首
_NO_LINE_START = set("。，、；：！？）】》」』·…—,.;:!?)]}%›")
# 行尾禁排：开放类标点不能挂在行尾
_NO_LINE_END = set("（【《「『([{")

_MD_PATTERNS = [
    (re.compile(r"```[\s\S]*?```"), lambda m: _strip_code_block(m.group(0))),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),          # 图片 → alt
    (re.compile(r"\[([^\]]*)\]\(([^)]*)\)"), lambda m: _link_text(m)),
    (re.compile(r"^#{1,6}\s*", re.M), ""),                    # 标题井号
    (re.compile(r"^\s*[-*+]\s+", re.M), "·"),                 # 列表符 → ·
    (re.compile(r"^\s*\d+\.\s+", re.M), lambda m: m.group(0).strip() + " "),
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),
    (re.compile(r"\*([^*]*)\*"), r"\1"),
    (re.compile(r"__([^_]*)__"), r"\1"),
    (re.compile(r"^>\s*", re.M), ""),                         # 引用
    (re.compile(r"^[-=_]{3,}\s*$", re.M), ""),                # 分割线
]

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_URL = re.compile(r"https?://([^/\s]+)\S*")


def _strip_code_block(block: str) -> str:
    inner = block.strip("`").strip()
    first_nl = inner.find("\n")
    if first_nl > -1:
        inner = inner[first_nl + 1 :]
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    if len(lines) > 3:
        return "\n".join(lines[:3]) + f"\n（代码共{len(lines)}行，手机查看全文）"
    return "\n".join(lines)


def _link_text(m: re.Match) -> str:
    text = m.group(1).strip()
    return text if text else _URL.sub(r"\1", m.group(2))


def strip_markdown(text: str) -> str:
    """剥离 markdown 标记；表格降级为一句话占位。"""
    rows = _TABLE_ROW.findall(text)
    if rows:
        n = max(0, len(rows) - 2)  # 去掉表头与分隔行
        text = _TABLE_ROW.sub("", text)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        text += f"\n（含表格共{n}行，请在手机查看）"
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)  # type: ignore[arg-type]
    text = _URL.sub(r"\1", text)                              # URL 只留域名
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def char_width(ch: str) -> int:
    """显示宽度：CJK 及全角=2，半角=1（以汉字格为单位时除以 2）。"""
    code = ord(ch)
    if (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE4F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x3000 <= code <= 0x303F
        or 0x2014 <= code <= 0x2026  # —…等常用中文标点宽符号
    ):
        return 2
    return 1


def wrap_line(text: str, max_cjk_chars: int) -> list[str]:
    """单段折行。预算以汉字数计（半角字符算 0.5 格），CJK 标点禁则，拉丁词不硬切。"""
    budget = max_cjk_chars * 2
    lines: list[str] = []
    cur = ""
    cur_w = 0
    i = 0
    while i < len(text):
        ch = text[i]
        w = char_width(ch)
        # 拉丁词整体处理（≤ 行宽的词不跨行切断）
        if ch.isascii() and (ch.isalnum() or ch in "_'-"):
            j = i
            while j < len(text) and text[j].isascii() and (text[j].isalnum() or text[j] in "_'-"):
                j += 1
            word = text[i:j]
            ww = len(word)
            if ww <= budget and cur_w + ww > budget:
                lines.append(cur)
                cur, cur_w = "", 0
            # 超长词只能硬切
            while ww > budget:
                take = budget - cur_w
                cur += word[:take]
                lines.append(cur)
                cur, cur_w = "", 0
                word = word[take:]
                ww = len(word)
            cur += word
            cur_w += ww
            i = j
            continue
        if cur_w + w > budget:
            # 行首禁排：标点回卷到上一行尾
            if ch in _NO_LINE_START and cur:
                cur += ch
                lines.append(cur)
                cur, cur_w = "", 0
                i += 1
                continue
            # 行尾禁排：开放标点带到下一行
            if cur and cur[-1] in _NO_LINE_END:
                hold = cur[-1]
                lines.append(cur[:-1])
                cur, cur_w = hold, char_width(hold)
            else:
                lines.append(cur)
                cur, cur_w = "", 0
        cur += ch
        cur_w += w
        i += 1
    if cur:
        lines.append(cur)
    return lines or [""]


def wrap_text(text: str, max_cjk_chars: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        para = para.rstrip()
        if not para:
            if out and out[-1] != "":
                out.append("")
            continue
        out.extend(wrap_line(para, max_cjk_chars))
    while out and out[-1] == "":
        out.pop()
    return out


@dataclass
class Paginator:
    """把持续到达的行流装订成页（lines_per_page 行/页），带上页锚点。"""

    lines_per_page: int = 5
    wrap_chars: int = 17
    anchor_chars: int = 10
    pages: list[list[str]] = field(default_factory=list)
    _raw: str = ""

    def set_text(self, full_text: str) -> None:
        """整体重排（流式期间以累计全文调用，幂等）。"""
        self._raw = full_text
        lines = wrap_text(strip_markdown(full_text), self.wrap_chars)
        pages: list[list[str]] = []
        body_budget = self.lines_per_page
        i = 0
        while i < len(lines):
            page: list[str] = []
            if pages:  # 非首页带锚点行
                prev_tail = pages[-1][-1] if pages[-1] else ""
                anchor = prev_tail[-self.anchor_chars :]
                page.append(f"…{anchor}" if anchor else "")
            while i < len(lines) and len(page) < body_budget:
                page.append(lines[i])
                i += 1
            pages.append(page)
        self.pages = pages or [[""]]

    @property
    def total(self) -> int:
        return len(self.pages)

    def page_text(self, idx: int) -> str:
        idx = max(0, min(idx, self.total - 1))
        return "\n".join(self.pages[idx])


def tail_window(text: str, max_cjk_chars: int, max_lines: int) -> str:
    """聆听态滚动窗：只保留能塞进 max_lines 的尾部，超出部分以「…」开头。"""
    lines = wrap_text(text, max_cjk_chars)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[-max_lines:]
    kept[0] = "…" + kept[0]
    return "\n".join(kept)
