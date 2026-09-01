"""提醒：状态与计时在 agent，响铃在网关。

这一层最容易出的错不是「不响」，而是**「说记住了，其实没记住」**和
**「说取消了，其实还会响」** —— 两者用户都要等到出事那一刻才发现。
所以测试重点全在状态一致性上。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest

from lens_agent import skills, tools
from lens_agent.reminders import GRACE_SECONDS, ReminderScheduler


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "reminders.json"
    monkeypatch.setattr(tools, "REMINDERS_PATH", path)
    tools.SESSION_KEY.set("lens:devA")
    tools.PENDING_REMINDERS.set([])
    return path


async def _swallow(_key: str, _text: str) -> None:
    """一个什么都不做的响铃端。**不能用 lambda** —— 它得是协程。"""


async def call(name: str, **kw) -> str:
    return (await tools.invoke(name, "c", json.dumps(kw))).content


class TestClockTimes:
    """「明天九点提醒我看牙医」。

    这条路曾经根本不存在：路由把它丢给兜底档，屏幕上回的是「我还不会设提醒」——
    而它刚刚才为「10 分钟后」设过一条。**说自己做不到一件做得到的事**，
    用户的反应和被编了一个答案是一样的：不会再问第二次。

    钟点换算放在工具里而不是让模型自己算，是因为算错的症状**要到几小时后才出现**：
    设的那一刻它回的仍然是「好的」。
    """

    def _minutes_to(self, hh: int, mm: int, day: str | None = None) -> float:
        """测试自己独立算一遍「还有多少分钟」，不复用被测代码的实现。"""
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day == "tomorrow" or (day is None and target <= now):
            target += timedelta(days=1)
        return (target - now).total_seconds() / 60

    async def test_a_clock_time_lands_on_the_next_occurrence(self, store):
        await call("remind_set", at="09:00", text="call the dentist")
        rows = json.loads(store.read_text())
        assert len(rows) == 1
        want = time.time() + self._minutes_to(9, 0) * 60
        assert abs(rows[0]["at"] - want) < 5

    async def test_tomorrow_is_a_day_later_than_today(self, store):
        """`day` 省略时取下一次出现 —— 显式说 tomorrow 必须比它晚整整一天。"""
        a = self._minutes_to(9, 0, "today")
        b = self._minutes_to(9, 0, "tomorrow")
        assert abs((b - a) - 24 * 60) < 0.01

    @pytest.mark.parametrize("bad", ["9am", "25:00", "09:60", "", "明早九点", "9"])
    async def test_unreadable_clock_is_refused_not_guessed(self, store, bad):
        r = await call("remind_set", at=bad, text="x")
        assert not store.exists() or json.loads(store.read_text()) == []
        assert not tools.PENDING_REMINDERS.get()
        assert "HH:MM" in r or "没有说要提醒什么" in r or "no reminder text" in r

    async def test_both_forms_at_once_is_refused(self, store):
        """两个都给了就不猜：猜错的后果是几小时后才暴露的错误时刻。"""
        r = await call("remind_set", at="09:00", minutes=10, text="x")
        assert not store.exists() or json.loads(store.read_text()) == []
        assert "minutes" in r

    async def test_a_clock_time_already_past_today_is_refused(self, store):
        now = datetime.now()
        # 往回退两小时是为了拿一个「今天已经过去的钟点」—— 但**午夜之后就退过头了**：
        # 00:25 减两小时是昨天的 22:25，而 "22:25" + day=today 指的是今晚，
        # 于是这条断言在每天 00:00–02:00 之间假失败。夹到今天零点为止。
        past = max(now - timedelta(hours=2),
                   now.replace(hour=0, minute=0, second=0, microsecond=0))
        r = await call("remind_set", at=f"{past.hour:02d}:{past.minute:02d}",
                       day="today", text="x")
        assert not store.exists() or json.loads(store.read_text()) == []
        assert "将来" in r or "future" in r

    def test_the_model_is_told_not_to_do_the_arithmetic(self):
        """契约里那句「不要自己算分钟」是这条路的**唯一**护栏 ——
        模型一旦自己算，错的时刻会带着一句「好的」一起交付。"""
        for rule in skills._REMIND_RULE.values():
            assert "at" in rule
        assert "不要自己去算" in skills._REMIND_RULE["zh"]
        assert "do not work out the minutes yourself" in skills._REMIND_RULE["en"]

    def test_no_time_at_all_has_an_honest_fallback(self):
        """没说时间、或超过 24 小时的，记进待办清单 —— 而不是回一句「做不到」。"""
        assert "list_add" in skills.REMIND.tools


class TestSetting:
    async def test_ids_are_unique_even_within_the_same_millisecond(self, store):
        """撞 id 的后果是静默的：`schedule()` 会把前一条取消再排新的 ⇒
        `remind_list` 说有两条，实际只响一条，而且没有任何报错。"""
        n = tools.MAX_REMINDERS
        for i in range(n + 5):                   # 多设几条，顺便验上限真的挡得住
            await call("remind_set", minutes=10, text=f"item {i}")
        ids = [r["id"] for r in json.loads(store.read_text())]
        assert len(ids) == n, "MAX_REMINDERS 没挡住"
        assert len(set(ids)) == n

    async def test_beyond_a_day_is_refused_not_faked(self, store):
        r = await call("remind_set", minutes=60 * 25, text="too far")
        assert not store.exists() or json.loads(store.read_text()) == []
        assert "24" in r

    async def test_past_time_is_refused(self, store):
        await call("remind_set", minutes=-5, text="yesterday")
        assert not store.exists() or json.loads(store.read_text()) == []

    async def test_the_scheduler_gets_told(self, store):
        await call("remind_set", minutes=3, text="stand up")
        queued = tools.PENDING_REMINDERS.get()
        assert len(queued) == 1 and queued[0]["text"] == "stand up"

    async def test_a_freshly_set_reminder_does_not_read_back_as_one_minute_short(self, store):
        """刚设的「10 分钟后」立刻读回来不能变成「9 分 59 秒后」——
        看着像它记错了，而用户没法验证到底记的是哪个。"""
        await call("remind_set", minutes=10, text="noodles")
        assert "10" in await call("remind_list")


class TestSessionIsolation:
    """一个 agent 进程服多副眼镜。A 不该读到、更不该取消 B 的提醒。"""

    async def test_list_only_shows_this_session(self, store):
        await call("remind_set", minutes=5, text="A's thing")
        tools.SESSION_KEY.set("lens:devB")
        assert "A's thing" not in await call("remind_list")

    async def test_cannot_cancel_another_devices_reminder(self, store):
        await call("remind_set", minutes=5, text="A's thing")
        tools.SESSION_KEY.set("lens:devB")
        await call("remind_cancel", text="thing")
        rows = json.loads(store.read_text())
        assert len(rows) == 1, "B 取消掉了 A 的提醒"

    async def test_cancelling_mine_leaves_other_sessions_alone(self, store):
        await call("remind_set", minutes=5, text="A's thing")
        tools.SESSION_KEY.set("lens:devB")
        await call("remind_set", minutes=5, text="B's thing")
        await call("remind_cancel", text="B's")
        rows = json.loads(store.read_text())
        assert [r["text"] for r in rows] == ["A's thing"]


class TestCancelling:
    async def test_ambiguous_cancel_refuses(self, store):
        await call("remind_set", minutes=5, text="call mom")
        await call("remind_set", minutes=6, text="call dad")
        r = await call("remind_cancel")          # 不说取消哪条，且有多条
        assert len(json.loads(store.read_text())) == 2, "有歧义时一条都不该取消"
        assert "which" in r or "哪一条" in r

    async def test_single_reminder_cancels_without_naming_it(self, store):
        await call("remind_set", minutes=5, text="call mom")
        await call("remind_cancel")
        assert json.loads(store.read_text()) == []

    async def test_cancelling_something_absent_says_so(self, store):
        await call("remind_set", minutes=5, text="call mom")
        r = await call("remind_cancel", text="laundry")
        assert len(json.loads(store.read_text())) == 1
        assert "没有" in r or "No reminder" in r

    async def test_cancel_matching_several_refuses_too(self, store):
        """给了关键词但对上好几条，同样一条都不能删 —— 替用户猜哪条更糟。"""
        await call("remind_set", minutes=5, text="call mom")
        await call("remind_set", minutes=6, text="call dad")
        r = await call("remind_cancel", text="call")
        assert len(json.loads(store.read_text())) == 2, "有歧义时一条都不该取消"
        assert "which" in r or "哪一条" in r

    async def test_cancel_reaches_the_scheduler(self, store):
        await call("remind_set", minutes=5, text="call mom")
        tools.PENDING_REMINDERS.set([])
        await call("remind_cancel", text="mom")
        queued = tools.PENDING_REMINDERS.get()
        assert queued and queued[0].get("cancel") is True


class TestScheduler:
    async def test_it_actually_fires(self, store):
        fired: list[tuple[str, str]] = []

        async def notify(key, text):
            fired.append((key, text))

        sched = ReminderScheduler(notify)
        sched.schedule("lens:devA", {"id": "rm_1", "at": time.time() + 0.02, "text": "ding"})
        await asyncio.sleep(0.15)
        assert fired == [("lens:devA", "ding")]

    async def test_reconnecting_does_not_erase_what_is_pending(self, store):
        """★ 真跑演示时抓到的：设完一条提醒，下一句问「有什么提醒」答「一条都没有」。

        链路是这样的 —— 网关每次重连都会把磁盘上待响的重新排一遍（幂等恢复），
        而旧实现的「重排」= 先取消再排；取消走的是 `_fire` 的收尾，
        那段收尾**以为自己是响过了**，于是把这条从磁盘上划掉。
        结果是两个互相矛盾的症状：`remind_list` 说没有，内存里那条却还会响。
        """
        row = {"id": "rm_1", "at": time.time() + 5, "text": "ding", "session": "lens:devA"}
        store.write_text(json.dumps([row]))
        sched = ReminderScheduler(_swallow)
        for _ in range(3):                 # 连上、断开、再连上、再连上
            for r in tools._load_reminders(GRACE_SECONDS):
                sched.schedule(str(r["session"]), r)
            # 让排下去的任务真的跑起来（跑到它那句 sleep 上）。少了这一步，
            # 任务还没开始就被取消，收尾代码根本不会执行 —— 测试会变成空转，
            # 把「取消时抹磁盘」这个 bug 一路放过去。
            await asyncio.sleep(0.01)
        assert json.loads(store.read_text()) == [row], "重连把待响的提醒抹掉了"
        assert sched.pending == 1
        assert (await call("remind_list")).startswith("1 ") or "1 条" in await call("remind_list")

    async def test_shutting_down_does_not_erase_what_is_pending(self, store):
        """进程退出时 `cancel_all()` 同样不是「响过了」。

        旧实现下这是一场竞态：收尾跑得赢就把所有待响的提醒清空，跑不赢就留着。
        用户看到的是「重启一次，交代过的事全没了」，而且没有任何日志。
        """
        rows = [{"id": f"rm_{i}", "at": time.time() + 5, "text": f"t{i}",
                 "session": "lens:devA"} for i in range(3)]
        store.write_text(json.dumps(rows))
        sched = ReminderScheduler(_swallow)
        for r in rows:
            sched.schedule("lens:devA", r)
        await asyncio.sleep(0.01)          # 同上：任务得先真的跑起来
        sched.cancel_all()
        await asyncio.sleep(0.01)          # 再给被取消的任务跑收尾的机会
        assert json.loads(store.read_text()) == rows
        assert sched.pending == 0

    async def test_firing_removes_it_from_disk(self, store):
        """响过不划掉的话，下次 restore 会在宽限期内**再响一遍**。"""
        store.write_text(json.dumps([{"id": "rm_1", "at": time.time() + 0.02,
                                      "text": "ding", "session": "lens:devA"}]))

        async def notify(key, text):
            pass

        sched = ReminderScheduler(notify)
        sched.schedule("lens:devA", {"id": "rm_1", "at": time.time() + 0.02, "text": "ding"})
        await asyncio.sleep(0.15)
        assert json.loads(store.read_text()) == []

    async def test_firing_one_does_not_wipe_another_waiting_for_its_grace(self, store):
        """A 响完之后收尾写盘，不能把还在宽限期里等着补发的 B 一起抹掉 ——
        B 再也不会响，而且磁盘上连痕迹都不剩。"""
        now = time.time()
        store.write_text(json.dumps([
            {"id": "a", "at": now + 0.02, "text": "fires now", "session": "lens:devA"},
            {"id": "b", "at": now - 20, "text": "waiting for grace", "session": "lens:devA"},
        ]))

        async def notify(key, text):
            pass

        sched = ReminderScheduler(notify)
        sched.schedule("lens:devA", {"id": "a", "at": now + 0.02, "text": "fires now"})
        await asyncio.sleep(0.15)
        left = [r["id"] for r in json.loads(store.read_text())]
        assert left == ["b"], f"宽限期里的那条被抹掉了：{left}"

    async def test_cancelled_reminder_never_fires(self, store):
        fired = []

        async def notify(key, text):
            fired.append(text)

        sched = ReminderScheduler(notify)
        sched.schedule("lens:devA", {"id": "rm_1", "at": time.time() + 0.05, "text": "ding"})
        sched.schedule("lens:devA", {"id": "rm_1", "cancel": True})
        await asyncio.sleep(0.15)
        assert fired == [], "取消掉的提醒还是响了"

    async def test_a_failing_notify_does_not_take_down_the_others(self, store):
        ok: list[str] = []

        async def notify(key, text):
            if text == "bad":
                raise ConnectionError("网关不在线")
            ok.append(text)

        sched = ReminderScheduler(notify)
        sched.schedule("k", {"id": "a", "at": time.time() + 0.02, "text": "bad"})
        sched.schedule("k", {"id": "b", "at": time.time() + 0.04, "text": "good"})
        await asyncio.sleep(0.2)
        assert ok == ["good"]

    async def test_restore_skips_what_is_far_past_but_keeps_what_just_missed(self, store):
        """关了一夜的机器不该在早上把昨晚所有提醒一次性糊到屏幕上；
        但断连三十秒里错过的那条，补发才是对的。"""
        now = time.time()
        store.write_text(json.dumps([
            {"id": "old", "at": now - GRACE_SECONDS - 60, "text": "last night",
             "session": "lens:devA"},
            {"id": "just", "at": now - 30, "text": "just missed", "session": "lens:devA"},
            {"id": "soon", "at": now + 600, "text": "later", "session": "lens:devA"},
        ]))

        async def notify(key, text):
            pass

        sched = ReminderScheduler(notify)
        assert sched.restore("lens:devA") == 2
        sched.cancel_all()

    def test_expired_entries_are_not_reported_as_pending(self, store):
        store.write_text(json.dumps([
            {"id": "old", "at": time.time() - 99999, "text": "ancient", "session": "lens:devA"}]))
        assert tools._load_reminders() == []


class TestGates:
    def test_write_tools_are_pinned_to_the_reminder_file(self):
        for name in ("remind_set", "remind_cancel"):
            tool = tools.REGISTRY[name]
            assert tool.capability is tools.Capability.WRITE
            assert tool.resources == (str(tools.REMINDERS_PATH),)

    def test_only_the_remind_skill_may_write_reminders(self):
        from lens_agent import policy
        for skill in skills.SKILLS.values():
            for name in ("remind_set", "remind_cancel"):
                if name in skill.tools:
                    assert skill.name == "remind"
                else:
                    with pytest.raises(policy.PolicyDenied):
                        policy.check(skill, name)


class TestDeliveryDoesNotSilentlyLoseReminders:
    """两条真跑演示时抓到的丢失路径。两条的症状是同一句话：**说好要提醒我，结果没响。**"""

    async def test_a_failed_send_keeps_it_on_disk(self, store):
        """★ 送不出去 ≠ 响过了。

        旧实现在 `_notify` 抛异常之后**继续往下走**，把这条从磁盘划掉 ——
        于是一次暂时的断连（网关重启、连接抖动）就把用户交代过的事永久丢掉，
        而且不留痕迹。它应该留在盘上，等 `restore` 在宽限期内补发。
        """
        row = {"id": "rm_1", "at": time.time() + 0.02, "text": "ding", "session": "lens:devA"}
        store.write_text(json.dumps([row]))

        async def notify(key, text):
            raise ConnectionError("网关不在线")

        sched = ReminderScheduler(notify)
        sched.schedule("lens:devA", row)
        await asyncio.sleep(0.15)
        assert [r["id"] for r in json.loads(store.read_text())] == ["rm_1"], \
            "送失败的提醒被从磁盘上划掉了 —— 它再也不会响，而且没有任何痕迹"

    async def test_a_successful_send_still_clears_it(self, store):
        """反向守着上一条：成功送达之后必须划掉，否则 restore 会在宽限期内重复响。"""
        row = {"id": "rm_1", "at": time.time() + 0.02, "text": "ding", "session": "lens:devA"}
        store.write_text(json.dumps([row]))

        async def notify(key, text):
            return None

        sched = ReminderScheduler(notify)
        sched.schedule("lens:devA", row)
        await asyncio.sleep(0.15)
        assert json.loads(store.read_text()) == []


class TestNotifyRouting:
    """★ 一条调试 CLI 连上来，提醒就再也送不到眼镜上了。

    agent 从前只留**一个**槽记「当前那条连接」。而说这套协议的不止网关一个：
    `demo/chat.py` 一 connect 就把那个槽覆盖掉，退出时再把它置空 ——
    此后 `_notify` 看到的永远是「网关不在线」。真跑时的症状是提醒静静地不响，
    而 `remind_set` 明明成功了。单测从前把 notify 整个 mock 掉，所以照不到这里。
    """

    class _FakeWs:
        """只实现 `_send` 真正用到的两样：`closed` 和 `send_str`。"""

        def __init__(self) -> None:
            self.closed = False
            self.sent: list[dict] = []

        async def send_str(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    @staticmethod
    def _server() -> "LensAgentServer":  # noqa: F821
        """绕开 `__init__`：它会建 aiohttp app、排程器和 HTTP 会话，
        而这几条测的只是 `_notify` 的选路。"""
        from lens_agent.server import LensAgentServer
        srv = LensAgentServer.__new__(LensAgentServer)
        srv._conns = []
        srv._session_ws = {}
        return srv

    async def test_a_cli_connecting_and_leaving_does_not_orphan_the_gateway(self):
        srv = self._server()
        gw, cli = self._FakeWs(), self._FakeWs()
        srv._conns.append(gw)                      # 网关先连上
        srv._session_ws["lens:devA"] = gw
        srv._conns.append(cli)                     # 调试 CLI 随后连上
        srv._conns.remove(cli)                     # …然后退出（模拟它的 finally）

        await srv._notify("lens:devA", "check the oven")
        assert gw.sent, "网关被一条来了又走的 CLI 挤掉了"
        assert gw.sent[0]["event"] == "notify"
        assert gw.sent[0]["payload"]["text"] == "check the oven"

    async def test_it_goes_back_to_whoever_asked_for_it(self):
        """两条连接同时在，提醒必须回到**当初交代这件事的那一头**。"""
        srv = self._server()
        gw, cli = self._FakeWs(), self._FakeWs()
        srv._conns += [gw, cli]
        srv._session_ws = {"lens:devA": gw, "cli-7f3": cli}

        await srv._notify("cli-7f3", "stretch")
        assert cli.sent, "提醒没送到交代它的那一头"
        assert not gw.sent, "提醒送到了别人的屏幕上"

    async def test_it_falls_back_to_the_newest_live_connection(self):
        """agent 重启后 `restore` 先于任何 chat.send 跑，那时还没有会话映射。"""
        srv = self._server()
        gw = self._FakeWs()
        srv._conns.append(gw)                      # 映射是空的

        await srv._notify("lens:devA", "ding")
        assert gw.sent and gw.sent[0]["payload"]["text"] == "ding"

    async def test_no_live_connection_raises_so_it_stays_on_disk(self):
        srv = self._server()
        dead = self._FakeWs()
        dead.closed = True
        srv._conns.append(dead)
        srv._session_ws["lens:devA"] = dead

        with pytest.raises(ConnectionError):
            await srv._notify("lens:devA", "ding")
