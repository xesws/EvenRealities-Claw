"""M6 验收：**真 DeepSeek** 的语音端到端，三个真进程串起来。

    本脚本（扮演插件，灌真实语音 PCM，收真实渲染帧）
        ↓ Lens 协议 v1.1 WebSocket
    lens_gateway 进程（真 faster-whisper 转写）
        ↓ Lens Agent Protocol v1
    lens_agent 进程（真 DeepSeek API，手写 loop，真工具调用）

与 `e2e_sim.py` 的区别只有一处：那边 agent 是写死剧本的**测试替身**，
这边是**真模型**。所以这里能断言、而那边断言不了的东西是：

- 回答不可能来自任何剧本（剧本文本被逐条排除）；
- `now` 工具**真的被调用了**，HUD 的 S5 工具态第一次在真链路里点亮；
- `/healthz` 的 agent 溯源显示 `backend=lens / production=true / model=deepseek-v4-flash`，
  状态条徽记**没有**「?」—— 与替身模式恰好相反。

需要 `LENS_LLM_API_KEY`（或 `OPENAI_API_KEY`）。会产生真实 API 调用。

运行：PYTHONPATH=. .venv/bin/python tests/e2e_agent.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lens_gateway.formatting import (  # noqa: E402
    DEFAULT_LAYOUT, glyph_set, missing_codepoints, text_width)

DATA = ROOT / "tests" / "data" / "asr"
BODY = DEFAULT_LAYOUT.body
GLYPHS = glyph_set()

#: `demo/fake_openclaw.py` 剧本里的特征串。真模型的回答里出现任何一条，
#: 都说明连错了 agent —— 这是"演示里没有替身"的可执行版本。
FIXTURE_PHRASES = ("眼镜链路畅通", "链路正常", "这是一条演示回复")

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))
    return bool(ok)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def load_pcm(name: str) -> bytes:
    from faster_whisper.audio import decode_audio
    audio = decode_audio(str(DATA / name), sampling_rate=16000)
    return (audio * 32768).clip(-32768, 32767).astype(np.int16).tobytes()


def wait_http(url: str, timeout: float, want: str | None = None) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                body = json.loads(r.read())
                if want is None or body.get(want):
                    return body
        except Exception:
            pass
        time.sleep(1)
    return None


class Client:
    """扮演手机插件：配对、灌音、收帧。"""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.frames: list[dict] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.ensure_future(self._pump())

    async def _pump(self) -> None:
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            obj = json.loads(msg.data)
            if obj.get("type") == "frame":
                self.frames.append(obj)
            await self._queue.put(obj)

    async def until(self, pred, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                obj = await asyncio.wait_for(self._queue.get(),
                                             timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                return None
            if pred(obj):
                return obj
        return None

    async def ask(self, audio: str) -> None:
        """按住说话 → 灌 PCM（1x 实时）→ 松手。"""
        pcm = load_pcm(audio)
        await self.ws.send_json({"type": "ptt", "action": "start"})
        for i in range(0, len(pcm), 3200):        # 100ms/块
            await self.ws.send_bytes(pcm[i:i + 3200])
            await asyncio.sleep(0.1)
        await self.ws.send_json({"type": "ptt", "action": "stop"})

    def close(self) -> None:
        self._task.cancel()


def frame_is(state: str):
    return lambda o: o.get("type") == "frame" and o.get("state") == state


async def run(gw_port: int, agent_port: int) -> None:
    base = f"http://127.0.0.1:{gw_port}"
    secret = CONTROL_SECRET

    def admin(path: str, method: str = "GET", body: bytes | None = None):
        return urllib.request.Request(
            base + path, data=body, method=method,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {secret}"})

    # ---------- 0. agent 自证 ----------
    ah = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{agent_port}/healthz", timeout=5).read())
    check("agent 进程报告自己用的是哪个模型",
          ah.get("model") == "deepseek-v4-flash" and ah.get("provider") == "deepseek",
          f'{ah.get("provider")}/{ah.get("model")}')
    tool_names = {t["name"] for t in ah.get("tools", [])}
    # 这一条原来断言「工具只有 now，且全是只读」。工具表长起来之后它就该改了 ——
    # 但**不能改成「有哪些工具都行」**：这里守的是「屏幕上跑的那个 agent
    # 能干什么」，是拿去给人看的自证。所以改成逐条对齐声明，外加闸 1/闸 3 的
    # 两条硬性质：没有 exec 档，写档必须钉死在具体资源上。
    declared = {"now", "days_until", "device", "weather", "calc", "currency",
                "list_show", "list_add", "list_remove",
                "remind_set", "remind_list", "remind_cancel"}
    check("agent 装的工具与声明逐条一致（多一个少一个都算没对上）",
          tool_names == declared,
          f"多出 {sorted(tool_names - declared)}；缺 {sorted(declared - tool_names)}")
    caps = {t["capability"] for t in ah["tools"]}
    check("★ 闸 1：能力枚举里没有 exec 档（只有 read / write）",
          caps <= {"read", "write"}, ", ".join(sorted(caps)))
    writers = [t for t in ah["tools"] if t["capability"] == "write"]
    check("★ 闸 3：每个写工具都钉死在具体资源上",
          bool(writers) and all(t.get("resources") for t in writers),
          ", ".join(f'{t["name"]}→{t.get("resources")}' for t in writers))

    gh = json.loads(urllib.request.urlopen(f"{base}/healthz", timeout=5).read())
    ag = gh.get("agent") or {}
    check("★ /healthz 显示网关接的是自研 agent，不是替身",
          ag.get("backend") == "lens" and ag.get("production") is True
          and ag.get("model") == "deepseek-v4-flash",
          f'backend={ag.get("backend")} production={ag.get("production")} model={ag.get("model")}')

    # ---------- 1. 配对 ----------
    async with aiohttp.ClientSession() as http:
        code = json.loads(urllib.request.urlopen(
            admin("/admin/pair-code", "POST", b""), timeout=5).read())["code"]
        ws = await http.ws_connect(f"{base}/ws")
        await ws.send_json({"type": "pair", "code": code, "deviceName": "真-agent-e2e"})
        cli = Client(ws)
        ok = await cli.until(lambda o: o.get("type") == "pair_ok", 10)
        if not check("配对成功", ok is not None, (ok or {}).get("deviceId", "")):
            return

        # ---------- 2. 走工具的一问：「现在几点了」 ----------
        print("\n— 第一问（03_daily.mp3「现在几点了」→ 应路由到 daily 并调用 now 工具）—")
        t0 = time.time()
        await cli.ask("03_daily.mp3")
        t_end = time.time()

        s3 = await cli.until(lambda o: frame_is("S3")(o) and o["containers"]["body"], 40)
        check("松手 → 转写上屏", s3 is not None and "几点" in s3["containers"]["body"],
              f'{time.time()-t_end:.1f}s: {s3["containers"]["body"]!r}' if s3 else "40s 内无 S3")

        s5 = await cli.until(frame_is("S5"), 40)
        check("★ S5 工具态第一次在真链路里点亮（真模型真的调了 now）",
              s5 is not None and "查时间" in s5["containers"]["status"],
              s5["containers"]["status"] if s5 else "40s 内未进入 S5 —— 模型没调工具")

        s7 = await cli.until(frame_is("S7"), 90)
        if not check("收到最终回复", s7 is not None,
                     f"松手→完成 {time.time()-t_end:.1f}s" if s7 else "90s 内无 S7"):
            return
        answer = s7["containers"]["body"]
        print(f"    回答：{answer!r}")
        now = time.localtime()
        check("★ 回答里是真实的当前时间（工具结果真的进了上下文）",
              str(now.tm_year) in answer or f"{now.tm_hour}" in answer
              or f"{now.tm_mon}月" in answer or "点" in answer,
              f"本机现在 {time.strftime('%Y-%m-%d %H:%M')}")

        # ★ 上面那条断言**曾经放过一个真 bug**：网关侧的 delta 归并启发式在工具轮之后
        # 会把正文拼成「让我查一下时间现现在现在是现在是下午三点。」—— 里面有"点"、
        # 也有数字，上面那条照样通过。教训：断言「包含某关键词」挡不住「正文被污染」。
        #
        # 判据必须是**结构性**的，不能是统计性的（那段坏文本里每个四字片段其实都只
        # 出现一次，n-gram 频次判据抓不住它）。真正的结构不变式是：
        # 工具跑完后模型会重新组织正文，**工具之前流出来的那段散文不该留在最终答案里**。
        pre_tool = [f["containers"]["body"] for f in cli.frames
                    if f["state"] == "S6" and f["seq"] < s5["seq"]]
        if pre_tool:
            leaked = pre_tool[-1].strip()
            check("★ 工具前的散文没有留在最终答案里（delta 归并的回归）",
                  bool(leaked) and leaked not in answer, f"工具前屏幕上是 {leaked!r}")
        else:
            # 模型这一轮没在工具前流正文。断言无从施加，如实说出来而不是假装通过。
            check("（本轮模型未在工具前流正文，跳过 delta 归并回归）", True,
                  "该不变式由 tests/test_providers.py::TestLensDeltaContract 单测守着")

        # ---------- 3. 无工具的一问：「什么是光的折射」 ----------
        # 刻意选转写 CER=0 的那条：如果转写本身就错了，"回答对不对"这个断言就没有意义。
        # （05_ask 会把"什么是光"听成"什么时光"，模型于是忠实地回答了"时光折射"——
        #  那是 ASR 误差在下游被放大，不是 agent 的问题，见 REPORT §11。）
        print("\n— 第二问（06_ask.mp3「北京到上海大概有多远」→ 应走 ask，无工具）—")
        before_s5 = len([f for f in cli.frames if f["state"] == "S5"])
        await cli.ask("06_ask.mp3")
        s7b = await cli.until(lambda o: frame_is("S7")(o) and o["seq"] > s7["seq"], 90)
        if check("第二问收到回复", s7b is not None):
            print(f"    回答：{s7b['containers']['body']!r}")
            check("ask 路径不带工具（最高频路径的首字延迟不该为工具编排买单）",
                  len([f for f in cli.frames if f["state"] == "S5"]) == before_s5)
            body = s7b["containers"]["body"]
            # 数字可以是中文数字：实测答过「直线约一千二百公里」，
            # 只认阿拉伯数字会把一个正确的答案判成失败。
            check("回答真的答了这个问题（含距离数字与单位）",
                  bool(re.search(r"[\d零一二三四五六七八九十百千万两]", body))
                  and any(u in body for u in ("公里", "千米", "km", "KM")),
                  body[:40])

        # ---------- 4. 「不是替身」的可执行证明 ----------
        all_body = "\n".join(f["containers"]["body"] for f in cli.frames)
        hit = [p for p in FIXTURE_PHRASES if p in all_body]
        check("★ 回答不含测试替身剧本里的任何一句", not hit, " ".join(hit))
        badges = {f["containers"]["status"].split(" ")[0] for f in cli.frames
                  if f["containers"]["status"] and f["containers"]["status"] != GLYPHS["idle"]}
        check("★ 状态条徽记没有「?」（真 agent，与替身模式恰好相反）",
              badges and not any(b.endswith("?") for b in badges), " ".join(sorted(badges)))

        # ---------- 5. 帧约束（真模型的输出同样要过排版引擎）----------
        wide = [(ln, text_width(ln)) for f in cli.frames
                for ln in f["containers"]["body"].split("\n") if text_width(ln) > BODY.inner_width]
        check(f"正文每行 ≤ {BODY.inner_width}px", not wide,
              f"{wide[0][0]!r} 宽{wide[0][1]}px" if wide else "")
        tall = [(f["seq"], n) for f in cli.frames
                if (n := len(f["containers"]["body"].split("\n"))) > BODY.max_lines]
        check(f"正文每帧 ≤ {BODY.max_lines} 行", not tall,
              f"seq={tall[0][0]} 有 {tall[0][1]} 行" if tall else "")
        bad = [(f["seq"], k, missing_codepoints(v)) for f in cli.frames
               for k, v in f["containers"].items() if missing_codepoints(v)]
        check("下发的每个字符都在 G2 字库内（真模型也不例外）", not bad,
              f'seq={bad[0][0]} {bad[0][1]}' if bad else "")
        md = [f["seq"] for f in cli.frames
              if re.search(r"(\*\*|^#{1,6} |```|^\s*[-*] )", f["containers"]["body"], re.M)]
        check("markdown 被剥干净（模型不听话时的第二道防线）", not md, str(md[:3]))
        seqs = [f["seq"] for f in cli.frames]
        check("seq 严格单调递增", all(b > a for a, b in zip(seqs, seqs[1:])), f"{len(seqs)} 帧")

        print(f"\n  两问共耗时 {time.time()-t0:.1f}s")
        cli.close()
        await ws.close()


CONTROL_SECRET = ""


def main() -> int:
    if not (os.environ.get("LENS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        print("需要 LENS_LLM_API_KEY（或 OPENAI_API_KEY）才能跑真 agent 端到端。")
        return 2
    if not (DATA / "03_daily.mp3").exists():
        print("缺语音素材，先跑 tests/make_asr_dataset.py")
        return 2

    gw_port, agent_port = free_port(), free_port()
    state = tempfile.mkdtemp(prefix="lens-agent-e2e-")
    (Path(state) / "config.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": gw_port,
        "agent": {"provider": "lens", "url": f"ws://127.0.0.1:{agent_port}",
                  "budget_ms": 12000},
    }))
    print(f"日志目录: {state}")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    agent_log = open(Path(state) / "agent.log", "w")
    gw_log = open(Path(state) / "gateway.log", "w")

    agent = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "lens_agent"],
        env={**env, "LENS_AGENT_PORT": str(agent_port),
             "LENS_AGENT_AUDIT": str(Path(state) / "audit.jsonl")},
        cwd=str(ROOT), stdout=agent_log, stderr=subprocess.STDOUT)
    procs = [agent]
    try:
        if not wait_http(f"http://127.0.0.1:{agent_port}/healthz", 30, "ok"):
            print("agent 未就绪，见 agent.log")
            return 1
        gw = subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), "-m", "lens_gateway.main", "serve"],
            env={**env, "LENS_STATE_DIR": state},
            cwd=str(ROOT), stdout=gw_log, stderr=subprocess.STDOUT)
        procs.append(gw)
        print("等待网关就绪（含 whisper 加载）…")
        if not wait_http(f"http://127.0.0.1:{gw_port}/healthz", 150, "asr_ready"):
            print("网关未就绪，见 gateway.log")
            return 1
        global CONTROL_SECRET
        CONTROL_SECRET = (Path(state) / "control.secret").read_text().strip()
        print("三进程真链路已就绪（真 whisper + 真 DeepSeek）：")
        asyncio.run(run(gw_port, agent_port))

        # 审计日志：工具调用必须留痕
        audit = Path(state) / "audit.jsonl"
        rows = ([json.loads(x) for x in audit.read_text().splitlines()]
                if audit.exists() else [])
        check("★ 工具调用留下了审计（闸 4）",
              any(r["tool"] == "now" and r["ok"] for r in rows),
              f"{len(rows)} 行：" + json.dumps(rows[0], ensure_ascii=False)[:90] if rows else "无")

        log_text = (Path(state) / "agent.log").read_text()
        check("全程没有收到 reasoning_content（thinking 确实被关掉了）",
              "reasoning_content" not in log_text,
              "agent.log 里没有相关告警")
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()
        agent_log.close()
        gw_log.close()

    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\n=== 真 agent 端到端：{passed}/{len(CHECKS)} 通过 ===")
    if passed != len(CHECKS):
        print("失败项：")
        for n, ok in CHECKS:
            if not ok:
                print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
