"""字形度量的**外部 oracle** 测试：Python 移植 vs 官方 `@evenrealities/pretext`。

这是整个排版引擎里唯一能回答「对不对」的测试，因为对照物与被测实现相互独立：
一边是 Even Realities 官方发布的 JS 库（内嵌固件字体度量），
一边是我们在 `lens_gateway/formatting/metrics.py` 里的 Python 移植。
两边一致，才能说服务器算出来的折行位置就是眼镜上真实的折行位置。

需要 Node 与已安装的 pretext（`plugin/node_modules`）；缺任一则整个模块 skip，
并在 CI 里以 `-W error::pytest.PytestUnhandledThreadExceptionWarning` 之外的方式显式报告。
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from lens_gateway.formatting import measure_wrap, text_width
from lens_gateway.formatting.metrics import metrics

_REPO = Path(__file__).resolve().parents[2]
_PLUGIN = _REPO / "plugin"
_ORACLE = _PLUGIN / "tools" / "pretext_oracle.mjs"
_PRETEXT = _PLUGIN / "node_modules" / "@evenrealities" / "pretext"

pytestmark = pytest.mark.skipif(
    not (shutil.which("node") and _ORACLE.exists() and _PRETEXT.exists()),
    reason="需要 node 与 plugin/node_modules/@evenrealities/pretext（先在 plugin/ 跑 npm install）",
)

_CJK = (
    "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家"
    "可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情"
)
_LATIN = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_PUNCT = "。，、；：！？（）《》「」…—·,.;:!?()[]{}\"'-_/@#$%&*+=<>"
_SYMBOL = "→√×★●○■□▲▼♠♣♥♪━─│╭╮╯╰█▇▆▅▄▃▂▁▌‹›"

_WIDTHS = (576, 552, 480, 400, 360, 288, 200, 120, 60, 30)


def _run_oracle(payload: dict) -> dict:
    proc = subprocess.run(
        ["node", str(_ORACLE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(_PLUGIN),
        timeout=120,
    )
    assert proc.returncode == 0, f"pretext oracle 失败：{proc.stderr[:2000]}"
    return json.loads(proc.stdout)


def _random_text(rng: random.Random, n: int) -> str:
    pools = (_CJK, _LATIN, _PUNCT, _SYMBOL, " ", "\n", "-")
    weights = (40, 30, 12, 4, 12, 2, 2)
    return "".join(rng.choice(rng.choices(pools, weights)[0]) for _ in range(n))


@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    rng = random.Random(20260831)
    cases: list[dict] = []
    fixed = [
        "", "a", "你", " ", "\n", "hello world", "你好，世界！",
        "The quick brown fox jumps over the lazy dog",
        "工 ● 聆听 0:07", "！ 连接丢失·重连中", "‹ 2/3 ›",
        "supercalifragilisticexpialidocious" * 3,
        "中英mixed混排test文本123abc测试",
        "AV Ta To Wa Ye P, r.",          # kerning 敏感字对
        "a\nb\n\nc", "  leading spaces", "trailing   ", "-hyphen-break-test-",
        "😀🎉", "𠮷野家", "école", "a‍b",
    ]
    for t in fixed:
        for w in _WIDTHS:
            cases.append({"text": t, "maxWidth": w})
    for _ in range(600):
        cases.append({
            "text": _random_text(rng, rng.randint(1, 200)),
            "maxWidth": rng.choice(_WIDTHS),
        })
    return cases


@pytest.fixture(scope="module")
def codepoints() -> list[int]:
    cps = set(range(32, 0x3400))
    cps |= set(range(0x4E00, 0xA000, 7))
    cps |= set(range(0xF900, 0xFB00))
    cps |= set(range(0x1F300, 0x1FB00, 13))
    cps |= {ord(c) for c in _CJK + _LATIN + _PUNCT + _SYMBOL}
    return sorted(cps)


@pytest.fixture(scope="module")
def oracle(corpus: list[dict], codepoints: list[int]) -> dict:
    return _run_oracle({"cases": corpus, "codepoints": codepoints})


def test_pretext_version_matches_generated_table(oracle: dict) -> None:
    """生成的度量表必须来自当前安装的 pretext 版本，否则数据可能已过时。"""
    assert metrics().source.get("version") == oracle["pretext"], (
        f"度量表来自 pretext {metrics().source.get('version')}，"
        f"但当前安装的是 {oracle['pretext']}；请重跑 plugin/tools/extract_metrics.mjs"
    )


def test_advance_width_matches_every_codepoint(oracle: dict, codepoints: list[int]) -> None:
    """逐码点的原始 advance 必须与官方完全一致。"""
    m = metrics()
    bad = [
        (cp, m.adv_w(cp), expect)
        for cp, expect in zip(codepoints, oracle["advs"])
        if m.adv_w(cp) != expect
    ]
    assert not bad, f"{len(bad)} 个码点度量不符，前 5 个：{bad[:5]}"


def test_single_line_width_matches(oracle: dict, corpus: list[dict]) -> None:
    """单行像素宽度（含 kerning）必须与官方完全一致。"""
    bad = [
        (c["text"][:30], text_width(c["text"]), ref["textWidth"])
        for c, ref in zip(corpus, oracle["results"])
        if text_width(c["text"]) != ref["textWidth"]
    ]
    assert not bad, f"{len(bad)} 条宽度不符，前 3 条：{bad[:3]}"


def test_wrap_matches_official_line_by_line(oracle: dict, corpus: list[dict]) -> None:
    """折行结果（行数、每行宽度、总高度）必须与官方完全一致。"""
    bad: list[tuple] = []
    for c, ref in zip(corpus, oracle["results"]):
        lc, h, lw = measure_wrap(c["text"], c["maxWidth"])
        if (lc, h, lw) != (ref["lineCount"], ref["height"], ref["lineWidths"]):
            bad.append((c["maxWidth"], c["text"][:30], (lc, lw[:6]), (ref["lineCount"], ref["lineWidths"][:6])))
    assert not bad, f"{len(bad)}/{len(corpus)} 条折行不符，前 3 条：{bad[:3]}"


def test_line_height_is_27() -> None:
    """G2 的 LVGL 行高固定 27px —— 这是版式计算的基石，直接从官方数据读出来核对。"""
    assert metrics().line_height == 27


def test_generated_table_is_in_sync_with_installed_package() -> None:
    """`extract_metrics.mjs --check` 必须通过，确保入库的数据没有落后于 npm 包。"""
    proc = subprocess.run(
        ["node", "tools/extract_metrics.mjs", "--check"],
        capture_output=True, text=True, cwd=str(_PLUGIN), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
