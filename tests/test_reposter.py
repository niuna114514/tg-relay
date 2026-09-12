"""定时重发（repost）测试：素材解析、循环节奏、慢速约束、配额分离。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.config import (  # noqa: E402
    AppConfig,
    Behavior,
    ConfigError,
    Filters,
    Rate,
    RepostConfig,
    Target,
    parse_range_spec,
)
from tgrelay.db import Store  # noqa: E402
from tgrelay.reposter import MIN_INTERVAL, Reposter  # noqa: E402
from tgrelay.sender import Sender  # noqa: E402

FAST_RATE = Rate(
    per_target_interval=(0.0, 0.0),
    cross_target_delay=(0.0, 0.0),
    global_per_minute=6000,
    daily_cap=100,
)


# --------------------------------------------------------------------------
# 范围解析
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6", (6, 6)),
        ("6-12", (6, 12)),
        ("6..12", (6, 12)),
        ("6~12", (6, 12)),
        (" 6 - 12 ", (6, 12)),
        (6, (6, 6)),
        (12, (12, 12)),
    ],
)
def test_parse_range_spec(text: object, expected: tuple[int, int]) -> None:
    assert parse_range_spec(text, "repost.ranges[0]") == expected


@pytest.mark.parametrize("text", ["12-6", "abc", "0", "6-0", ""])
def test_parse_range_spec_rejects_bad_input(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_range_spec(text, "repost.ranges[0]")


def test_repost_message_ids_merges_and_dedupes() -> None:
    config = RepostConfig(ids=(9, 5), ranges=((6, 8),))
    assert config.message_ids() == (6, 7, 8, 9, 5)


def test_repost_message_ids_removes_overlap() -> None:
    config = RepostConfig(ids=(7,), ranges=((6, 8),))
    assert config.message_ids() == (6, 7, 8)


# --------------------------------------------------------------------------
# 配置校验
# --------------------------------------------------------------------------


def write_config(tmp_path: Path, repost_block: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
sources:
  - "@src"
targets:
  - id: -1001
""" + repost_block,
        encoding="utf-8",
    )
    return path


def test_repost_interval_below_slow_mode_is_rejected(tmp_path: Path) -> None:
    from tgrelay.config import load_config

    path = write_config(
        tmp_path,
        """
repost:
  enabled: true
  ranges: ["6-10"]
  interval: 10
""",
    )
    with pytest.raises(ConfigError, match="慢速模式"):
        load_config(path)


def test_repost_interval_at_31_is_accepted(tmp_path: Path) -> None:
    from tgrelay.config import load_config

    path = write_config(
        tmp_path,
        """
repost:
  enabled: true
  ranges: ["6-10"]
  interval: 31
""",
    )
    config = load_config(path)
    assert config.repost.interval == 31
    assert config.repost.message_ids() == (6, 7, 8, 9, 10)


def test_repost_daily_limit_above_ceiling_is_rejected(tmp_path: Path) -> None:
    from tgrelay.config import load_config

    path = write_config(
        tmp_path,
        """
repost:
  enabled: true
  ids: [6]
  daily_limit: 99999
""",
    )
    with pytest.raises(ConfigError, match="理论"):
        load_config(path)


def test_repost_ids_must_be_single(tmp_path: Path) -> None:
    from tgrelay.config import load_config

    path = write_config(
        tmp_path,
        """
repost:
  enabled: true
  ids: ["6-10"]
""",
    )
    with pytest.raises(ConfigError, match="ranges"):
        load_config(path)


def test_repost_bad_run_mode_is_rejected(tmp_path: Path) -> None:
    from tgrelay.config import load_config

    path = write_config(
        tmp_path,
        """
repost:
  enabled: true
  ids: [6]
  run_mode: forever
""",
    )
    with pytest.raises(ConfigError, match="run_mode"):
        load_config(path)


def test_repost_disabled_allows_loose_values(tmp_path: Path) -> None:
    """没开启校验时不要拦人——用户可能先写好配置但暂时不启用。"""
    from tgrelay.config import load_config

    path = write_config(
        tmp_path,
        """
repost:
  enabled: false
  ids: [6]
  interval: 5
""",
    )
    config = load_config(path)
    assert config.repost.enabled is False
    assert config.repost.interval == 5


