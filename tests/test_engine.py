"""引擎与队列集成测试：去重、过滤、多目标分发、限速、相册整组转发。

用 FakeClient 顶替 TelegramClient，不联网、不登录，秒级跑完。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.config import AppConfig, Behavior, Filters, Rate, Target  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.engine import RelayEngine  # noqa: E402
from tgrelay.listener import AlbumCollector, _group_albums  # noqa: E402
from tgrelay.sender import Pacer, Sender  # noqa: E402

FAST_RATE = Rate(
    per_target_interval=(0.0, 0.0),
    cross_target_delay=(0.0, 0.0),
    global_per_minute=6000,
    daily_cap=1000,
)


def make_config(
    targets: list[Target] | None = None,
    *,
    filters: Filters | None = None,
    behavior: Behavior | None = None,
    rate: Rate | None = None,
    premium: bool = False,
) -> AppConfig:
    return AppConfig(
        sources=(-1001,),
        targets=tuple(targets or [Target(id=-2001), Target(id=-2002)]),
        filters=filters or Filters(),
        rate=rate or FAST_RATE,
        behavior=behavior or Behavior(queue_size=10),
        premium=premium,
        db_path=":memory:",
    )


async def build(
    tmp_path: Path,
    *,
    config: AppConfig | None = None,
    client: FakeClient | None = None,
) -> tuple[RelayEngine, Store, FakeClient, Sender]:
    config = config or make_config()
    store = Store(tmp_path / "relay.db")
    client = client or FakeClient()
    sender = Sender(client, config, store)
    engine = RelayEngine(config, store, sender)
    engine.attach(client)
    await engine.start()
    return engine, store, client, sender


async def idle() -> None:
    """给 worker 一点时间把队列消费完。"""
    for _ in range(40):
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------
# 基础分发
# --------------------------------------------------------------------------


async def test_single_message_fans_out_to_all_targets(tmp_path: Path) -> None:
    engine, store, client, _ = await build(tmp_path)
    try:
        queued = await engine.handle_message(fake_message(msg_id=7, text="你好"), source_peer=1)
        assert queued is True
        await idle()

        assert len(client.sent) == 2
        assert {call[0] for call in client.sent} == {-2001, -2002}
        assert all(call[1] == (7,) for call in client.sent)
        assert all(call[2] == 1 for call in client.sent)  # from_peer 保留来源
        assert store.sent_today() == 2
        assert store.find_delivery("-1001:7", -2001).status == "sent"
    finally:
        await engine.stop()
        store.close()


async def test_duplicate_message_is_not_forwarded_twice(tmp_path: Path) -> None:
    engine, store, client, _ = await build(tmp_path)
    try:
        message = fake_message(msg_id=7, text="你好")
        assert await engine.handle_message(message, source_peer=1) is True
        assert await engine.handle_message(message, source_peer=1) is False
        await idle()
        assert len(client.sent) == 2  # 两个目标各一次，不是四次
        assert engine.stats.duplicates == 1
    finally:
        await engine.stop()
        store.close()


async def test_replayed_message_after_reconnect_is_deduped(tmp_path: Path) -> None:
    config = make_config(targets=[Target(id=-2001)])
    store = Store(tmp_path / "relay.db")
    client = FakeClient()

    sender = Sender(client, config, store)
    first = RelayEngine(config, store, sender)
    first.attach(client)
    await first.start()
    await first.handle_message(fake_message(msg_id=42, text="hi"), source_peer=1)
    await idle()
    await first.stop()

    # 模拟重启后 Telethon 重放同一条更新
    sender2 = Sender(client, config, store)
    second = RelayEngine(config, store, sender2)
    second.attach(client)
    await second.start()
    assert await second.handle_message(fake_message(msg_id=42, text="hi"), source_peer=1) is False
    await idle()
    await second.stop()

    assert len(client.sent) == 1
    store.close()


async def test_filtered_message_is_skipped(tmp_path: Path) -> None:
    config = make_config(filters=Filters(keywords=("上新",)))
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        assert await engine.handle_message(fake_message(msg_id=1, text="普通消息"), source_peer=1) is False
        assert await engine.handle_message(fake_message(msg_id=2, text="今天上新"), source_peer=1) is True
        await idle()
        assert len(client.sent) == 2
        assert engine.stats.filtered == 1
    finally:
        await engine.stop()
        store.close()


async def test_cursor_advances(tmp_path: Path) -> None:
    engine, store, _, _ = await build(tmp_path)
    try:
        await engine.handle_message(fake_message(msg_id=5), source_peer=1)
        await engine.handle_message(fake_message(msg_id=9), source_peer=1)
        await idle()
        assert store.get_cursor(-1001) == 9
    finally:
        await engine.stop()
        store.close()


async def test_message_without_ids_is_ignored(tmp_path: Path) -> None:
    engine, store, client, _ = await build(tmp_path)
    try:
        broken = fake_message(msg_id=1)
        broken.chat_id = None
        assert await engine.handle_message(broken, source_peer=1) is False
        await idle()
        assert client.sent == []
    finally:
        await engine.stop()
        store.close()


# --------------------------------------------------------------------------
# 相册
# --------------------------------------------------------------------------


async def test_album_is_forwarded_as_one_group(tmp_path: Path) -> None:
    config = make_config(targets=[Target(id=-2001)])
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        photos = [fake_message(msg_id=100 + i, kind="photo", grouped_id=777) for i in range(4)]
        assert await engine.handle_album(photos, 1) is True
        await idle()

        assert len(client.sent) == 1  # 4 张图只发 1 次
        assert client.sent[0][1] == (100, 101, 102, 103)
        assert client.as_album_flags == [True]
        assert store.find_delivery("-1001:100:album4", -2001).target_msg_ids == (5001, 5002, 5003, 5004)
    finally:
        await engine.stop()
        store.close()


async def test_album_longer_than_ten_is_truncated(tmp_path: Path) -> None:
    config = make_config(targets=[Target(id=-2001)])
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        photos = [fake_message(msg_id=i, kind="photo", grouped_id=1) for i in range(15)]
        await engine.handle_album(photos, 1)
        await idle()
        assert client.sent[0][1] == tuple(range(10))
    finally:
        await engine.stop()
        store.close()


async def test_album_partially_seen_only_forwards_new_items(tmp_path: Path) -> None:
    config = make_config(targets=[Target(id=-2001)])
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        store.mark_seen(-1001, 100)
        photos = [fake_message(msg_id=100 + i, kind="photo", grouped_id=5) for i in range(3)]
        await engine.handle_album(photos, 1)
        await idle()
        assert client.sent[0][1] == (101, 102)
    finally:
        await engine.stop()
        store.close()


async def test_album_fully_seen_is_skipped(tmp_path: Path) -> None:
    config = make_config(targets=[Target(id=-2001)])
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        for msg_id in (100, 101):
            store.mark_seen(-1001, msg_id)
        photos = [fake_message(msg_id=100 + i, kind="photo", grouped_id=5) for i in range(2)]
        assert await engine.handle_album(photos, 1) is False
        await idle()
        assert client.sent == []
    finally:
        await engine.stop()
        store.close()


async def test_album_collector_batches_by_grouped_id(tmp_path: Path) -> None:
    batches: list[tuple[list[int], object]] = []

    async def callback(messages: list[object], peer: object) -> None:
        batches.append(([m.id for m in messages], peer))  # type: ignore[attr-defined]

    collector = AlbumCollector(callback, window=0.05)
    for msg_id in (1, 2, 3):
        collector.add(fake_message(msg_id=msg_id, kind="photo", grouped_id=99), "peer")
    await asyncio.sleep(0.15)

    assert batches == [([1, 2, 3], "peer")]
    await collector.flush_all()


async def test_album_collector_ignores_messages_without_group(tmp_path: Path) -> None:
    batches: list[list[int]] = []

    async def callback(messages: list[object], peer: object) -> None:
        batches.append([m.id for m in messages])  # type: ignore[attr-defined]

    collector = AlbumCollector(callback, window=0.05)
    collector.add(fake_message(msg_id=1), None)
    await asyncio.sleep(0.1)
    assert batches == []


async def test_album_collector_flush_all_emits_pending(tmp_path: Path) -> None:
    batches: list[list[int]] = []

    async def callback(messages: list[object], peer: object) -> None:
        batches.append([m.id for m in messages])  # type: ignore[attr-defined]

    collector = AlbumCollector(callback, window=30)
    collector.add(fake_message(msg_id=1, kind="photo", grouped_id=1), None)
    collector.add(fake_message(msg_id=2, kind="photo", grouped_id=1), None)
    await collector.flush_all()
    assert batches == [[1, 2]]


def test_group_albums_helper() -> None:
    messages = [
        fake_message(msg_id=1, kind="photo", grouped_id=7),
        fake_message(msg_id=2, kind="photo", grouped_id=7),
        fake_message(msg_id=3, text="单条"),
        fake_message(msg_id=4, kind="photo", grouped_id=8),
        fake_message(msg_id=5, kind="photo", grouped_id=8),
        fake_message(msg_id=6, kind="photo", grouped_id=8),
    ]
    grouped = _group_albums(messages)
    assert [len(item) if isinstance(item, list) else 1 for item in grouped] == [2, 1, 3]


# --------------------------------------------------------------------------
# 队列与限速
# --------------------------------------------------------------------------


async def test_queue_full_evicts_oldest_and_keeps_latest(tmp_path: Path) -> None:
    """队列满时丢最旧的、保最新的：慢速模式把目标压住时，用户最想看的还是新内容。"""
    config = make_config(
        targets=[Target(id=-2001)],
        rate=Rate(
            per_target_interval=(0.0, 0.0),
            cross_target_delay=(0.0, 0.0),
            global_per_minute=6000,
            daily_cap=1000,
        ),
        behavior=Behavior(queue_size=2),
    )
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        # 第一条发送卡在 gate 上：后续消息堆在队列里，确定性地触发淘汰
        gate = asyncio.Event()
        calls = {"n": 0}
        original = client.forward_messages

        async def gated(entity: object, messages: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                await gate.wait()
            return await original(entity, messages, **kwargs)  # type: ignore[arg-type]

        client.forward_messages = gated  # type: ignore[assignment]

        total = 11
        results = [
            await engine.handle_message(fake_message(msg_id=msg_id), source_peer=1)
            for msg_id in range(1, total + 1)
        ]

        worker = engine.workers["-2001"]
        # 滑窗：入队全部成功（监听永远不被阻塞），但队列长度封顶
        assert all(results)
        assert worker.queue.qsize() <= 2
        assert worker.stats.peak_backlog <= 2
        assert worker.stats.dropped >= total - 3  # 除在发的那条和队列里剩的，其余都被淘汰

        # 队列里留下的是最新的两条
        remaining = sorted(
            worker.queue._queue[i].source_msg_id for i in range(worker.queue.qsize())
        )
        assert remaining[-1] == total
        assert remaining == list(range(total - len(remaining) + 1, total + 1))

        gate.set()
        await idle()
        assert calls["n"] >= 2
    finally:
        await engine.stop()
        store.close()


async def test_queue_drop_policy_rejects_when_configured(tmp_path: Path) -> None:
    """drop_on_queue_full=false 时退回"拒绝新任务"的老行为。"""
    config = make_config(
        targets=[Target(id=-2001)],
        rate=Rate(
            per_target_interval=(0.0, 0.0),
            cross_target_delay=(0.0, 0.0),
            global_per_minute=6000,
            daily_cap=1000,
        ),
        behavior=Behavior(queue_size=2, drop_on_queue_full=False),
    )
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        gate = asyncio.Event()
        calls = {"n": 0}
        original = client.forward_messages

        async def gated(entity: object, messages: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                await gate.wait()
            return await original(entity, messages, **kwargs)  # type: ignore[arg-type]

        client.forward_messages = gated  # type: ignore[assignment]

        results = [
            await engine.handle_message(fake_message(msg_id=msg_id), source_peer=1)
            for msg_id in range(1, 12)
        ]
        worker = engine.workers["-2001"]
        assert results.count(False) >= 8  # 新消息被拒
        assert worker.stats.dropped == results.count(False)
        assert engine.stats.dropped == results.count(False)

        gate.set()
        await idle()
    finally:
        await engine.stop()
        store.close()


# --------------------------------------------------------------------------
# 慢速模式（群设置 30s 这种）
# --------------------------------------------------------------------------


def _slow_mode_error(seconds: int):
    from telethon.errors import SlowModeWaitError

    error = SlowModeWaitError(request=None)  # type: ignore[arg-type]
    error.seconds = seconds
    return error


async def test_slow_mode_waits_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """慢速模式不消耗发送尝试额度：撞上后应当等待并最终成功，而不是把消息丢掉。"""
    config = make_config(
        targets=[Target(id=-2001)],
        rate=Rate(
            per_target_interval=(0.0, 0.0),
            cross_target_delay=(0.0, 0.0),
            global_per_minute=6000,
            daily_cap=1000,
        ),
    )
    engine, store, client, sender = await build(tmp_path, config=config)
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(0 if seconds > 0.05 else seconds)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    try:
        client.fail_with = [_slow_mode_error(30)]
        await engine.handle_message(fake_message(msg_id=1), source_peer=1)
        await idle()

        assert sender.stats.slow_waits == 1
        assert len(client.sent) == 1  # 等完慢速窗口后成功了
        assert sender.stats.skipped == 0
        assert sender.stats.failed == 0
        assert store.find_delivery("-1001:1", -2001).status == "sent"
        # 记住了这个群的慢速窗口，供后续消息预判
        assert sender.slow.seconds_for(-2001) == 30
    finally:
        await engine.stop()
        store.close()


async def test_slow_mode_is_predicted_for_following_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次发送应当在**发送前**就等到窗口结束，而不是再撞一次墙。"""
    config = make_config(
        targets=[Target(id=-2001)],
        rate=Rate(
            per_target_interval=(0.0, 0.0),
            cross_target_delay=(0.0, 0.0),
            global_per_minute=6000,
            daily_cap=1000,
        ),
    )
    engine, store, client, sender = await build(tmp_path, config=config)

    real_sleep = asyncio.sleep
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        await real_sleep(0 if seconds > 0.05 else seconds)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    try:
        # 第一次只撞一次墙，让 sender 记住"这个群 30s"
        client.fail_with = [_slow_mode_error(30)]
        await engine.handle_message(fake_message(msg_id=1), source_peer=1)
        await idle()
        assert sender.stats.slow_waits == 1

        # 第二次：不再有错误注入，应当靠预判等待完成，slow_waits 不增加
        slept.clear()
        await engine.handle_message(fake_message(msg_id=2), source_peer=1)
        await idle()

        assert sender.stats.slow_waits == 1  # 没有第二次撞墙
        assert len(client.sent) == 2
        assert any(value > 1 for value in slept)  # 确实在发送前等了窗口
        assert sender.slow.remaining(-2001) >= 0
    finally:
        await engine.stop()
        store.close()


