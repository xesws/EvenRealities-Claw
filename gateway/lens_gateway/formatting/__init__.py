"""排版引擎：把 agent 的自由文本变成 G2 眼镜上一帧一帧的整屏画面。

眼镜是远端哑屏，**所有**排版都在服务器完成，它只负责显示。本包为纯函数/纯状态，无 IO。

模块分工：

- `metrics`   固件字形度量（`@evenrealities/pretext` 的 Python 移植，逐位一致）
- `layout`    576×288 画布与 27px 行高栅格下的三容器版式
- `glyphs`    HUD 语义字形表，加载时按字库自校验
- `markdown`  剥离 markdown 标记
- `sanitize`  删控制字符/双向覆盖、剔行首状态字形、丢弃字库外字符
- `wrap`      像素折行 + 中文标点禁则
- `paginate`  像素盒分页 + 页码状态

度量数据由 `plugin/tools/extract_metrics.mjs` 生成，一致性由
`tests/test_metrics_oracle.py` 用官方 JS 作为外部 oracle 逐条比对。
规格出处见 `docs/HARDWARE-SPEC.md`。
"""
from .glyphs import DEFAULT_PROFILE, GlyphError, GlyphSet, available_profiles, glyph_set
from .layout import CANVAS_HEIGHT, CANVAS_WIDTH, DEFAULT_LAYOUT, Box, Layout
from .markdown import strip_markdown
from .metrics import (
    in_font,
    line_height,
    lines_that_fit,
    measure_wrap,
    missing_codepoints,
    px_truncate,
    text_width,
)
from .paginate import Paginator, paginate
from .sanitize import SanitizeReport, sanitize_body, sanitize_report
from .wrap import MIN_WIDTH_PX, WidthTooSmall, tail_window, wrap_line, wrap_text

__all__ = [
    # 度量
    "text_width", "line_height", "lines_that_fit", "in_font", "missing_codepoints",
    "px_truncate", "measure_wrap",
    # 版式
    "Box", "Layout", "DEFAULT_LAYOUT", "CANVAS_WIDTH", "CANVAS_HEIGHT",
    # 字形
    "GlyphSet", "GlyphError", "glyph_set", "available_profiles", "DEFAULT_PROFILE",
    # 文本处理
    "strip_markdown", "sanitize_body", "sanitize_report", "SanitizeReport",
    "wrap_line", "wrap_text", "tail_window", "WidthTooSmall", "MIN_WIDTH_PX",
    # 分页
    "Paginator", "paginate",
]
