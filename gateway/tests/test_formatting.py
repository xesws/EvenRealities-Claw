"""排版引擎测试：不变量、缺陷回归、golden 快照。

与 `test_metrics_oracle.py` 的分工见 `tests/data/formatting/corpus.py` 的模块说明。
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest

from lens_gateway.formatting import (
    DEFAULT_LAYOUT,
    Box,
    GlyphError,
    Paginator,
    available_profiles,
    glyph_set,
    in_font,
    line_height,
    missing_codepoints,
    paginate,
    sanitize_body,
    sanitize_report,
    strip_markdown,
    tail_window,
    text_width,
    wrap_line,
    wrap_text,
)
from lens_gateway.formatting.wrap import NO_LINE_END, NO_LINE_START, WidthTooSmall
from tests.data.formatting.corpus import CASES, WIDTHS, stream_prefixes

_GOLDEN = Path(__file__).parent / "data" / "formatting" / "golden.json"
BODY = DEFAULT_LAYOUT.body


def _box(width: int) -> Box:
    return Box(name="body", x=0, y=0, width=width, height=BODY.height)


# --------------------------------------------------------------- 版式


class TestLayout:
    def test_canvas_fully_covered_without_gaps(self) -> None:
        DEFAULT_LAYOUT.validate()   # 越界/重叠/空隙都会抛
        boxes = DEFAULT_LAYOUT.boxes()
        assert sum(b.height for b in boxes) == 288
        assert all(b.width == 576 for b in boxes)

    def test_body_holds_eight_lines(self) -> None:
        """27px 固定行高下，216px 的正文容器正好 8 行。"""
        assert line_height() == 27
        assert BODY.max_lines == 8
        assert BODY.height % line_height() == 0

    def test_status_and_foot_hold_one_line(self) -> None:
        assert DEFAULT_LAYOUT.status.max_lines == 1
        assert DEFAULT_LAYOUT.foot.max_lines == 1

    def test_safety_px_shrinks_usable_width(self) -> None:
        assert Box(name="b", x=0, y=0, width=576, height=216, safety_px=16).inner_width == 560

    def test_out_of_canvas_rejected(self) -> None:
        from lens_gateway.formatting.layout import Layout

        bad = Layout(
            status=Box(name="s", x=0, y=0, width=576, height=36),
            body=Box(name="b", x=0, y=36, width=576, height=216),
            foot=Box(name="f", x=0, y=252, width=576, height=64),   # 越界 28px
        )
        with pytest.raises(ValueError, match="越出"):
            bad.validate()


# --------------------------------------------------------------- 字形


class TestGlyphs:
    def test_all_profiles_render_on_device(self) -> None:
        """每个预置档位的每个字形都必须在 G2 字库内（导入时已校验，这里显式复核）。"""
        for profile in available_profiles():
            for name, glyph in glyph_set(profile).table.items():
                for ch in glyph:
                    assert in_font(ch), f"{profile}.{name} 的 {ch!r} 不在 G2 字库"

    def test_legacy_glyphs_are_genuinely_missing(self) -> None:
        """记录判决：仓库早期用的这 10 个字形确实不在 G2 字库，不是猜的。"""
        for ch in "⛓◉◔⚙▸✓✕⚠⏸⏹⚡":
            assert not in_font(ch), f"{ch} 竟然在字库里——字形表需要重新评估"

    def test_replacement_glyphs_are_present(self) -> None:
        for ch in "·→…‹›▌●◐◆▶√×！‖■":
            assert in_font(ch), f"{ch} 不在字库，字形表选错了"

    def test_override_with_missing_glyph_is_rejected(self) -> None:
        with pytest.raises(GlyphError, match="U\\+2713"):
            glyph_set("symbol", {"done": "✓"})

    def test_unknown_profile_rejected(self) -> None:
        with pytest.raises(GlyphError, match="未知字形档位"):
            glyph_set("neon")

    def test_cjk_profile_strips_nothing_from_body(self) -> None:
        """`cjk` 档的字形是常用汉字，绝不能被当成状态字形从正文剥掉。"""
        gs = glyph_set("cjk")
        assert all(not ("一" <= g <= "鿿") for g in gs.strippable())


# --------------------------------------------------------------- markdown


class TestStripMarkdown:
    def test_basic_marks(self) -> None:
        out = strip_markdown("# 标题\n**加粗** 与 *斜体* 和 `代码`")
        assert "#" not in out and "*" not in out and "`" not in out
        assert "标题" in out and "加粗" in out

    def test_table_degrades(self) -> None:
        out = strip_markdown("前文\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n后文")
        assert "|" not in out and "表格" in out and "手机" in out

    def test_link_keeps_text(self) -> None:
        assert strip_markdown("[点这里](https://example.com/x/y)") == "点这里"

    def test_bare_url_to_domain(self) -> None:
        out = strip_markdown("详见 https://docs.example.com/a/b?q=1 哦")
        assert "docs.example.com" in out and "/a/b" not in out

    def test_code_block_truncated(self) -> None:
        block = "```python\n" + "\n".join(f"line{i}" for i in range(10)) + "\n```"
        out = strip_markdown(block)
        assert "line0" in out and "line9" not in out and "代码共10行" in out

    def test_unclosed_fence_never_leaks_backticks(self) -> None:
        """修 F7：流式中途只有一个 ``` 时，旧实现会把裸围栏直接送上眼镜。"""
        out = strip_markdown("开始说明\n```python\ndef f():\n    return 1")
        assert "```" not in out and "def f():" in out

    def test_every_streaming_prefix_is_backtick_free(self) -> None:
        full = "解释一下\n```py\nfor i in range(3):\n    print(i)\n```\n就是这样"
        for i in range(1, len(full) + 1):
            assert "```" not in strip_markdown(full[:i]), f"前缀长度 {i} 泄漏了围栏"


