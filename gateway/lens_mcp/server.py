"""Even Realities G2 眼镜的 MCP 表面。

把一副物理眼镜的能力（HUD 渲染、分页、遥测）抽象成标准 MCP Tools，
让任何厂商的模型都能直接驱动它。**独立进程**：官方 `mcp` SDK 的 Streamable HTTP
是 ASGI/Starlette，而网关是 aiohttp；更重要的是，MCP 表面是面向外部厂商的攻击面，
不该与持有麦克风、ASR、设备凭证的网关同进程。两者之间只有一条控制面 HTTP。

## 三条必须写进工具描述里的事实

1. **"监控"只能是轮询。** MCP 2026-07-28 规范下服务器是无状态的，且**不能主动
   发起 JSON-RPC 请求** —— 做不到推送。所以每个读接口都返回 `as_of`，
   工具描述里明写"这是一次采样，不是订阅"。
2. **屏幕只有一块，写屏要租约。** 用户按下 PTT 的那一刻，屏幕无条件归语音链路，
   你的租约会被抢占。`glasses_events` 是唯一能让你发现这件事的途径。
3. **遥测有可能是缓存值。** 官方没说明 `getDeviceInfo()` 是否真的触发 BLE 读取，
   所以返回体里带 `source`（push=设备主动报告 / poll=我们问的）与 `stale`。
"""
from __future__ import annotations

import functools
import logging
import os

from mcp.server.mcpserver import MCPServer

from lens_gateway.formatting import DEFAULT_LAYOUT, Paginator, glyph_set, sanitize_report

from .client import ControlClient, ControlError

log = logging.getLogger(__name__)

SERVER_NAME = "even-glasses"
DEFAULT_HOLDER = "mcp"
DEFAULT_TTL_MS = 60_000

server: MCPServer = MCPServer(
    name=SERVER_NAME,
    title="Even Realities G2 智能眼镜",
    version="1.1.0",
    instructions=(
        "这台设备是一副 Even Realities G2 智能眼镜，屏幕 576×288 像素、单色绿、"
        "固定 27px 行高，正文区一屏 8 行。写屏前请先读 small-screen-style 提示。\n"
        "重要：屏幕只有一块，用户随时可能按下 PTT 说话并**无条件抢占**你的写屏租约；"
        "MCP 无法接收推送，请用 glasses_events 轮询是否被抢占。"
    ),
)

_client: ControlClient | None = None


def client() -> ControlClient:
    global _client
    if _client is None:
        _client = ControlClient()
    return _client


