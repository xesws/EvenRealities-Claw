"""G2 固件字形度量 —— @evenrealities/pretext 的 Python 移植。

**为什么需要它**：眼镜是哑屏，排版必须在服务器完成；而 G2 的字体是**非等宽**的
LVGL 位图字体，且**不可设字号**。任何「一个汉字算 2 格、半角算 1 格」的字符网格模型
都必然与固件实际折行位置不符（官方原话：*"Pagination is driven by the container's
real pixel box, not a character budget."*）。

本模块把官方度量库的算法逐行移植到 Python，度量数据由
`plugin/tools/extract_metrics.mjs` 从 npm 包原样导出到 `data/g2_font_metrics.json`。
两边的一致性由 `tests/test_metrics_oracle.py` 用官方 JS 作为外部 oracle 逐条比对。

移植的固件行为（来自 LVGL）：
1. 字形查找走三段回退链 evenroster → evenroster_crylgrek → cn（范围表）→ evenemoji
2. kerning 用固件公式 ``(kern_value * kern_scale) >> 4``（kern_scale=16 时即原值）
3. **逐字形**取整到像素后再累加：``(adv_w + kern + 8) >> 4``，不是对 1/16px 总和取整
4. 字库外的可打印字符按占位符宽度 4px 计（box_w=2, adv_w=box_w+2）
5. 折行点：空格、连字符、CJK 边界；无折行点时硬切

规格出处见 `docs/HARDWARE-SPEC.md`。
"""
from __future__ import annotations

import json
from bisect import bisect_right
from pathlib import Path
from typing import Iterable

_DATA_PATH = Path(__file__).with_name("data") / "g2_font_metrics.json"

# 固件字库外可打印字符的占位宽度（像素）。见 pretext getAdvWPx：box_w=2, adv_w=box_w+2。
_PLACEHOLDER_ADV_PX = 4


class _GlyphStage:
    """回退链上的一段：要么是显式字形表，要么是 cn 的「范围 + 例外」表。"""

    __slots__ = ("name", "glyphs", "ranges_start", "ranges_end", "exceptions", "default_adv_w")

    def __init__(self, font: dict) -> None:
        self.name: str = font["name"]
        self.glyphs: dict[int, int] = {int(k): v for k, v in (font.get("glyphs") or {}).items()}
        raw_ranges: list[list[int]] = font.get("ranges") or []
        pairs = sorted((int(a), int(b)) for a, b in raw_ranges)
        self.ranges_start: list[int] = [a for a, _ in pairs]
        self.ranges_end: list[int] = [b for _, b in pairs]
        self.exceptions: dict[int, int] = {int(k): v for k, v in (font.get("exceptions") or {}).items()}
        self.default_adv_w: int = font.get("default_adv_w") or 0

    def lookup(self, cp: int) -> int | None:
        """返回该段给出的 advance（1/16 px），本段不认识该码点则返回 None。"""
        adv = self.glyphs.get(cp)
        if adv is not None:
            return adv
        if self.ranges_start:
            # ranges 已按起点排序，二分定位「起点 ≤ cp」的最后一段
            i = bisect_right(self.ranges_start, cp) - 1
            if i >= 0 and cp <= self.ranges_end[i]:
                return self.exceptions.get(cp, self.default_adv_w)
        return None


class _KernTable:
    """一张 LVGL 的成对 kerning 表（左类 × 右类 的稠密矩阵）。"""

    __slots__ = ("to_left", "to_right", "right_cnt", "values", "owns")

    def __init__(self, font: dict) -> None:
        kern: dict = font["kern"]
        self.to_left: dict[int, int] = {int(k): v for k, v in kern["cp_to_left"].items()}
        self.to_right: dict[int, int] = {int(k): v for k, v in kern["cp_to_right"].items()}
        self.right_cnt: int = int(kern["right_cnt"])
        self.values: list[int] = list(kern["values"])
        # 「这张表所属字体是否拥有该码点」——用于复刻 pretext 的 break 语义
        self.owns: frozenset[int] = frozenset(int(k) for k in (font.get("glyphs") or {}))


class FontMetrics:
    """G2 字形度量表。线程安全（只读），进程内单例即可。"""

    def __init__(self, data: dict) -> None:
        self.line_height: int = int(data["line_height"])
        self.source: dict = data.get("source", {})
        self._stages: list[_GlyphStage] = [_GlyphStage(f) for f in data["fonts"]]
        self._kern: list[_KernTable] = [_KernTable(f) for f in data["fonts"] if f.get("kern")]
        # 热路径缓存：像素级 advance（无 kerning）。CJK 语料命中率极高。
        self._adv_px_cache: dict[int, int] = {}

    # ---------- 基础查询 ----------

    def adv_w(self, cp: int) -> int:
        """码点的原始 advance（1/16 px，不含 kerning）。字库外返回 0。"""
        for stage in self._stages:
            adv = stage.lookup(cp)
            if adv is not None:
                return adv
        return 0

    def in_font(self, cp: int) -> bool:
        """该码点是否真的能被固件画出来。

        字库外的字符固件不会正常渲染（官方文档记为静默跳过，pretext 记为 4px 占位框）
        —— 两种说法都意味着**看不到你想要的那个字形**，所以排版阶段一律视为不可用。
        """
        return self.adv_w(cp) != 0

    def kern_adj(self, cp: int, next_cp: int) -> int:
        """字符对 (cp, next_cp) 的 kerning 调整（1/16 px）。"""
        for table in self._kern:
            lc = table.to_left.get(cp)
            rc = table.to_right.get(next_cp)
            if lc is not None and rc is not None:
                return table.values[(lc - 1) * table.right_cnt + (rc - 1)]
            # 只有「拥有」该码点的第一张表参与 kerning（与 pretext 的 break 一致）
            if cp in table.owns:
                break
        return 0

    def adv_px(self, cp: int, next_cp: int = 0) -> int:
        """码点的像素 advance，含到 next_cp 的 kerning。固件逐字形取整：(adv+kern+8)>>4。"""
        if next_cp <= 0:
            cached = self._adv_px_cache.get(cp)
            if cached is not None:
                return cached
        raw = self.adv_w(cp)
        if raw == 0 and cp >= 32:
            px = _PLACEHOLDER_ADV_PX
        else:
            kern = self.kern_adj(cp, next_cp) if next_cp > 0 else 0
            px = (raw + kern + 8) >> 4
        if next_cp <= 0:
            self._adv_px_cache[cp] = px
        return px

    # ---------- 文本测量 ----------

    def text_width(self, text: str) -> int:
        """单行像素宽度（不折行，含 kerning）。"""
        cps = [ord(c) for c in text]
        n = len(cps)
        return sum(self.adv_px(cps[i], cps[i + 1] if i + 1 < n else 0) for i in range(n))

    def missing_codepoints(self, text: str) -> list[int]:
        """文本里固件画不出来的码点（去重，保序）。"""
        seen: set[int] = set()
        out: list[int] = []
        for ch in text:
            cp = ord(ch)
            if cp in seen or cp < 32:
                continue
            seen.add(cp)
            if not self.in_font(cp):
                out.append(cp)
        return out