# --------------------------------------------------------------- 净化


class TestSanitize:
    def test_control_chars_removed_but_newlines_kept(self) -> None:
        rep = sanitize_report("行一\x00\x07\x1b行二\n行三")
        assert "\x00" not in rep.text and "\x1b" not in rep.text
        assert "\n" in rep.text and rep.removed_control == 3

    def test_bidi_override_removed(self) -> None:
        rep = sanitize_report("转账给 ‮kcatta‬ 这个账户")
        assert "‮" not in rep.text and rep.removed_bidi == 2

    def test_legit_chinese_never_eaten(self) -> None:
        """回归：`转`/`完` 是 cjk 档的状态字形，但它们首先是常用汉字。"""
        assert sanitize_body("转账给这个账户").startswith("转账")
        assert sanitize_body("完成了三件事") == "完成了三件事"

    def test_fake_status_glyph_stripped_from_line_start(self) -> None:
        out = sanitize_body("√ 完成\n正文")
        assert out.startswith("完成") and "√" not in out.split("\n")[0]

    def test_bullet_survives(self) -> None:
        """列表符不能被当成状态字形剥掉。"""
        assert sanitize_body("• 第一条\n• 第二条") == "• 第一条\n• 第二条"

    def test_markdown_list_keeps_its_bullets(self) -> None:
        out = sanitize_body(strip_markdown("- 甲\n- 乙"))
        assert out.count("•") == 2

    def test_out_of_font_chars_dropped_and_reported(self) -> None:
        rep = sanitize_report("完成 ✓ 了")
        assert "✓" not in rep.text
        assert 0x2713 in rep.dropped_codepoints
        assert "U+2713" in rep.summary()

    def test_fullwidth_punctuation_preserved(self) -> None:
        """用 NFC 而非 NFKC：全角括号不能被折成半角，否则中文排版会变形。"""
        assert sanitize_body("（括号）％") == "（括号）％"


# --------------------------------------------------------------- 折行


