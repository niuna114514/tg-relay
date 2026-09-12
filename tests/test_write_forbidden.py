"""「禁止发言」的处置测试 —— 2026-09-12 那次账号被限制事故的回归。

事故经过：
    号被 Telegram 反垃圾限制了（@SpamBot 确认），往目标群转发一律
    `UserBannedInChannelError`。但当时：
      1. 定时重发是**直接调 Sender.send()** 的，不走 TargetWorker 队列，
         所以 worker 那层"永久失败就暂停目标"的保护对它完全无效；
      2. `_probe_resume` 每 10 分钟还会自动解除暂停；
      3. 结果就是每 31 秒往一个已经被限制的号上再撞一次，撞了一整晚。
      4. 而且群权限自检一路报"OK" —— 账号级限制在群权限里根本看不出来。

这几个测试锁死修复后的行为：
    * 单个目标报这个错 → 只封这个目标，冷却期内**连 API 都不碰**；
    * 多个目标在窗口内都报 → 判定账号级限制，直接熔断；
    * 熔断/封禁都要发告警（人去处理），而不是闷头重试。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.alerts import AlertHub  # noqa: E402
from tgrelay.config import AppConfig, Behavior, Filters, Rate, RepostConfig, Target  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.reposter import Reposter  # noqa: E402
from tgrelay.sender import RelayJob, Sender, WriteForbiddenGuard  # noqa: E402

FAST_RATE = Rate(
    per_target_interval=(0.0, 0.0),
    cross_target_delay=(0.0, 0.0),
    global_per_minute=6000,
    daily_cap=100,
)


class ProbeClient(FakeClient):
    """带 get_messages 的假客户端：probe_send 要先从源频道读一条消息。"""

    async def get_messages(self, peer: Any, ids: Any = None, **kwargs: Any) -> Any:
        return [fake_message(msg_id=1, chat_id=-1001, text="源消息")]


class CountingClient(FakeClient):
    """额外数 forward_messages 的**调用次数**（失败也算）。

    `FakeClient.sent` 只在成功时追加，所以"撞了错误"的那次调用看不见 ——
    而本次要断言的恰恰是"有没有再去撞"。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    async def forward_messages(
        self, entity: Any, messages: Any, from_peer: Any = None, **kwargs: Any
    ) -> Any:
        self.calls += 1
        return await super().forward_messages(entity, messages, from_peer=from_peer, **kwargs)


def make_job(msg_id: int = 6) -> RelayJob:
    return RelayJob(
        job_id=f"repost:-1001:{msg_id}:1",
        source_id=-1001,
        source_peer="src",
        msg_ids=(msg_id,),
        kind="repost",
        preview="广告",
    )


def make_sender(
    tmp_path: Path,
    *,
    targets: list[Target] | None = None,
    client: FakeClient | None = None,
) -> tuple[Sender, Store, FakeClient, AlertHub]:
    targets = targets or [Target(id=-2001)]
    config = AppConfig(
        sources=(-1001,),
        targets=tuple(targets),
        filters=Filters(),
        rate=FAST_RATE,
        behavior=Behavior(queue_size=10),
        repost=RepostConfig(ids=(6,), interval=300, daily_limit=60),
    )
    store = Store(tmp_path / "relay.db")
    client = client or FakeClient()
    alerts = AlertHub()
    sender = Sender(client, config, store, alerts=alerts)
    # 别在测试里去问真的 @SpamBot
    sender.account_check_interval = 0.0
    sender.account_check_polls = 1
    return sender, store, client, alerts


# --------------------------------------------------------------------------
# 守卫本身：单目标 vs 账号级
# --------------------------------------------------------------------------


def test_guard_blocks_single_target() -> None:
    guard = WriteForbiddenGuard()
    account_level, count = guard.note(-2001, now=1000.0)

    assert account_level is False, "只有一个群出事时不该判定账号级"
    assert count == 1
    assert guard.blocked_remaining(-2001, now=1000.0) == guard.PER_TARGET_COOLDOWN
    assert guard.blocked_remaining(-2001, now=1000.0 + guard.PER_TARGET_COOLDOWN + 1) == 0


def test_guard_detects_account_level_across_targets() -> None:
    guard = WriteForbiddenGuard()
    assert guard.note(-2001, now=1000.0)[0] is False
    account_level, count = guard.note(-2002, now=1010.0)

    assert account_level is True, "两个不同的群都报禁止发言 = 账号级限制"
    assert count == 2


def test_guard_forgets_old_hits() -> None:
    """窗口外的旧记录不算数：昨天那个群出过问题，不代表今天也是账号级。"""
    guard = WriteForbiddenGuard()
    guard.note(-2001, now=1000.0)
    old = 1000.0 + guard.ACCOUNT_LEVEL_WINDOW + 1
    account_level, count = guard.note(-2002, now=old)

    assert account_level is False
    assert count == 1