def _guard(fn):
    """把控制面的结构化错误变成工具返回体，而不是一句干巴巴的协议错误。

    必须用 `functools.wraps`：MCP SDK 会用 `inspect.signature` 反推工具的参数
    schema，裸的 `*args, **kwargs` 包装器会让它看到两个虚假参数
    （资源那边直接报 "handler declares parameters {args, kwargs}"）。
    `wraps` 设置 `__wrapped__`，`signature` 默认会跟过去拿到真实签名。
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ControlError as exc:
            return exc.as_result()
        except Exception as exc:                       # 网关没起来 / 网络断了
            log.exception("控制面调用失败")
            return {"ok": False, "error": {"code": "control_plane_unreachable",
                                           "message": str(exc)[:200]}}
    return wrapper


# ---------------------------------------------------------------- 1. 纯排版


@server.tool(
    title="按眼镜真实版式分页",
    description=(
        "把一段文本按 G2 眼镜的**真实像素版式**排版分页，返回每一页的整屏文本。"
        "不需要连接任何设备，是纯函数。\n\n"
        "注意这里没有「每行几个字」这类参数：G2 的字体是非等宽的，分页由容器的"
        "真实像素盒决定（正文区 576×216px，固定 27px 行高 ⇒ 每页 8 行）。"
        "字宽用的是官方 @evenrealities/pretext 的固件字形度量，与眼镜上的折行位置逐位一致。\n\n"
        "返回体里的 dropped_glyphs 是**会在真机上被静默丢弃**的字符"
        "（G2 字库外的字符不会显示豆腐块，而是什么都不画）——出现了就说明该换个写法。"
    ),
)
def textkit_paginate(text: str, container: str = "body") -> dict:
    box = {"body": DEFAULT_LAYOUT.body, "status": DEFAULT_LAYOUT.status,
           "foot": DEFAULT_LAYOUT.foot}.get(container)
    if box is None:
        return {"ok": False, "error": {"code": "bad_container",
                                       "message": 'container 只能是 body / status / foot'}}
    glyphs = glyph_set()
    p = Paginator(box=box, glyphs=glyphs)
    p.set_text(text)
    report = sanitize_report(text, glyphs=glyphs)
    return {
        "ok": True,
        "pages": [p.page_text(i) for i in range(p.total)],
        "total": p.total,
        "lines_per_page": box.max_lines,
        "inner_width_px": box.inner_width,
        "line_height_px": 27,
        # 这些字符会在真机上**什么都不画**（不是豆腐块），出现了就该换写法
        "dropped_glyphs": sorted({chr(cp) for cp in report.dropped_codepoints}),
        "sanitized": {
            "changed": report.changed,
            "summary": report.summary() or "无改动",
            "removed_control": report.removed_control,
            "removed_bidi": report.removed_bidi,
            "stripped_status_lines": report.stripped_status_lines,
        },
    }


# ---------------------------------------------------------------- 2. 设备列表


@server.tool(
    title="列出眼镜",
    description=(
        "列出这台网关上已配对的眼镜及其当前状态。**这是一次采样**（返回 as_of 时间戳），"
        "不是订阅——MCP 服务器无法主动推送。\n"
        "online=false 表示手机端插件此刻没连着；仍然可以写屏，画面会在重连时恢复。"
    ),
)
@_guard
async def glasses_list() -> dict:
    return {"ok": True, **await client().devices()}


# ---------------------------------------------------------------- 3. 写屏


@server.tool(
    title="在眼镜上显示文本",
    description=(
        "在眼镜 HUD 上显示一段文本，自动按真实版式分页（一页 8 行）。\n\n"
        "会先取得**写屏租约**再渲染：屏幕只有一块，同一时刻只允许一个持有者。"
        "若被别人持有，返回 LEASE_HELD 与持有者、剩余毫秒，请勿重试轰炸。\n"
        "返回的 lease_id 用于后续 hud_page / hud_clear；用户按下 PTT 说话会**无条件抢占**它，"
        "之后再用同一个 lease_id 会得到 LEASE_INVALID —— 这不是 bug，是设计。\n\n"
        "hold_ms 到点后屏幕回待机；不给则沿用阅读态的回落时长。"
    ),
)
@_guard
async def hud_show(device_id: str, text: str, title: str | None = None,
                   hold_ms: int | None = None, holder: str = DEFAULT_HOLDER,
                   ttl_ms: int = DEFAULT_TTL_MS) -> dict:
    lease = await client().acquire(device_id, holder, ttl_ms)
    result = await client().render(device_id, lease["lease_id"], text,
                                   title=title, hold_ms=hold_ms)
    return {**result, "lease_id": lease["lease_id"],
            "lease_expires_in_ms": lease["expires_in_ms"]}


@server.tool(
    title="翻页",
    description=(
        "翻动眼镜上正在显示的分页内容。需要 hud_show 返回的 lease_id。\n"
        "turned=false 表示已经在第一页或最后一页（不会发冗余帧），不是错误。"
    ),
)
@_guard
async def hud_page(device_id: str, lease_id: str, direction: str = "next") -> dict:
    return await client().page(device_id, lease_id, direction)


@server.tool(
    title="清屏",
    description="清空眼镜画面回到待机，并保留租约。用完请再调 hud_release 把屏幕还回去。",
)
@_guard
async def hud_clear(device_id: str, lease_id: str) -> dict:
    return await client().clear(device_id, lease_id)


@server.tool(
    title="释放写屏租约",
    description="主动交还屏幕控制权。不调也没关系——租约到期会自动失效——但主动释放更礼貌。",
)
@_guard
async def hud_release(device_id: str, lease_id: str) -> dict:
    return {"ok": True, **await client().release(device_id, lease_id)}


# ---------------------------------------------------------------- 4. 遥测与事件


@server.tool(
    title="读眼镜遥测",
    description=(
        "读取眼镜的电量、佩戴、连接状态。**一次采样，不是订阅**（返回 as_of / age_ms）。\n\n"
        "必看返回体里的三个字段：\n"
        "- available=false 表示这台设备从未上报过遥测，此时 telemetry 为 null —— "
        "不要臆造一个电量数字。\n"
        "- source=push 表示设备主动报告的状态变化（新鲜）；source=poll 表示网关问来的，"
        "手机端可能返回缓存值，**不保证是此刻的真实状态**。\n"
        "- stale=true 表示数据已超过新鲜窗口，只能当作「最后已知值」。"
    ),
)
@_guard
async def glasses_telemetry(device_id: str) -> dict:
    return {"ok": True, **await client().telemetry(device_id)}


@server.tool(
    title="轮询设备事件",
    description=(
        "取回自 after 之后发生的设备事件（租约取得 / 释放 / **被抢占**）。\n"
        "这是你发现「用户开口说话，屏幕已经不归你了」的唯一途径 —— "
        "MCP 服务器不能主动发起请求，做不到推送，只能由你轮询。\n"
        "把返回的 next 作为下次调用的 after。"
    ),
)
@_guard
async def glasses_events(device_id: str, after: int = 0) -> dict:
    return {"ok": True, **await client().events(device_id, after)}


# ---------------------------------------------------------------- 资源


@server.resource("glasses://devices", title="已配对的眼镜",
                 description="当前网关上所有已配对眼镜的快照", mime_type="application/json")
@_guard
async def resource_devices() -> dict:
    return await client().devices()


@server.resource("glasses://{device_id}/frame", title="眼镜当前画面",
                 description="这副眼镜此刻屏幕上的三个容器内容与页码", mime_type="application/json")
@_guard
async def resource_frame(device_id: str) -> dict:
    return await client().state(device_id)


@server.resource("glasses://{device_id}/telemetry", title="眼镜遥测",
                 description="电量/佩戴/连接，带 source 与 stale 标注", mime_type="application/json")
@_guard
async def resource_telemetry(device_id: str) -> dict:
    return await client().telemetry(device_id)


# ---------------------------------------------------------------- 提示


@server.prompt(name="small-screen-style", title="小屏写作风格",
               description="为 576×288、一页 8 行的眼镜 HUD 写作时应遵循的风格")
def small_screen_style() -> str:
    box = DEFAULT_LAYOUT.body
    per_line = box.inner_width // 20
    return (
        f"你正在为一副智能眼镜的 HUD 写字。屏幕正文区 {box.width}×{box.height} 像素，"
        f"固定 27px 行高 ⇒ 一页 {box.max_lines} 行，每行约 {per_line} 个汉字"
        f"（一页约 {box.max_lines * per_line} 字）。\n\n"
        "规则：\n"
        "1. 先结论后细节。用户瞥一眼就要拿到答案。\n"
        "2. 短句。不用 markdown、表格、代码块 —— 眼镜不渲染它们，只会显示成一堆符号。\n"
        "3. 列表用「一是…二是…」行文，不要用 - 或 1. 起头。\n"
        f"4. 非必要不超过 {box.max_lines * per_line * 2} 字（两页）。\n"
        "5. 只用常见汉字、数字和基本标点。字库外的字符在真机上**什么都不显示**"
        "（不是豆腐块，是直接消失），emoji、生僻符号一律避免。\n"
        "6. 不写时间戳、ID、模型名、token 数 —— 屏幕太小，这些都是噪音。"
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LENS_MCP_LOG", "INFO"))
    transport = os.environ.get("LENS_MCP_TRANSPORT", "streamable-http")
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http",
                   host=os.environ.get("LENS_MCP_HOST", "127.0.0.1"),
                   port=int(os.environ.get("LENS_MCP_PORT", "8765")))


if __name__ == "__main__":
    main()