async def test_slow_mode_window_expires_when_idle(tmp_path: Path) -> None:
    """窗口过期后要能自愈，否则一条错误估计会把之后所有消息无限延后。"""
    from tgrelay.sender import SlowModeGuard

    guard = SlowModeGuard()
    assert guard.remaining(-2001) == 0.0
    guard.penalize(-2001, 0.05)
    assert guard.remaining(-2001) > 0
    await asyncio.sleep(0.12)
    assert guard.remaining(-2001) == 0.0  # 过期并自动清空
    assert guard.active() == {}


async def test_slow_mode_too_long_is_skipped_not_failed(tmp_path: Path) -> None:
    """等太久的慢速窗口直接跳过本条，且不算失败（不污染失败统计、不重试轰炸）。"""
    from tgrelay.sender import RelayJob

    config = make_config(targets=[Target(id=-2001)])
    engine, store, client, sender = await build(tmp_path, config=config)
    try:
        sender.slow.cooldown_cap = 60.0
        sender.slow.penalize(-2001, 3600)  # 1 小时的慢速窗口
        job = RelayJob(job_id="-1001:1", source_id=-1001, source_peer=1, msg_ids=(1,))
        outcome = await sender.send(job, config.targets[0])

        assert outcome.status == "skipped"
        assert outcome.permanent is False  # 不暂停目标，下一条还会尝试
        assert client.sent == []
        assert sender.stats.skipped == 1
        assert sender.stats.failed == 0
    finally:
        await engine.stop()
        store.close()


