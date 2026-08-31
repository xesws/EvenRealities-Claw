"""端到端闭环验收：自足运行，不依赖任何仓库外服务。

链路：拉起真实网关子进程（独立状态目录）+ agent 测试夹具子进程
  → 配对 → PTT → 灌入真实中文语音(MP3→PCM) → 真实 faster-whisper 转写
  → agent 回复 → 校验帧流约束 → 翻页 → 断线重连恢复 → reset。

agent 侧默认使用 `demo/fake_openclaw.py` 作为**测试夹具**：它跑的是与真网关
完全相同的 protocol v3，唯一区别是回复内容来自剧本而非模型。这是测试里的
test double，不是演示链路的替身——演示必须接真 agent。

打真 agent 跑同一套断言：
    LENS_E2E_AGENT_URL=ws://127.0.0.1:18789 \
    LENS_E2E_AGENT_CONFIG=~/.openclaw/openclaw.json \
    PYTHONPATH=. .venv/bin/python tests/e2e_sim.py

运行：PYTHONPATH=. .venv/bin/python tests/e2e_sim.py
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]          # gateway/
REPO = ROOT.parent                                  # 仓库根
sys.path.insert(0, str(ROOT))

from lens_gateway.formatting import (  # noqa: E402
    DEFAULT_LAYOUT, glyph_set, missing_codepoints, text_width)

PORT = 18900
FIXTURE = ROOT / "tests/fixtures/q3_echo.mp3"
AGENT_FIXTURE = REPO / "demo" / "fake_openclaw.py"

# 宽度口径与生产代码同源（修 T4：原来用的是脚本自带的近似函数 + 阈值 35）。
# 现在直接用真实版式的像素宽度 —— 与固件 LVGL 的字形度量逐位一致，见 formatting/metrics.py。
BODY = DEFAULT_LAYOUT.body
BUDGET_PX = BODY.inner_width                        # 576px
MAX_LINES = BODY.max_lines                          # 8 行（27px 固定行高）
GLYPHS = glyph_set()                                # 与网关默认档位同源


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _load_pcm() -> bytes:
    from faster_whisper.audio import decode_audio
    audio = decode_audio(str(FIXTURE), sampling_rate=16000)
    return (audio * 32768).clip(-32768, 32767).astype(np.int16).tobytes()


def line_width(line: str) -> int:
    """行的真实像素宽度（G2 字形度量）。"""
    return text_width(line)


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


def wait_port(port: int, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def admin_live(device_id: str) -> dict | None:
    """从 /admin/devices 读某台设备的实时快照（含遥测）。"""
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/admin/devices", timeout=5) as r:
        for row in json.loads(r.read()):
            if row.get("device_id") == device_id:
                return row.get("live")
    return None


def pair_code() -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/admin/pair-code", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())["code"]


CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录一项断言。返回 ok 供调用方决定是否继续依赖该结果。"""
    CHECKS.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))
    return bool(ok)


