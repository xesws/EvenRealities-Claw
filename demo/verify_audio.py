#!/usr/bin/env python3
"""命令行复验：拿一段 WAV 当麦克风，把整条链路跑一遍并打印帧序列。

**不经过浏览器**。这个脚本自己就是一台设备：连网关的 `/ws`、配对、按 PTT、
把 PCM 一块块推上去、松手，然后把网关下发的每一帧原样打出来。

所以它验的是真东西 —— 真 whisper 转写、真 agent、真工具调用、真排版分页。
唯一被换掉的是声音的来源（麦克风 → 文件），而那是数据。

    cd gateway
    .venv/bin/python ../demo/verify_audio.py ../demo/audio/en-weather.wav

环境变量：
    LENS_STATE_DIR   网关状态目录（默认 ~/.lens-gateway-lens-en，即 --lens --en 用的那个）
    LENS_BASE        网关地址（默认 http://127.0.0.1:8443）

`--linger=<秒>` 会在收尾帧之后继续听：提醒到点是**后来**发生的事，
那一轮早就结束了，不多听一会儿就看不到它上屏。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
import urllib.request
import wave

import aiohttp

BASE = os.environ.get("LENS_BASE", "http://127.0.0.1:8443")
STATE = pathlib.Path(os.environ.get("LENS_STATE_DIR",
                                   str(pathlib.Path.home() / ".lens-gateway-lens-en")))

#: 20ms @ 16kHz 单声道 16bit = 640 字节；这里按 100ms 一块推，和插件的攒包节奏一致
CHUNK = 3200
CHUNK_INTERVAL = 0.1


def _admin(path: str) -> urllib.request.Request:
    """控制面要 Bearer —— peername 判据在反代之后整体失效（W4）。"""
    token = (STATE / "control.secret").read_text().strip()
    return urllib.request.Request(BASE + path, data=b"", method="POST",
                                  headers={"Authorization": f"Bearer {token}"})


async def main(wav_path: str, linger: float = 0.0) -> int:
    with wave.open(wav_path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, (
            f"需要 16kHz 单声道，实际 {w.getframerate()}Hz/{w.getnchannels()}ch")
        pcm = w.readframes(w.getnframes())
    print(f"素材：{wav_path}（{len(pcm) / 32000:.2f}s）")

    code = json.loads(urllib.request.urlopen(_admin("/admin/pair-code"), timeout=5).read())["code"]

    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(f"{BASE}/ws")
        await ws.send_json({"type": "pair", "code": code, "deviceName": "verify_audio.py"})

        frames: list[dict] = []
        inbox: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                o = json.loads(msg.data)
                if o.get("type") == "frame":
                    frames.append(o)
                await inbox.put(o)

        pumping = asyncio.ensure_future(pump())

        async def until(pred, timeout: float):
            end = time.time() + timeout
            while time.time() < end:
                try:
                    o = await asyncio.wait_for(inbox.get(), max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    return None
                if pred(o):
                    return o
            return None

        ok = await until(lambda o: o.get("type") == "pair_ok", 10)
        if not ok:
            print("✗ 配对超时"); return 1
        print(f"配对：{ok['deviceId']}")

        t0 = time.time()
        await ws.send_json({"type": "ptt", "action": "start"})
        for i in range(0, len(pcm), CHUNK):
            await ws.send_bytes(pcm[i:i + CHUNK])
            await asyncio.sleep(CHUNK_INTERVAL)     # 按真实时间推，别让 ASR 一次吃完
        await ws.send_json({"type": "ptt", "action": "stop"})

        done = await until(lambda o: o.get("type") == "frame" and o["state"] in ("S7", "S8"), 90)

        print(f"\n--- 帧序列（{time.time() - t0:.1f}s）---")
        for f in frames:
            c = f["containers"]
            body = c["body"].replace("\n", " / ")[:66]
            print(f'  {f["state"]}  {c["status"]:26} | {body}')

        if not done:
            print("\n✗ 未在 90s 内收到收尾帧"); pumping.cancel(); await ws.close(); return 1

        print(f"\n--- 收尾帧（{done['state']}）---")
        print(done["containers"]["body"])
        print(f"\n页脚：{done['containers']['foot']!r}")

        if linger > 0:
            # 提醒到点是**后来**发生的事：那一轮早结束了，帧是 agent 主动
            # 请求、网关按租约写上去的。不多听一会儿就看不到它。
            print(f"\n--- 继续听 {linger:.0f}s，看有没有后续帧（提醒到点）---")
            end = time.time() + linger
            while time.time() < end:
                o = await until(lambda o: o.get("type") == "frame", end - time.time())
                if o is None:
                    break
                c = o["containers"]
                print(f'  +{time.time() - t0:5.1f}s  {o["state"]}  {c["status"]:26} | '
                      f'{c["body"].replace(chr(10), " / ")[:66]}')

        pumping.cancel()
        await ws.close()
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    linger = 0.0
    for a in sys.argv[1:]:
        if a.startswith("--linger="):
            linger = float(a.split("=", 1)[1])
    if len(args) != 1:
        sys.exit(__doc__)
    raise SystemExit(asyncio.run(main(args[0], linger)))