async def test_paused_target_auto_resumes(tmp_path: Path) -> None:
    """暂停的目标在空转一段时间后自动恢复探测，实现自愈。"""
    from tgrelay.sender import RESUME_PROBE_INTERVAL

    config = make_config(targets=[Target(id=-2001)])
    engine, store, _, _ = await build(tmp_path, config=config)
    try:
        worker = engine.workers["-2001"]
        worker.paused = True

        from tgrelay.sender import RelayJob

        assert worker.submit(RelayJob(job_id="x", source_id=-1001, source_peer=1, msg_ids=(1,))) is False

        worker._probe_resume()
        assert worker.paused is False
        assert RESUME_PROBE_INTERVAL > 0
    finally:
        await engine.stop()
        store.close()


async def test_daily_cap_skips_remaining_sends(tmp_path: Path) -> None:
    config = make_config(
        targets=[Target(id=-2001)],
        rate=Rate(
            per_target_interval=(0.0, 0.0),
            cross_target_delay=(0.0, 0.0),
            global_per_minute=6000,
            daily_cap=1,
        ),
    )
    engine, store, client, sender = await build(tmp_path, config=config)
    try:
        await engine.handle_message(fake_message(msg_id=1), source_peer=1)
        await engine.handle_message(fake_message(msg_id=2), source_peer=1)
        await idle()
        assert len(client.sent) == 1
        assert sender.stats.skipped >= 1
    finally:
        await engine.stop()
        store.close()


