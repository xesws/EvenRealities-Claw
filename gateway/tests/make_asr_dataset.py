"""生成 ASR 数据集（开发期脚本，产物入库；网关运行时不需要它）。

**为什么要自己造数据集**：M7 之前 e2e 的转写断言是硬编码的「'畅通' 在不在正文里」——
那既不是准确率的度量，也无法回答"换个模型/换台机器之后变好了还是变坏了"。
现在每条素材配一份 ground truth，用 **CER（字错误率）** 卡阈值。

中文用 CER 而不是 WER：中文没有天然词边界，分词器一换 WER 就变，
而 CER 只依赖字符，跨环境可比。

音色刻意用了三个不同的（女声/男声/大陆/台湾），避免"只在一个音色上调好了"。

用法（需要联网，产物已入库，平时不用重跑）：
    .venv/bin/pip install edge-tts
    PYTHONPATH=. .venv/bin/python tests/make_asr_dataset.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data" / "asr"

# text: 真值；voice: edge-tts 音色；tag: 这条素材是用来考什么的
CORPUS = [
    # 域内术语 —— hotwords 生效与否看这几条
    ("眼镜链路现在通不通", "zh-CN-XiaoxiaoNeural", "domain"),
    ("帮我问一下工部，网关还活着吗", "zh-CN-YunxiNeural", "domain"),
    # 日常问答 —— 会被 skill 路由到 daily，触发 now 工具
    ("现在几点了", "zh-CN-XiaoxiaoNeural", "daily"),
    ("今天是星期几", "zh-CN-YunxiNeural", "daily"),
    # 一般问答 —— 走 ask，无工具
    ("用一句话说明什么是光的折射", "zh-CN-XiaoxiaoNeural", "ask"),
    ("北京到上海大概有多远", "zh-TW-HsiaoChenNeural", "ask"),
    # 数字与英文混排 —— 转写最容易出错的地方
    ("把音量调到百分之六十", "zh-CN-YunxiNeural", "mixed"),
    ("OpenClaw 的版本号是多少", "zh-CN-XiaoxiaoNeural", "mixed"),
    # 短句 —— 短音频是 whisper 的弱项（容易补字）
    ("停", "zh-CN-XiaoxiaoNeural", "short"),
    ("再说一遍", "zh-CN-YunxiNeural", "short"),
]


async def main() -> None:
    import edge_tts

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, (text, voice, tag) in enumerate(CORPUS, 1):
        name = f"{i:02d}_{tag}.mp3"
        await edge_tts.Communicate(text, voice).save(str(OUT / name))
        manifest.append({"audio": name, "text": text, "voice": voice, "tag": tag})
        print(f"  ✓ {name}  {text}")
    (OUT / "manifest.json").write_text(
        json.dumps({"corpus": manifest,
                    "note": "ground truth 为 TTS 的输入文本；CER 阈值见 tests/test_asr_quality.py"},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(manifest)} 条 → {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