# ---------- 折行判定（复刻 pretext / LVGL） ----------


def is_cjk(cp: int) -> bool:
    """CJK 码点：任意边界都是折行机会。范围与 pretext isCJK 完全一致。"""
    return (0x2E80 <= cp <= 0x9FFF) or (0xF900 <= cp <= 0xFAFF) or (0xAC00 <= cp <= 0xD7AF)


def is_breakable(cp: int) -> bool:
    """该字符之后是否构成折行机会：空格、连字符、CJK。"""
    return cp == 32 or cp == 45 or is_cjk(cp)


# ---------- 单例 ----------

_METRICS: FontMetrics | None = None


def metrics() -> FontMetrics:
    """进程内共享的度量表（首次调用时加载 ~128KB JSON）。"""
    global _METRICS
    if _METRICS is None:
        _METRICS = FontMetrics(json.loads(_DATA_PATH.read_text(encoding="utf-8")))
    return _METRICS


# ---------- 便捷函数 ----------


def text_width(text: str) -> int:
    """单行像素宽度。"""
    return metrics().text_width(text)


def line_height() -> int:
    """固件固定行高（px）。G2 上恒为 27，不可配。"""
    return metrics().line_height


def lines_that_fit(box_height_px: int) -> int:
    """给定容器像素高度能放下几行文本。官方 paginate.ts：floor(h / 27)，至少 1。"""
    return max(1, box_height_px // line_height())


def in_font(ch: str) -> bool:
    """单个字符是否在 G2 字库内。"""
    return metrics().in_font(ord(ch))


def missing_codepoints(text: str) -> list[int]:
    """文本中固件画不出的码点。"""
    return metrics().missing_codepoints(text)


def px_truncate(text: str, max_px: int) -> str:
    """截断到像素预算内，超出则以 '…' 收尾。

    与官方 pxTruncate 的差别：官方用三个半角点 '...'（10px×3），
    这里用单个 '…'（U+2026，已确认在字库内，10px）—— 省 20px，中文更自然。
    """
    m = metrics()
    if m.text_width(text) <= max_px:
        return text
    ellipsis = "…"
    chars = list(text)
    lo, hi = 0, len(chars)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        # 量完整拼接串，这样接缝处的 kerning 也算进去
        if m.text_width("".join(chars[:mid]) + ellipsis) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return "".join(chars[:lo]) + ellipsis


def measure_wrap(text: str, max_width_px: int) -> tuple[int, int, list[int]]:
    """按固件规则折行并测量。返回 (行数, 总高度px, 每行宽度px)。

    这是 pretext.measureTextWrap 的等价实现，仅用于**与官方 oracle 交叉验证**；
    生产折行请用 `wrap.wrap_text()`（它额外做中文标点禁则并返回文本行）。
    """
    m = metrics()
    if not text:
        return 0, 0, []
    line_widths: list[int] = []
    current = 0
    last_break_idx = -1
    last_break_width = 0
    cps = [ord(c) for c in text]
    n = len(cps)
    i = 0
    while i < n:
        cp = cps[i]
        if cp == 10:  # '\n' 显式换行
            line_widths.append(current)
            current = 0
            last_break_idx = -1
            i += 1
            continue
        if current == 0 and cp == 32:  # 行首空格丢弃（LVGL 隐式行为）
            i += 1
            continue
        adv = m.adv_px(cp, cps[i + 1] if i + 1 < n else 0)
        if current + adv > max_width_px:
            if cp == 32:
                line_widths.append(current)
                current = 0
                last_break_idx = -1
                i += 1
            elif last_break_idx != -1:
                line_widths.append(last_break_width)
                current = 0
                i = last_break_idx + 1
                last_break_idx = -1
            else:  # 硬切：溢出字符另起一行
                line_widths.append(current)
                current = adv
                last_break_idx = -1
                i += 1
        else:
            current += adv
            if is_breakable(cp):
                last_break_idx = i
                last_break_width = current - adv if cp == 32 else current
            i += 1
    line_widths.append(current)
    return len(line_widths), len(line_widths) * m.line_height, line_widths


def widths_of(chars: Iterable[str]) -> dict[str, int]:
    """调试用：一组字符各自的像素宽度（无 kerning）。"""
    m = metrics()
    return {c: m.adv_px(ord(c)) for c in chars}
