"""ASR 管线：faster-whisper，PTT（按住说话）模式。

双模型策略（红队 R4/R5）：
- partial：小模型（base/int8），仅解码最近 N 秒尾部，聆听态 ~700ms 一跳，只供 HUD 显示；
- final：大一档模型（small/int8），松手后整段重解码，路由与发送只认 final。
local-agreement：连续两次 partial 的公共前缀视为“稳定”，HUD 稳定区不回改。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import numpy as np

from .config import AsrConfig

log = logging.getLogger(__name__)


@dataclass
class FinalResult:
    text: str
    avg_logprob: float  # 置信代理：低于阈值 → 确认态延长倒计时


def _common_prefix(a: str, b: str) -> str:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


class AsrEngine:
    """惰性加载两个 whisper 模型；解码在线程池执行避免阻塞事件循环。"""

    def __init__(self, cfg: AsrConfig):
        self.cfg = cfg
        self._partial_model = None
        self._final_model = None
        self._lock = asyncio.Lock()  # 同一时刻只跑一个解码，防止 CPU 互踩
        self.ready = False  # warmup 完成标志（ARM 上首次解码有 20-35s 的进程级初始化）

    def _load(self):
        from faster_whisper import WhisperModel

        if self._partial_model is None:
            log.info("loading partial model %s", self.cfg.partial_model)
            self._partial_model = WhisperModel(
                self.cfg.partial_model, device="cpu",
                compute_type=self.cfg.compute_type, cpu_threads=self.cfg.cpu_threads)
        if self._final_model is None:
            if self.cfg.final_model == self.cfg.partial_model:
                self._final_model = self._partial_model
            else:
                log.info("loading final model %s", self.cfg.final_model)
                self._final_model = WhisperModel(
                    self.cfg.final_model, device="cpu",
                    compute_type=self.cfg.compute_type, cpu_threads=self.cfg.cpu_threads)

    async def warmup(self) -> None:
        # 必须与 partial/final 共用同一把锁：两个 ctranslate2 实例并发解码
        # （各自 cpu_threads 个 OMP 线程）在 4 核 ARM 上会互锁，全局严格串行。
        loop = asyncio.get_running_loop()
        async with self._lock:
            await loop.run_in_executor(None, self._load)
            # 用 0.5s 静音各跑一遍，吃掉首调延迟
            silent = np.zeros(8000, dtype=np.float32)
            await loop.run_in_executor(None, self._decode, self._partial_model, silent, 1)
            if self._final_model is not self._partial_model:
                await loop.run_in_executor(None, self._decode, self._final_model, silent, 1)
        self.ready = True
        log.info("asr warmup done")

    @staticmethod
    def pcm_to_float(pcm: bytes) -> np.ndarray:
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    def _decode(self, model, audio: np.ndarray, beam: int) -> tuple[str, float]:
        segments, _info = model.transcribe(
            audio,
            language=self.cfg.language,
            beam_size=beam,
            initial_prompt=self.cfg.hotwords,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        texts, logprobs = [], []
        for seg in segments:
            texts.append(seg.text)
            logprobs.append(seg.avg_logprob)
        text = "".join(texts).strip()
        avg = float(np.mean(logprobs)) if logprobs else -2.0
        return text, avg

    async def partial(self, pcm: bytes) -> str:
        """解码最近 partial_tail_seconds 的音频，返回 partial 文本。"""
        loop = asyncio.get_running_loop()
        async with self._lock:
            await loop.run_in_executor(None, self._load)
            tail = int(self.cfg.partial_tail_seconds * 16000) * 2
            audio = self.pcm_to_float(pcm[-tail:])
            if len(audio) < 1600:  # <0.1s 不解
                return ""
            text, _ = await loop.run_in_executor(None, self._decode, self._partial_model, audio, 1)
            return text

    async def final(self, pcm: bytes) -> FinalResult:
        loop = asyncio.get_running_loop()
        async with self._lock:
            await loop.run_in_executor(None, self._load)
            audio = self.pcm_to_float(pcm)
            if len(audio) < 1600:
                return FinalResult(text="", avg_logprob=-2.0)
            text, avg = await loop.run_in_executor(None, self._decode, self._final_model, audio, 2)
            return FinalResult(text=text, avg_logprob=avg)


class StablePrefixTracker:
    """local-agreement：连续两次 partial 的公共前缀为稳定文本。"""

    def __init__(self):
        self._prev = ""
        self.stable = ""

    def update(self, partial_text: str) -> tuple[str, str]:
        """返回 (稳定前缀, 未稳定尾段)。稳定前缀只增不减。"""
        agreed = _common_prefix(self._prev, partial_text)
        if len(agreed) > len(self.stable):
            self.stable = agreed
        self._prev = partial_text
        tail = partial_text[len(self.stable):] if partial_text.startswith(self.stable) else partial_text
        return self.stable, tail

    def reset(self) -> None:
        self._prev = ""
        self.stable = ""