async def test_premium_relaxes_limits(tmp_path: Path) -> None:
    base = Pacer(Rate(global_per_minute=20, daily_cap=200), premium=False)
    premium = Pacer(Rate(global_per_minute=20, daily_cap=200), premium=True)
    assert base.global_per_minute == 20
    assert premium.global_per_minute == 80
    assert premium.daily_cap == 800


async def test_flood_wait_penalizes_and_slows_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from telethon.errors import FloodWaitError

    config = make_config(
        targets=[Target(id=-2001)],
        rate=Rate(
            per_target_interval=(0.0, 0.0),
            cross_target_delay=(0.0, 0.0),
            global_per_minute=6000,
            daily_cap=1000,
        ),
    )
    engine, store, client, sender = await build(tmp_path, config=config)

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        """跳过退避等待，让测试秒级跑完（阈值逻辑本身照常验证）。"""
        await real_sleep(0 if seconds > 0.05 else seconds)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    try:
        def make_flood() -> FloodWaitError:
            error = FloodWaitError(request=None)  # type: ignore[arg-type]
            error.seconds = 60
            return error

        client.fail_with = [make_flood(), make_flood()]
        await engine.handle_message(fake_message(msg_id=1), source_peer=1)
        await idle()

        assert sender.stats.flood_waits == 2  # 前两次被 FloodWait 罚站
        assert sender.pacer.global_per_minute < 6000  # 自适应降速生效
        assert sender.pacer.global_per_minute == 1500  # 6000 -> 3000 -> 1500
        assert sender.flood.active()  # 该目标被记号
        assert len(client.sent) == 1  # 退避后第三次成功
    finally:
        await engine.stop()
        store.close()


