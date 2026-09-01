"""工具注册表。

准入标准（AGENT-LAYER §9.1）：**一句话能问、一屏能答、两秒内能返。**
这条标准同时排掉了绝大多数危险工具 —— 需要二次确认的操作天然违反"一屏能答"。

闸 1：能力枚举里**根本没有 exec 这一档**。没有 shell、没有任意文件读写、
没有代码执行、没有任意网络请求。新工具若无法归入 READ / WRITE 两类，
说明它不该出现在一副眼镜的 agent 里。
"""
from __future__ import annotations

import ast
import asyncio
import contextvars
import json
import math
import operator
import os
import pathlib
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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

# ---------------------------------------------------------------- 眼镜自身状态

#: 这一轮请求随身带来的眼镜遥测（电量、佩戴、连接）。
#:
#: 用 ContextVar 而不是模块级全局：agent 是一个进程服多副眼镜，每轮 `chat.send`
#: 跑在自己的 asyncio task 里，全局变量会让 A 的电量串到 B 的回答里。
#: ContextVar 在 task 之间天然隔离。
DEVICE_STATE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "device_state", default=None)


def _pct(v: Any) -> str | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return f"{n}%" if 0 <= n <= 100 else None


async def _device(_args: dict) -> str:
    st = DEVICE_STATE.get()
    if not st:
        # **不能返回一个编出来的默认值。**没有数据就说没有 —— 这个工具存在的
        # 全部意义就是把「我猜眼镜有 82% 电」换成「我确实读到了 41%」。
        return _t("现在拿不到眼镜的状态（可能刚连上，还没上报过遥测）。"
                  "如实告诉用户读不到，不要猜一个数。",
                  "No device telemetry is available right now (the glasses may have "
                  "just connected and not reported yet). Tell the user you cannot "
                  "read it; do not guess a number.")
    bits: list[str] = []
    batt = _pct(st.get("battery"))
    if batt:
        bits.append(_t(f"电量 {batt}", f"battery {batt}"))
    if st.get("charging"):
        bits.append(_t("正在充电", "charging"))
    worn = st.get("worn")
    if worn is not None:
        bits.append(_t("正在佩戴" if worn else "没有佩戴",
                       "being worn" if worn else "not being worn"))
    if not bits:
        return _t("遥测里没有电量或佩戴信息。如实说读不到。",
                  "The telemetry has no battery or wear information. Say you cannot read it.")
    age_s = int((st.get("age_ms") or 0) / 1000)
    freshness = (_t(f"这份读数是 {age_s} 秒前的", f"this reading is {age_s}s old")
                 if age_s >= 5 else _t("刚刚读到的", "read just now"))
    if st.get("stale"):
        freshness += _t("，已经过期，只能当作最后已知值",
                        ", and it is stale -- treat it as the last known value")
    return _t(f"眼镜：{'，'.join(bits)}（{freshness}）。",
              f"Glasses: {', '.join(bits)} ({freshness}).")


DEVICE = Tool(
    name="device",
    description=_t("读眼镜自己的状态：电量、是否在充电、是否戴着。"
                   "问到「我眼镜还有多少电」「充上电了吗」时用它，不要猜。",
                   "Read the state of the glasses themselves: battery level, whether "
                   "they are charging, whether they are being worn. Use it whenever "
                   "the user asks about their glasses; never guess."),
    capability=Capability.READ,
    budget_ms=50,
    parameters={"type": "object", "properties": {}},
    handler=_device,
    label=_t("眼镜", "Device"),
)

# ---------------------------------------------------------------- 日期差

