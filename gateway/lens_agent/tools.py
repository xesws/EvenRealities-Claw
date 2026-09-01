"""工具注册表。

准入标准（AGENT-LAYER §9.1）：**一句话能问、一屏能答、两秒内能返。**
这条标准同时排掉了绝大多数危险工具 —— 需要二次确认的操作天然违反"一屏能答"。

闸 1：能力枚举里**根本没有 exec 这一档**。没有 shell、没有任意文件读写、
没有代码执行、没有任意网络请求。新工具若无法归入 READ / WRITE 两类，
说明它不该出现在一副眼镜的 agent 里。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable


class Capability(str, Enum):
    READ = "read"      # 无副作用
    WRITE = "write"    # 有副作用，且必须绑定到具体资源（闸 3）


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    capability: Capability
    budget_ms: int
    parameters: dict
    handler: Callable[[dict], Awaitable[str]]
    label: str = ""          # 上屏用的短词（S5 工具态），≤4 字
    #: 闸 3：这个工具被允许改动的资源，写死在代码里。
    #:
    #: 这里曾经**只是一句注释** —— 设计文档和交付报告都把「WRITE 必须绑定到具体
    #: 资源」当作四道闸之一在宣称，而代码里一行实现都没有。当时没有任何 WRITE 工具，
    #: 所以没出事；但那意味着第一个 WRITE 工具可以毫无阻力地带着"模型给什么路径就
    #: 写什么路径"进来，而四道闸的说法会继续成立地写在报告里。
    #:
    #: 现在它是构造期强制的：WRITE 工具必须声明非空 `resources`，READ 必须为空。
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.capability is Capability.WRITE and not self.resources:
            raise ValueError(
                f"工具 {self.name!r} 声明了写能力却没有绑定资源（闸 3）。"
                f"写能力必须钉在代码里写死的资源上，不能由模型给。")
        if self.capability is Capability.READ and self.resources:
            raise ValueError(f"工具 {self.name!r} 是只读的，不该绑定可写资源")

    def schema(self) -> dict:
        """OpenAI function-calling 格式。"""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    ok: bool
    elapsed_ms: int

    def as_message(self) -> dict:
        return {"role": "tool", "tool_call_id": self.call_id, "content": self.content}


# ---------------------------------------------------------------- 第一批工具

#: agent 进程的语言。工具的 label 会**上屏**（S5 工具态），description 和返回值
#: 会进 prompt —— 三者都得跟着走，否则英文模式下屏幕上会冒出一个中文词。
LOCALE = os.environ.get("LENS_AGENT_LOCALE", "zh")


def _t(zh: str, en: str) -> str:
    """按 locale 二选一。写成函数而不是字典字面量，是为了让调用处一眼看出配对关系。"""
    return en if LOCALE == "en" else zh


WEEKDAYS = "一二三四五六日"


async def _now(_args: dict) -> str:
    t = datetime.now()
    if LOCALE == "en":
        return t.strftime("%A, %B %-d, %Y, %-I:%M %p")
    return (f"{t.year}年{t.month}月{t.day}日 星期{WEEKDAYS[t.weekday()]} "
            f"{t.hour:02d}:{t.minute:02d}")


NOW = Tool(
    name="now",
    description=_t("获取当前的本地日期、星期与时间。问到「今天几号」「现在几点」「星期几」时用它。",
                   "Current local date, weekday and time. Use it for any question "
                   "about what day or what time it is."),
    capability=Capability.READ,
    budget_ms=50,
    parameters={"type": "object", "properties": {}},
    handler=_now,
    label=_t("查时间", "Time"),
)

# ---------------------------------------------------------------- 天气

#: WMO weather code → 一个短词。眼镜上没有图标，只有字，所以这里就是全部的表达力。
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}

#: ★ 闸 1 的具体形态：**这两个 URL 写死在代码里**。
#:
#: 模型能给的只有一个城市名，它进的是 query string 的值，进不了 host、
#: 进不了路径、也换不掉协议。这不是"任意网络请求"这一档能力 —— agent 拿不到
#: 那一档，能力枚举里根本没有。加任何新的联网工具都必须照这个形状写：
#: 端点是常量，模型只填参数。
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

#: 用户没说地方时用哪儿。眼镜知道你的常驻城市是合理的，猜一个城市不是。
DEFAULT_CITY = os.environ.get("LENS_HOME_CITY", "San Francisco")


def _c(v: Any) -> str:
    """摄氏度取整成一个短到能上屏的数。"""
    try:
        return f"{round(float(v))}°C"
    except (TypeError, ValueError):
        return "?"


#: 城市名 → 坐标。**预填了一批常见城市**，理由是实测出来的：geocoding 端点
#: 慢且不稳（冷启动实测 2.2s 直接顶穿预算，而 forecast 端点只要 0.24s）。
#: 一次工具调用要串两个 HTTP，慢的那个又恰好是最不必要的 —— 地名的坐标不会变。
#:
#: 表里的坐标是从官方 geocoding API 真拉下来的，不是手写的近似值；表外的地名
#: 照常走 API 查。
_GEO_CACHE: dict[str, dict] = {
    'san francisco': {"name": 'San Francisco', "country": 'United States', "latitude": 37.77493, "longitude": -122.41942},
    'new york': {"name": 'New York', "country": 'United States', "latitude": 40.71427, "longitude": -74.00597},
    'london': {"name": 'London', "country": 'United Kingdom', "latitude": 51.50853, "longitude": -0.12574},
    'tokyo': {"name": 'Tokyo', "country": 'Japan', "latitude": 35.6895, "longitude": 139.69171},
    'paris': {"name": 'Paris', "country": 'France', "latitude": 48.85341, "longitude": 2.3488},
    'berlin': {"name": 'Berlin', "country": 'Germany', "latitude": 52.52437, "longitude": 13.41053},
    'beijing': {"name": 'Beijing', "country": 'China', "latitude": 39.9075, "longitude": 116.39723},
    'shanghai': {"name": 'Shanghai', "country": 'China', "latitude": 31.22222, "longitude": 121.45806},
    'hong kong': {"name": 'Hong Kong', "country": '', "latitude": 22.27832, "longitude": 114.17469},
    'singapore': {"name": 'Singapore', "country": 'Singapore', "latitude": 1.28967, "longitude": 103.85007},
    'sydney': {"name": 'Sydney', "country": 'Australia', "latitude": -33.86785, "longitude": 151.20732},
    'toronto': {"name": 'Toronto', "country": 'Canada', "latitude": 43.70643, "longitude": -79.39864},
    'seattle': {"name": 'Seattle', "country": 'United States', "latitude": 47.60621, "longitude": -122.33207},
    'austin': {"name": 'Austin', "country": 'United States', "latitude": 30.26715, "longitude": -97.74306},
    'los angeles': {"name": 'Los Angeles', "country": 'United States', "latitude": 34.05223, "longitude": -118.24368},
    'chicago': {"name": 'Chicago', "country": 'United States', "latitude": 41.85003, "longitude": -87.65005},
    'boston': {"name": 'Boston', "country": 'United States', "latitude": 42.35843, "longitude": -71.05977},
    'seoul': {"name": 'Seoul', "country": 'South Korea', "latitude": 37.566, "longitude": 126.9784},
    'amsterdam': {"name": 'Amsterdam', "country": 'The Netherlands', "latitude": 52.37403, "longitude": 4.88969},
    'zurich': {"name": 'Zurich', "country": 'Switzerland', "latitude": 47.36667, "longitude": 8.55},
}

#: 复用连接。实测这一条比什么都值：每次新建 ClientSession 要付 DNS + TLS 握手，
#: 单次请求 1.27s；复用之后第二次起只要 0.3~0.5s。工具的准入标准是"两秒内能返"，
#: 不复用就等于每次都贴着预算上限跑。
_HTTP: "aiohttp.ClientSession | None" = None


def _http() -> "aiohttp.ClientSession":
    import aiohttp
    global _HTTP
    if _HTTP is None or _HTTP.closed:
        _HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.8))
    return _HTTP


async def close_http() -> None:
    """进程退出时收掉长连接。没有它 aiohttp 会在 stderr 上抱怨 session 未关闭。"""
    global _HTTP
    if _HTTP is not None and not _HTTP.closed:
        await _HTTP.close()
    _HTTP = None


async def _weather(args: dict) -> str:
    place = str(args.get("location") or "").strip() or DEFAULT_CITY
    http = _http()

    spot = _GEO_CACHE.get(place.casefold())
    if spot is None:
        try:
            async with http.get(_GEOCODE_URL, params={
                    "name": place, "count": 1, "language": "en", "format": "json"}) as r:
                hits = (await r.json()).get("results") or []
        except (asyncio.TimeoutError, OSError):
            # 慢的是查地名那一步。别把整轮拖垮 —— 告诉模型这一次查不到，
            # 让它说"没查到"，而不是让它拿着一个空结果自己编一个天气出来。
            return (f"Could not look up {place!r} in time. Tell the user the lookup "
                    f"failed; do not guess the weather.")
        if not hits:
            # 让模型看到"没找到"而不是一个空结果 —— 否则它会自己编一个天气出来。
            return f"No place named {place!r} was found. Ask the user to name a city."
        spot = hits[0]
        _GEO_CACHE[place.casefold()] = spot

    async with http.get(_FORECAST_URL, params={
            "latitude": spot["latitude"], "longitude": spot["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": 1}) as r:
        fc = await r.json()

    cur, day = fc.get("current") or {}, fc.get("daily") or {}

    def first(key: str) -> Any:
        return (day.get(key) or [None])[0]

    where = ", ".join(x for x in (spot.get("name"), spot.get("country")) if x)
    return (
        f"{where}. Now {_c(cur.get('temperature_2m'))}, "
        f"feels like {_c(cur.get('apparent_temperature'))}, "
        f"{_WMO.get(cur.get('weather_code'), 'unknown conditions')}, "
        f"wind {round(float(cur.get('wind_speed_10m') or 0))} km/h. "
        f"Today {_c(first('temperature_2m_min'))} to {_c(first('temperature_2m_max'))}, "
        f"{first('precipitation_probability_max')}% chance of precipitation.")


WEATHER = Tool(
    name="weather",
    description=_t("查一个地方的实时天气与当天温度区间。问到天气、气温、下雨、"
                   "穿什么时用它。不传 location 就用用户的常驻城市。",
                   "Current conditions and today's range for a place. Use it for any "
                   "question about weather, temperature, rain, or what to wear. "
                   "Omit `location` to use the user's home city."),
    capability=Capability.READ,
    # 准入标准是"两秒内能返"（AGENT-LAYER §9.1），这个工具也不例外。实测：
    # 冷启动 1041ms（含 TLS 握手），之后 ~220ms。下面 `_http()` 的 total=1.8s
    # **必须小于这个预算** —— 否则 budget 先触发，工具自己那句友好的失败提示
    # 永远没机会返回，模型看到的只会是"超时"两个字。
    budget_ms=2000,
    parameters={"type": "object", "properties": {
        "location": {"type": "string",
                     "description": "City name, e.g. 'Tokyo'. Omit for the home city."}}},
    handler=_weather,
    label=_t("天气", "Weather"),
)

REGISTRY: dict[str, Tool] = {t.name: t for t in (NOW, WEATHER)}


class ToolError(RuntimeError):
    pass


async def invoke(name: str, call_id: str, arguments: str, *,
                 deadline: float | None = None) -> ToolResult:
    """执行一次工具调用。**不做授权** —— 授权是 `policy.check` 的事，只有那一处。

    参数解析失败不抛给 loop，而是作为工具结果回给模型：让它自己纠正一次参数，
    比让整轮对话直接失败对用户友好得多。
    """
    tool = REGISTRY.get(name)
    if tool is None:
        raise ToolError(f"未注册的工具：{name}")
    started = time.monotonic()
    try:
        args = json.loads(arguments) if arguments.strip() else {}
        if not isinstance(args, dict):
            raise ValueError("参数必须是一个 JSON 对象")
    except Exception as exc:
        return ToolResult(call_id, name, f"参数解析失败：{exc}", False,
                          int((time.monotonic() - started) * 1000))

    budget = tool.budget_ms / 1000
    if deadline is not None:
        budget = min(budget, max(0.05, deadline - time.monotonic()))
    try:
        content = await asyncio.wait_for(tool.handler(args), timeout=budget)
        ok = True
    except asyncio.TimeoutError:
        content, ok = f"{tool.name} 超时（预算 {tool.budget_ms}ms）", False
    except Exception as exc:                       # 工具坏了不该炸掉整轮对话
        content, ok = f"{tool.name} 执行失败：{str(exc)[:120]}", False
    return ToolResult(call_id, name, content, ok,
                      int((time.monotonic() - started) * 1000))


def schemas(names: tuple[str, ...]) -> list[dict]:
    return [REGISTRY[n].schema() for n in names if n in REGISTRY]


def label_of(name: str) -> str:
    tool = REGISTRY.get(name)
    return (tool.label or tool.name) if tool else name


def capability_of(name: str) -> Capability | None:
    tool = REGISTRY.get(name)
    return tool.capability if tool else None


def describe() -> list[dict[str, Any]]:
    """给 /healthz 之类的自证接口用：现在到底装了哪些工具、各是什么能力。"""
    return [{"name": t.name, "capability": t.capability.value,
             "budget_ms": t.budget_ms, "label": t.label} for t in REGISTRY.values()]