async def test_permanent_error_pauses_target(tmp_path: Path) -> None:
    from telethon.errors import ChatWriteForbiddenError

    config = make_config(targets=[Target(id=-2001), Target(id=-2002)])
    engine, store, client, sender = await build(tmp_path, config=config)
    try:
        error = ChatWriteForbiddenError(request=None)  # type: ignore[arg-type]
        client.fail_with = [error]

        await engine.handle_message(fake_message(msg_id=1), source_peer=1)
        await idle()

        paused = [worker.target.display for worker in engine.workers.values() if worker.paused]
        assert len(paused) == 1  # 只暂停出错的那个目标
        assert store.find_delivery("-1001:1", paused[0]).status == "failed"
        healthy = [t for t in ("-2001", "-2002") if t != paused[0]]
        assert store.find_delivery("-1001:1", healthy[0]).status == "sent"
    finally:
        await engine.stop()
        store.close()


async def test_worker_pause_stops_accepting(tmp_path: Path) -> None:
    from tgrelay.sender import RelayJob

    config = make_config(targets=[Target(id=-2001)])
    engine, store, _, _ = await build(tmp_path, config=config)
    try:
        worker = engine.workers["-2001"]
        worker.paused = True
        job = RelayJob(job_id="x", source_id=-1001, source_peer=1, msg_ids=(1,))
        assert worker.submit(job) is False
        assert engine.enqueue(job) == 0
        assert engine.stats.dropped == 1
    finally:
        await engine.stop()
        store.close()