class TestWrap:
    def test_width_too_small_rejected(self) -> None:
        for bad in (0, -5, 23):
            with pytest.raises(WidthTooSmall):
                wrap_line("hello", bad)

    def test_zero_width_does_not_hang(self) -> None:
        """回归 F8：旧实现 `wrap_line('hello', 0)` 会永远循环，且它跑在事件循环里。"""
        with pytest.raises(WidthTooSmall):
            wrap_line("hello", 0)

    def test_latin_word_not_split_when_it_fits(self) -> None:
        lines = wrap_line("模型名是 OpenClaw，它跑在服务器上，名字不能被切断，再多写一点凑满一行看看", 300)
        assert any("OpenClaw" in ln for ln in lines)

    def test_overlong_word_is_hard_cut(self) -> None:
        lines = wrap_line("supercalifragilisticexpialidocious", 100)
        assert len(lines) > 1
        assert "".join(lines) == "supercalifragilisticexpialidocious"

    def test_no_closing_punct_at_line_start(self) -> None:
        text = "一二三四五六七八九十一二三四五六七八九十一二三四五六七八。后续内容"
        for ln in wrap_line(text, BODY.inner_width)[1:]:
            assert ln[0] not in "。，、；：！？"

    def test_no_opening_punct_at_line_end(self) -> None:
        text = "一二三四五六七八九十一二三四五六七八九十一二三四五六七（括号内容）"
        for ln in wrap_line(text, BODY.inner_width)[:-1]:
            assert ln[-1] not in "（【《「"

    def test_kinsoku_never_reorders_characters(self) -> None:
        """回归：禁则「追出」曾把字符与待排单元的入队顺序弄反，`（括` 变成 `括（`。"""
        out = "".join(wrap_line("这是一段话（括号内容）结束", 120))
        assert out == "这是一段话（括号内容）结束"

    def test_consecutive_punctuation_does_not_starve_lines(self) -> None:
        """回归：连续标点曾让禁则反复拽字，把每行掏成只剩 1 个字符。"""
        lines = wrap_line("测试。。。。。。。。。。测试", 60)
        assert max(len(ln) for ln in lines) >= 3

    def test_paragraph_break_preserved(self) -> None:
        assert "" in wrap_text("第一段\n\n第二段", BODY.inner_width)

    def test_tail_window_short_passthrough(self) -> None:
        assert tail_window("你好", BODY.inner_width, 5) == "你好"

    def test_tail_window_keeps_line_count_and_width(self) -> None:
        """修 F2：加了省略号的首行也必须在宽度内，否则固件会二次折行多出一行。"""
        for width in (BODY.inner_width, 200, 60, 24):
            out = tail_window("很长的转写内容" * 40, width, 3)
            lines = out.split("\n")
            assert out.startswith("…")
            assert len(lines) == 3
            for ln in lines:
                assert text_width(ln) <= width, (width, ln)


class TestKinsoku:
    """标点禁则：**段落内**的性质（显式换行是作者的意图，不受禁则约束）。"""

    _POOL = (
        "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里"
        "abcXYZ019。，、；：！？（）《》「」…—·,.;:!?()[]{}   --"
    )

    def test_no_violation_within_a_paragraph(self) -> None:
        rng = random.Random(31337)
        checked = 0
        for _ in range(800):
            text = "".join(rng.choice(self._POOL) for _ in range(rng.randint(1, 300)))
            assert "\n" not in text
            lines = wrap_line(text, rng.choice(WIDTHS))
            for idx in range(1, len(lines)):
                ln, prev = lines[idx], lines[idx - 1]
                if ln and ln[0] in NO_LINE_START:
                    # 唯一允许的例外：连续标点悬挂 —— 上一行也以禁排标点结尾，
                    # 再往下拽只是把违例平移一格（见 wrap._apply_kinsoku）
                    assert prev[-1:] in NO_LINE_START, (prev, ln)
                if prev and prev[-1] in NO_LINE_END:
                    assert False, f"开放标点挂在行尾：{prev!r} -> {ln!r}"
                checked += 1
        assert checked > 1000, "语料没覆盖到足够多的折行点"

    def test_explicit_newline_is_authoritative(self) -> None:
        """作者写「…（\n」就是要在那里断，禁则不该去动它。"""
        out = wrap_text("第一行以开放括号收尾（\n…第二行以省略号开头", BODY.inner_width)
        assert out[0].endswith("（") and out[1].startswith("…")


# --------------------------------------------------------------- 分页