# --------------------------------------------------------------------------
# Sender：撞上禁止发言之后不再空转
# --------------------------------------------------------------------------


async def test_send_marks_target_blocked_and_alerts(tmp_path: Path) -> None:
    client = FakeClient(fail_with=[UserBannedInChannelError(request=None)])  # type: ignore[arg-type]
    sender, store, client, alerts = make_sender(tmp_path, client=client)
    try:
        outcome = await sender.send(make_job(), Target(id=-2001))

        assert outcome.permanent is True
        assert sender.write_forbidden.blocked_remaining(-2001) > 0
        assert sender.breaker.tripped is False, "单群问题不该熔断整个程序"
        assert [level for level, _ in alerts.history] == ["warning"]
        assert "禁止发言" in alerts.history[0][1] or "UserBanned" in alerts.history[0][1]
    finally:
        store.close()


async def test_blocked_target_does_not_touch_api(tmp_path: Path) -> None:
    """冷却期内再发同一条：直接返回 skipped，**不能**再调 forward_messages。"""
    client = CountingClient(fail_with=[ChatWriteForbiddenError(request=None)])  # type: ignore[arg-type]
    sender, store, client, _ = make_sender(tmp_path, client=client)
    try:
        await sender.send(make_job(), Target(id=-2001))
        calls_after_first = client.calls

        outcome = await sender.send(make_job(), Target(id=-2001))

        assert outcome.status == "skipped"
        assert client.calls == calls_after_first, "被停发的目标不该再产生 API 调用"
        # 而且不要再消耗额度
        assert store.checked_today(as_repost=True) == 0
    finally:
        store.close()


async def test_two_targets_trip_breaker(tmp_path: Path) -> None:
    """两个群都报禁止发言 -> 账号级限制，熔断停止一切发送。"""
    client = FakeClient(
        fail_with=[
            UserBannedInChannelError(request=None),  # type: ignore[arg-type]
            UserBannedInChannelError(request=None),  # type: ignore[arg-type]
        ]
    )
    sender, store, client, alerts = make_sender(
        tmp_path, targets=[Target(id=-2001), Target(id=-2002)], client=client
    )
    try:
        await sender.send(make_job(), Target(id=-2001))
        assert sender.breaker.tripped is False

        await sender.send(make_job(), Target(id=-2002))

        assert sender.breaker.tripped is True
        assert "账号级" in sender.breaker.reason
        assert any(level == "critical" for level, _ in alerts.history)

        # 熔断之后再发：连关都过不去
        outcome = await sender.send(make_job(), Target(id=-2001))
        assert outcome.status == "skipped"
        assert "熔断" in outcome.error
    finally:
        store.close()


async def test_probe_send_success_clears_block(tmp_path: Path) -> None:
    """探测成功 = 现在确实能发，把冷板凳清掉（不用等 30 分钟）。"""
    client = ProbeClient()
    sender, store, _client, _ = make_sender(tmp_path, client=client)
    try:
        sender.write_forbidden.note(-2001)
        assert sender.write_forbidden.blocked_remaining(-2001) > 0

        allowed, _note = await sender.probe_send(Target(id=-2001))

        assert allowed is True
        assert sender.write_forbidden.blocked_remaining(-2001) == 0
    finally:
        store.close()


# --------------------------------------------------------------------------
# 额度：没送出去就不该占着
# --------------------------------------------------------------------------


async def test_budget_released_on_permanent_failure(tmp_path: Path) -> None:
    client = FakeClient(fail_with=[UserBannedInChannelError(request=None)])  # type: ignore[arg-type]
    sender, store, _client, _ = make_sender(tmp_path, client=client)
    try:
        await sender.send(make_job(), Target(id=-2001), daily_limit=60)

        assert store.checked_today(as_repost=True) == 0
        assert store.target_checked_today(-2001) == 0
        assert store.reposted_today() == 0
    finally:
        store.close()


def test_release_checked_never_goes_negative(tmp_path: Path) -> None:
    store = Store(tmp_path / "relay.db")
    try:
        store.release_checked(5, as_repost=True)
        store.release_target_checked(-2001, 5)
        assert store.checked_today(as_repost=True) == 0
        assert store.target_checked_today(-2001) == 0

        store.note_checked(3, as_repost=True)
        store.note_target_checked(-2001, 3)
        assert store.release_checked(1, as_repost=True) == 2
        assert store.release_target_checked(-2001, 1) == 2
    finally:
        store.close()


# --------------------------------------------------------------------------
# 重发循环：走的是 Sender.send()，必须同样受保护
# --------------------------------------------------------------------------