# --------------------------------------------------------------------------
# 编辑 / 删除同步
# --------------------------------------------------------------------------


async def test_edit_sync_updates_delivered_targets(tmp_path: Path) -> None:
    config = make_config(targets=[Target(id=-2001)], behavior=Behavior(sync_edits=True))
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        await engine.handle_message(fake_message(msg_id=1, text="原文"), source_peer=1)
        await idle()
        edited = fake_message(msg_id=1, text="改过的文本")
        await engine.handle_edit(edited)
        edited_calls = [call for call in client.sent if call[2] == "edit"]
        assert len(edited_calls) == 1
        assert engine.stats.edited == 1
    finally:
        await engine.stop()
        store.close()


async def test_edit_sync_disabled_by_default(tmp_path: Path) -> None:
    config = make_config(targets=[Target(id=-2001)], behavior=Behavior(sync_edits=False))
    engine, store, client, _ = await build(tmp_path, config=config)
    try:
        await engine.handle_message(fake_message(msg_id=1, text="原文"), source_peer=1)
        await idle()
        before = len(client.sent)
        await engine.handle_edit(fake_message(msg_id=1, text="改过"))
        assert len(client.sent) == before
    finally:
        await engine.stop()
        store.close()


async def test_report_has_expected_shape(tmp_path: Path) -> None:
    engine, store, _, _ = await build(tmp_path)
    try:
        await engine.handle_message(fake_message(msg_id=1), source_peer=1)
        await idle()
        report = engine.report()
        assert report["engine"]["received"] == 1
        assert report["engine"]["jobs"] == 1
        assert report["sender"]["sent"] == 2
        assert set(report["targets"]) == {"-2001", "-2002"}
        assert report["store"]["sent_today"] == 2
    finally:
        await engine.stop()
        store.close()