class TestPaginator:
    def test_single_page(self) -> None:
        p = paginate("短回答。")
        assert p.total == 1 and "短回答" in p.page_text()

    def test_anchor_on_continuation_pages(self) -> None:
        p = paginate("句子甲乙丙丁戊己庚辛。" * 40)
        assert p.total >= 2
        for i in range(1, p.total):
            assert p.pages[i][0].startswith("…")

    def test_anchor_line_fits_in_width(self) -> None:
        """修 F4：锚点行按像素截断，不能自己超宽。"""
        p = paginate("句子甲乙丙丁戊己庚辛。" * 40)
        for page in p.pages:
            for ln in page:
                assert text_width(ln) <= BODY.inner_width

    def test_page_line_count_within_budget(self) -> None:
        p = paginate("内容很长" * 200)
        assert all(len(page) <= BODY.max_lines for page in p.pages)

    def test_idempotent_restream(self) -> None:
        p = Paginator(box=BODY)
        p.set_text("流式内容一二三")
        first = p.page_text()
        p.set_text("流式内容一二三")
        assert p.page_text() == first

    def test_streaming_never_moves_the_reader(self) -> None:
        """流式重排**不许**动读者所在的页。

        这一条曾经是反的（`follow` 跟随末页）。8 行的屏幕上那等于读不完：
        回答一超过一页，读者正读着第一页就被甩到末尾。真机复现见下一个用例。
        """
        p = Paginator(box=BODY)
        # 语料必须真的涨到多页 —— 用 stream_prefixes() 的短语料，这条断言是空的
        # （每个前缀都只有一页，cur 恒等于 0，跟随与否都测不出来）。
        full = "句子甲乙丙丁戊己庚辛，再写长一点凑够好几页。" * 30
        grew = False
        for n in range(1, len(full), 37):
            p.set_text(full[:n])
            assert p.cur == 0, f"流式到第 {n} 字时把读者从首页甩走了（现在 {p.cur + 1}/{p.total}）"
            grew = grew or p.total > 1
        assert grew, "语料没跨过页边界，这条断言测不到东西"

    def test_crossing_the_page_boundary_does_not_yank_the_reader(self) -> None:
        """回归：真机上抓到的原样 —— 最后一个 token 让正文从 1 页涨到 2 页，
        屏幕当场跳到 `2/2`，只剩半句结尾。读者必须留在第 1 页。"""
        p = Paginator(box=BODY)
        p.set_text("句子甲乙丙丁戊己庚辛。" * 8)
        assert p.total == 1 and p.cur == 0
        p.set_text("句子甲乙丙丁戊己庚辛。" * 40)
        assert p.total >= 2, "语料不够长，这个回归测试测不到东西"
        assert p.cur == 0, "越过页边界时把读者甩到了末页"
        assert p.footer().startswith("1/") or " 1/" in p.footer()

    def test_turning_back_pins_the_page(self) -> None:
        p = paginate("句子甲乙丙丁戊己庚辛。" * 40)
        assert p.total >= 2
        assert p.turn(p.total) is True and p.at_last     # 先跟到最新一页
        assert p.turn(-1) is True
        assert p.at_last is False
        pinned = p.cur
        p.set_text("句子甲乙丙丁戊己庚辛。" * 60)   # 继续流式：不应把用户拽走
        assert p.cur == pinned and p.at_last is False

    def test_turn_at_boundary_is_a_noop(self) -> None:
        """到边界返回 False，调用方据此不发冗余帧。"""
        p = paginate("短回答。")
        assert p.turn(1) is False and p.turn(-1) is False

    def test_cur_clamped_when_text_shrinks(self) -> None:
        """回归 F7：重排后页数变少时 cur 必须被夹紧，否则页脚会显示「4/2」。"""
        p = paginate("句子甲乙丙丁戊己庚辛。" * 60)
        p.turn(p.total)          # 先翻到末页，才可能出现「cur 越界」
        assert p.cur > 0
        far = p.cur
        p.set_text("短。")
        assert p.total == 1 and p.cur == 0 and far > 0

    def test_footer_and_body_always_agree(self) -> None:
        """页脚里的页码必须与 page_text() 取的是同一页 —— 结构上不可能不一致。"""
        p = paginate("句子甲乙丙丁戊己庚辛。" * 40)
        for i in range(p.total):
            while p.cur > i:
                p.turn(-1)
            while p.cur < i:
                p.turn(1)
            assert f"{i + 1}/{p.total}" in p.footer()
            assert p.page_text() == "\n".join(p.pages[i])

    def test_footer_arrows_reflect_available_directions(self) -> None:
        p = paginate("句子甲乙丙丁戊己庚辛。" * 40)
        assert p.total >= 3, "语料需要至少 3 页才测得出中间页"
        gs = glyph_set()
        while p.turn(-1):
            pass
        assert gs["page_prev"] not in p.footer() and gs["page_next"] in p.footer()
        p.turn(1)
        assert gs["page_prev"] in p.footer() and gs["page_next"] in p.footer()
        while p.turn(1):
            pass
        assert gs["page_prev"] in p.footer() and gs["page_next"] not in p.footer()

    def test_single_page_has_no_footer(self) -> None:
        assert paginate("短回答。").footer() == ""

    def test_empty_text(self) -> None:
        p = paginate("")
        assert p.total == 1 and p.page_text() == ""


