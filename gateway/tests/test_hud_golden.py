"""HUD 帧序列 golden + 每帧硬性不变量（M7 / T-HUD）。

分工与排版引擎那套一致：
- **golden 快照**只做回归检测 —— 它是我们自己的输出，不能自证正确。
  任何改动只要动了用户看到的画面，就必须显式 --regen，让 diff 里有人看见。
- **不变量**才是"对不对"：每一帧都必须是这块 576×288 屏幕真的画得出来的东西。
  这部分不依赖快照，新场景加进来自动被覆盖。

语料与驱动在 `tests/data/hud/scenes.py`（真 VoicePipeline + 真 HUD + 真排版，
只有 ASR 转写和 agent 回答是脚本化数据源）。
"""
from __future__ import annotations

import json

import pytest

from lens_gateway.formatting import DEFAULT_LAYOUT, text_width
from lens_gateway.formatting.metrics import missing_codepoints
from tests.data.hud import scenes

#: 固件行高，来自官方 text-heavy 模板的 `const LINE_HEIGHT = 27`
LINE_HEIGHT = 27


@pytest.fixture(scope="module")
def golden() -> dict:
    assert scenes.GOLDEN.exists(), (
        "缺 golden.json：PYTHONPATH=. .venv/bin/python -m tests.data.hud.scenes --regen")
    return json.loads(scenes.GOLDEN.read_text(encoding="utf-8"))


class TestGolden:
    def test_corpus_and_snapshot_are_in_sync(self, golden: dict) -> None:
        assert sorted(golden) == sorted(scenes.SCENES), "场景与快照不同步，请 --regen"

    @pytest.mark.parametrize("name", sorted(scenes.SCENES))
    async def test_frames_match_snapshot(self, golden: dict, name: str) -> None:
        actual = await scenes.run_scene(name)
        expected = golden[name]
        assert actual == expected, (
            f"场景「{name}」（{scenes.SCENES[name]}）的帧序列变了。\n"
            f"确认是预期改动后重新生成：\n"
            f"  PYTHONPATH=. .venv/bin/python -m tests.data.hud.scenes --regen"
        )


def all_frames(golden: dict):
    for name, frames in golden.items():
        for f in frames:
            yield name, f


class TestFrameInvariants:
    """每一帧都得是真机画得出来的。这些断言不看快照，加新场景自动生效。"""

    def test_every_glyph_is_in_the_g2_font(self, golden: dict) -> None:
        """字库外的字符在真机上**什么都不画**（不是豆腐块），是最难发现的一类错。"""
        offenders: dict[str, set[str]] = {}
        for name, f in all_frames(golden):
            for slot, text in f["containers"].items():
                for cp in missing_codepoints(text):
                    offenders.setdefault(f"{name}/{slot}", set()).add(chr(cp))
        assert not offenders, f"这些字形在 G2 上会被静默丢弃：{offenders}"

    @pytest.mark.parametrize("slot,box", [
        ("status", DEFAULT_LAYOUT.status),
        ("body", DEFAULT_LAYOUT.body),
        ("foot", DEFAULT_LAYOUT.foot),
    ])
    def test_no_line_overflows_its_container(self, golden: dict, slot: str, box) -> None:
        for name, f in all_frames(golden):
            for line in f["containers"][slot].split("\n"):
                w = text_width(line)
                assert w <= box.inner_width, (
                    f"{name} 的 {slot} 行宽 {w}px 超出容器 {box.inner_width}px：{line!r}")

    def test_no_container_exceeds_its_line_budget(self, golden: dict) -> None:
        """行数由容器像素高除以固定 27px 行高决定，多出来的行在真机上看不见。"""
        for slot, box in (("status", DEFAULT_LAYOUT.status),
                          ("body", DEFAULT_LAYOUT.body),
                          ("foot", DEFAULT_LAYOUT.foot)):
            assert box.max_lines == max(1, box.height // LINE_HEIGHT)
            for name, f in all_frames(golden):
                lines = f["containers"][slot].split("\n")
                assert len(lines) <= box.max_lines, (
                    f"{name} 的 {slot} 有 {len(lines)} 行，超过 {box.max_lines} 行上限")

    def test_no_raw_markdown_reaches_the_screen(self, golden: dict) -> None:
        """眼镜不渲染 markdown，漏上去就是一堆符号。"""
        for name, f in all_frames(golden):
            body = f["containers"]["body"]
            for mark in ("```", "**", "##"):
                assert mark not in body, f"{name} 的正文里漏了 markdown 标记 {mark!r}"

    def test_seq_is_strictly_increasing(self, golden: dict) -> None:
        """seq 是客户端丢弃过期帧的唯一依据，出现空洞或回退就会丢画面。"""
        for name, frames in golden.items():
            seqs = [f["seq"] for f in frames]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"{name}: {seqs}"

    def test_page_meta_is_consistent_with_the_footer(self, golden: dict) -> None:
        """页脚和 meta.page 必须同源 —— 修 F7 时立的规矩（曾出现过「4/2」）。"""
        for name, f in all_frames(golden):
            page = f["meta"]["page"]
            assert 1 <= page["cur"] <= page["total"], f"{name}: {page}"
            foot = f["containers"]["foot"]
            if page["total"] > 1:
                assert f'{page["cur"]}/{page["total"]}' in foot, f"{name}: 页脚 {foot!r} 对不上 {page}"

    def test_every_frame_carries_the_agent_identity(self, golden: dict) -> None:
        """W6：屏幕要能自己告状。非生产 agent 的徽记必须带「?」，且不能只标一半。"""
        for name, frames in golden.items():
            untrusted = name == "untrusted_agent"
            badged = [f["containers"]["status"] for f in frames
                      if f["state"] not in ("S0",)]
            assert badged, f"{name} 没有任何带状态条的帧"
            for status in badged:
                assert status.startswith("工?" if untrusted else "工 "), (
                    f"{name} 的状态条徽记不对：{status!r}")


class TestScenesCoverTheStateMachine:
    """快照只有在真的走过这些状态时才有价值。"""

    def test_all_reachable_states_appear(self, golden: dict) -> None:
        seen = {f["state"] for _, f in all_frames(golden)}
        # S1（配对）与 S9 不由语音链路产生，在 test_device 里单独覆盖
        assert {"S0", "S2", "S3", "S4", "S5", "S6", "S7", "S8"} <= seen, f"缺状态：{seen}"

    def test_error_paths_are_covered(self, golden: dict) -> None:
        bodies = " ".join(f["containers"]["body"] for _, f in all_frames(golden))
        for phrase in ("没听清", "上一条还在跑", "无法连接 agent", "按住说话可重试"):
            assert phrase in bodies, f"没有场景覆盖到「{phrase}」"