async def run_client() -> None:
    pcm = _load_pcm()
    frames: list[dict] = []

    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(f"ws://127.0.0.1:{PORT}/ws")

        async def recv_until(pred, timeout=90):
            """收到匹配帧则返回它；超时返回 None（修 T2：超时是失败，不是崩溃）。"""
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    return None
                try:
                    msg = await ws.receive(timeout=left)
                except (asyncio.TimeoutError, TimeoutError):
                    return None
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                    aiohttp.WSMsgType.ERROR):
                        return None
                    continue
                obj = json.loads(msg.data)
                if obj.get("type") == "frame":
                    frames.append(obj)
                if pred(obj):
                    return obj

        async def expect_no_frame(seconds: float) -> dict | None:
            """在 seconds 内不应出现任何 frame；出现则返回它（断言失败证据）。"""
            deadline = time.time() + seconds
            while True:
                left = deadline - time.time()
                if left <= 0:
                    return None
                try:
                    msg = await ws.receive(timeout=left)
                except (asyncio.TimeoutError, TimeoutError):
                    return None
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                obj = json.loads(msg.data)
                if obj.get("type") == "frame":
                    frames.append(obj)
                    return obj

        def is_frame(state):
            return lambda o: o.get("type") == "frame" and o["state"] == state

        # ---------------- 1. 配对 ----------------
        await ws.send_json({"type": "pair", "code": pair_code(), "deviceName": "e2e-sim"})
        pair_ok = await recv_until(lambda o: o.get("type") == "pair_ok", 15)
        if not check("配对成功（pair_ok 含双 token）",
                     bool(pair_ok) and bool(pair_ok.get("accessToken"))
                     and bool(pair_ok.get("refreshToken"))):
            return

        # ---------------- 2. resume 帧 ----------------
        hello = await recv_until(lambda o: o.get("type") == "hello_ok", 15)
        check("hello_ok 携带 resume 现场帧",
              bool(hello) and hello.get("resume", {}).get("type") == "frame")

        # ---------------- 2.5 遥测上行通路（协议 v1.1）----------------
        # 网关在 hello_ok 之后立刻拉一次遥测，插件（这里是本脚本）必须回执。
        cmd = await recv_until(lambda o: o.get("type") == "cmd" and o.get("cmd") == "telemetry", 8)
        check("hello 后网关主动拉一次遥测（cmd）", cmd is not None and bool(cmd.get("id")),
              f'id={cmd.get("id")}' if cmd else "8s 内未收到 telemetry 命令")
        if cmd:
            await ws.send_json({"type": "cmd_result", "id": cmd["id"], "ok": True, "data": {
                "model": "g2", "sn": "E2E-SN-7788", "isGlasses": True,
                "connectType": "connected", "connected": True,
                "batteryLevel": 64, "isCharging": False, "isWearing": True, "isInCase": False,
            }})
        # 主动上报（真实场景是 onDeviceStatusChanged 触发）
        await ws.send_json({"type": "telemetry", "data": {
            "model": "g2", "sn": "E2E-SN-7788", "isGlasses": True,
            "connectType": "connected", "connected": True,
            "batteryLevel": 58, "isCharging": True, "isWearing": True, "isInCase": False,
        }})
        # 戒指的状态：**必须**被网关整条拒收，否则 41% 会被当成眼镜电量
        await ws.send_json({"type": "telemetry", "data": {
            "model": "ring1", "sn": "E2E-RING-0002", "isGlasses": False,
            "connectType": "connected", "connected": True, "batteryLevel": 41,
        }})
        await asyncio.sleep(0.4)
        live = admin_live(pair_ok["deviceId"])
        tel = (live or {}).get("telemetry") or {}
        check("遥测已到网关且是主动上报的那条", tel.get("batteryLevel") == 58 and tel.get("source") == "push",
              json.dumps(tel, ensure_ascii=False)[:80])
        check("遥测带 sampled_at / age_ms / stale 三件套",
              all(k in tel for k in ("sampled_at", "age_ms", "stale")) and tel.get("stale") is False,
              f'age={tel.get("age_ms")}ms stale={tel.get("stale")}')
        check("★ 戒指遥测被拒收（41% 没有变成眼镜电量）",
              tel.get("batteryLevel") == 58
              and (live or {}).get("telemetry_diagnostics", {}).get("rejected", {}).get("not_glasses") == 1,
              json.dumps((live or {}).get("telemetry_diagnostics", {}), ensure_ascii=False))
        check("SN 出网关只留后 4 位", tel.get("sn") == "…7788", str(tel.get("sn")))

        # ---------------- 3. PTT + 实时灌入 PCM（100ms/块，1x 速度）----------------
        await ws.send_json({"type": "ptt", "action": "start"})
        s2 = await recv_until(is_frame("S2"), 10)
        check("PTT 后立即进入 S2 聆听（免节流）", s2 is not None,
              "" if s2 else "10s 内未收到 S2 帧")

        chunk = 3200  # 100ms
        for i in range(0, len(pcm), chunk):
            await ws.send_bytes(pcm[i:i + chunk])
            await asyncio.sleep(0.1)
        t_speech_end = time.time()
        await ws.send_json({"type": "ptt", "action": "stop"})

        # ---------------- 4. 转写确认帧（S3 带 final 文本）----------------
        s3 = await recv_until(
            lambda o: o.get("type") == "frame" and o["state"] == "S3" and o["containers"]["body"], 30)
        s3_lag = time.time() - t_speech_end
        if not check("松手→final 转写上屏（<8s）",
                     s3 is not None and s3_lag < 8,
                     (f'{s3_lag:.1f}s: ' + s3["containers"]["body"][:30]) if s3
                     else f"{s3_lag:.1f}s 内未收到带正文的 S3 帧"):
            if s3 is None:
                return
        check("转写文本命中关键词「畅通」",
              bool(s3) and "畅通" in s3["containers"]["body"])

        # ---------------- 5. 思考帧 → 回复帧 ----------------
        s4 = await recv_until(is_frame("S4"), 15)
        check("进入 S4 思考（含状态条计秒）", s4 is not None,
              s4["containers"]["status"] if s4 else "15s 内未收到 S4 帧")

        s7 = await recv_until(is_frame("S7"), 120)
        if not check("收到 agent 最终回复 S7", s7 is not None,
                     f"说完→完成 {time.time() - t_speech_end:.1f}s" if s7
                     else "120s 内未收到 S7 帧"):
            return
        check("回复含问题回显（agent 确实读到了转写文本）",
              "畅通" in s7["containers"]["body"] or "畅通" in json.dumps(frames, ensure_ascii=False),
              s7["containers"]["body"][:40])

        # ---------------- 6. 帧约束校验 ----------------
        seqs = [f["seq"] for f in frames]
        check("seq 严格单调递增", len(seqs) > 1 and all(b > a for a, b in zip(seqs, seqs[1:])),
              f"{len(seqs)} 帧")

        wide = [(ln, line_width(ln)) for f in frames
                for ln in f["containers"]["body"].split("\n") if line_width(ln) > BUDGET_PX]
        check(f"正文每行 ≤ {BUDGET_PX}px（固件字形度量）", not wide,
              f"{wide[0][0]!r} 宽{wide[0][1]}px" if wide else "")

        tall = [(f["seq"], n) for f in frames
                if (n := len(f["containers"]["body"].split("\n"))) > MAX_LINES]
        check(f"正文每帧 ≤ {MAX_LINES} 行（216px / 27px 行高）", not tall,
              f"seq={tall[0][0]} 有 {tall[0][1]} 行" if tall else "")

        # 下发到眼镜的每一个字符都必须真的能被 G2 画出来（字库外字符固件会静默丢弃）
        bad_glyph = [(f["seq"], k, missing_codepoints(v))
                     for f in frames for k, v in f["containers"].items()
                     if missing_codepoints(v)]
        check("所有下发字符都在 G2 字库内", not bad_glyph,
              f"seq={bad_glyph[0][0]} {bad_glyph[0][1]} 含 "
              + " ".join(f"U+{c:04X}({chr(c)})" for c in bad_glyph[0][2]) if bad_glyph else "")

        check("帧 containers 结构恒定",
              all(set(f["containers"]) == {"status", "body", "foot"} for f in frames))

        # ---------------- 7. 翻页（修 T3：原来只发了 reset，从没测过翻页）----------------
        # S7 落在末页（_on_reply_text 跟随模式 _page = total-1），因此先 prev 再 next。
        total = int(s7["containers"]["foot"].split("/")[-1].split()[0]) if "/" in s7["containers"]["foot"] else 1
        if check("回复分页 >1（翻页可测）", total > 1, f"共 {total} 页"):
            page_last = s7["containers"]["body"]

            # 页脚形如「‹ 2/3 ›」：箭头只在**那个方向真的还有页**时出现
            prev_g, next_g = GLYPHS["page_prev"], GLYPHS["page_next"]

            await ws.send_json({"type": "page", "dir": "prev"})
            p1 = await recv_until(is_frame("S7"), 8)
            foot1 = p1["containers"]["foot"] if p1 else ""
            check("翻页 prev：页码递减且正文变化",
                  p1 is not None and f"{total - 1}/{total}" in foot1
                  and p1["containers"]["body"] != page_last,
                  foot1 or "8s 内未收到翻页帧")
            check("页脚箭头指向可翻方向（在首页则无 ‹）",
                  p1 is not None and next_g in foot1 and (prev_g in foot1) == (total > 2),
                  foot1)

            await ws.send_json({"type": "page", "dir": "next"})
            p2 = await recv_until(is_frame("S7"), 8)
            foot2 = p2["containers"]["foot"] if p2 else ""
            check("翻页 next：回到末页且正文还原",
                  p2 is not None and f"{total}/{total}" in foot2
                  and p2["containers"]["body"] == page_last,
                  foot2 or "8s 内未收到翻页帧")
            check("末页无 › 箭头", p2 is not None and next_g not in foot2 and prev_g in foot2, foot2)

            # 边界：已在末页再 next 应当不产生任何帧（_turn_page 的 new == _page 早返回）
            await ws.send_json({"type": "page", "dir": "next"})
            stray = await expect_no_frame(2.0)
            check("边界：末页再 next 不产生冗余帧", stray is None,
                  f"意外帧 seq={stray['seq']}" if stray else "")

        # ---------------- 8. 断线重连：重放现场 ----------------
        last_seq = frames[-1]["seq"]
        await ws.close()
        ws = await http.ws_connect(f"ws://127.0.0.1:{PORT}/ws")
        await ws.send_json({"type": "refresh", "refreshToken": pair_ok["refreshToken"]})
        robj = await recv_until(lambda o: o.get("type") in ("refresh_ok", "error"), 10)
        if not check("refreshToken 换发新 access", bool(robj) and robj.get("type") == "refresh_ok",
                     "" if robj else "10s 内无应答"):
            return

        await ws.send_json({"type": "hello", "token": robj["accessToken"]})
        hobj = await recv_until(lambda o: o.get("type") in ("hello_ok", "error"), 10)
        resume = (hobj or {}).get("resume", {})
        check("重连后 1 帧恢复现场（resume = 断前画面）",
              bool(hobj) and resume.get("state") in ("S7", "S0") and resume.get("seq", 0) >= last_seq,
              f"state={resume.get('state')} seq={resume.get('seq')} (断前 {last_seq})")

        # ---------------- 9. 清屏 ----------------
        await ws.send_json({"type": "reset"})
        s0 = await recv_until(is_frame("S0"), 8)
        check("reset → 回 S0 待机", s0 is not None,
              s0["containers"]["status"] if s0 else "8s 内未回 S0")
        await ws.close()

        # ---------------- 10. 断线后遥测仍可读，且如实标 stale ----------------
        await asyncio.sleep(0.4)
        live = admin_live(pair_ok["deviceId"])
        tel = (live or {}).get("telemetry") or {}
        check("断线后遥测仍返回最后已知值（不是报错、也不是清空）",
              (live or {}).get("online") is False and tel.get("batteryLevel") in (58, 64),
              f'online={(live or {}).get("online")} battery={tel.get("batteryLevel")}')


