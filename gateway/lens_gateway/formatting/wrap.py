"""按**像素**折行，附加中文标点禁则。

与 `metrics.measure_wrap`（官方算法的等价移植）的分工：
`measure_wrap` 用来和官方 oracle 对齐、证明度量无误；本模块是**生产折行**，
在此基础上多做两件事 ——

1. **中文标点禁则**：闭合类标点不留在行首、开放类标点不挂在行尾。
   采用「追出」策略（把上一行末尾的字符拽到下一行），因此每一行的像素宽度
   **只会变小、绝不会超**；固件永远不需要再折一次。
2. **拉丁词不腰斩**：整词放不下就整体挪到下一行；单词本身超过一行宽才硬切。

不变量（由 `tests/test_wrap.py` 逐条断言）：
- 每一行的 `text_width(line) <= max_width_px`
- 拼接所有行（去掉折行处）与原文的可见字符完全一致，不丢字不重字
- 任何输入下都会终止（宽度过小直接 `ValueError`，不进死循环）
"""
from __future__ import annotations

from collections import deque

from .metrics import metrics

# 行首禁排：闭合类标点不能出现在行首
NO_LINE_START: frozenset[str] = frozenset("。，、；：！？）】》」』〉·…—,.;:!?)]}%›»’”")
# 行尾禁排：开放类标点不能挂在行尾
NO_LINE_END: frozenset[str] = frozenset("（【《「『〈([{‹«‘“")

# 禁则调整时最多从上一行拽下几个字符（防止病态输入把整行掏空）
_MAX_KINSOKU_PULL = 3

# 能容纳的最窄宽度 = 字库里最宽的单个字形（emoji 24px；CJK 20px）。
# 取这个值才能保证「任何单个字符都放得进一行」，折行循环因此必然前进。
MIN_WIDTH_PX = 24


class WidthTooSmall(ValueError):
    """容器宽度小到放不下一个汉字。"""


def _check_width(max_width_px: int) -> None:
    if not isinstance(max_width_px, int) or max_width_px < MIN_WIDTH_PX:
        raise WidthTooSmall(
            f"容器可用宽度 {max_width_px}px 过小（至少 {MIN_WIDTH_PX}px 才放得下一个汉字）"
        )


class _Line:
    """增量维护一行的像素宽度，结果与 `metrics.text_width` 逐位一致。

    每个字符的贡献是 ``adv_px(cp, 下一个字符)``，所以追加/弹出时都要修正
    前一个字符的 kerning 贡献。
    """

    __slots__ = ("_cps", "_w", "_m")

    def __init__(self) -> None:
        self._cps: list[int] = []
        self._w: int = 0
        self._m = metrics()

    def __len__(self) -> int:
        return len(self._cps)

    @property
    def width(self) -> int:
        return self._w

    def text(self) -> str:
        return "".join(chr(c) for c in self._cps)

    def last(self) -> str | None:
        return chr(self._cps[-1]) if self._cps else None

    def width_after(self, s: str) -> int:
        """若把 s 追加上去，这一行会有多宽（不改变自身状态）。"""
        if not s:
            return self._w
        m = self._m
        cps = [ord(c) for c in s]
        w = self._w
        if self._cps:
            prev = self._cps[-1]
            w += m.adv_px(prev, cps[0]) - m.adv_px(prev, 0)
        n = len(cps)
        for i in range(n):
            w += m.adv_px(cps[i], cps[i + 1] if i + 1 < n else 0)
        return w

    def append(self, s: str) -> None:
        m = self._m
        for ch in s:
            cp = ord(ch)
            if self._cps:
                prev = self._cps[-1]
                self._w += m.adv_px(prev, cp) - m.adv_px(prev, 0)
            self._w += m.adv_px(cp, 0)
            self._cps.append(cp)

    def pop(self) -> str:
        m = self._m
        cp = self._cps.pop()
        self._w -= m.adv_px(cp, 0)
        if self._cps:
            prev = self._cps[-1]
            self._w -= m.adv_px(prev, cp) - m.adv_px(prev, 0)
        return chr(cp)


def _is_word_char(ch: str) -> bool:
    """拉丁词的构成字符（这些字符组成的连续段不被腰斩）。"""
    return ch.isascii() and (ch.isalnum() or ch in "_'-")


def _tokenize(text: str) -> list[str]:
    """切成「不可再分的排版单元」：拉丁词整体，其余逐字符。"""
    units: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if _is_word_char(text[i]):
            j = i
            while j < n and _is_word_char(text[j]):
                j += 1
            units.append(text[i:j])
            i = j
        else:
            units.append(text[i])
            i += 1
    return units


