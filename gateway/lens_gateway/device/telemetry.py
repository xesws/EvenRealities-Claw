"""设备遥测缓存（协议 v1.1）。

## 为什么必须有这一层

遥测数据以前**从来没离开过手机**：插件订阅了 `onDeviceStatusChanged`，
但只把它拼成一个中文串显示在手机页上。协议上行只有 ptt/PCM/page/abort/reset/ping ⇒
**网关对电量、佩戴、连接状态的知识为零**。于是 MCP 的 `glasses_telemetry` 工具
只有两条路：要么编，要么没有 —— 编就直接违反"演示不能有任何 fake"。

## 两个必须诚实对待的坑

1. **`getDeviceInfo()` 是不是真的触发一次 BLE 读取，官方没说。**
   所以 poll 回来的值**不能无条件当作"新鲜"**：它可能是手机端 BLE 栈缓存的旧值。
   本模块因此把 `source` 一路带到出口 —— `push` 是设备真的报告了状态变化，
   `poll` 只代表"我们问了，手机这么答的"。消费方（MCP 工具描述里也写明）自己判断。

2. **Even 生态里不止眼镜。** R1 戒指与眼镜走同一套 `DeviceStatus` 推送，
   而 `DeviceStatus` 里**只有 sn、没有 model**（SDK `dist/index.d.ts:143`）。
   只认 sn 是分不出戒指的 —— 一不留神就会把戒指的电量当成眼镜的报上去。
   判定只能由插件侧结合 `getDeviceInfo()` 的 `model` 做，网关这边**只接受
   明确自称眼镜的记录**，其余计数后丢弃（计数是为了让它可见，而不是静默吞掉）。

## 没有数据时返回 None，不返回零值

`snapshot()` 在从未收到过遥测时返回 `None`，调用方必须如实说"没有数据"。
返回一个 `battery=0` 的默认结构就是在编。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: 允许上报的字段白名单。插件多送的字段一律丢弃 ——
#: 遥测会经 MCP 流向第三方 LLM 厂商，字段集必须是我们审过的。
_FIELDS: tuple[str, ...] = (
    "model", "sn", "isGlasses", "connectType", "connected",
    "batteryLevel", "isCharging", "isWearing", "isInCase",
)

_SOURCES = ("push", "poll")


def _mask_sn(sn: Any) -> str | None:
    """序列号是硬件标识符，出网关一律只留后 4 位。

    网关是用户自己的服务器，收全量没问题；但遥测会经 MCP 交给第三方模型厂商，
    完整 SN 属于可跨服务关联的设备指纹，没有任何理由让它离开这台机器。
    """
    if not isinstance(sn, str) or not sn:
        return None
    return f"…{sn[-4:]}" if len(sn) > 4 else sn


@dataclass
class TelemetryRecord:
    data: dict
    source: str
    sampled_at: float          # time.monotonic()，算 age 用
    wall_at: float             # time.time()，给人看的

    def age_ms(self, now: float | None = None) -> int:
        return max(0, int(((now if now is not None else time.monotonic()) - self.sampled_at) * 1000))


@dataclass
class TelemetryStore:
    """一台设备的遥测。整个类只有一个可变状态：最后一条已知记录。"""

    stale_seconds: float = 60.0
    _record: TelemetryRecord | None = None
    #: 被拒绝的上报计数。可见 > 静默丢弃。
    rejected: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stale_seconds <= 0:
            raise ValueError(f"stale_seconds 必须为正：{self.stale_seconds}")

    # ------------------------------------------------------------------ 写入

    def _reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def update(self, payload: Any, source: str, *, now: float | None = None) -> bool:
        """收一条上报。返回是否被接受。

        拒绝的三种情况都会计数：不是眼镜、载荷形状不对、来源不认识。
        """
        if source not in _SOURCES:
            self._reject("bad_source")
            log.warning("遥测来源不认识：%r", source)
            return False
        if not isinstance(payload, dict):
            self._reject("bad_payload")
            return False
        if payload.get("isGlasses") is not True:
            # 戒指、或插件还没能确认型号 —— 两种都不能当成眼镜的电量报出去
            self._reject("not_glasses")
            log.info("丢弃非眼镜遥测（model=%r sn=%r）", payload.get("model"), payload.get("sn"))
            return False

        data = {k: payload[k] for k in _FIELDS if k in payload}
        mono = now if now is not None else time.monotonic()
        self._record = TelemetryRecord(data=data, source=source,
                                       sampled_at=mono, wall_at=time.time())
        return True

    # ------------------------------------------------------------------ 读出

    def snapshot(self, *, now: float | None = None) -> dict | None:
        """给控制面 / MCP 的只读视图。从未收到过遥测则返回 `None`。"""
        r = self._record
        if r is None:
            return None
        age = r.age_ms(now)
        data = dict(r.data)
        data["sn"] = _mask_sn(data.get("sn"))
        return {
            **data,
            "source": r.source,
            "sampled_at": r.wall_at,
            "age_ms": age,
            "stale": age > self.stale_seconds * 1000,
            # poll 可能拿到的是手机端 BLE 栈的缓存值，官方未说明 getDeviceInfo 是否真读设备
            "source_note": ("设备主动上报的状态变化" if r.source == "push"
                            else "网关拉取；手机端可能返回缓存值，不保证是此刻的真实状态"),
        }

    def diagnostics(self) -> dict:
        return {"has_data": self._record is not None, "rejected": dict(self.rejected)}