def _spawn_agent_fixture(state_dir: str) -> tuple[subprocess.Popen | None, str, str]:
    """默认拉起 demo/fake_openclaw.py 作为 agent 测试夹具；返回 (进程, url, 鉴权配置路径)。"""
    ext_url = os.environ.get("LENS_E2E_AGENT_URL")
    ext_cfg = os.environ.get("LENS_E2E_AGENT_CONFIG")
    if ext_url and ext_cfg:
        print(f"agent: 外部真实 agent {ext_url}")
        return None, ext_url, str(Path(ext_cfg).expanduser())

    port = _free_port()
    auth = Path(state_dir) / "agent-auth.json"
    auth.write_text(json.dumps({"gateway": {"auth": {"token": "e2e-fixture-token"}}}))
    log_file = open(Path(state_dir) / "agent.log", "w")
    proc = subprocess.Popen(
        [sys.executable, str(AGENT_FIXTURE), "--port", str(port), "--think", "1.5"],
        stdout=log_file, stderr=subprocess.STDOUT, cwd=str(REPO))
    if not wait_port(port):
        proc.kill()
        raise TimeoutError(f"agent 夹具未在 {port} 就绪，见 {state_dir}/agent.log")
    print(f"agent: 测试夹具 demo/fake_openclaw.py @ ws://127.0.0.1:{port}")
    return proc, f"ws://127.0.0.1:{port}", str(auth)


async def main() -> None:
    state_dir = tempfile.mkdtemp(prefix="lens-e2e-")
    agent_proc, agent_url, agent_cfg = _spawn_agent_fixture(state_dir)

    # 修 T1：把 agent 端点写进配置，端到端不再依赖仓库外的服务。
    (Path(state_dir) / "config.json").write_text(json.dumps({
        "port": PORT,
        "host": "127.0.0.1",
        "openclaw": {"url": agent_url, "config_path": agent_cfg},
    }))
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
        for p in (proc, agent_proc):
            if p is None:
                continue
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)

    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n=== e2e 结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过 ===")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
