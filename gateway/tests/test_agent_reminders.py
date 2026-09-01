"""提醒：状态与计时在 agent，响铃在网关。

这一层最容易出的错不是「不响」，而是**「说记住了，其实没记住」**和
**「说取消了，其实还会响」** —— 两者用户都要等到出事那一刻才发现。
所以测试重点全在状态一致性上。
"""
from __future__ import annotations

import asyncio
import json
import time

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


async def call(name: str, **kw) -> str:
    return (await tools.invoke(name, "c", json.dumps(kw))).content


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