async def _days_until(args: dict) -> str:
    """到某个日期还有几天。**一次调用就出答案** —— 这是它存在的全部理由。

    没有它的时候，「离圣诞还有几天」要走 now（今天几号）→ calc（做减法）→ 组织回答
    三次模型往返，稳定顶穿 6 秒预算；而模型还常常绕开 calc 自己逐月去数
    （"September has 30, October has 31…"），数到一半被掐断。
    """
    raw = " ".join(str(args.get("date") or "").split())
    if not raw:
        return _t("没有给日期", "no date given")
    today = datetime.now().date()
    norm = raw.replace("/", "-")
    parsed = None
    try:
        parsed = datetime.strptime(norm, "%Y-%m-%d").date()
    except ValueError:
        # 只给月日 ⇒ 按「下一次」算：8 月问圣诞是今年的，12 月 26 日问就是明年的。
        # 拼上年份再解析，而不是解析后 `replace(year=...)` —— 后者对 2-29 会先
        # 落到 1900 年（非闰年）直接报错，而且 Python 3.15 起无年份解析行为要变。
        # 往后找几年而不是一两年：2-29 的下一次可能在三年后，
        # 而「看不懂这个日期」对一个完全合法的日期是错的回答。
        for year in range(today.year, today.year + 5):
            try:
                got = datetime.strptime(f"{year}-{norm}", "%Y-%m-%d").date()
            except ValueError:
                continue
            if got >= today:
                parsed = got
                break
    if parsed is None:
        return _t(f"看不懂日期 {raw!r}，请给成 12-25 或 2026-12-25 这样的格式。",
                  f"Could not read the date {raw!r}. Use 12-25 or 2026-12-25.")
    days = (parsed - today).days
    when = parsed.strftime("%A, %B %-d, %Y") if LOCALE == "en" else \
        f"{parsed.year}年{parsed.month}月{parsed.day}日"
    if days == 0:
        return _t(f"{when} 就是今天。", f"{when} is today.")
    if days < 0:
        return _t(f"{when} 已经过去 {-days} 天了。", f"{when} was {-days} days ago.")
    return _t(f"从今天（{today}）到 {when} 还有 {days} 天。",
              f"{days} days from today ({today}) to {when}.")


