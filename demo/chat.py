#!/usr/bin/env python3
"""跟眼镜 agent 直接对话的命令行。**不经过浏览器，也不注入任何脚本。**

它说的就是 agent 自己的协议（Lens Agent Protocol v1，见 `docs/AGENT-LAYER.md`），
和网关说的是同一套 —— 换句话说，你在这里问什么、怎么问都行，agent 的行为
和戴着眼镜说话时**完全一致**。

每一轮都把过程摊开给你看：

    route   代码选中的 skill（闸 2：模型无权参与），以及它拿到的工具子集
    tool    真实发生的工具调用、耗时、成功与否
    screen  答案按 G2 的 576×288 真实版式排出来是几页、每页长什么样

用法：

    cd gateway
    .venv/bin/python ../demo/chat.py                 # 交互，随便问
    .venv/bin/python ../demo/chat.py -q "现在几点"    # 问一句就退
    .venv/bin/python ../demo/chat.py -f cases.txt    # 一行一题，批量跑

交互里的命令：
    /new     开一个新会话（清空 agent 的对话记忆）
    /raw     切换：是否显示未经排版的原始答案
    /quit    退出

环境变量：
    LENS_AGENT_URL     agent 地址（默认 ws://127.0.0.1:18790）
    LENS_AGENT_LOCALE  只影响本地复算路由时的显示，agent 自己的语言由它的进程决定
    LENS_BUDGET_MS     每轮预算上限（默认 12000，与网关 `agent.budget_ms` 一致）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import secrets
import sys
import time

import aiohttp

# 让 `gateway/` 与仓库根都在 path 上：路由复算和版式预览都要 import 真代码，
# 而不是在这里重写一份"差不多"的逻辑 —— 重写出来的东西迟早和真代码不一致。
_HERE = pathlib.Path(__file__).resolve().parent
for p in (_HERE.parent / "gateway", _HERE.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

URL = os.environ.get("LENS_AGENT_URL", "ws://127.0.0.1:18790")
BUDGET_MS = int(os.environ.get("LENS_BUDGET_MS", "12000"))

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"


def _c(s: str, color: str) -> str:
    return f"{color}{s}{RESET}" if sys.stdout.isatty() else s


# ---------------------------------------------------------------- 版式预览

def _cols(s: str) -> int:
    """终端列宽：CJK 一个字占两列，否则框线对不齐。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "FW" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _cols(s))


def _paginator():
    """真的排版引擎，不是近似。拿不到就退化成不预览。"""
    try:
        from lens_gateway.formatting import Paginator
        return Paginator()
    except Exception:
        return None


def show_screen(text: str) -> None:
    """按 G2 的真实版式把答案画出来：8 行一页、576px 内宽折行、真页脚。"""
    p = _paginator()
    if p is None:
        print(text)
        return
    p.set_text(text)
    rows = p.box.max_lines
    width = max((_cols(l) for pg in p.pages for l in pg), default=0)
    width = max(width, 40)
    for i in range(p.total):
        lines = p.page_text(i).split("\n")
        foot = p.footer()                      # 真页脚：箭头只在那个方向真有页时出现
        print(_c("    ┌" + "─" * (width + 2) + "┐", DIM))
        for ln in lines[:rows]:
            print(_c("    │ ", DIM) + _pad(ln, width) + _c(" │", DIM))
        for _ in range(max(0, rows - len(lines))):
            print(_c("    │ ", DIM) + " " * width + _c(" │", DIM))
        print(_c("    └" + (foot or "").center(width + 2, "─") + "┘", DIM))
        if not p.turn(1):
            break


# ---------------------------------------------------------------- 协议

class Agent:
    def __init__(self, ws) -> None:
        self.ws = ws

    @classmethod
    async def connect(cls, session: aiohttp.ClientSession):
        ws = await session.ws_connect(URL)
        await ws.send_json({"type": "req", "id": "c", "method": "connect", "params": {}})
        msg = await ws.receive()
        hello = json.loads(msg.data).get("payload", {})
        return cls(ws), hello

    async def ask(self, session_key: str, question: str) -> dict:
        rid = f"r{time.time_ns()}"
        await self.ws.send_json({"type": "req", "id": rid, "method": "chat.send",
                                 "params": {"sessionKey": session_key,
                                            "message": question, "budgetMs": BUDGET_MS}})
        t0 = time.perf_counter()
        out = {"ttft": None, "tools": [], "text": "", "error": None}
        pending: list[tuple[str, float]] = []
        while True:
            msg = await asyncio.wait_for(self.ws.receive(), timeout=90)
            if msg.type is not aiohttp.WSMsgType.TEXT:
                out["error"] = f"连接断开（{msg.type.name}）"
                break
            frame = json.loads(msg.data)
            if frame.get("type") == "res":
                continue
            ev = frame.get("payload") or {}
            state = ev.get("state")
            if state == "delta":
                if out["ttft"] is None:
                    out["ttft"] = time.perf_counter() - t0
            elif state == "tool":
                t = ev.get("tool") or {}
                name, phase = t.get("name", "?"), t.get("phase")
                if phase == "start":
                    pending.append((name, time.perf_counter()))
                    print(f"  {_c('tool  ', CYAN)} {t.get('label') or name} …", flush=True)
                else:
                    started = next((t for n, t in pending if n == name), t0)
                    pending[:] = [(n, t) for n, t in pending if not (n == name and t == started)]
                    ms = int((time.perf_counter() - started) * 1000)
                    out["tools"].append((name, ms))
            elif state == "final":
                out["text"] = ev["message"]["content"][0]["text"]
                break
            elif state == "error":
                out["error"] = str(ev.get("errorMessage"))
                break
        out["total"] = time.perf_counter() - t0
        # 工具的 end 事件不一定有（协议只保证 start），补一条时长未知的记录
        for name, _ in pending:
            out["tools"].append((name, -1))
        return out


