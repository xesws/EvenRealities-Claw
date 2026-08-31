"""正文净化（W5）：把要上屏的文本收敛到「固件画得出、且不会伪装成系统 UI」的子集。

三类处理，按顺序：

1. **控制字符与双向覆盖**：C0/C1 控制符会让 LVGL 行为未定义；
   `U+202A–202E` / `U+2066–2069` 能把文本视觉顺序倒过来，是经典的欺骗手段。一律删除。
2. **行首状态字形**：状态条的字形集合若出现在正文**行首**，眼镜上看起来就像多了一条
   系统状态行（模型只要写一句「工 √ 完成」就能伪造）。只从行首剔除，正文中间保留。
3. **字库外字符**：G2 画不出来的码点（官方文档记为静默跳过，官方度量库记为 4px 占位框）
   —— 无论哪种，用户看到的都不是想要的字形。这里直接删掉，并把删掉了什么报告出来，
   便于在日志里发现「模型爱用某个画不出的符号」这类问题。

归一化用 **NFC 而非 NFKC**：NFKC 会把全角括号、全角百分号等映射成半角
（`（` → `(`），破坏中文排版观感；NFC 只做规范合成，安全。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .glyphs import STATUS_GLYPHS, GlyphSet
from .metrics import metrics

# C0（保留 \n）与 C1 控制字符
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
# 双向文本覆盖 / 隔离
_BIDI = re.compile(r"[‪-‮⁦-⁩]")


def _leading_status_re(glyphs: frozenset[str]) -> re.Pattern[str]:
    """行首的状态字形（可带空格），例如「√ 完成…」中的「√ 」。"""
    if not glyphs:
        return re.compile(r"(?!)")   # 永不匹配
    cls = re.escape("".join(sorted(glyphs)))
    return re.compile(r"^[ \t]*(?:[" + cls + r"][ \t]*)+", re.M)


_DEFAULT_LEADING_STATUS = _leading_status_re(STATUS_GLYPHS)


@dataclass
class SanitizeReport:
    """净化过程中改了什么，供日志与测试断言。"""

    text: str
    removed_control: int = 0
    removed_bidi: int = 0
    stripped_status_lines: int = 0
    dropped_codepoints: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.removed_control or self.removed_bidi
            or self.stripped_status_lines or self.dropped_codepoints
        )

    def summary(self) -> str:
        bits: list[str] = []
        if self.removed_control:
            bits.append(f"控制字符×{self.removed_control}")
        if self.removed_bidi:
            bits.append(f"双向覆盖×{self.removed_bidi}")
        if self.stripped_status_lines:
            bits.append(f"行首状态字形×{self.stripped_status_lines}")
        if self.dropped_codepoints:
            chars = " ".join(f"U+{cp:04X}({chr(cp)})" for cp in self.dropped_codepoints[:8])
            bits.append(f"字库外字符 {chars}")
        return "；".join(bits) or "无改动"


def sanitize_report(text: str, glyphs: GlyphSet | None = None,
                    drop_missing: bool = True) -> SanitizeReport:
    """净化并返回详细报告。

    :param glyphs: 当前生效的字形档位；只有**这一档**里的纯符号状态字形会从行首剥离。
                   不传则用 symbol 档的默认集合。
    """
    if not text:
        return SanitizeReport(text="")

    rep = SanitizeReport(text=text)

    rep.removed_control = len(_CONTROL.findall(text))
    text = _CONTROL.sub("", text)

    rep.removed_bidi = len(_BIDI.findall(text))
    text = _BIDI.sub("", text)

    text = unicodedata.normalize("NFC", text)

    pattern = _DEFAULT_LEADING_STATUS if glyphs is None else _leading_status_re(glyphs.strippable())
    stripped = pattern.sub("", text)
    if stripped != text:
        rep.stripped_status_lines = sum(1 for m in pattern.finditer(text) if m.group(0).strip())
        text = stripped

    if drop_missing:
        m = metrics()
        missing = m.missing_codepoints(text)
        if missing:
            rep.dropped_codepoints = missing
            drop = {chr(cp) for cp in missing}
            text = "".join(ch for ch in text if ch not in drop)

    rep.text = text
    return rep


def sanitize_body(text: str, glyphs: GlyphSet | None = None, drop_missing: bool = True) -> str:
    """净化正文，返回可以安全下发到眼镜的文本。"""
    return sanitize_report(text, glyphs=glyphs, drop_missing=drop_missing).text