# --------------------------------------------------------------------------
# 发送行为
# --------------------------------------------------------------------------


async def make_reposter(
    tmp_path: Path,
    *,
    repost: RepostConfig,
    targets: list[Target] | None = None,
    client: FakeClient | None = None,
) -> tuple[Reposter, Store, FakeClient, Sender]:
    targets = targets or [Target(id=-2001)]
    config = AppConfig(
        sources=(-1001,),
        targets=tuple(targets),
        filters=Filters(),
        rate=FAST_RATE,
        behavior=Behavior(queue_size=10),
        repost=repost,
    )
    store = Store(tmp_path / "relay.db")
    client = client or FakeClient()
    sender = Sender(client, config, store)
    reposter = Reposter(config, store, sender, client=client)
    # 直接注入素材，跳过网络拉取
    reposter.messages = [fake_message(msg_id=i, chat_id=-1001, text=f"素材{i}") for i in repost.message_ids()]
    reposter._order = list(range(len(reposter.messages)))
    reposter.source_peer = "src"
    reposter.source_id = -1001
    return reposter, store, client, sender


async def test_run_cycle_posts_every_message_once(tmp_path: Path) -> None:
    reposter, store, client, _ = await make_reposter(
        tmp_path, repost=RepostConfig(ids=(6, 7), ranges=())
    )
    try:
        ok, bad = await reposter.run_cycle()
        assert (ok, bad) == (2, 0)
        assert [call[1] for call in client.sent] == [(6,), (7,)]
        assert reposter.stats.cycles == 1
        # 跨重启可查的重发计数
        assert store.repost_counts() == {6: 1, 7: 1}
        assert store.reposted_today() == 2
    finally:
        store.close()


async def test_repost_does_not_consume_relay_budget(tmp_path: Path) -> None:
    """重发和实时转发是两本账，互相不挤占。"""
    reposter, store, client, _ = await make_reposter(
        tmp_path,
        repost=RepostConfig(ids=(6,), daily_limit=50),
    )
    try:
        await reposter.run_cycle()
        assert store.reposted_today() == 1
        assert store.sent_today() == 0  # 实时转发的账没被碰
    finally:
        store.close()


async def test_repost_respects_its_own_daily_limit(tmp_path: Path) -> None:
    reposter, store, client, _ = await make_reposter(
        tmp_path,
        repost=RepostConfig(ids=(6, 7, 8), daily_limit=2),
    )
    try:
        await reposter.run_cycle()
        assert len(client.sent) == 2  # 限额 2，第三条被跳过
        assert store.reposted_today() == 2
        assert reposter.stats.skipped == 1
        # 被配额跳过的那条不算"重发过"，计数里不应出现
        assert store.repost_counts() == {6: 1, 7: 1}
    finally:
        store.close()


