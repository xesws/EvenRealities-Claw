"""G2 画布版式 —— 所有像素魔数的唯一真源。

规格出处见 `docs/HARDWARE-SPEC.md`：
- 开发者可寻址画布 **576 × 288 px / 眼**，左上原点，X 向右 Y 向下
- LVGL 行高**固定 27px**，不可配，也没有字号控制

**为什么要按 27px 栅格重新排**：早先的版式是 status 32 / body 220 / foot 36，
每个容器高度都不是 27 的整数倍 —— body 220px 实际只能显示 ``220 // 27 = 8`` 行，
但代码按 5 行分页，**屏幕底部恒定空着 85px（37%）**，一段回答被切成接近两倍的页数。
现在三个容器都按整行数取高：36 / 216 / 36 = 288，body 正好 8 行。
"""
from __future__ import annotations

from dataclasses import dataclass

from .contract import contract
from .metrics import line_height, lines_that_fit

_CANVAS = contract()["canvas"]
CANVAS_WIDTH: int = int(_CANVAS["width"])
CANVAS_HEIGHT: int = int(_CANVAS["height"])


@dataclass(frozen=True)
class Box:
    """一个文本容器的像素几何。

    `border_width` / `padding` 对应 SDK 的 `borderWidth` / `paddingLength`；
    折行用的**内容宽度**要把它们减掉（官方 pretext 的 maxWidth 就是内容宽度）。
    """

    name: str
    x: int
    y: int
    width: int
    height: int
    border_width: int = 0
    padding: int = 0
    #: SDK 0.0.14+ 的文本亮度 0~4（省略即设备默认 4）。G2 上唯一真实存在的视觉分层手段。
    text_color: int | None = None
    #: 额外退让的像素。默认 0 —— 我们的字形度量与固件**逐位一致**（见 metrics.py 的
    #: 交叉验证），不需要靠猜安全系数。留这个阀门是为了万一固件字体版本与
    #: `@evenrealities/pretext` 的内嵌度量出现偏差时，能改一个数字止血而不必改代码。
    safety_px: int = 0

    @property
    def inner_width(self) -> int:
        """可用于排文字的像素宽度。"""
        return self.width - 2 * (self.border_width + self.padding) - self.safety_px

    @property
    def inner_height(self) -> int:
        return self.height - 2 * (self.border_width + self.padding)

    @property
    def max_lines(self) -> int:
        """这个盒子能显示几行（官方 paginate.ts：floor(h / 27)）。"""
        return lines_that_fit(self.inner_height)


@dataclass(frozen=True)
class Layout:
    """整屏三容器版式。默认值即 G2 上的生产版式。"""

    status: Box
    body: Box
    foot: Box

    def boxes(self) -> tuple[Box, Box, Box]:
        return (self.status, self.body, self.foot)

    def validate(self) -> None:
        """自检：不越界、不重叠、纵向铺满、每个盒子至少一行。"""
        lh = line_height()
        prev_bottom = 0
        for b in self.boxes():
            if b.x < 0 or b.y < 0 or b.x + b.width > CANVAS_WIDTH or b.y + b.height > CANVAS_HEIGHT:
                raise ValueError(
                    f"容器 {b.name} 越出 {CANVAS_WIDTH}×{CANVAS_HEIGHT} 画布："
                    f"x={b.x} y={b.y} w={b.width} h={b.height}"
                )
            if b.y != prev_bottom:
                raise ValueError(f"容器 {b.name} 与上一个容器之间有空隙或重叠（y={b.y}，期望 {prev_bottom}）")
            if b.inner_height < lh:
                raise ValueError(f"容器 {b.name} 内高 {b.inner_height}px 放不下一行（行高 {lh}px）")
            prev_bottom = b.y + b.height


def _from_contract() -> Layout:
    """按 `protocol/hud-contract.json` 的 containers 构造版式。"""
    boxes = {
        c["name"]: Box(
            name=c["name"], x=int(c["x"]), y=int(c["y"]),
            width=int(c["w"]), height=int(c["h"]),
            text_color=c.get("textColor"),
        )
        for c in contract()["containers"]
    }
    missing = {"status", "body", "foot"} - set(boxes)
    if missing:
        raise ValueError(f"HUD 契约缺少容器：{sorted(missing)}")
    return Layout(status=boxes["status"], body=boxes["body"], foot=boxes["foot"])


#: 生产版式：36 / 216 / 36 = 288，body 恰好 8 行 × 27px。
#:
#: 注意：整屏正好铺满 576×288 是**未经真机验证**的边界（官方示例最大只到 420×270，
#: 且 SDK 未说明 oversize 的触发条件）。见 HARDWARE-SPEC.md 的风险条目。
DEFAULT_LAYOUT = _from_contract()
DEFAULT_LAYOUT.validate()

# 契约里每个容器都声明了自己能放几行 —— 与 27px 行高算出来的必须一致，
# 否则说明契约被改错了（比如把高度改成非 27 的倍数）。
for _c in contract()["containers"]:
    _declared = int(_c["lines"])
    _actual = getattr(DEFAULT_LAYOUT, _c["name"]).max_lines
    if _declared != _actual:
        raise ValueError(
            f"HUD 契约不自洽：容器 {_c['name']} 声明 {_declared} 行，"
            f"但 {_c['h']}px / {line_height()}px 行高实际是 {_actual} 行"
        )