DAYS_UNTIL = Tool(
    name="days_until",
    description=_t("算今天到某个日期还有多少天。问到「离X还有几天」「还有多久到」时用它，"
                   "不要自己数月份。只给月日（如 12-25）就按下一次那天算。",
                   "How many days from today until a date. Use it for any question "
                   "about how long until something; never count the months yourself. "
                   "Give just month-day (e.g. 12-25) for the next occurrence."),
    capability=Capability.READ,
    budget_ms=50,
    parameters={"type": "object", "properties": {
        "date": {"type": "string",
                 "description": "'12-25' for the next occurrence, or '2026-12-25'."}},
        "required": ["date"]},
    handler=_days_until,
    label=_t("日期", "Date"),
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

# ---------------------------------------------------------------- 算术

#: 允许的运算符。**没有 eval** —— 模型给的是一串它自己拼的表达式，
#: `eval` 会让一次提示注入直接变成任意代码执行，那是闸 1 明确排掉的那一档能力。
_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max,
          "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil}

#: 指数上限。`9**9**9` 在语法上完全合法，求值时会把进程挂住 —— 白名单挡不住
#: 这种「合法但代价爆炸」的表达式，得单独限。
_MAX_EXP = 64


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("只支持数字")
        return node.value
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持的运算符")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if op is operator.pow and (abs(right) > _MAX_EXP or abs(left) > 1e6):
            raise ValueError("指数太大")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNOPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持的运算符")
        return op(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError("不支持的函数")
        if node.keywords:
            raise ValueError("不支持关键字参数")
        return _FUNCS[node.func.id](*(_eval_node(a) for a in node.args))
    raise ValueError("表达式里有不允许的东西")


async def _calc(args: dict) -> str:
    expr = str(args.get("expression") or "").strip()
    if not expr:
        return _t("没有给表达式", "no expression given")
    if len(expr) > 200:
        return _t("表达式太长", "expression too long")
    try:
        tree = ast.parse(expr, mode="eval")
        value = _eval_node(tree.body)
    except ZeroDivisionError:
        return _t("除以零", "division by zero")
    except Exception as exc:
        return _t(f"算不了这个表达式：{exc}", f"cannot evaluate that expression: {exc}")
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return _t("结果不是一个有限的数", "the result is not a finite number")
        # 15 位有效数字之后是浮点噪音，报出去只会变成屏幕上的一串假精度
        shown = f"{value:.10g}"
    else:
        shown = str(value)
    return f"{expr} = {shown}"


CALC = Tool(
    name="calc",
    description=_t(
        "精确计算一个算术表达式。**任何算术都必须用它**，包括小费、折扣、分账、"
        "单位换算（自己写成表达式，例如华氏转摄氏写 (350-32)*5/9）、天数差。"
        "支持 + - * / // % ** 括号，以及 round/abs/min/max/sqrt/floor/ceil。",
        "Evaluate an arithmetic expression exactly. **Use it for every "
        "calculation**: tips, discounts, splitting a bill, unit conversion "
        "(write it as an expression yourself, e.g. Fahrenheit to Celsius is "
        "(350-32)*5/9), day counts. Supports + - * / // % ** parentheses and "
        "round/abs/min/max/sqrt/floor/ceil."),
    capability=Capability.READ,
    budget_ms=50,
    parameters={"type": "object", "properties": {
        "expression": {"type": "string",
                       "description": "e.g. '64 * 0.18' or 'round((350-32)*5/9)'"}},
        "required": ["expression"]},
    handler=_calc,
    label=_t("算", "Math"),
)

# ---------------------------------------------------------------- 汇率

#: 闸 1 的同一个形状：端点写死，模型只填 query 的值。
#: 欧洲央行的公开参考汇率，无需 key、无需账号。
_FX_URL = "https://api.frankfurter.dev/v1/latest"

_CURRENCY_ALIASES = {
    "dollar": "USD", "dollars": "USD", "usd": "USD", "美元": "USD", "美金": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR", "欧元": "EUR",
    "pound": "GBP", "pounds": "GBP", "gbp": "GBP", "英镑": "GBP",
    "yen": "JPY", "jpy": "JPY", "日元": "JPY", "日圆": "JPY",
    "yuan": "CNY", "rmb": "CNY", "cny": "CNY", "人民币": "CNY", "元": "CNY",
    "won": "KRW", "krw": "KRW", "韩元": "KRW",
    "franc": "CHF", "chf": "CHF", "瑞郎": "CHF", "瑞士法郎": "CHF",
}


def _code(raw: str) -> str:
    """把模型可能给的各种写法归一成 ISO 代码。"""
    v = (raw or "").strip()
    return _CURRENCY_ALIASES.get(v.casefold(), v.upper())


async def _currency(args: dict) -> str:
    src, dst = _code(str(args.get("from_currency") or "")), _code(str(args.get("to_currency") or ""))
    if len(src) != 3 or len(dst) != 3 or not src.isalpha() or not dst.isalpha():
        return ("Give from_currency and to_currency as three-letter ISO codes, "
                "e.g. USD, EUR, JPY.")
    try:
        amount = float(args.get("amount") or 1)
    except (TypeError, ValueError):
        amount = 1.0
    if src == dst:
        return f"{amount:g} {src} = {amount:g} {dst} (same currency)."
    try:
        async with _http().get(_FX_URL, params={
                "base": src, "symbols": dst, "amount": amount}) as r:
            data = await r.json()
    except (asyncio.TimeoutError, OSError):
        # 和天气工具同一条原则：让模型看见「查不到」，而不是拿着空结果自己编一个汇率
        return ("The exchange rate lookup timed out. Tell the user it failed; "
                "do not guess a rate.")
    rate = (data.get("rates") or {}).get(dst)
    if rate is None:
        return (f"No rate for {src} to {dst}. The reference set covers major "
                f"currencies only. Tell the user, do not guess.")
    return (f"{amount:g} {src} = {rate:.4g} {dst} "
            f"(European Central Bank reference rate, {data.get('date')}).")


CURRENCY = Tool(
    name="currency",
    description=_t("按欧洲央行参考汇率换算货币。问到汇率、换钱、某个价格折成别的货币时用它。",
                   "Convert money between currencies at the European Central Bank "
                   "reference rate. Use it for any question about exchange rates or "
                   "what a price is in another currency."),
    capability=Capability.READ,
    budget_ms=2000,
    parameters={"type": "object", "properties": {
        "from_currency": {"type": "string", "description": "ISO code, e.g. 'USD'"},
        "to_currency": {"type": "string", "description": "ISO code, e.g. 'EUR'"},
        "amount": {"type": "number", "description": "How much to convert. Default 1."}},
        "required": ["from_currency", "to_currency"]},
    handler=_currency,
    label=_t("汇率", "Rate"),
)

# ---------------------------------------------------------------- 提醒

#: ★ 闸 3 的第二个落点：提醒也只写这一个文件，路径写死。
REMINDERS_PATH = pathlib.Path(
    os.environ.get("LENS_AGENT_REMINDERS", "~/.lens-agent/reminders.json")).expanduser()

MAX_REMINDERS = 20
#: 上限 24 小时。一副眼镜不是日程表 —— 超过这个尺度的事应该进日历，
#: 而我们**没有**日历工具，所以这里必须拒绝而不是假装能记。
MAX_DELAY_SECONDS = 24 * 3600

#: 本轮新排的提醒。loop 在工具跑完后取走，交给网关去真的响 ——
#: **agent 不拥有屏幕**，它只能请求。用 ContextVar 的理由同 DEVICE_STATE。
PENDING_REMINDERS: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "pending_reminders", default=None)

#: 本轮是哪副眼镜在说话。提醒必须**连人一起记**：进程重启后从磁盘恢复时，
#: 没有它就不知道该把「面条好了」发给谁 —— 一个进程是服多副眼镜的。
SESSION_KEY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_key", default="default")


def _load_reminders(grace_seconds: float = 0.0) -> list[dict]:
    """读盘。默认只给还没到点的。

    `grace_seconds` 是给**恢复**用的：进程或连接断开的那段时间里到点的提醒，
    晚这么久以内仍然值得补发。默认 0 是给 `remind_list` 用的 —— 用户问
    「有什么提醒」时，已经响过的不该还在列表里。

    这个参数不是可有可无的装饰：没有它的时候 `restore()` 拿到的是过滤后的列表，
    **宽限期补发整个是空的** —— 断连期间到点的提醒永远不会响，而且没有任何症状，
    因为磁盘上那条也会在下次读盘时被这里悄悄丢掉。
    """
    try:
        data = json.loads(REMINDERS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    floor = time.time() - max(0.0, grace_seconds)
    return [r for r in data if isinstance(r, dict) and float(r.get("at", 0)) > floor]


def _save_reminders(rows: list[dict]) -> None:
    REMINDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REMINDERS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(REMINDERS_PATH)


def _when(at: float) -> str:
    """还有多久。**分钟以上不报秒** —— 刚设的「10 分钟后」立刻读回来会变成
    「9 分 59 秒后」，看着像它记错了。"""
    left = max(0, at - time.time())
    if left >= 3600:
        h, m = int(left // 3600), int(round(left % 3600 / 60))
        return f"in {h}h {m}m" if LOCALE == "en" else f"{h} 小时 {m} 分钟后"
    if left >= 60:
        m = int(round(left / 60))
        return f"in {m} min" if LOCALE == "en" else f"{m} 分钟后"
    sec = int(round(left))
    return f"in {sec}s" if LOCALE == "en" else f"{sec} 秒后"


#: `at` 只认 24 小时制的 `HH:MM`。刻意不做自然语言解析 —— 「明早九点半」
#: 那种话由模型翻成 `09:30`，翻不出来就该说不知道，而不是让这里去猜。
_CLOCK = re.compile(r"^\s*([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)\s*$")


def _minutes_until_clock(at: str, day: str | None) -> float | str:
    """把「几点」换算成「还有几分钟」。返回 str 表示这是一句给模型看的拒绝。

    `day` 省略时取**下一次**出现的那个钟点：现在 21:09 说「9 点」指的是明早，
    说「23 点」指的是今晚。这是人话的默认含义，把它交给模型去判反而多一个
    出错的地方 —— 而错的后果是提醒晚响 24 小时，用户第二天才发现。
    """
    m = _CLOCK.match(str(at))
    if not m:
        return _t(f"看不懂的时刻「{at}」，要 24 小时制的 HH:MM，比如 09:00、21:30。",
                  f"I cannot read the time '{at}'. Use 24-hour HH:MM, e.g. 09:00 or 21:30.")
    hour, minute = int(m.group(1)), int(m.group(2))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = (day or "").strip().lower()
    if day == "tomorrow":
        target += timedelta(days=1)
    elif day != "today" and target <= now:
        target += timedelta(days=1)      # 省略 day：取下一次出现的这个钟点
    return (target - now).total_seconds() / 60


async def _remind_set(args: dict) -> str:
    text = " ".join(str(args.get("text") or "").split())[:MAX_ITEM_CHARS]
    if not text:
        return _t("没有说要提醒什么", "no reminder text given")
    at = args.get("at")
    has_at = isinstance(at, str) and at.strip() != ""
    has_min = args.get("minutes") is not None
    if has_at and has_min:
        # 两个都给了就不猜。猜错的后果是提醒在错误的时间响，而用户在设的
        # 那一刻收到的是「好的」—— 错误要到几小时后才暴露。
        return _t("minutes 和 at 只能给一个。相对时间用 minutes，钟点用 at。",
                  "Give either minutes or at, not both. minutes for a delay, at for a clock time.")
    if not has_at and not has_min:
        return _t("得说什么时候：相对时间用 minutes（分钟），钟点用 at（HH:MM）。",
                  "Say when: minutes for a delay, at for a clock time (HH:MM).")
    if has_at:
        minutes = _minutes_until_clock(at, args.get("day"))
        if isinstance(minutes, str):
            return minutes
    else:
        try:
            minutes = float(args.get("minutes"))
        except (TypeError, ValueError):
            return _t("minutes 必须是数字（分钟）", "minutes must be a number of minutes")
    if minutes <= 0:
        return _t("时间必须是将来。", "The time must be in the future.")
    if minutes * 60 > MAX_DELAY_SECONDS:
        return _t("最多只能提醒 24 小时以内的事。更久的事请记进日历 —— 我没有日历工具。",
                  "I can only remind you within 24 hours. Anything further out "
                  "belongs in a calendar, and I have no calendar tool.")
    rows = _load_reminders()
    if len(rows) >= MAX_REMINDERS:
        return _t(f"待响的提醒已经有 {MAX_REMINDERS} 条了，先取消一条。",
                  f"There are already {MAX_REMINDERS} reminders pending. Cancel one first.")
    # id 必须真的唯一。原来用毫秒时间戳取模，同一秒里连排两条就会撞 ——
    # 而 `ReminderScheduler.schedule` 看到相同 id 会把前一条**取消**再排新的：
    # `remind_list` 说有两条，实际只会响一条，且没有任何报错。
    row = {"id": "rm_" + secrets.token_hex(5),
           "at": time.time() + minutes * 60, "text": text,
           "session": SESSION_KEY.get()}
    rows.append(row)
    _save_reminders(rows)
    # 排程请求交给上层 —— 真正到点响铃的是网关，因为屏幕是它的。
    queue = PENDING_REMINDERS.get()
    if queue is not None:
        queue.append(dict(row))
    return _t(f"好，{_when(row['at'])}提醒你：{text}",
              f"OK, I will remind you {_when(row['at'])}: {text}")


def _mine(rows: list[dict]) -> list[dict]:
    """只看本会话的。一个 agent 进程服多副眼镜，A 不该读到、更不该取消 B 的提醒。"""
    me = SESSION_KEY.get()
    return [r for r in rows if str(r.get("session") or "default") == me]


async def _remind_list(_args: dict) -> str:
    rows = sorted(_mine(_load_reminders()), key=lambda r: r["at"])
    if not rows:
        return _t("没有待响的提醒。", "There are no reminders pending.")
    body = "；".join(f"{_when(r['at'])}：{r['text']}" for r in rows) if LOCALE != "en" \
        else "; ".join(f"{r['text']} ({_when(r['at'])})" for r in rows)
    return _t(f"有 {len(rows)} 条待响的提醒：{body}",
              f"{len(rows)} reminders pending: {body}")


async def _remind_cancel(args: dict) -> str:
    want = " ".join(str(args.get("text") or "").split()).casefold()
    everyone = _load_reminders()
    rows = _mine(everyone)
    if not rows:
        return _t("没有待响的提醒。", "There are no reminders pending.")
    if not want:
        # 只有一条时「取消提醒」没有歧义；多条时必须问清楚，不能替用户猜。
        if len(rows) > 1:
            return _t("有好几条提醒，问用户要取消哪一条，先别取消。",
                      "There are several reminders. Ask the user which one; cancel none yet.")
        hits = rows
    else:
        hits = [r for r in rows if want in r["text"].casefold()]
    if not hits:
        return _t(f"没有和「{args.get('text')}」对得上的提醒。什么都没取消。",
                  f"No reminder matches that. Nothing was cancelled.")
    if len(hits) > 1:
        return _t("有好几条对得上：" + "、".join(r["text"] for r in hits) +
                  "。问用户要取消哪一条，先别取消。",
                  "Several match: " + ", ".join(r["text"] for r in hits) +
                  ". Ask the user which one; cancel none yet.")
    gone = hits[0]
    _save_reminders([r for r in everyone if r["id"] != gone["id"]])
    queue = PENDING_REMINDERS.get()
    if queue is not None:
        queue.append({"id": gone["id"], "cancel": True})
    return _t(f"已取消提醒：{gone['text']}", f"Cancelled the reminder: {gone['text']}")


REMIND_SET = Tool(
    name="remind_set",
    description=_t("提醒用户一件事：要么「若干分钟之后」（minutes），要么「几点钟」（at）。"
                   "「10 分钟后叫我」用 minutes=10；「明早九点提醒我看牙医」用 at=\"09:00\"。"
                   "**必须真的调用**，不能只是嘴上答应。分钟数可以是小数（0.5 就是 30 秒）。"
                   "只支持 24 小时以内。",
                   "Remind the user of something, either after a number of minutes "
                   "(minutes) or at a clock time (at). 'in 10 minutes' is minutes=10; "
                   "'tomorrow at 9, call the dentist' is at=\"09:00\". "
                   "**You must actually call it** -- never just promise. Minutes may be "
                   "fractional (0.5 = 30 seconds). Within 24 hours only."),
    capability=Capability.WRITE,
    budget_ms=200,
    parameters={"type": "object", "properties": {
        "minutes": {"type": "number",
                    "description": "How many minutes from now. Fractions are fine: "
                                   "0.5 is 30 seconds, 90 is an hour and a half."},
        "at": {"type": "string",
               "description": "A clock time in 24-hour HH:MM, e.g. '09:00' or '21:30'. "
                              "Use this instead of minutes when the user names a time "
                              "of day. Do not compute the delay yourself."},
        "day": {"type": "string", "enum": ["today", "tomorrow"],
                "description": "Only with at. Omit it and the next occurrence of that "
                               "clock time is used, which is what people usually mean."},
        "text": {"type": "string", "description": "What to say when it fires, short."}},
        "required": ["text"]},
    handler=_remind_set,
    label=_t("提醒", "Remind"),
    resources=(str(REMINDERS_PATH),),
)

REMIND_LIST = Tool(
    name="remind_list",
    description=_t("列出还没响的提醒。", "List the reminders that have not fired yet."),
    capability=Capability.READ,
    budget_ms=100,
    parameters={"type": "object", "properties": {}},
    handler=_remind_list,
    label=_t("提醒", "Remind"),
)

REMIND_CANCEL = Tool(
    name="remind_cancel",
    description=_t("取消一条还没响的提醒。", "Cancel a reminder that has not fired yet."),
    capability=Capability.WRITE,
    budget_ms=200,
    parameters={"type": "object", "properties": {
        "text": {"type": "string",
                 "description": "Part of the reminder text. Omit if there is only one."}}},
    handler=_remind_cancel,
    label=_t("提醒", "Remind"),
    resources=(str(REMINDERS_PATH),),
)

# ---------------------------------------------------------------- 清单

#: ★ 闸 3 的落点：**这是 agent 唯一被允许写的文件，路径写死在这里。**
#:
#: 模型能给的只有清单名和条目文本，它们进的是 JSON 的 key 和 value，
#: 进不了路径。写工具拿不到"任意文件写"那一档能力 —— 能力枚举里没有那一档。
LISTS_PATH = pathlib.Path(
    os.environ.get("LENS_AGENT_LISTS", "~/.lens-agent/lists.json")).expanduser()

#: 一条 12 个字以内、一屏放得下几十条。上限存在的理由是眼镜屏，不是磁盘。
MAX_ITEM_CHARS = 80
MAX_ITEMS = 50
MAX_LISTS = 12


def _list_key(raw: str) -> str:
    """清单名归一。空名归到 default，这样「帮我记一下」不必先起名字。"""
    v = " ".join(str(raw or "").split()).casefold()[:24]
    return v or _t("默认", "list")


def _load_lists() -> dict[str, list[str]]:
    try:
        data = json.loads(LISTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(i) for i in v][:MAX_ITEMS]
            for k, v in data.items() if isinstance(v, list)}


def _save_lists(data: dict[str, list[str]]) -> None:
    LISTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LISTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(LISTS_PATH)          # 原子替换：断电也不会留下半个 JSON


def _fmt_list(name: str, items: list[str]) -> str:
    if not items:
        return _t(f"清单「{name}」是空的。", f"The {name} list is empty.")
    body = "; ".join(f"{i+1}. {it}" for i, it in enumerate(items))
    return _t(f"清单「{name}」有 {len(items)} 条：{body}",
              f"The {name} list has {len(items)} items: {body}")


async def _list_show(args: dict) -> str:
    data = _load_lists()
    if not data:
        return _t("一条清单都还没有。", "There are no lists yet.")
    raw = str(args.get("list") or "").strip()
    # 只有一条清单时，不管模型给的是什么名字都用它。
    # 戴着眼镜的人说的是「我的清单上有啥」，不是「读 shopping 清单」——
    # 名字对不上就回一句「没有这个清单」，在这个场景里等于坏了。
    if len(data) == 1:
        name, items = next(iter(data.items()))
        return _fmt_list(name, items)
    if not raw:
        return _t("现有清单：" + "、".join(f"{k}（{len(v)} 条）" for k, v in data.items()),
                  "Lists: " + ", ".join(f"{k} ({len(v)})" for k, v in data.items()))
    name = _list_key(raw)
    if name in data:
        return _fmt_list(name, data[name])
    return _t(f"没有叫「{name}」的清单。现有：" + "、".join(data),
              f"There is no {name} list. Existing lists: " + ", ".join(data))


async def _list_add(args: dict) -> str:
    item = " ".join(str(args.get("item") or "").split())[:MAX_ITEM_CHARS]
    if not item:
        return _t("没有给要记的内容", "nothing to add")
    name = _list_key(args.get("list"))
    data = _load_lists()
    if name not in data and len(data) >= MAX_LISTS:
        return _t(f"清单数量已到上限（{MAX_LISTS}）。先删掉一条再加。",
                  f"Too many lists (limit {MAX_LISTS}). Remove one first.")
    items = data.setdefault(name, [])
    if len(items) >= MAX_ITEMS:
        return _t(f"清单「{name}」已经有 {MAX_ITEMS} 条了，放不下。",
                  f"The {name} list already has {MAX_ITEMS} items.")
    if any(i.casefold() == item.casefold() for i in items):
        return _t(f"「{item}」已经在清单「{name}」上了，没有重复添加。",
                  f"{item!r} is already on the {name} list; nothing was added.")
    items.append(item)
    _save_lists(data)
    return _t(f"已把「{item}」加到清单「{name}」，现在共 {len(items)} 条。",
              f"Added {item!r} to the {name} list. It now has {len(items)} items.")


async def _list_remove(args: dict) -> str:
    item = " ".join(str(args.get("item") or "").split())[:MAX_ITEM_CHARS]
    if not item:
        return _t("没有给要删的内容", "nothing to remove")
    data = _load_lists()
    if not data:
        return _t("一条清单都还没有。", "There are no lists yet.")

    # 先在模型指定的清单里找；找不到就在**所有**清单里找。
    # 理由是实测出来的：用户说「牛奶买到了，划掉」时不会报清单名，模型于是
    # 省略 list 参数、落到默认清单，而东西在 shopping 清单里 —— 结果是
    # 「删成功了但其实什么都没删」。一副眼镜上的清单只有几十条，全局找一遍是对的。
    def find(where: str) -> str | None:
        return next((i for i in data.get(where, []) if i.casefold() == item.casefold()), None)

    name = _list_key(args.get("list"))
    hit = find(name)
    if hit is None:
        owners = [k for k in data if find(k) is not None]
        if not owners:
            return _t(f"哪个清单上都没有「{item}」。什么都没删。",
                      f"{item!r} is not on any list. Nothing was removed.")
        if len(owners) > 1:
            return _t(f"「{item}」同时在这几个清单上：" + "、".join(owners) +
                      "。问用户要删哪一个，先别删。",
                      f"{item!r} is on several lists: " + ", ".join(owners) +
                      ". Ask the user which one; do not remove it yet.")
        name = owners[0]
        hit = find(name)

    data[name].remove(hit)
    left = len(data[name])
    if not data[name]:
        data.pop(name, None)
    _save_lists(data)
    return _t(f"已从清单「{name}」删掉「{hit}」，还剩 {left} 条。",
              f"Removed {hit!r} from the {name} list. {left} items left.")


LIST_SHOW = Tool(
    name="list_show",
    description=_t("读出用户的清单（购物清单、待办等）。问到「清单上有什么」「我要买什么」时用它。",
                   "Read back one of the user's lists (shopping, to-do, ...). Use it "
                   "when they ask what is on a list."),
    capability=Capability.READ,
    budget_ms=100,
    parameters={"type": "object", "properties": {
        "list": {"type": "string",
                 "description": "Which list, e.g. 'shopping'. Omit to show them all."}}},
    handler=_list_show,
    label=_t("清单", "List"),
)

LIST_ADD = Tool(
    name="list_add",
    description=_t("往清单里加一条。用户说「记一下」「加到购物清单」「提醒我买…」时用它。"
                   "**必须真的调用它**，不能只是嘴上说记住了。",
                   "Add one item to a list. Use it when the user says to remember "
                   "something, or to add it to a shopping or to-do list. **You must "
                   "actually call it** -- never just say you saved it."),
    capability=Capability.WRITE,
    budget_ms=200,
    parameters={"type": "object", "properties": {
        "item": {"type": "string", "description": "The item text, short."},
        "list": {"type": "string",
                 "description": "Which list, e.g. 'shopping'. Omit for the default."}},
        "required": ["item"]},
    handler=_list_add,
    label=_t("记下", "Save"),
    resources=(str(LISTS_PATH),),
)

LIST_REMOVE = Tool(
    name="list_remove",
    description=_t("从清单里删掉一条。用户说「买到了」「删掉」「做完了」时用它。",
                   "Remove one item from a list. Use it when the user says they got "
                   "it, finished it, or want it deleted."),
    capability=Capability.WRITE,
    budget_ms=200,
    parameters={"type": "object", "properties": {
        "item": {"type": "string", "description": "The item text to remove."},
        "list": {"type": "string", "description": "Which list. Omit for the default."}},
        "required": ["item"]},
    handler=_list_remove,
    label=_t("删掉", "Remove"),
    resources=(str(LISTS_PATH),),
)

REGISTRY: dict[str, Tool] = {t.name: t for t in (
    NOW, DAYS_UNTIL, DEVICE, WEATHER, CALC, CURRENCY,
    LIST_SHOW, LIST_ADD, LIST_REMOVE,
    REMIND_SET, REMIND_LIST, REMIND_CANCEL)}


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
            raise ValueError(_t("参数必须是一个 JSON 对象", "arguments must be a JSON object"))
    except Exception as exc:
        return ToolResult(call_id, name,
                          _t(f"参数解析失败：{exc}", f"could not parse arguments: {exc}"), False,
                          int((time.monotonic() - started) * 1000))

    budget = tool.budget_ms / 1000
    if deadline is not None:
        budget = min(budget, max(0.05, deadline - time.monotonic()))
    try:
        content = await asyncio.wait_for(tool.handler(args), timeout=budget)
        ok = True
    except asyncio.TimeoutError:
        content, ok = _t(f"{tool.name} 超时（预算 {tool.budget_ms}ms）",
                         f"{tool.name} timed out (budget {tool.budget_ms}ms)"), False
    except Exception as exc:                       # 工具坏了不该炸掉整轮对话
        content, ok = _t(f"{tool.name} 执行失败：{str(exc)[:120]}",
                         f"{tool.name} failed: {str(exc)[:120]}"), False
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