def _apply_kinsoku(line: _Line, pending: str) -> tuple[str, str]:
    """在断行处施加禁则，返回 (本行文本, 下一行开头要带走的字符)。

    只会把字符从本行**拽到**下一行，因此本行宽度只减不增 —— 不可能造成溢出。
    """
    pulled: list[str] = []
    for _ in range(_MAX_KINSOKU_PULL):
        if len(line) <= 1:
            break  # 绝不把整行掏空
        head = (pulled[0] if pulled else (pending[0] if pending else ""))
        tail = line.last() or ""
        if head and head in NO_LINE_START:
            # 下一行行首是闭合标点 → 把本行末字一起带下去。
            # 但若本行末字**本身也是**闭合标点，拽下去只是把违例平移一格
            # （连续标点会把整行掏空），此时就地悬挂，接受这一处违例。
            if tail in NO_LINE_START:
                break
        elif not (tail and tail in NO_LINE_END):
            break  # 本行行尾不是开放标点 → 无需调整
        pulled.insert(0, line.pop())
    return line.text(), "".join(pulled)


def wrap_line(text: str, max_width_px: int, kinsoku: bool = True) -> list[str]:
    """把一个段落（不含换行符）折成若干行。

    实现要点：待排单元放在一个队列里，禁则「追出」拽下来的字符**塞回队头**重新参与
    宽度判定 —— 这样被拽下的字符不会绕过宽度检查（模糊测试曾在这里抓到超宽行）。

    终止性：每次断行都会产出一个非空行，且只在行内已有 ≥2 个字符时才拽字符回队；
    单个字符必定放得下（`MIN_WIDTH_PX` = 字库最宽字形），所以队列必然被消耗完。
    """
    _check_width(max_width_px)
    if not text:
        return [""]

    lines: list[str] = []
    line = _Line()
    queue: deque[str] = deque(_tokenize(text))

    def flush(pending: str) -> None:
        """收掉当前行；禁则拽下的字符回到队头重新排。"""
        nonlocal line
        if kinsoku:
            text_out, carry = _apply_kinsoku(line, pending)
        else:
            text_out, carry = line.text(), ""
        lines.append(text_out)
        line = _Line()
        for ch in reversed(carry):
            queue.appendleft(ch)

    while queue:
        unit = queue.popleft()
        # 整词放不下一整行 → 拆成单字重新入队，由下面的通用逻辑硬切
        if len(unit) > 1 and _Line().width_after(unit) > max_width_px:
            for ch in reversed(unit):
                queue.appendleft(ch)
            continue
        if len(line) and line.width_after(unit) > max_width_px:
            # 顺序要紧：先把本单元放回队头，flush 再把禁则拽下的字符压在它前面，
            # 得到 [拽下的字符…, 本单元, 其余]。反过来会把字序弄乱。
            queue.appendleft(unit)
            flush(unit)
            continue
        line.append(unit)   # 行空时无条件放入，保证前进

    lines.append(line.text())
    # 折行本身不产生空行（空输入已在前面短路）
    return [ln for ln in lines if ln != ""] or [""]


def wrap_text(text: str, max_width_px: int, kinsoku: bool = True) -> list[str]:
    """按 `\\n` 分段后逐段折行。段间空行保留一行，首尾空行去掉。

    G2 固件明确支持 `'\\n' is a line break`，所以这里保留的换行在真机上有效。
    """
    _check_width(max_width_px)
    out: list[str] = []
    for para in text.split("\n"):
        para = para.rstrip()
        if not para:
            if out and out[-1] != "":
                out.append("")
            continue
        out.extend(wrap_line(para, max_width_px, kinsoku=kinsoku))
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return out


def tail_window(text: str, max_width_px: int, max_lines: int, ellipsis: str = "…") -> str:
    """尾部滚动窗（聆听态用）：只保留最后 `max_lines` 行，被截掉时以省略号开头。

    修 F2：省略号是**加宽**首行的，必须把加了省略号之后仍然超宽的情况处理掉，
    否则真机上这一行会被固件二次折行、把窗口挤成 max_lines+1 行。
    """
    _check_width(max_width_px)
    if max_lines < 1:
        raise ValueError(f"max_lines 必须 ≥1，收到 {max_lines}")
    lines = wrap_text(text, max_width_px)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[-max_lines:]
    m = metrics()
    head = ellipsis + kept[0]
    if m.text_width(head) > max_width_px:
        # 加了省略号放不下 → 从行首削字，直到塞得进去
        body = kept[0]
        while body and m.text_width(ellipsis + body) > max_width_px:
            body = body[1:]
        head = ellipsis + body
    kept[0] = head
    return "\n".join(kept)
