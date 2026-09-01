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
        """排一条（或按 `cancel` 撤一条）。**已经排着的同一个 id 直接跳过。**

        跳过而不是重排，是因为「重复排同一个 id」在这套系统里只有一个来源：
        网关重连时的恢复扫描，而它扫的正是当前排着的这些。重排要先取消，
        取消会走 `_fire` 的收尾 —— 于是每重连一次，磁盘上的提醒就被"响过了"
        的清理抹掉一批。症状是矛盾的：`remind_list` 说一条都没有，
        内存里那条却还会到点响。
        """
        rid = str(row.get("id") or "")
        if not rid:
            return
        if row.get("cancel"):
            self.cancel(rid)
            return
        live = self._tasks.get(rid)
        if live is not None and not live.done():
            return
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
            # ★ 取消**不是**响过。这里以前走的是共用的 finally，于是取消也会
            # 把这条从磁盘上划掉 —— 而取消的来源有两个都无辜：网关重连时的
            # 恢复扫描（重排同一条），和进程退出时的 `cancel_all()`。
            # 后者意味着**每次重启都会清空所有待响的提醒**，而且是竞态的：
            # 收尾跑得赢就清空，跑不赢就留着。谁都不报错。
            #
            # 真正需要从磁盘划掉的只有两种情况：响过了，和用户取消了。
            # 后者由 `remind_cancel` 自己写盘，不经过这里。
            self._tasks.pop(rid, None)
            raise
        except Exception:
            # 一条提醒响不出去不该带走别的。日志里必须留痕：
            # 用户唯一能察觉的症状是「说好要提醒我，结果没响」。
            log.exception("提醒 %s 送不出去", rid)
            # ★ **送不出去 ≠ 响过了。** 这里以前会继续往下走，把这条从磁盘上
            # 划掉 —— 于是一次暂时的断连（网关重启、连接抖动）就把用户交代过的
            # 事**永久**丢掉了，而且没有任何痕迹。留在盘上，让 `restore` 在宽限期
            # 内补发；真过了宽限期，`_load_reminders` 自己会滤掉它。
            self._tasks.pop(rid, None)
            return
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
