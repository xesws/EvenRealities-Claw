from lens_gateway.textkit import (
    Paginator, strip_markdown, tail_window, wrap_line, wrap_text)


def width_ok(line: str, max_cjk: int) -> bool:
    from lens_gateway.textkit import char_width
    return sum(char_width(c) for c in line) <= max_cjk * 2


class TestStripMarkdown:
    def test_basic_marks(self):
        out = strip_markdown("# 标题\n**加粗** 与 *斜体* 和 `代码`")
        assert "#" not in out and "*" not in out and "`" not in out
        assert "标题" in out and "加粗" in out

    def test_table_degrades(self):
        md = "前文\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n后文"
        out = strip_markdown(md)
        assert "|" not in out
        assert "表格" in out and "手机" in out

    def test_link_keeps_text(self):
        assert strip_markdown("[点这里](https://example.com/x/y)") == "点这里"

    def test_bare_url_to_domain(self):
        out = strip_markdown("详见 https://docs.example.com/a/b?q=1 哦")
        assert "docs.example.com" in out and "/a/b" not in out

    def test_code_block_truncated(self):
        block = "```python\n" + "\n".join(f"line{i}" for i in range(10)) + "\n```"
        out = strip_markdown(block)
        assert "line0" in out and "line9" not in out and "代码共10行" in out


class TestWrap:
    def test_cjk_budget(self):
        text = "这是一段用于测试折行的中文文本，总共应当被折成多行显示。"
        for line in wrap_line(text, 17):
            assert width_ok(line, 17)

    def test_no_line_start_punct(self):
        # 构造让标点恰好落在行首的文本
        text = "一二三四五六七八九十一二三四五六七。后续内容"
        lines = wrap_line(text, 17)
        for line in lines:
            assert line[0] not in "。，、；：！？", lines

    def test_no_line_end_open_punct(self):
        text = "一二三四五六七八九十一二三四五六（括号内容）"
        lines = wrap_line(text, 17)
        for line in lines[:-1]:
            assert line[-1] not in "（【《「", lines

    def test_latin_word_not_split(self):
        text = "模型名是" + "OpenClaw" + "，它跑在服务器上，名字不能被切断测试一下"
        lines = wrap_line(text, 17)
        joined = "\n".join(lines)
        assert "OpenClaw" in joined.replace("\n", "") and any("OpenClaw" in l for l in lines)

    def test_halfwidth_half_cost(self):
        # 34 个半角字符应恰好占满 17 汉字预算的一行
        lines = wrap_line("a" * 34, 17)
        assert lines == ["a" * 34]

    def test_multiline_paragraphs(self):
        out = wrap_text("第一段\n\n第二段", 17)
        assert "" in out  # 空行分段保留


class TestPaginator:
    def test_single_page(self):
        p = Paginator(lines_per_page=5, wrap_chars=17)
        p.set_text("短回答。")
        assert p.total == 1
        assert "短回答" in p.page_text(0)

    def test_anchor_on_following_pages(self):
        p = Paginator(lines_per_page=5, wrap_chars=17, anchor_chars=10)
        p.set_text("句子甲乙丙丁。" * 40)
        assert p.total >= 2
        page2 = p.page_text(1)
        assert page2.startswith("…")

    def test_page_lines_within_budget(self):
        p = Paginator(lines_per_page=5, wrap_chars=17)
        p.set_text("内容很长" * 100)
        for i in range(p.total):
            assert len(p.page_text(i).split("\n")) <= 5

    def test_idempotent_restream(self):
        p = Paginator(lines_per_page=5, wrap_chars=17)
        p.set_text("流式内容一二三")
        first = p.page_text(0)
        p.set_text("流式内容一二三")
        assert p.page_text(0) == first


class TestTailWindow:
    def test_short_passthrough(self):
        assert tail_window("你好", 17, 5) == "你好"

    def test_long_keeps_tail_with_ellipsis(self):
        text = "很长的转写内容" * 30
        out = tail_window(text, 17, 5)
        assert out.startswith("…")
        assert len(out.split("\n")) == 5
