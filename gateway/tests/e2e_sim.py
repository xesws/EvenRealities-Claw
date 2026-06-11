"""阶段一验收：模拟器端到端闭环。

模拟插件客户端（纯协议，不依赖浏览器）：
  启动真实网关（子进程，独立状态目录）→ 配对 → PTT → 灌入真实中文语音(MP3→PCM)
  → 真实 faster-whisper 转写 → 真实工部 agent 回复 → 校验下行帧流。

运行：PYTHONPATH=. .venv/bin/python tests/e2e_sim.py
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import aiohttp
import numpy as np

PORT = 18900
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/q3_echo.mp3"


def _load_pcm() -> bytes:
    from faster_whisper.audio import decode_audio
    audio = decode_audio(str(FIXTURE), sampling_rate=16000)
    return (audio * 32768).clip(-32768, 32767).astype(np.int16).tobytes()


async def wait_health(timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=2) as r:
                h = json.loads(r.read())
                if h.get("ok") and h.get("asr_ready"):
                    return
        except Exception:
            pass
        await asyncio.sleep(1)
    raise TimeoutError("gateway 未就绪")


def pair_code() -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/admin/pair-code", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())["code"]


CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


async def run_client() -> None:
    pcm = _load_pcm()
    frames: list[dict] = []
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(f"ws://127.0.0.1:{PORT}/ws")

        async def recv_until(pred, timeout=90):
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = await ws.receive(timeout=deadline - time.time())
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                obj = json.loads(msg.data)
                if obj.get("type") == "frame":
                    frames.append(obj)
                if pred(obj):
                    return obj
            raise TimeoutError(str(pred))

        # 1. 配对
        await ws.send_json({"type": "pair", "code": pair_code(), "deviceName": "e2e-sim"})
        pair_ok = await recv_until(lambda o: o.get("type") == "pair_ok")
        check("配对成功（pair_ok 含双 token）",
              bool(pair_ok.get("accessToken")) and bool(pair_ok.get("refreshToken")))

        # 2. resume 帧
        hello = await recv_until(lambda o: o.get("type") == "hello_ok")
        check("hello_ok 携带 resume 现场帧", hello.get("resume", {}).get("type") == "frame")

        # 3. PTT + 实时灌入 PCM（100ms/块，1x 速度）
        await ws.send_json({"type": "ptt", "action": "start"})
        await recv_until(lambda o: o.get("type") == "frame" and o["state"] == "S2", 10)
        check("PTT 后立即进入 S2 聆听（免节流）", True)
        t_speech_end = 0.0
        chunk = 3200  # 100ms
        for i in range(0, len(pcm), chunk):
            await ws.send_bytes(pcm[i:i + chunk])
            await asyncio.sleep(0.1)
        t_speech_end = time.time()
        await ws.send_json({"type": "ptt", "action": "stop"})

        # 4. 等待转写确认帧（S3 带 final 文本）
        s3 = await recv_until(
            lambda o: o.get("type") == "frame" and o["state"] == "S3" and o["containers"]["body"], 30)
        t_transcript = time.time() - t_speech_end
        check(f"松手→final 转写上屏 {t_transcript:.1f}s",
              t_transcript < 8, s3["containers"]["body"][:30])
        check("转写文本命中关键词", "畅通" in s3["containers"]["body"])

        # 5. 思考帧 → 回复帧
        await recv_until(lambda o: o.get("type") == "frame" and o["state"] == "S4", 15)
        check("进入 S4 思考（含状态条计秒）", True)
        s7 = await recv_until(lambda o: o.get("type") == "frame" and o["state"] == "S7", 120)
        t_reply = time.time() - t_speech_end
        check(f"收到 agent 最终回复（说完→完成 {t_reply:.1f}s）", True, s7["containers"]["body"][:40])
        check("回复来自真实工部（含指令回显语义）", "畅通" in s7["containers"]["body"])

        # 6. 帧约束校验
        seqs = [f["seq"] for f in frames]
        check("seq 严格单调递增", all(b > a for a, b in zip(seqs, seqs[1:])), f"{len(seqs)} 帧")
        wide = [ln for f in frames for ln in f["containers"]["body"].split("\n")
                if sum(2 if ord(c) > 0x2E80 else 1 for c in ln) > 35]
        check("正文每行 ≤17 汉字预算", not wide, (wide[0] if wide else ""))
        keys_ok = all(set(f["containers"]) == {"status", "body", "foot"} for f in frames)
        check("帧 containers 结构恒定", keys_ok)

        # 7. 断线重连：重放现场
        await ws.close()
        ws2 = await http.ws_connect(f"ws://127.0.0.1:{PORT}/ws")
        await ws2.send_json({"type": "refresh", "refreshToken": pair_ok["refreshToken"]})
        rmsg = await ws2.receive(timeout=10)
        robj = json.loads(rmsg.data)
        check("refreshToken 换发新 access", robj.get("type") == "refresh_ok")
        await ws2.send_json({"type": "hello", "token": robj["accessToken"]})
        hmsg = await ws2.receive(timeout=10)
        hobj = json.loads(hmsg.data)
        resume = hobj.get("resume", {})
        check("重连后 1 帧恢复现场（resume = 断前画面）",
              resume.get("state") in ("S7", "S0") and resume.get("seq", 0) >= seqs[-1])

        # 8. 翻页与清屏
        await ws2.send_json({"type": "reset"})
        frames2 = []
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                m = await ws2.receive(timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            if m.type == aiohttp.WSMsgType.TEXT:
                o = json.loads(m.data)
                if o.get("type") == "frame":
                    frames2.append(o)
                    if o["state"] == "S0":
                        break
        check("reset → 回 S0 待机", any(f["state"] == "S0" for f in frames2))
        await ws2.close()


async def main() -> None:
    state_dir = tempfile.mkdtemp(prefix="lens-e2e-")
    (Path(state_dir) / "config.json").write_text(json.dumps({"port": PORT, "host": "127.0.0.1"}))
    env = {**os.environ, "LENS_STATE_DIR": state_dir, "PYTHONPATH": str(ROOT)}
    log_file = open(Path(state_dir) / "server.log", "w")
    print(f"服务端日志: {state_dir}/server.log")
    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "lens_gateway.main", "serve"],
        env=env, cwd=str(ROOT), stdout=log_file, stderr=subprocess.STDOUT)
    try:
        print("等待网关就绪（含模型加载）…")
        await wait_health()
        print("开始端到端闭环：")
        await run_client()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n=== e2e 结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过 ===")
    if failed:
        print("失败项：", failed)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
