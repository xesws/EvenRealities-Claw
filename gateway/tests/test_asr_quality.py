"""ASR 质量：用 CER 阈值取代硬编码的关键词断言。

**这个文件存在的理由**：M7 之前，e2e 对转写的全部要求是「'畅通' 这两个字在不在
正文里」。那不是准确率的度量 —— 它既容忍"畅通"前后全是错字，也无法回答
"换了模型/换台机器之后是变好了还是变坏了"。

用 **CER（字错误率）** 而不是 WER：中文没有天然词边界，换个分词器 WER 就变，
而 CER 只依赖字符，跨环境可比、可回归。

阈值不是拍脑袋定的，是**先测出基线再往上留余量**（见每个断言上的注释）。
阈值定得太紧会让 CI 天天红，太松等于没测 —— 所以按"当前基线 + 一倍余量"取整。

标记为 slow：要真的加载 whisper 并解码 10 条音频。CI 里单独一个 job 跑。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pytest

from lens_gateway.asr import prompt_echo
from lens_gateway.config import AsrConfig

DATA = Path(__file__).resolve().parent / "data" / "asr"
MANIFEST = DATA / "manifest.json"

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL = {"十": 10, "百": 100, "千": 1000}
_BIG = {"万": 10_000, "亿": 100_000_000}
_NUM_RUN = re.compile("[" + "".join(_DIGITS) + "".join(_SMALL) + "".join(_BIG) + "]+")


def _cn2int(run: str) -> int:
    """中文数字串 → 整数。

    中文有两种读法，必须分开处理，否则年份会被读错：
    - **位值读法**（逐字念）：``二零二六`` = 2026、``一二三`` = 123。特征是**不含单位字**。
    - **结构读法**：``三十一`` = 31、``一万两千`` = 12000。含 十/百/千/万/亿。

    只按结构读法处理的话，``二零二六`` 会被算成 6（每个数字覆盖前一个）。
    """
    if not any(ch in _SMALL or ch in _BIG for ch in run):
        return int("".join(str(_DIGITS[ch]) for ch in run))
    total = section = number = 0
    for ch in run:
        if ch in _DIGITS:
            number = _DIGITS[ch]
        elif ch in _SMALL:
            unit = _SMALL[ch]
            section += (number or (1 if unit == 10 else 0)) * unit
            number = 0
        elif ch in _BIG:
            total += (section + number) * _BIG[ch]
            section = number = 0
    return total + section + number


def normalize_numbers(text: str) -> str:
    """把中文数字统一成阿拉伯数字，「百分之X」统一成「X%」。

    **为什么必须做**：模型把「把音量调到百分之六十」转成「把音量调到60%」，
    语义完全相同，但按字符比对是 5 个字错 / 10 个字 = CER 0.50 —— 这是**度量的错**，
    不是模型的错。转写的下游是眼镜屏幕和 LLM，两者对这两种写法没有任何偏好。

    两侧字符串都过同一遍归一化，所以它只会抹掉写法差异，不会掩盖真实错字。
    """
    text = re.sub(r"百分之([零〇一二三四五六七八九十百千万亿]+)",
                  lambda m: f"{_cn2int(m.group(1))}%", text)
    return _NUM_RUN.sub(lambda m: str(_cn2int(m.group(0))), text)

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------- CER


def normalize(text: str) -> str:
    """比对前的归一化。只抹掉**与识别质量无关**的差异，绝不抹掉错字。

    - NFKC：全角/半角统一（"６" 与 "6" 不该算错）
    - 去标点与空白：TTS 输入里的逗号 whisper 不一定输出，这不是识别错误
    - 数字写法统一（见 `normalize_numbers`）：「百分之六十」与「60%」下游等价
    - 繁转简**不做**：模型输出繁体是真实差异，掩盖它等于自欺
    """
    text = normalize_numbers(unicodedata.normalize("NFKC", text))
    return re.sub(r"[\s,.，。、？?！!：:；;「」『』\"'（）()]", "", text).lower()


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离。逐行滚动数组，O(min(|a|,|b|)) 空间。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))                    # prev:(|b|+1,)
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)                      # cur:(|b|+1,)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1,                 # 删
                         cur[j - 1] + 1,              # 插
                         prev[j - 1] + (ca != cb))    # 替
        prev = cur
    return prev[-1]


def cer(hypothesis: str, reference: str) -> float:
    """字错误率 = 编辑距离 / 真值长度。真值为空时定义为 0。"""
    ref, hyp = normalize(reference), normalize(hypothesis)
    return 0.0 if not ref else edit_distance(hyp, ref) / len(ref)


class TestCerMetric:
    """先把度量本身测对 —— 一个算错的 CER 比没有 CER 更糟。"""

    def test_identical_is_zero(self):
        assert cer("眼镜链路现在通不通", "眼镜链路现在通不通") == 0.0

    def test_one_wrong_char_in_ten(self):
        assert cer("眼睛链路现在通不通", "眼镜链路现在通不通") == pytest.approx(1 / 9)

    def test_punctuation_and_width_do_not_count(self):
        assert cer("现在几点了？", "现在几点了") == 0.0
        assert cer("调到６０", "调到60") == 0.0

    def test_traditional_chars_do_count(self):
        """模型吐繁体是真实差异，归一化不该把它抹掉。"""
        assert cer("眼鏡鏈路", "眼镜链路") > 0

    def test_empty_hypothesis_is_total_loss(self):
        assert cer("", "现在几点了") == 1.0

    def test_insertion_is_penalized(self):
        """短音频上 whisper 爱补字，这条要能被度量抓到。"""
        assert cer("停一下", "停") == 2.0

    def test_number_notation_is_not_an_error(self):
        """「百分之六十」与「60%」下游完全等价，算成 5 个错字是度量的错。"""
        assert cer("把音量调到60%", "把音量调到百分之六十") == 0.0
        assert cer("2026年8月31日", "二零二六年八月三十一日") == 0.0

    def test_number_normalization_still_catches_wrong_numbers(self):
        """归一化不能把"听错了数字"也一起抹掉。"""
        assert cer("把音量调到70%", "把音量调到百分之六十") > 0

    def test_cn2int_handles_bare_ten(self):
        assert normalize_numbers("十点") == "10点"
        assert normalize_numbers("二十三") == "23"
        assert normalize_numbers("一万两千") == "12000"


# ---------------------------------------------------------------- 真解码


@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    if not MANIFEST.exists():
        pytest.skip("ASR 数据集未生成，先跑 tests/make_asr_dataset.py")
    return json.loads(MANIFEST.read_text())["corpus"]


@pytest.fixture(scope="module")
def transcripts(corpus) -> list[tuple[dict, str]]:
    """走**生产解码路径**（`AsrEngine.final`），不是裸的 whisper。

    这一点是刻意的：裸调 `model.transcribe` 测出来的数字，用户永远不会经历 ——
    它绕过了热词回声防线、beam 设置、以及将来可能加的任何后处理。
    只有把被测对象定成"用户实际拿到的那个字符串"，这些阈值才有意义。
    """
    import asyncio

    from lens_gateway.asr import AsrEngine
    from faster_whisper.audio import decode_audio
    import numpy as np

    engine = AsrEngine(AsrConfig())

    async def run() -> list[tuple[dict, str]]:
        out = []
        for row in corpus:
            audio = decode_audio(str(DATA / row["audio"]), sampling_rate=16000)
            pcm = (audio * 32768).clip(-32768, 32767).astype(np.int16).tobytes()
            out.append((row, (await engine.final(pcm)).text))
        return out

    return asyncio.run(run())


class TestTranscriptionQuality:
    """**弃权与答错必须分开算。**

    转写为空在产品里表现为「没听清，请再说一次」—— 那是一次诚实的弃权，
    用户重说一遍就好。把它和"听错并把错的当成用户原话发给 agent"记同样的分，
    等于在鼓励系统瞎猜。所以：
    - CER 预算只统计**有输出**的那些（答错要扣分）；
    - 弃权率单独设上限（否则"全部弃权"能拿满分）；
    - 编造（热词回声）是**零容忍**，单独一条断言。
    """

    #: 有输出的 9 条上，实测基线（base / int8 / macOS arm64）平均 CER = **0.0085**
    #: （唯一非零的是 05：「什么是光」→「什么时光」）。阈值取基线的约 6 倍，
    #: 留给跨平台 int8 量化差异；再松就等于没测。
    MEAN_BUDGET = 0.05
    #: 单条最差不得超过这个 —— 平均值会掩盖"某一条彻底听错"。
    WORST_BUDGET = 0.50
    #: 10 条里最多允许 1 条弃权（0.6 秒的单字"停"是 whisper 的已知弱项）。
    MAX_ABSTENTIONS = 1

    @staticmethod
    def _answered(transcripts):
        return [(row, hyp) for row, hyp in transcripts if hyp.strip()]

    def test_mean_cer_within_budget(self, transcripts):
        answered = self._answered(transcripts)
        scores = [cer(hyp, row["text"]) for row, hyp in answered]
        mean = sum(scores) / len(scores)
        detail = "\n".join(f"  {row['audio']}: {c:.2f}  真值={row['text']!r} 识别={hyp!r}"
                           for (row, hyp), c in zip(answered, scores))
        assert mean <= self.MEAN_BUDGET, f"平均 CER {mean:.3f} 超预算\n{detail}"

    def test_no_single_utterance_is_catastrophic(self, transcripts):
        bad = [(row["audio"], row["text"], hyp, round(c, 2))
               for row, hyp in self._answered(transcripts)
               if (c := cer(hyp, row["text"])) > self.WORST_BUDGET]
        assert not bad, f"这些素材识别得太差：{bad}"

    def test_abstention_rate_is_bounded(self, transcripts):
        """没有这条，一个"永远返回空"的实现能在上面两条上拿满分。"""
        silent = [row["audio"] for row, hyp in transcripts if not hyp.strip()]
        assert len(silent) <= self.MAX_ABSTENTIONS, f"弃权太多：{silent}"

    def test_never_fabricates_the_hotword_prompt(self, transcripts):
        """★ 回归：用户说「停」（0.6s），转写曾经返回 ``链路、网关。`` ——
        那是热词表的尾巴，是**系统凭空编造的用户原话**，还会被当成问题发给 agent。
        零容忍，与 CER 无关。"""
        cfg = AsrConfig()
        echoes = [(row["audio"], hyp) for row, hyp in transcripts
                  if prompt_echo(hyp, cfg.hotwords)]
        assert not echoes, f"转写把热词表吐了回来：{echoes}"

    def test_domain_terms_survive_hotwords(self, transcripts):
        """热词表存在的意义就是这几个词。它们错了 = 热词没生效。"""
        for row, hyp in transcripts:
            if row["tag"] != "domain":
                continue
            keep = [w for w in ("眼镜", "链路", "工部", "网关") if w in row["text"]]
            hit = [w for w in keep if w in hyp]
            assert hit, f"{row['audio']}: 域内词 {keep} 一个都没识别出来，识别结果={hyp!r}"

    def test_short_utterance_abstains_instead_of_guessing(self, transcripts):
        """0.6 秒的单字「停」是 whisper 的已知弱项：它转不出来。

        产品上正确的行为是**弃权**（→「没听清，请再说一次」），而不是编一句。
        这条把这个已知限制钉成契约 —— 哪天它开始"猜"出内容了，要么是真的变好了
        （那就更新这条），要么是回声防线被绕过了（那就是回归）。
        """
        row, hyp = next((r, h) for r, h in transcripts if r["audio"] == "09_short.mp3")
        assert not hyp.strip() or cer(hyp, row["text"]) == 0.0, (
            f"既没转对也没弃权，而是编了一句：{hyp!r}")
