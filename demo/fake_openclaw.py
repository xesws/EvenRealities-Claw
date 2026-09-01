"""演示用的 OpenClaw 网关替身（本地无真 agent 时使用）。

说的是与真工部网关同一套 protocol v3（见 gateway/lens_gateway/providers/openclaw.py）：
  connect  → hello-ok
  chat.send → res{runId}，随后流式 event:chat（delta×N → final）
  chat.abort → res{ok}

与真 agent 的唯一区别：回复内容来自本文件的剧本，不来自模型。
**它在握手时自报 `fixture: true`** —— 网关据此在 `/healthz` 标注、并在眼镜状态条
上显示「工?」。替身不伪装成真 agent，是「演示不能有 fake」这条要求可被验证的前提。
其余每一环（麦克风、faster-whisper 转写、HUD 状态机、折行分页、BLE 渲染节流）
全部走真实代码路径。

运行：
    gateway/.venv/bin/python demo/fake_openclaw.py            # 监听 127.0.0.1:18789
    gateway/.venv/bin/python demo/fake_openclaw.py --port 18789 --think 2.0
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from typing import Any

from aiohttp import WSMsgType, web

# ---------------------------------------------------------------- 剧本

# 关键词 → 回复。命中第一个 key 出现在问题里的条目；都不命中用 DEFAULT。
# 回复按「小屏风格」写：先结论后细节、短句、无 markdown。
#
# 长度参考（真实版式，见 gateway/lens_gateway/formatting/layout.py）：
# 正文容器 576×216px，LVGL 行高固定 27px ⇒ 8 行/页；一个汉字 20px ⇒ 每行约 28 字。
# 也就是**约 224 汉字/页**。想演示翻页就写 250 字以上。
SCRIPT: list[tuple[tuple[str, ...], str]] = [
    (
        ("你是谁", "自我介绍", "介绍一下你", "什么系统"),
        "我是跑在你私有服务器上的工部 agent，现在通过这副眼镜跟你说话。"
        "先说结论：你看到的每一个字，都是服务器排好版之后整屏推过来的。"
        "链路是这样的：你按住手机说话，声音从麦克风采样成 16k 单声道，"
        "上行到你自己的网关；网关用 faster-whisper 转成文字，再交给我；"
        "我的回复不直接丢给眼镜，而是先在服务器上折行、分页、剥掉 markdown，"
        "编排成一帧一帧的整屏画面下发。眼镜本身不做任何判断，它只负责显示。"
        "这样做的好处是：眼镜永远不会显示一个过期的、撒谎的画面。"
        "顺带一提，屏幕上的图标也不是随便挑的——G2 的字库有限，"
        "对勾、齿轮、警告这些常见符号根本不在字库里，下发过去只会消失，"
        "所以每一个图标都对着字库校验过。",
    ),
    (
        ("架构", "链路", "怎么实现", "技术", "原理", "眼镜"),
        "三段式：眼镜、手机、服务器。眼镜只有麦克风和一块 576×288 的绿色屏，"
        "四位灰阶、十六级绿，行高固定二十七像素，一页正好八行。"
        "它是一块哑屏，不跑任何业务逻辑，也不做任何判断。手机上的插件是个转发器，"
        "把语音往上送、把画面往下发，外加一个看门狗。"
        "真正的大脑全在你自己的服务器上：语音识别、状态机、排版、调度 agent，"
        "以及最重要的一点——所有凭证都锁在服务器里，手机端只拿一张随时可以吊销的短期票。"
        "所以手机丢了不等于你的 agent 被别人拿走了。"
        "排版这一层值得多说一句：折行不是按字数估的，而是按固件真实的字形宽度算的，"
        "服务器算出来的每一行宽度，和眼镜上画出来的一模一样。"
        "这样分页才是确定的，翻页才不会串行。",
    ),
    (
        ("延迟", "多快", "多久", "性能", "速度"),
        "从松手到眼镜上出现第一个字，实测大约两秒半。"
        "拆开看：语音转写占一秒四，agent 首字大约七百毫秒，渲染到眼镜约三百毫秒。"
        "说话过程中还有一路更快的小模型在跑，六百多毫秒刷新一次，"
        "所以你说话的时候眼镜上就在逐字出字，不用等松手才有反馈。",
    ),
    (
        ("为什么", "解决什么", "有什么用", "价值", "意义"),
        "解决的是一个很具体的场景：你人不在电脑前，但你的 agent 在干活。"
        "过去你得掏手机、解锁、打开应用、找到会话、打字。"
        "现在你抬眼就能看到它干到哪一步了，按住说一句话就能派新活。"
        "关键设计是零点五秒瞥视契约——任何时刻，屏幕最左边三个字符就能告诉你系统在干什么，"
        "不需要读完整屏。这是为走路时抬眼一瞥设计的，不是为坐着阅读设计的。",
    ),
    (
        ("天气",),
        "今天晴，二十六度，东南风三级。晚上转多云，最低十九度。适合出门。",
    ),
]

DEFAULT = (
    "收到。这条回复来自演示替身，不是真的模型输出，"
    "但你听到的转写、看到的分页和翻页全都是真实链路跑出来的。"
    "接上真的工部 agent 之后，这个位置会换成它的真实回答，其余一个字都不用改。"
)

# session.py 首条消息会注入小屏风格指令，真正的用户问题在末尾；只取最后一行做匹配。
_PREFIX = re.compile(r"^.*\n", re.S)


def reply_for(message: str) -> str:
    """按剧本选回复。message 可能带 session.py 注入的风格指令前缀。"""
    question: str = _PREFIX.sub("", message).strip() or message.strip()
    for keys, text in SCRIPT:
        if any(k in question for k in keys):
            return f"你问的是「{question}」。{text}"
    return f"你问的是「{question}」。{DEFAULT}"


def chunks(text: str, size: int = 11) -> list[str]:
    """切成小块模拟 LLM 流式吐字。"""
    return [text[i:i + size] for i in range(0, len(text), size)]


# ---------------------------------------------------------------- 服务

class FakeGateway:
    def __init__(self, think_seconds: float, chunk_ms: int) -> None:
        self.think_seconds: float = think_seconds      # 首字前的思考停顿（演示 S4 计秒）
        self.chunk_ms: int = chunk_ms                  # 每块之间的间隔
        self.aborted: set[str] = set()                 # 被打断的 sessionKey
        self.running: dict[str, str] = {}              # sessionKey -> runId

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        print("[fake-openclaw] 网关已接入")
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                frame: dict[str, Any] = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if frame.get("type") != "req":
                continue
            await self._on_req(ws, frame)
        print("[fake-openclaw] 网关断开")
        return ws

    async def _on_req(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        req_id: str = frame.get("id", "")
        method: str = frame.get("method", "")
        params: dict[str, Any] = frame.get("params") or {}

        if method == "connect":
            # ★ W6：**自报家门**。真的 OpenClaw 网关不会发 `fixture`，所以网关
            # 一握手就知道自己接的是替身，`/healthz` 能当场自证、HUD 状态条会带「?」。
            # 替身主动暴露自己的身份，才谈得上"演示里没有 fake"这句话可被验证。
            await self._res(ws, req_id, {
                "protocol": 3,
                "server": {"name": "fake-openclaw", "version": "0.1.0", "fixture": True},
                "fixture": True,
            })
            print("[fake-openclaw] connect 握手完成")

        elif method == "chat.send":
            session_key: str = params.get("sessionKey", "default")
            message: str = params.get("message", "")
            run_id: str = "run_" + uuid.uuid4().hex[:10]
            self.aborted.discard(session_key)
            self.running[session_key] = run_id
            await self._res(ws, req_id, {"runId": run_id})
            print(f"[fake-openclaw] chat.send run={run_id} q={message[-40:]!r}")
            asyncio.create_task(self._stream(ws, session_key, run_id, reply_for(message)))

        elif method == "chat.abort":
            session_key = params.get("sessionKey", "default")
            self.aborted.add(session_key)
            await self._res(ws, req_id, {"ok": True})
            print(f"[fake-openclaw] chat.abort session={session_key}")

        else:
            await self._res(ws, req_id, {})

    async def _stream(self, ws: web.WebSocketResponse, session_key: str,
                      run_id: str, reply: str) -> None:
        """思考停顿 → 逐块 delta（累计全文形态）→ final。"""
        await asyncio.sleep(self.think_seconds)
        acc: str = ""
        for piece in chunks(reply):
            if session_key in self.aborted or ws.closed:
                print(f"[fake-openclaw] run={run_id} 被打断，停止吐字")
                return
            acc += piece
            await self._event(ws, run_id, "delta", acc)
            await asyncio.sleep(self.chunk_ms / 1000)
        if session_key in self.aborted or ws.closed:
            return
        await self._event(ws, run_id, "final", acc)
        self.running.pop(session_key, None)
        print(f"[fake-openclaw] run={run_id} 完成，{len(acc)} 字")

    @staticmethod
    async def _res(ws: web.WebSocketResponse, req_id: str, payload: dict[str, Any]) -> None:
        await ws.send_str(json.dumps(
            {"type": "res", "id": req_id, "ok": True, "payload": payload}, ensure_ascii=False))

    @staticmethod
    async def _event(ws: web.WebSocketResponse, run_id: str, state: str, text: str) -> None:
        await ws.send_str(json.dumps({
            "type": "event",
            "event": "chat",
            "payload": {
                "runId": run_id,
                "state": state,
                "message": {"content": [{"type": "text", "text": text}]},
            },
        }, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description="OpenClaw 网关替身（演示用）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18789)
    ap.add_argument("--think", type=float, default=2.0, help="首字前思考秒数（演示 S4 计秒）")
    ap.add_argument("--chunk-ms", type=int, default=110, help="流式每块间隔毫秒")
    args = ap.parse_args()

    gw = FakeGateway(think_seconds=args.think, chunk_ms=args.chunk_ms)
    app = web.Application()
    app.router.add_get("/", gw.handle)
    print(f"[fake-openclaw] 监听 ws://{args.host}:{args.port}  思考={args.think}s  块间隔={args.chunk_ms}ms")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