async def test_repost_counts_only_successful_posts(tmp_path: Path) -> None:
    """失败/跳过都不算"成功重发过一次"，只有真发出去的才计数。"""
    from telethon.errors import ChatWriteForbiddenError

    reposter, store, client, _ = await make_reposter(
        tmp_path, repost=RepostConfig(ids=(6, 7))
    )
    try:
        # 让第 6 条成功、第 7 条撞上"禁止发言"
        original = client.forward_messages
        calls = {"n": 0}

        async def flaky(entity: object, messages: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 2:
                raise ChatWriteForbiddenError(request=None)  # type: ignore[arg-type]
            return await original(entity, messages, **kwargs)  # type: ignore[arg-type]

        client.forward_messages = flaky  # type: ignore[assignment]
        await reposter.run_cycle()

        assert reposter.stats.failed == 1
        assert store.repost_counts() == {6: 1}  # 只有第 6 条真的发出去了

        # 语义区分（重要）：
        #   reposted_today / sent_today = 真正发出去的条数
        #   checked_today               = 额度占位次数
        # 失败那条**占位已经退回去了**（release_checked）：它根本没送到群里，
        # 不该继续占着当天的配额，否则一个被封的目标会把配额整个吃光。
        assert store.reposted_today() == 1
        assert store.checked_today(as_repost=True) == 1
        assert store.sent_ok_today(as_repost=True) == 1
        assert store.sent_ok_today() == 1          # 总共只发出 1 条
        assert store.sent_ok_today(as_repost=False) == 0  # 实时转发一条都没发
    finally:
        store.close()


async def test_repost_fans_out_to_all_targets(tmp_path: Path) -> None:
    reposter, store, client, _ = await make_reposter(
        tmp_path,
        repost=RepostConfig(ids=(6,)),
        targets=[Target(id=-2001), Target(id=-2002)],
    )
    try:
        ok, _ = await reposter.run_cycle()
        assert ok == 2
        assert {call[0] for call in client.sent} == {-2001, -2002}
        assert store.reposted_today() == 2
    finally:
        store.close()


async def test_repost_target_filter(tmp_path: Path) -> None:
    reposter, store, client, _ = await make_reposter(
        tmp_path,
        repost=RepostConfig(ids=(6,), targets=("-2002",)),
        targets=[Target(id=-2001), Target(id=-2002)],
    )
    try:
        await reposter.run_cycle()
        assert [call[0] for call in client.sent] == [-2002]
    finally:
        store.close()


async def test_repost_order_can_be_shuffled(tmp_path: Path) -> None:
    reposter, store, client, _ = await make_reposter(
        tmp_path,
        repost=RepostConfig(ids=(1, 2, 3, 4, 5), shuffle=True),
    )
    try:
        await reposter.run_cycle()
        sent_ids = [call[1][0] for call in client.sent]
        assert sorted(sent_ids) == [1, 2, 3, 4, 5]  # 条数不变，只是顺序可能不同
    finally:
        store.close()


async def test_repost_skips_when_slow_mode_window_too_long(tmp_path: Path) -> None:
    """慢速窗口太长时记 skipped，不算失败，也不停下整个循环。"""
    reposter, store, client, sender = await make_reposter(
        tmp_path, repost=RepostConfig(ids=(6,))
    )
    try:
        sender.slow.cooldown_cap = 60.0
        sender.slow.penalize(-2001, 3600)
        ok, bad = await reposter.run_cycle()
        assert ok == 0 and bad == 1
        assert reposter.stats.skipped == 1
        assert reposter.stats.failed == 0
    finally:
        store.close()


async def test_repost_records_delivery_rows(tmp_path: Path) -> None:
    """重发也进投递账本，job_id 带 repost 前缀，不会和实时转发撞键。"""
    reposter, store, _, _ = await make_reposter(
        tmp_path, repost=RepostConfig(ids=(6,))
    )
    try:
        await reposter.run_cycle()
        rows = store._conn.execute(
            "SELECT job_id, status, source_msg_id FROM deliveries WHERE job_id LIKE 'repost:%'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["job_id"].startswith("repost:-1001:6:")
        assert rows[0]["status"] == "sent"
        assert rows[0]["source_msg_id"] == 6
    finally:
        store.close()


async def test_repeated_cycles_append_history(tmp_path: Path) -> None:
    """同一素材重发多次要留下多行记录，否则没法统计'今天发了几次'。"""
    reposter, store, _, _ = await make_reposter(
        tmp_path, repost=RepostConfig(ids=(6,))
    )
    try:
        await reposter.run_cycle()
        await reposter.run_cycle()
        await reposter.run_cycle()
        rows = store._conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE job_id LIKE 'repost:%' AND status = 'sent'"
        ).fetchone()
        assert rows["n"] == 3
        assert store.sent_ok_today(as_repost=True) == 3
        assert store.repost_counts() == {6: 3}
    finally:
        store.close()


async def test_loop_stops_promptly_on_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """常驻循环必须能被快速叫停，否则退出时会卡住。"""
    reposter, store, _, _ = await make_reposter(
        tmp_path, repost=RepostConfig(ids=(6,), interval=300)
    )
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(0 if seconds > 0.05 else seconds)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    try:
        reposter.start()
        await real_sleep(0.15)
        await reposter.stop()
        assert reposter.stats.cycles >= 1
    finally:
        store.close()


def test_min_interval_matches_slow_mode() -> None:
    assert MIN_INTERVAL >= 31  # 群慢速 30s，留 1s 余量
