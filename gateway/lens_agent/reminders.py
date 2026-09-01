"""提醒的排程器。

**分工**：状态和计时归 agent（它才知道有哪些提醒、才能 list/cancel），
到点的**响铃归网关**（屏幕是它的，agent 只能请求）。所以这里做的事只有两件：
到点了叫一声、以及在进程重启后把还没响的重新排上。

为什么不让网关来计时：那样状态就有两份（agent 的 `reminders.json` 用于 list/cancel，
网关的 task 用于响铃），取消要两头同步，重启要两头恢复 —— 两份状态迟早会分叉，
而分叉的表现是「它说取消了，结果还是响了」。单一真源换来的代价只是多一种事件。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from . import tools

log = logging.getLogger(__name__)

#: 进程/连接不在的那段时间里到点的提醒，晚这么久以内仍然补发。
#: 再晚就不发了 —— 一个迟到半小时的「面条好了」只会让人困惑。
GRACE_SECONDS = 300.0

#: (session_key, text) → 送到那副眼镜上
Notify = Callable[[str, str], Awaitable[None]]


class ReminderScheduler:
    def __init__(self, notify: Notify) -> None:
        self._notify = notify
        self._tasks: dict[str, asyncio.Task] = {}

    def schedule(self, session_key: str, row: dict) -> None:
        """排一条（或按 `cancel` 撤一条）。重复排同一个 id = 重排。"""
        rid = str(row.get("id") or "")
        if not rid:
            return
        if row.get("cancel"):
            self.cancel(rid)
            return
        self.cancel(rid)
        self._tasks[rid] = asyncio.ensure_future(
            self._fire(session_key, rid, float(row.get("at", 0)), str(row.get("text", ""))))

    def cancel(self, rid: str) -> bool:
        task = self._tasks.pop(rid, None)
        if task is None:
            return False
        task.cancel()
        return True

    def cancel_all(self) -> None:
        for rid in list(self._tasks):
            self.cancel(rid)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def restore(self, session_key: str) -> int:
        """进程刚起来 / 网关刚连上：把磁盘上还没响的重新排上。

        补发的判据是 `GRACE_SECONDS`，不是「只要还没响就发」—— 后者会让一台
        关了一夜的机器在早上开机时把昨晚所有提醒一次性糊到屏幕上。
        """
        n = 0
        for row in tools._load_reminders(GRACE_SECONDS):
            self.schedule(session_key, row)
            n += 1
        if n:
            log.info("恢复了 %d 条待响的提醒", n)
        return n

    async def _fire(self, session_key: str, rid: str, at: float, text: str) -> None:
        try:
            delay = at - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            await self._notify(session_key, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 一条提醒响不出去不该带走别的。日志里必须留痕：
            # 用户唯一能察觉的症状是「说好要提醒我，结果没响」。
            log.exception("提醒 %s 送不出去", rid)
        finally:
            self._tasks.pop(rid, None)
            # 响过就从磁盘上划掉，否则下次 restore 会在宽限期内再响一遍。
            # **必须按宽限期读**：默认读法会丢掉所有已到点的条目，于是这次保存
            # 会把别的、还在宽限期里等着补发的提醒一起抹掉 —— 它们再也不会响，
            # 而且没有任何痕迹。
            rows = [r for r in tools._load_reminders(GRACE_SECONDS) if r.get("id") != rid]
            try:
                tools._save_reminders(rows)
            except OSError:
                log.exception("提醒 %s 响过之后没能从磁盘划掉", rid)