CUT_MARKS = ("(cut off)", "（未说完）", "could not finish", "答不上来")


async def turn(agent: Agent, session_key: str, question: str, *, raw: bool) -> dict:
    from lens_agent import skills                       # 真路由，纯函数，可本地复算
    skill = skills.route(question)
    print(f"  {_c('route ', YELLOW)} {skill.name}"
          f"  budget {skill.budget_ms/1000:.1f}s"
          f"  tools: {', '.join(skill.tools) or '—'}")
    res = await agent.ask(session_key, question)
    if res["error"]:
        print(f"  {_c('ERROR ', RED)} {res['error']}")
        return res
    for name, ms in res["tools"]:
        print(f"  {_c('tool  ', CYAN)} {name} {'?' if ms < 0 else ms}ms")
    cut = any(m in res["text"] for m in CUT_MARKS)
    ttft = f"{res['ttft']:.2f}s" if res["ttft"] else "—"
    print(f"  {_c('timing', DIM)} 首字 {ttft} · 总 {res['total']:.2f}s"
          f"{_c('  ← 被预算掐断', RED) if cut else ''}")
    if raw:
        print(f"  {_c('raw   ', DIM)} {res['text']}")
    show_screen(res["text"])
    return res


async def main() -> None:
    ap = argparse.ArgumentParser(description="跟眼镜 agent 直接对话")
    ap.add_argument("-q", "--question", help="问一句就退")
    ap.add_argument("-f", "--file", help="一行一题，批量跑")
    ap.add_argument("--raw", action="store_true", help="同时显示未排版的原始答案")
    ap.add_argument("--session", default="",
                    help="会话 key（决定对话记忆）。默认每次启动都是新的；"
                         "显式给一个值才能接着上次的对话往下问")
    args = ap.parse_args()
    # 默认每次启动换一个 key。agent 的对话记忆是**按 key 存在它进程里**的，
    # 固定成 "cli" 的话，这一次的第一题会接着上一次第一题的历史往下答 ——
    # 实测撞见过：上一轮问的是中文，这一轮的英文问题被用中文回答了。
    args.session = args.session or "cli-" + secrets.token_hex(3)

    async with aiohttp.ClientSession() as http:
        try:
            agent, hello = await Agent.connect(http)
        except Exception as exc:
            print(f"连不上 agent（{URL}）：{type(exc).__name__}: {exc}")
            print("先起 agent：cd gateway && .venv/bin/python -m lens_agent.server")
            raise SystemExit(1)

        print(_c(f"{hello.get('agent')} v{hello.get('version')}  "
                 f"model={hello.get('model')}  production={hello.get('production')}", BOLD))
        try:
            from lens_agent import tools as _tools
            print(_c("tools: " + ", ".join(
                f"{t['name']}({t['capability']},{t['budget_ms']}ms)"
                for t in _tools.describe()), DIM))
        except Exception:
            pass

        if args.question or args.file:
            questions = ([args.question] if args.question
                         else [l.strip() for l in pathlib.Path(args.file).read_text().splitlines()
                               if l.strip() and not l.startswith("#")])
            for i, q in enumerate(questions):
                print(f"\n{_c('›', GREEN)} {BOLD}{q}{RESET}")
                # 批量时每题独立会话：一题的答案不该污染下一题的路由与记忆
                await turn(agent, f"{args.session}{i}", q, raw=args.raw)
            return

        print(_c("随便问。/new 换会话，/raw 切原文，/quit 退出。", DIM))
        session_key, n = args.session, 0
        loop = asyncio.get_running_loop()
        raw = args.raw
        while True:
            try:
                line = await loop.run_in_executor(None, input, f"\n{GREEN}›{RESET} ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            q = line.strip()
            if not q:
                continue
            if q in ("/quit", "/exit"):
                break
            if q == "/new":
                n += 1
                session_key = f"{args.session}-{n}"
                print(_c(f"  新会话 {session_key}（记忆已清空）", DIM))
                continue
            if q == "/raw":
                raw = not raw
                print(_c(f"  原文显示：{'开' if raw else '关'}", DIM))
                continue
            try:
                await turn(agent, session_key, q, raw=raw)
            except asyncio.TimeoutError:
                print(_c("  超时，没有收到 final", RED))


if __name__ == "__main__":
    asyncio.run(main())
