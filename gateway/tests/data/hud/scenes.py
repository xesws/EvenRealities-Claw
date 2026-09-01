"""HUD 帧序列 golden 语料（M7 / T-HUD）。

**被测的是真东西**：真的 `DeviceSession` + 真的 `VoicePipeline` + 真的 `HudDevice`
+ 真的排版引擎。只有两个**数据源**是脚本化的 —— ASR 的转写结果和 agent 的回答文本，
它们本来就是外部输入，在这里用固定值是为了让快照可比。状态机、帧构造、折行、
分页、状态条、页脚，一行都没有被替换。

golden 本身**不证明正确性**（它是我们自己的输出），它的作用是**回归检测**：
任何改动只要动了用户看到的画面，就必须显式重新生成，在 diff 里被人看见。
不变量与行为断言在 `test_device.py` / `test_session.py` / `test_voice.py`。

重新生成：``PYTHONPATH=. .venv/bin/python -m tests.data.hud.scenes --regen``（在 gateway/ 目录下）
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden.json"


@dataclass
class ScriptedAsr:
    """脚本化的转写结果。`text=""` 表示"没听清"（含热词回声被拦截的情形）。"""

    text: str
    avg_logprob: float = -0.30

    async def partial(self, pcm: bytes) -> str:
        return self.text

    async def final(self, pcm: bytes):
        from lens_gateway.asr import FinalResult
        return FinalResult(text=self.text, avg_logprob=self.avg_logprob)


class ScriptedAgent:
    """脚本化的 agent 数据源。按给定的事件列表逐条回调，不含任何网络。

    `injects_style` 跟随被模拟的那一档：openclaw 需要网关注入小屏风格，
    自研 lens agent 自带契约。这会改变发出去的 message，但不改变帧内容。
    """

    def __init__(self, events, *, injects_style: bool = False, busy: bool = False,
                 production: bool = True, raises: str | None = None,
                 pause_after: int | None = None) -> None:
        self.injects_style = injects_style
        self._events = events
        self._busy = busy
        self._raises = raises
        #: 发完第 N 条事件后停住，等 `resume` 被设置再继续。
        #: 用来让「打断」真的落在**流式中途**，而不是 final 之后 —— 后者根本
        #: 走不到僵尸 run 过滤那条分支。
        self._pause_after = pause_after
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()
        self.aborted = False
        self.connected = asyncio.Event()
        self.connected.set()
        from lens_gateway.providers import AgentInfo
        self._info = AgentInfo(backend="scripted", name="脚本", endpoint="-",
                               production=production)

    def info(self):
        return self._info

    def session_busy(self, key: str) -> bool:
        return self._busy

    async def chat_send(self, key: str, message: str, on_event) -> None:
        if self._raises:
            raise RuntimeError(self._raises)
        for i, (kind, payload, extra) in enumerate(self._events):
            await on_event(kind, payload, extra)
            if self._pause_after is not None and i == self._pause_after:
                self.paused.set()
                await self.resume.wait()
                if self.aborted:
                    return                  # 被打断：不再发后续事件（含 final）

    async def abort(self, key: str) -> None:
        self.aborted = True
        self.resume.set()

    async def ensure_connected(self) -> None:
        pass

    async def close(self) -> None:
        pass


#: 一段够长的回答，**必须真的分到三页以上** —— 页脚页码、翻页锚点、跟随末页
#: 这些行为只有多页时才有东西可比。第一版语料只有一页，页脚全是空的，
#: 等于翻页场景什么都没测到；这里按每页约 8 行 × 26 字 ≈ 208 字放大到三页。
LONG_ANSWER = (
    "G2 的可寻址画布是每只眼 576×288 像素，左上角为原点，X 向右、Y 向下。"
    "行高固定 27px，所以正文区 216px 正好八行。字体是非等宽的 LVGL 字体，"
    "没有字号控制，也不能右对齐，所谓居中只是拿空格去凑。"
    "文本亮度只有 0 到 4 五级，和边框的 0 到 15 是两套完全不同的刻度。"
    "字库外的字符不会显示豆腐块，而是直接什么都不画，所以生僻符号和 emoji 一律要避开。"
    "每页最多十二个容器，其中文本容器不超过八个，图片容器不超过四个，"
    "并且必须恰好有一个容器把 isEventCapture 置为 1，否则整页建立会失败。"
    "建页只能调用一次 createStartUpPageContainer，之后所有改动都要走 rebuildPageContainer。"
    "麦克风是四麦阵列，单路 16kHz PCM；触控在两侧镜腿都有，还可以外接 R1 戒指。"
)

SHORT_ANSWER = "今天晴，二十六度。"

#: 模型不听话时会吐的东西。眼镜**不渲染 markdown**，漏上去就是一堆符号，
#: 所以排版层有一道强制剥离（第二道防线，AGENT-LAYER §4.1）。
#: 这段语料存在的理由：不喂 markdown 的话，golden 里那条
#: `test_no_raw_markdown_reaches_the_screen` 就是**恒真断言** ——
#: 全绿但什么都没保护。
MARKDOWN_ANSWER = (
    "## 安装步骤\n\n"
    "先跑 `npm install`，再执行 **build**。\n\n"
    "```bash\nnpm run build\n```\n\n"
    "- 第一步：装依赖\n"
    "- 第二步：打包\n\n"
    "详见 [官方文档](https://hub.evenrealities.com/docs)。"
)


#: 每个场景 = (名字, 说明, 驱动函数)。驱动函数收到已 attach 的 session，
#: 返回时所有帧都已经落到 sink 里。
SCENES: dict[str, str] = {
    "happy_path_long": "完整问答，回答长到分页：S2 聆听 → S3 确认 → S4 思考 → S6 流式 → S7 定稿",
    "happy_path_short": "单页回答，不出现页脚页码",
    "with_tool_call": "带工具调用：S4 → S5 工具态 → S6 → S7",
    "paging_after_answer": "定稿后翻页：上一页、下一页、越界不发冗余帧（三种触发源）",
    "misfire_too_short": "误触（<0.5s 音频）直接回待机",
    "asr_heard_nothing": "转写为空（含热词回声被拦截）：S8 未听清",
    "agent_busy": "上一条还在跑：S8 占用",
    "agent_error": "agent 连不上：S8 错误 + 重试提示",
    "agent_reports_error": "agent 主动报错事件",
    "untrusted_agent": "对端不是生产 agent：状态条徽记带「?」（W6）",
    "low_battery": "低电量提示只出现一次，且只在真的发出的帧上",
    "abort_midway": "**流式中途**打断（不是 final 之后）：S7 已打断，画面停在已收到的部分",
    "markdown_leaks_from_model": "模型不听话吐了 markdown：排版层必须剥干净再上屏",
}


# --------------------------------------------------------------- 场景驱动


def make_session(agent: ScriptedAgent, asr: ScriptedAsr, **cfg_over):
    """构造一个**确定性**的会话。

    两处旋钮只为可比性而调，不改变任何被测逻辑：
    - `throttle_ms=0`：关掉 2Hz 合并，让每一帧都落到快照里（节流本身在 test_device 有专测）
    - `partial_interval_ms` 极大：聆听态的滚动 partial 依赖真实时钟，
      快照里留它只会得到一串随时间抖动的秒数
    """
    from tests.test_device import make_config
    from lens_gateway.session import DeviceSession

    cfg = make_config(
        asr_over={"partial_interval_ms": 10_000_000},
        throttle_ms=0, confirm_seconds=0.01, confirm_seconds_low_conf=0.01,
        **cfg_over,
    )
    return DeviceSession("dev_golden", cfg, asr, agent)


#: 0.6s 的 16kHz s16le 静音 —— 过了 0.5s 误触阈值
PCM_OK = b"\x00\x00" * 9600
#: 0.2s —— 在误触阈值以下
PCM_TOO_SHORT = b"\x00\x00" * 3200


async def _speak(session, pcm: bytes = PCM_OK) -> None:
    """按住说话再松手。确认窗口很短，这里等它自然走完。"""
    await session.voice.ptt_start()
    await session.voice.feed_pcm(pcm)
    await session.voice.ptt_stop()
    await asyncio.sleep(0.10)          # 跨过 confirm_seconds=0.01 并让 dispatch 跑完
    await asyncio.sleep(0)


async def scene_happy_path_long(session):
    await _speak(session)


async def scene_happy_path_short(session):
    await _speak(session)


async def scene_with_tool_call(session):
    await _speak(session)


async def scene_paging_after_answer(session):
    # 定稿后画面停在**首页**（重排不移动读者），所以顺序是先往后翻
    await _speak(session)
    session.hud.page(-1, source="glasses")   # 已在首页：不该发冗余帧
    await asyncio.sleep(0)
    session.hud.page(1, source="phone")      # → 末页
    await asyncio.sleep(0)
    session.hud.page(-1, source="mcp")       # → 回首页
    await asyncio.sleep(0)
    session.hud.page(1, source="voice")      # → 末页
    await asyncio.sleep(0)
    session.hud.page(1, source="glasses")    # 已在末页：不该再发冗余帧
    await asyncio.sleep(0)


async def scene_misfire_too_short(session):
    await _speak(session, PCM_TOO_SHORT)


async def scene_asr_heard_nothing(session):
    await _speak(session)


async def scene_agent_busy(session):
    await _speak(session)


async def scene_agent_error(session):
    await _speak(session)


async def scene_agent_reports_error(session):
    await _speak(session)


async def scene_untrusted_agent(session):
    await _speak(session)


async def scene_low_battery(session):
    session.hud.note_battery(11, False)
    await _speak(session)


async def scene_abort_midway(session):
    """打断必须落在**流式中途**。

    第一版这个场景是 `_speak()` 跑完（含 final）之后才 abort —— 那时 run 已经结束，
    走的是"定稿后打断"，与场景名说的完全是两码事，僵尸 run 过滤那条分支零覆盖。
    """
    agent = session.voice.claw
    await session.voice.ptt_start()
    await session.voice.feed_pcm(PCM_OK)
    await session.voice.ptt_stop()
    await asyncio.wait_for(agent.paused.wait(), 2.0)   # 停在第二个 delta 上
    await session.voice.abort()
    await asyncio.sleep(0.05)


async def scene_markdown_leaks_from_model(session):
    # 定稿停在首页 ⇒ 只有首页会成帧。必须把每一页都翻出来发一遍，
    # 否则「正文里没有裸 markdown」这条不变量只检查到了第一页。
    await _speak(session)
    for _ in range(session.hud.paginator.total - 1):
        session.hud.page(1, source="glasses")
        await asyncio.sleep(0)


#: 场景名 → (agent, asr, 驱动函数, 额外配置)
def build(name: str):
    stream = [("partial", LONG_ANSWER[:40], ""),
              ("partial", LONG_ANSWER[:90], ""),
              ("final", LONG_ANSWER, "")]
    table = {
        "happy_path_long": (ScriptedAgent(stream), ScriptedAsr("眼镜的屏幕多大"), {}),
        "happy_path_short": (
            ScriptedAgent([("final", SHORT_ANSWER, "")]), ScriptedAsr("今天天气怎么样"), {}),
        "with_tool_call": (
            ScriptedAgent([("tool", "查时间", "正在查当前时间"),
                           ("final", "现在是下午三点十七分。", "")]),
            ScriptedAsr("现在几点了"), {}),
        "paging_after_answer": (ScriptedAgent(stream), ScriptedAsr("眼镜的屏幕多大"), {}),
        "misfire_too_short": (ScriptedAgent([]), ScriptedAsr("不该被用到"), {}),
        "asr_heard_nothing": (ScriptedAgent([]), ScriptedAsr(""), {}),
        "agent_busy": (ScriptedAgent([], busy=True), ScriptedAsr("在吗"), {}),
        "agent_error": (
            ScriptedAgent([], raises="Connection refused"), ScriptedAsr("在吗"), {}),
        "agent_reports_error": (
            ScriptedAgent([("error", "模型返回 429，稍后重试", "")]), ScriptedAsr("在吗"), {}),
        "untrusted_agent": (
            ScriptedAgent([("final", SHORT_ANSWER, "")], production=False),
            ScriptedAsr("今天天气怎么样"), {}),
        "low_battery": (
            ScriptedAgent([("final", SHORT_ANSWER, "")]), ScriptedAsr("今天天气怎么样"), {}),
        "abort_midway": (ScriptedAgent(stream, pause_after=1),
                         ScriptedAsr("眼镜的屏幕多大"), {}),
        "markdown_leaks_from_model": (
            ScriptedAgent([("final", MARKDOWN_ANSWER, "")]),
            ScriptedAsr("怎么装这个插件"), {}),
    }
    return table[name]


async def run_scene(name: str) -> list[dict]:
    """跑一个场景，返回它产生的全部帧。"""
    agent, asr, over = build(name)
    session = make_session(agent, asr, **over)
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    session.attach(sink)
    await asyncio.sleep(0)
    await globals()[f"scene_{name}"](session)
    session.voice.reset()
    session.hud.cancel_timer()
    if session.voice._partial_task:
        session.voice._partial_task.cancel()
    await asyncio.sleep(0)
    return frames


async def regen() -> None:
    out = {name: await run_scene(name) for name in SCENES}
    GOLDEN.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(len(v) for v in out.values())
    print(f"已写入 {GOLDEN}：{len(out)} 个场景 / {total} 帧")


if __name__ == "__main__":
    import sys
    if "--regen" not in sys.argv:
        print(__doc__)
        sys.exit(1)
    asyncio.run(regen())
