"""像素盒分页。

官方 text-heavy 模板的原话：*"Pagination is driven by the container's real pixel box,
not a character budget."* —— 每页能放几行由 ``floor(box.height / 27)`` 决定，
每行能放多少字由真实字形宽度决定，两者都不是「多少个汉字」能表达的。

**修 F7（页脚与正文对不上）**：当前页码 `cur` 与跟随标志 `follow` 从调用方
（`session.py`）**下沉到了 Paginator 内部**。以前 session 自己存 `_page`，
而 `set_text()` 重排后页数可能变少，`_page` 却没被夹紧，于是出现过页脚显示「4/2」、
正文却是第 2 页的情况。现在 `page_text()` 和 `footer()` 读的是同一个 `_cur`，
且 `set_text()` 末尾必定夹紧 —— 两者不可能再不一致。

**修 F4（锚点占位）**：续页首行的「…上页结尾」锚点是按**像素**截断的，
不再按字符数截 —— 否则锚点行自己就可能超宽被固件二次折行，把每页挤成 N+1 行。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .glyphs import GlyphSet, glyph_set
from .layout import DEFAULT_LAYOUT, Box
from .markdown import strip_markdown
from .metrics import text_width
from .sanitize import sanitize_body
from .wrap import wrap_text


@dataclass
class Paginator:
    """把一段（可能还在增长的）文本装订成整屏页面。

    典型用法：流式期间用累计全文反复调用 `set_text()`（幂等），
    然后读 `page_text()` / `footer()` 组帧。
    """

    box: Box = field(default_factory=lambda: DEFAULT_LAYOUT.body)
    glyphs: GlyphSet = field(default_factory=glyph_set)
    #: 锚点行最多占正文宽度的几成（其余留给「…」和视觉留白）
    anchor_width_ratio: float = 0.55

    pages: list[list[str]] = field(default_factory=lambda: [[""]])
    _cur: int = 0
    _raw: str = ""

    # ---------- 排版 ----------

    def set_text(self, full_text: str) -> None:
        """整体重排。流式期间以累计全文调用，幂等。"""
        self._raw = full_text
        body = sanitize_body(strip_markdown(full_text), glyphs=self.glyphs)
        lines = wrap_text(body, self.box.inner_width) if body else []

        per_page = self.box.max_lines
        use_anchor = per_page > 2   # 容器只有 1~2 行时，锚点会吃掉一半正文，不值得

        pages: list[list[str]] = []
        i = 0
        while i < len(lines):
            page: list[str] = []
            if pages and use_anchor:
                anchor = self._anchor_for(pages[-1])
                if anchor:                       # 锚点为空就不占行
                    page.append(anchor)
            while i < len(lines) and len(page) < per_page:
                page.append(lines[i])
                i += 1
            pages.append(page)

        self.pages = pages or [[""]]
        # ★ 重排**永远不移动读者**，只把 cur 夹进合法区间（修 F7：页数变少时会越界）。
        #
        # 这里曾经是「跟随末页」——流式期间自动翻到最后一页。那在终端里是对的，
        # 在 8 行的屏幕上是**读不完**：回答一旦超过一页，读者正读着第一页就会被
        # 甩到末尾，开头再也看不见。真机上抓到的原样：导航那一问在 S6 全程停在
        # 第 1 页，最后一个 token 落地的瞬间跳到 `2/2`，屏幕上只剩半句结尾。
        #
        # Paginator 的三个使用者（S6/S7 回答、MCP 写屏、打断回顾）没有一个需要
        # 跟随。真正该像实时字幕一样跟着最新的是 S2 的部分转写，它走的是
        # `tail_window()`，根本不经过这里。
        self._cur = max(0, min(self._cur, self.total - 1))

    def _anchor_for(self, prev_page: list[str]) -> str:
        """续页首行：「…」+ 上一页末行的尾部，按像素截断到不超过锚点预算。"""
        tail = prev_page[-1] if prev_page else ""
        if not tail:
            return ""
        budget = int(self.box.inner_width * self.anchor_width_ratio)
        ell = self.glyphs["ellipsis"]
        # px_truncate 是从**头部**保留，这里要保留尾部，所以先反向取够再截
        kept = tail
        while kept and text_width(ell + kept) > budget:
            kept = kept[1:]
        return ell + kept if kept else ""

    # ---------- 页面状态 ----------

    @property
    def total(self) -> int:
        return len(self.pages)

    @property
    def cur(self) -> int:
        """当前页序号（0 基）。"""
        return self._cur

    @property
    def at_last(self) -> bool:
        """读者是否停在最后一页。

        流式期间它就是「有没有跟上正在写的那一头」——调用方据此决定要不要在
        页脚打「还有新内容」的标记。
        """
        return self._cur >= self.total - 1

    def page_text(self, idx: int | None = None) -> str:
        """某一页的整屏文本。默认取当前页。"""
        i = self._cur if idx is None else max(0, min(idx, self.total - 1))
        return "\n".join(self.pages[i])

    def turn(self, delta: int) -> bool:
        """翻页。返回是否真的翻动了（到边界不动，也不产生冗余帧）。"""
        new = max(0, min(self._cur + delta, self.total - 1))
        if new == self._cur:
            return False
        self._cur = new
        return True

    def reset(self) -> None:
        """回到「空内容、停在首页」的初始态。"""
        self.pages = [[""]]
        self._cur = 0
        self._raw = ""

    # ---------- 页脚 ----------

    def footer(self, suffix: str = "") -> str:
        """页码指示。左右箭头只在**那个方向真的还有页**时出现。

        单页时返回空串（没有翻页可言，不占屏）。
        """
        if self.total <= 1:
            return suffix
        prev = self.glyphs["page_prev"] if self._cur > 0 else " "
        nxt = self.glyphs["page_next"] if not self.at_last else " "
        core = f"{prev} {self._cur + 1}/{self.total} {nxt}".strip()
        return f"{core} {suffix}".strip() if suffix else core


def paginate(text: str, box: Box | None = None) -> Paginator:
    """便捷构造：排一段静态文本。"""
    p = Paginator(box=box or DEFAULT_LAYOUT.body)
    p.set_text(text)
    return p


__all__ = ["Paginator", "paginate"]