# --------------------------------------------------------------- 不变量


class TestInvariants:
    """在随机与 golden 语料上断言六条硬性性质，三档宽度全覆盖。"""

    @staticmethod
    def _check(text: str, width: int) -> None:
        box = _box(width)
        p = Paginator(box=box)
        p.set_text(text)

        for page in p.pages:
            # ① 每页行数不超过容器能显示的行数
            assert len(page) <= box.max_lines, (width, len(page), box.max_lines)
            for ln in page:
                # ② 每行不超宽（超了固件会二次折行，分页就崩了）
                assert text_width(ln) <= box.inner_width, (width, text_width(ln), ln)
                # ③ 只含 G2 画得出的字符
                assert not missing_codepoints(ln), (width, missing_codepoints(ln), ln)
                # ④ 不含裸 markdown 标记
                assert "```" not in ln and "|" not in ln
        # ⑤ 页码始终有效
        assert 0 <= p.cur < p.total
        # 注：标点禁则是**段落内**的性质，作者写的显式换行不受它约束，
        #     所以它由 TestKinsoku 直接在 wrap_line 上验证，不放进页级不变量。

    @pytest.mark.parametrize("width", WIDTHS)
    @pytest.mark.parametrize("name", sorted(CASES))
    def test_corpus_invariants(self, name: str, width: int) -> None:
        self._check(CASES[name], width)

    @pytest.mark.parametrize("width", WIDTHS)
    def test_streaming_prefix_invariants(self, width: int) -> None:
        for prefix in stream_prefixes():
            self._check(prefix, width)

    def test_random_invariants(self) -> None:
        rng = random.Random(4242)
        pool = (
            "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里"
            "abcdefghijklmnopqrstuvwxyzABCXYZ0123456789"
            "。，、；：！？（）《》「」…—·,.;:!?()[]{}   --\n"
        )
        for _ in range(600):
            text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 400)))
            self._check(text, rng.choice(WIDTHS))

    def test_no_characters_lost_or_duplicated(self) -> None:
        """折行不丢字不重字（忽略折行处被吃掉的空白）。"""
        rng = random.Random(99)
        pool = "的一是了我不人在他有这个上们来到时大地abcXYZ019。，、；：！？（）  "
        for _ in range(600):
            text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 300)))
            width = rng.choice(WIDTHS)
            joined = "".join(wrap_line(text, width))
            assert re.sub(r"\s+", "", joined) == re.sub(r"\s+", "", text)


# --------------------------------------------------------------- golden 快照


class TestGolden:
    """回归检测：排版结果变了就必须显式重新生成，让 diff 看得见。"""

    @pytest.fixture(scope="class")
    @classmethod
    def golden(cls) -> dict:
        assert _GOLDEN.exists(), "缺 golden.json：python -m tests.data.formatting.corpus --regen"
        return json.loads(_GOLDEN.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("width", WIDTHS)
    def test_matches_snapshot(self, golden: dict, width: int) -> None:
        expected = golden[str(width)]
        assert sorted(expected) == sorted(CASES), "语料与快照不同步，请 --regen"
        for name, text in CASES.items():
            p = Paginator(box=_box(width))
            p.set_text(text)
            assert p.pages == expected[name], (
                f"语料 {name}@{width}px 的排版结果变了。确认是预期改动后重新生成：\n"
                f"  python -m tests.data.formatting.corpus --regen"
            )
