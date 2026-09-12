"""Bot /status 的渲染测试：把真实输出跑出来，防止 NameError 之类的运行时错误。

之前踩过：`_cmd_status` 里写了个不存在的 `store` 变量，
因为测试只测了 dispatch 没测实际渲染，直到线上才暴露。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.bot import BotController  # noqa: E402
from tgrelay.config import AppConfig, Behavior, Filters, Rate, RepostConfig, Target, load_config  # noqa: E402
from tgrelay.config_store import ConfigStore  # noqa: E402
from tgrelay.control import RuntimeControl  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.engine import RelayEngine  # noqa: E402
from tgrelay.reposter import Reposter  # noqa: E402
from tgrelay.sender import Sender  # noqa: E402

CONFIG = """\
sources: ["@src"]
targets:
  - id: -1001
    label: 群A
    interval: [30, 35]
rate:
  per_target_interval: [30, 35]
  cross_target_delay: [5, 15]
  global_per_minute: 10
  daily_cap: 100
behavior:
  queue_size: 10
repost:
  enabled: true
  ids: [6]
  interval: 300
  daily_limit: 200
"""


def plain(text: str) -> str:
    """去掉 HTML 标签，方便断言。"""
    return re.sub(r"<[^>]+>", "", text)


async def make_bot(tmp_path: Path, *, repost_interval: float = 300.0, daily_limit: int = 200):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    config = load_config(path)
    from dataclasses import replace

    config = replace(
        config,
        repost=replace(config.repost, interval=repost_interval, daily_limit=daily_limit),
    )
    store = Store(tmp_path / "relay.db")
    client = FakeClient()
    sender = Sender(client, config, store)
    engine = RelayEngine(config, store, sender)
    engine.attach(client)
    reposter = Reposter(config, store, sender, client=client)
    reposter.messages = [fake_message(msg_id=6, chat_id=-1001, text="素材6")]
    reposter._order = [0]
    control = RuntimeControl(
        config,
        store,
        sender,
        engine,
        config_store=ConfigStore(path),
        reposter=reposter,
        client=client,
        log_path=tmp_path / "relay.log",
        config_path=path,
    )
    bot = BotController(control, token="x:y", admins=(1,), admin_file=tmp_path / "ai.txt")
    return bot, control, store


async def test_status_renders_without_error(tmp_path: Path) -> None:
    """核心回归：/status 必须能真的渲染出来（曾经因为变量名写错而崩）。"""
    bot, control, store = await make_bot(tmp_path)
    try:
        text = await bot.dispatch("/status", "")
        body = plain(text)
        assert "运行状态" in body
        assert "群A" in body
        # 发送间隔要显示出来
        assert "发送间隔" in body
        assert "30~35s" in body
    finally:
        store.close()


async def test_status_shows_repost_cycle_interval(tmp_path: Path) -> None:
    bot, control, store = await make_bot(tmp_path, repost_interval=300.0)
    try:
        body = plain(await bot.dispatch("/status", ""))
        assert "轮次间隔" in body
        assert "300s" in body
        # 每小时发多少条（300s -> 12 条/小时）
        assert "12" in body
    finally:
        store.close()


async def test_status_warns_when_cycle_shorter_than_material_time(tmp_path: Path) -> None:
    """轮次间隔小于"一轮最少耗时"时要提示（会不停发）。"""
    bot, control, store = await make_bot(tmp_path, repost_interval=31.0)
    try:
        body = plain(await bot.dispatch("/status", ""))
        assert "31s" in body
        assert "每小时" in body
        assert "不停发" in body or "至少需" in body
    finally:
        store.close()


async def test_status_shows_quota_runway(tmp_path: Path) -> None:
    """偏快节奏下最实用的信息：日额度还能撑多久。

    注意这里必须让"实发数"动起来 —— 额度的剩余量是按实发算的
    （add_sent 只改额度计数，deliveries 才是实发）。
    """
    bot, control, store = await make_bot(tmp_path, repost_interval=300.0, daily_limit=100)
    try:
        # 造 10 条真实成功的重发记录（走 record_delivery，这才是"实发"）
        for i in range(10):
            store.record_delivery(
                job_id=f"repost:-1001:6:{i}",
                target_id=-1001,
                source_id=-1001,
                source_msg_id=6,
                status="sent",
                target_msg_ids=[1000 + i],
            )
            store.add_sent(1, as_repost=True)

        body = plain(await bot.dispatch("/status", ""))
        assert "剩 90 条额度" in body, body
        assert "小时" in body
        # 300s 一轮 -> 每小时 12 条
        assert "12" in body
    finally:
        store.close()


async def test_status_labels_quota_vs_actual(tmp_path: Path) -> None:
    """额度占用和实发是两个数，标签必须区分开（用户曾经被这个数字搞混）。"""
    bot, control, store = await make_bot(tmp_path)
    try:
        store.add_sent(3, as_repost=True)  # 只占额度，没真发
        body = plain(await bot.dispatch("/status", ""))
        assert "额度占用" in body
        assert "实发" in body
    finally:
        store.close()


async def test_status_marks_global_interval(tmp_path: Path) -> None:
    """没单独配间隔的群，显示全局值并加 * 说明。"""
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG.replace("    interval: [30, 35]\n", ""), encoding="utf-8")
    config = load_config(path)
    store = Store(tmp_path / "relay2.db")
    client = FakeClient()
    sender = Sender(client, config, store)
    engine = RelayEngine(config, store, sender)
    engine.attach(client)
    control = RuntimeControl(
        config, store, sender, engine,
        config_store=ConfigStore(path), client=client,
        log_path=tmp_path / "relay.log", config_path=path,
    )
    bot = BotController(control, token="x:y", admins=(1,), admin_file=tmp_path / "ai.txt")
    try:
        body = plain(await bot.dispatch("/status", ""))
        # 用全局的 [30, 35]，并标 *
        assert "30~35s*" in body
        assert "没单独配发送间隔" in body
    finally:
        store.close()


async def test_where_renders(tmp_path: Path) -> None:
    bot, control, store = await make_bot(tmp_path)
    try:
        body = plain(await bot.dispatch("/where", ""))
        assert "关键参数" in body
        assert "@src" in body
    finally:
        store.close()


async def test_sources_renders(tmp_path: Path) -> None:
    bot, control, store = await make_bot(tmp_path)
    try:
        body = plain(await bot.dispatch("/sources", ""))
        assert "源频道" in body
        assert "@src" in body
        assert "主源" in body
    finally:
        store.close()


async def test_help_mentions_both_intervals(tmp_path: Path) -> None:
    """帮助里必须说清两个"间隔"的区别（用户真的搞混过）。"""
    bot, control, store = await make_bot(tmp_path)
    try:
        body = plain(await bot.dispatch("/help", ""))
        assert "发送间隔" in body
        assert "轮次间隔" in body
        assert "搞混" in body or "别把" in body
    finally:
        store.close()