def make_reposter(tmp_path: Path, *, targets: list[Target], client: FakeClient):
    config = AppConfig(
        sources=(-1001,),
        targets=tuple(targets),
        filters=Filters(),
        rate=FAST_RATE,
        behavior=Behavior(queue_size=10),
        repost=RepostConfig(ids=(6, 7), interval=300, daily_limit=60),
    )
    store = Store(tmp_path / "relay.db")
    sender = Sender(client, config, store)
    sender.account_check_interval = 0.0
    sender.account_check_polls = 1
    reposter = Reposter(config, store, sender, client=client)
    reposter.messages = [fake_message(msg_id=i, chat_id=-1001, text=f"素材{i}") for i in (6, 7)]
    reposter._order = [0, 1]
    reposter.source_peer = "src"
    reposter.source_id = -1001
    return reposter, store, sender


async def test_repost_cycle_stops_hammering_after_forbidden(tmp_path: Path) -> None:
    """一轮里第一条撞上禁止发言后，后续素材不该再往同一个群撞。

    这正是 2026-09-12 的现场：一轮里每条素材都试一次、下一轮又来一遍。
    """
    client = CountingClient(fail_with=[UserBannedInChannelError(request=None)])  # type: ignore[arg-type]
    reposter, store, sender = make_reposter(tmp_path, targets=[Target(id=-2001)], client=client)
    try:
        ok, bad = await reposter.run_cycle()
        # 第一条真的调了一次 API，第二条被停发闸挡住；
        # 而且永久失败后本轮就把该目标摘掉了，所以 bad 只记 1（不是 2 × 素材数）
        assert (ok, bad) == (0, 1)
        assert client.calls == 1, f"第二条不该再撞一次，实际调用 {client.calls} 次"

        ok2, bad2 = await reposter.run_cycle()
        assert (ok2, bad2) == (0, 1)
        assert client.calls == 1, "冷却期内整个下一轮都不该产生 API 调用"
        assert store.checked_today(as_repost=True) == 0
    finally:
        store.close()


# --------------------------------------------------------------------------
# stop 必须可逆（面板上的"停止重发"曾是单向操作）
# --------------------------------------------------------------------------


async def test_reposter_start_after_stop_really_restarts(tmp_path: Path) -> None:
    client = FakeClient()
    reposter, store, _sender = make_reposter(tmp_path, targets=[Target(id=-2001)], client=client)
    try:
        reposter.start()
        assert reposter.running is True

        await reposter.stop()
        assert reposter.running is False

        reposter.start()
        assert reposter.running is True, "/stop 之后必须还能重新 start"

        await reposter.stop()
    finally:
        store.close()


async def test_reposter_start_is_idempotent(tmp_path: Path) -> None:
    client = FakeClient()
    reposter, store, _sender = make_reposter(tmp_path, targets=[Target(id=-2001)], client=client)
    try:
        reposter.start()
        first = reposter._task
        reposter.start()
        assert reposter._task is first, "重复 start 不该叠加出第二个循环"
        await reposter.stop()
    finally:
        store.close()


# --------------------------------------------------------------------------
# 问 @SpamBot
# --------------------------------------------------------------------------


class SpamBotClient(FakeClient):
    """假 @SpamBot：返回一段可配置的回复。"""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
        self.sent_to: list[Any] = []

    async def get_entity(self, peer: Any) -> Any:
        return "SpamBotEntity"

    async def send_message(self, peer: Any, message: str) -> Any:
        self.sent_to.append((peer, message))
        return type("M", (), {"id": 1})()

    async def get_messages(self, peer: Any, **kwargs: Any) -> Any:
        return [type("M", (), {"id": 2, "text": self.text})()]


LIMITED_TEXT = (
    "I'm very sorry that you had to contact me. Unfortunately, some actions can "
    "trigger a harsh response from our anti-spam systems. While the account is limited…"
)
FREE_TEXT = "Good news, no limits are currently applied to your account. You're free as a bird!"


@pytest.mark.parametrize(
    ("text", "expected"),
    [(LIMITED_TEXT, True), (FREE_TEXT, False), ("完全没有听说过的东西", None)],
)
async def test_account_status_parses_spambot(tmp_path: Path, text: str, expected: bool | None) -> None:
    client = SpamBotClient(text)
    sender, store, _client, _ = make_sender(tmp_path, client=client)
    try:
        limited, detail = await sender.account_status()
        assert limited is expected
        assert detail
        assert client.sent_to and client.sent_to[0][1] == "/start"
    finally:
        store.close()


async def test_account_status_survives_failure(tmp_path: Path) -> None:
    """问不出来时返回 None，而不是抛异常（这只是一条诊断信息）。"""
    sender, store, _client, _ = make_sender(tmp_path)
    try:
        limited, detail = await sender.account_status()
        assert limited is None
        assert "SpamBot" in detail
    finally:
        store.close()
