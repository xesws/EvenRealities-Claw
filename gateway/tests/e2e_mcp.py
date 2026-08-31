"""MCP 表面端到端：**四个真进程**，中间没有任何打桩。

    MCP 客户端（官方 mcp SDK）
        ↓ Streamable HTTP /mcp
    lens_mcp 服务器进程
        ↓ 控制面 HTTP（共享密钥 Bearer）
    lens_gateway 网关进程
        ↓ Lens 协议 v1.1 WebSocket
    设备端（本脚本扮演插件，收真实渲染帧）

这条链路就是「厂商的大模型直接驱动这副眼镜」时实际走的路。断言落在**最远端**：
MCP 工具调用之后，帧必须真的从设备 WebSocket 出来，内容与分页都对。

运行：PYTHONPATH=. .venv/bin/python tests/e2e_mcp.py
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
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession                                   # noqa: E402
from mcp.client.streamable_http import streamable_http_client   # noqa: E402

CHECKS: list[tuple[str, bool]] = []
LONG_TEXT = "".join(f"第{i}条：这是通过 MCP 下发到眼镜上的长文本，用来验证分页。" for i in range(20))


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))
    return bool(ok)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, headers: dict, timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True          # 有响应就算起来了（401 也说明在监听）
        except Exception:
            time.sleep(0.5)
    return False


def payload(result) -> dict:
    """CallToolResult → 工具返回的 dict。"""
    return json.loads(result.content[0].text)


async def run(gw_port: int, mcp_port: int, secret: str) -> None:
    base = f"http://127.0.0.1:{gw_port}"
    auth = {"Authorization": f"Bearer {secret}"}

    # ---------- 1. 扮演插件：配对并保持连接 ----------
    async with aiohttp.ClientSession() as http:
        req = urllib.request.Request(f"{base}/admin/pair-code", data=b"", method="POST",
                                     headers=auth)
        with urllib.request.urlopen(req, timeout=5) as r:
            code = json.loads(r.read())["code"]

        ws = await http.ws_connect(f"{base}/ws")
        await ws.send_json({"type": "pair", "code": code, "deviceName": "MCP-e2e 眼镜"})

        device_id = ""
        frames: list[dict] = []

        async def pump() -> None:
            """后台收帧，模拟插件。"""
            nonlocal device_id
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                obj = json.loads(msg.data)
                if obj.get("type") == "pair_ok":
                    device_id = obj["deviceId"]
                elif obj.get("type") == "cmd" and obj.get("cmd") == "telemetry":
                    await ws.send_json({"type": "cmd_result", "id": obj["id"], "ok": True, "data": {
                        "model": "g2", "sn": "MCP-E2E-4321", "isGlasses": True,
                        "connectType": "connected", "connected": True,
                        "batteryLevel": 73, "isCharging": False,
                        "isWearing": True, "isInCase": False}})
                elif obj.get("type") == "frame":
                    frames.append(obj)

        pump_task = asyncio.ensure_future(pump())
        for _ in range(100):
            if device_id:
                break
            await asyncio.sleep(0.05)
        if not check("插件配对成功（拿到 deviceId）", bool(device_id), device_id):
            return

        async def wait_frame(pred, timeout=5.0) -> dict | None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                for f in reversed(frames):
                    if pred(f):
                        return f
                await asyncio.sleep(0.05)
            return None

        # ---------- 2. 连上 MCP 服务器 ----------
        async with streamable_http_client(f"http://127.0.0.1:{mcp_port}/mcp") as (read, write):
            async with ClientSession(read, write) as mcp:
                init = await mcp.initialize()
                check("MCP initialize 成功（Streamable HTTP）",
                      init.server_info.name == "even-glasses", init.server_info.name)
                check("服务器 instructions 提示了「屏幕只有一块 / 会被抢占」",
                      "抢占" in (init.instructions or ""), (init.instructions or "")[:40])

                tools = {t.name for t in (await mcp.list_tools()).tools}
                check("八个工具全部注册", tools == {
                    "textkit_paginate", "glasses_list", "hud_show", "hud_page",
                    "hud_clear", "hud_release", "glasses_telemetry", "glasses_events"},
                      ", ".join(sorted(tools)))

                # ---------- 3. 纯排版工具（不依赖设备） ----------
                pg = payload(await mcp.call_tool("textkit_paginate", {"text": LONG_TEXT}))
                check("textkit_paginate 用真实像素版式分页",
                      pg["ok"] and pg["lines_per_page"] == 8 and pg["line_height_px"] == 27
                      and pg["total"] > 1,
                      f'{pg["total"]} 页 × {pg["lines_per_page"]} 行 / {pg["inner_width_px"]}px')
                miss = payload(await mcp.call_tool("textkit_paginate",
                                                   {"text": "完成 ✓ 电量 ⚡"}))
                check("字库外字符被如实报出（真机上它们什么都不画）",
                      set(miss["dropped_glyphs"]) == {"✓", "⚡"},
                      str(miss["dropped_glyphs"]))

                # ---------- 4. 列设备 ----------
                lst = payload(await mcp.call_tool("glasses_list", {}))
                dev = next((d for d in lst["devices"] if d["device_id"] == device_id), None)
                check("glasses_list 看到这台在线眼镜",
                      dev is not None and dev["online"] is True and "as_of" in lst,
                      json.dumps(dev, ensure_ascii=False)[:70] if dev else "没找到")

                # ---------- 5. ★ 写屏：帧必须真的从设备 WS 出来 ----------
                before = len(frames)
                show = payload(await mcp.call_tool(
                    "hud_show", {"device_id": device_id, "text": LONG_TEXT,
                                 "title": "检索结果", "holder": "e2e-甲"}))
                check("hud_show 取得租约并渲染",
                      show.get("ok") is True and show["page"]["total"] > 1,
                      f'{show.get("page")} lease={str(show.get("lease_id"))[:6]}…')
                lease_id = show.get("lease_id", "")

                got = await wait_frame(lambda f: f["state"] == "S9" and f["seq"] >= show["seq"])
                check("★ 帧真的送到了设备 WebSocket（不是只改了服务器状态）",
                      got is not None and "检索结果" in got["containers"]["status"],
                      f'seq={got["seq"]} status={got["containers"]["status"]}' if got else "5s 内无 S9 帧")
                check("设备收到的正文与 MCP 返回的一致",
                      got is not None and got["containers"]["body"] == show["containers"]["body"])
                check("页脚是真实页码", bool(got) and "/" in got["containers"]["foot"],
                      got["containers"]["foot"] if got else "")
                assert before <= len(frames)

                # ---------- 6. 第二个客户端必须冲突，而不是最后写入者赢 ----------
                second = payload(await mcp.call_tool(
                    "hud_show", {"device_id": device_id, "text": "乙想插队", "holder": "e2e-乙"}))
                check("★ 并发写屏返回结构化 LEASE_HELD（不是最后写入者赢）",
                      second.get("ok") is False and second["error"]["code"] == "LEASE_HELD"
                      and second["error"]["holder"] == "e2e-甲",
                      json.dumps(second.get("error", {}), ensure_ascii=False)[:80])
                still = frames[-1]
                check("被拒的写入没有污染屏幕", "乙想插队" not in still["containers"]["body"])

                # ---------- 7. 翻页 ----------
                turn = payload(await mcp.call_tool(
                    "hud_page", {"device_id": device_id, "lease_id": lease_id,
                                 "direction": "prev"}))
                check("hud_page 翻页生效", turn.get("turned") is True,
                      json.dumps(turn.get("page", {}), ensure_ascii=False))
                paged = await wait_frame(lambda f: f["seq"] == turn["seq"])
                check("翻页帧同样送达设备", paged is not None and paged["state"] == "S9",
                      f'seq={paged["seq"]}' if paged else "未收到")

                # ---------- 8. ★ 用户开口抢占，MCP 只能靠轮询发现 ----------
                await ws.send_json({"type": "ptt", "action": "start"})
                await wait_frame(lambda f: f["state"] == "S2")
                after_preempt = payload(await mcp.call_tool(
                    "hud_page", {"device_id": device_id, "lease_id": lease_id,
                                 "direction": "next"}))
                check("★ 用户开口后原租约立即失效",
                      after_preempt.get("ok") is False
                      and after_preempt["error"]["code"] == "LEASE_INVALID",
                      json.dumps(after_preempt.get("error", {}), ensure_ascii=False)[:60])

                ev = payload(await mcp.call_tool("glasses_events", {"device_id": device_id}))
                kinds = [e["kind"] for e in ev["events"]]
                check("抢占事件可被轮询到（MCP 收不到推送）",
                      "lease_preempted" in kinds
                      and ev["events"][-1].get("reason") == "local_render",
                      " → ".join(kinds))
                empty = payload(await mcp.call_tool(
                    "glasses_events", {"device_id": device_id, "after": ev["next"]}))
                check("事件游标是增量的", empty["events"] == [], f'next={ev["next"]}')
                await ws.send_json({"type": "ptt", "action": "cancel"})

                # ---------- 9. 遥测 ----------
                await ws.send_json({"type": "telemetry", "data": {
                    "model": "g2", "sn": "MCP-E2E-4321", "isGlasses": True,
                    "connectType": "connected", "connected": True,
                    "batteryLevel": 73, "isCharging": False,
                    "isWearing": True, "isInCase": False}})
                await asyncio.sleep(0.3)
                tel = payload(await mcp.call_tool("glasses_telemetry", {"device_id": device_id}))
                t = tel.get("telemetry") or {}
                check("glasses_telemetry 读到真实电量",
                      tel.get("available") is True and t.get("batteryLevel") == 73,
                      json.dumps(t, ensure_ascii=False)[:70])
                check("遥测带溯源三件套（source / age_ms / stale）",
                      t.get("source") == "push" and "age_ms" in t and t.get("stale") is False,
                      f'source={t.get("source")} age={t.get("age_ms")}ms')
                check("SN 出网关只留后 4 位", t.get("sn") == "…4321", str(t.get("sn")))

                unknown = payload(await mcp.call_tool("glasses_telemetry",
                                                      {"device_id": "dev_不存在"}))
                check("未知设备返回结构化错误而不是崩溃",
                      unknown.get("ok") is False
                      and unknown["error"]["code"] == "device_unknown")

                # ---------- 10. 资源与提示 ----------
                res = await mcp.read_resource("glasses://devices")
                data = json.loads(res.contents[0].text)
                check("资源 glasses://devices 可读",
                      any(d["device_id"] == device_id for d in data["devices"]))
                pr = await mcp.get_prompt("small-screen-style")
                ptext = pr.messages[0].content.text
                check("提示 small-screen-style 由真实版式推导",
                      "一页 8 行" in ptext and "不用 markdown" in ptext,
                      ptext.split("\n")[0][:50])

                # ---------- 11. 收尾：重新取租约并清屏 ----------
                again = payload(await mcp.call_tool(
                    "hud_show", {"device_id": device_id, "text": "收尾", "holder": "e2e-甲"}))
                check("抢占之后可以重新取得租约", again.get("ok") is True)
                cleared = payload(await mcp.call_tool(
                    "hud_clear", {"device_id": device_id, "lease_id": again["lease_id"]}))
                check("hud_clear 回到待机", cleared.get("state") == "S0")
                rel = payload(await mcp.call_tool(
                    "hud_release", {"device_id": device_id, "lease_id": again["lease_id"]}))
                check("hud_release 交还控制权", rel.get("released") is True)

        pump_task.cancel()
        await ws.close()


def main() -> int:
    gw_port, mcp_port = free_port(), free_port()
    state_dir = tempfile.mkdtemp(prefix="lens-mcp-e2e-")
    (Path(state_dir) / "config.json").write_text(json.dumps({
        "port": gw_port, "host": "127.0.0.1",
        # 本脚本不用 ASR / agent，指向一个不存在的地址即可（网关会重试但不阻塞）
        "openclaw": {"url": "ws://127.0.0.1:1", "config_path": "/dev/null"},
    }))
    env = {**os.environ, "LENS_STATE_DIR": state_dir, "PYTHONPATH": str(ROOT)}
    gw_log = open(Path(state_dir) / "gateway.log", "w")
    mcp_log = open(Path(state_dir) / "mcp.log", "w")
    print(f"日志目录: {state_dir}")

    gw = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "lens_gateway.main", "serve"],
                          env=env, cwd=str(ROOT), stdout=gw_log, stderr=subprocess.STDOUT)
    procs = [gw]
    try:
        # 网关一起来就有控制面（不等 ASR：本脚本不用它）
        if not wait_http(f"http://127.0.0.1:{gw_port}/healthz", {}, 60):
            print("网关未就绪，见 gateway.log")
            return 1
        secret = (Path(state_dir) / "control.secret").read_text().strip()

        mcp_env = {**env, "LENS_MCP_PORT": str(mcp_port),
                   "LENS_CONTROL_URL": f"http://127.0.0.1:{gw_port}",
                   "LENS_CONTROL_SECRET": secret,
                   "LENS_MCP_TRANSPORT": "streamable-http"}
        mp = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "lens_mcp"],
                              env=mcp_env, cwd=str(ROOT), stdout=mcp_log, stderr=subprocess.STDOUT)
        procs.append(mp)
        if not wait_http(f"http://127.0.0.1:{mcp_port}/mcp", {}, 30):
            print("MCP 服务器未就绪，见 mcp.log")
            return 1

        print("四进程链路已就绪，开始验收：")
        asyncio.run(run(gw_port, mcp_port, secret))
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        gw_log.close()
        mcp_log.close()

    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\n=== MCP 端到端结果：{passed}/{len(CHECKS)} 通过 ===")
    if passed != len(CHECKS):
        print("失败项：")
        for name, ok in CHECKS:
            if not ok:
                print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
