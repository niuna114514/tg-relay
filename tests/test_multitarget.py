"""多群批量管理测试：按目标独立配额、批量操作、以及配额在真实发送中的生效。

核心场景：N 个群共享一个全局额度会让"排在后面的群永远发不出去"，
所以每个目标要有自己的 daily_limit。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from helpers import FakeClient, fake_message  # noqa: E402
from tgrelay.config import (  # noqa: E402
    AppConfig,
    Behavior,
    Filters,
    Rate,
    RepostConfig,
    Target,
    load_config,
)
from tgrelay.config_store import ConfigStore  # noqa: E402
from tgrelay.control import ControlError, RuntimeControl  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.engine import RelayEngine  # noqa: E402
from tgrelay.sender import RelayJob, Sender  # noqa: E402
from tgrelay.webapp import create_app  # noqa: E402

TOKEN = "test-token"

FAST_RATE = Rate(
    per_target_interval=(0.0, 0.0),
    cross_target_delay=(0.0, 0.0),
    global_per_minute=6000,
    daily_cap=100,
)

CONFIG = """\
sources: ["@src"]
targets:
  - id: -1001
    label: 群A
  - id: -1002
    label: 群B
  - id: -1003
    label: 群C
rate:
  per_target_interval: [5, 10]
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


def build(
    tmp_path: Path,
    *,
    specs: list[tuple[Any, str, int]] | None = None,
):
    """建一套可用的 control/engine/sender。

    specs 用 (id, label, daily_limit) 描述目标**并写进 config.yaml**，
    这样 engine 从配置里建出来的 worker 和 config.targets 是一致的
    （不能"先建 worker 再改 config"，那会造成两边不一致）。
    """
    if specs is None:
        specs = [(-1001, "群A", 0), (-1002, "群B", 0), (-1003, "群C", 0)]

    lines = ['sources: ["@src"]', "targets:"]
    for target_id, label, limit in specs:
        lines.append(f"  - id: {target_id}")
        lines.append(f"    label: {label}")
        if limit:
            lines.append(f"    daily_limit: {limit}")
    lines += [
        "rate:",
        # 测试要用"能瞬间发完"的配速，否则每条要等 5~15 秒，
        # 测试会在发送完成前就断言（这里踩过一次坑）。
        "  per_target_interval: [0, 0]",
        "  cross_target_delay: [0, 0]",
        "  global_per_minute: 6000",
        "  daily_cap: 100",
        "behavior:",
        "  queue_size: 10",
        "repost:",
        "  enabled: true",
        "  ids: [6]",
        "  interval: 300",
        "  daily_limit: 200",
    ]
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    config = load_config(path)
    store = Store(tmp_path / "relay.db")
    client = FakeClient()
    sender = Sender(client, config, store)
    engine = RelayEngine(config, store, sender)
    engine.attach(client)
    control = RuntimeControl(
        config,
        store,
        sender,
        engine,
        config_store=ConfigStore(path),
        client=client,
        log_path=tmp_path / "relay.log",
        config_path=path,
    )
    return control, engine, sender, store, client, path


# --------------------------------------------------------------------------
# 按目标配额：配置解析
# --------------------------------------------------------------------------


def test_target_daily_limit_parsed(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'sources: ["@src"]\ntargets:\n'
        "  - id: -1001\n    label: 群A\n    daily_limit: 30\n"
        "  - id: -1002\n    label: 群B\n"
        "rate:\n  global_per_minute: 10\n  daily_cap: 100\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.targets[0].daily_limit == 30
    assert config.targets[1].daily_limit == 0  # 未设 = 不限


# --------------------------------------------------------------------------
# 按目标配额：实际发送时生效
# --------------------------------------------------------------------------


async def drain(engine: Any, rounds: int = 200) -> None:
    """等所有 worker 把队列处理完。

    注意：不能只看 queue.empty() —— 队列在 worker **取走**任务时就空了，
    但任务还在处理中（发送是异步的）。必须等 queue.join()，
    否则会在发送完成前就去断言。
    """
    for _ in range(rounds):
        busy = [w for w in engine.workers.values() if w.queue.qsize() or w.stats.queued > w.stats.processed]
        if not busy:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.15)  # 给最后一次发送的落库留一点时间


async def test_per_target_limit_caps_that_target_only(tmp_path: Path) -> None:
    """群A 限额 2，群B 不限额：群A 第 3 条被跳过，群B 照常。"""
    control, engine, sender, store, client, _ = build(
        tmp_path,
        specs=[(-1001, "群A", 2), (-1002, "群B", 0)],
    )
    try:
        await engine.start()
        for msg_id in range(1, 5):
            await engine.handle_message(fake_message(msg_id=msg_id), source_peer=1)
        await drain(engine)
        await engine.stop()

        a_msgs = [call for call in client.sent if call[0] == -1001]
        b_msgs = [call for call in client.sent if call[0] == -1002]
        assert len(a_msgs) == 2          # 被自己的额度卡住
        assert len(b_msgs) == 4          # 不受影响
        assert store.target_sent_today(-1001) == 2
        assert store.target_sent_today(-1002) == 4
    finally:
        store.close()


async def test_target_limit_takes_min_of_global_and_target(tmp_path: Path) -> None:
    """目标额度比全局小 -> 用目标的；比全局大 -> 用全局的。"""
    control, engine, sender, store, _, _ = build(tmp_path)
    try:
        small = Target(id=-1001, label="小", daily_limit=5)
        big = Target(id=-1002, label="大", daily_limit=9999)
        none = Target(id=-1003, label="无")
        assert sender.effective_limit(small) == 5
        assert sender.effective_limit(big) == 100     # 被全局 100 限制
        assert sender.effective_limit(none) == 100
    finally:
        store.close()


async def test_repost_uses_its_own_ceiling_with_target_limit(tmp_path: Path) -> None:
    control, engine, sender, store, _, _ = build(tmp_path)
    try:
        target = Target(id=-1001, label="群A", daily_limit=7)
        # 重发时取 min(repost.daily_limit=200, target.daily_limit=7)
        assert sender.effective_limit(target, as_repost=True) == 7
        # 转发时取 min(全局 100, 7)
        assert sender.effective_limit(target, as_repost=False) == 7
    finally:
        store.close()


async def test_quota_counters_are_separate_per_target(tmp_path: Path) -> None:
    """按目标的额度占位互不干扰，且和全局占位同时记。"""
    control, engine, sender, store, _, _ = build(tmp_path)
    try:
        await sender._take_budget(1, target=Target(id=-1001, label="A"))
        await sender._take_budget(1, target=Target(id=-1001, label="A"))
        await sender._take_budget(1, target=Target(id=-1002, label="B"))

        assert store.target_checked_today(-1001) == 2
        assert store.target_checked_today(-1002) == 1
        assert store.target_checked_today(-1003) == 0
        assert store.checked_today() == 3            # 全局占位仍然记
        # sent 只在真正发出去后才记，这里没发，所以是 0
        assert store.target_sent_today(-1001) == 0
    finally:
        store.close()


async def test_budget_exhausted_message_names_the_target(tmp_path: Path) -> None:
    from tgrelay.sender import BudgetExhausted

    control, engine, sender, store, _, _ = build(tmp_path)
    try:
        target = Target(id=-1001, label="群A", daily_limit=1)
        await sender._take_budget(1, target=target)
        with pytest.raises(BudgetExhausted, match="群A"):
            await sender._take_budget(1, target=target)
    finally:
        store.close()


async def test_report_exposes_per_target_quota(tmp_path: Path) -> None:
    control, engine, sender, store, _, _ = build(tmp_path)
    try:
        await engine.start()
        await engine.handle_message(fake_message(msg_id=1), source_peer=1)
        await drain(engine)
        await engine.stop()

        report = engine.report()
        assert report["targets"], "应该有目标"
        for name, info in report["targets"].items():
            assert "quota_used" in info
            assert "quota_sent" in info
            assert "quota_limit" in info
            assert info["quota_limit"] == 100
            assert info["quota_used"] >= 1, f"{name} 的额度占用没记上：{info}"
            assert info["quota_sent"] >= 1, f"{name} 的实发数没记上：{info}"
    finally:
        store.close()


# --------------------------------------------------------------------------
# 批量操作
# --------------------------------------------------------------------------


def test_bulk_pause_and_resume_all(tmp_path: Path) -> None:
    control, engine, _, store, _, _ = build(tmp_path)
    try:
        result = control.bulk_targets(action="pause")
        assert result["count"] == 3
        assert all(worker.paused for worker in engine.workers.values())

        result = control.bulk_targets(action="resume")
        assert result["count"] == 3
        assert not any(worker.paused for worker in engine.workers.values())
    finally:
        store.close()


def test_bulk_pause_selected_only(tmp_path: Path) -> None:
    control, engine, _, store, _, _ = build(tmp_path)
    try:
        control.bulk_targets(action="pause", keys=["群A", "-1003"])
        states = {w.target.display: w.paused for w in engine.workers.values()}
        assert states["群A"] is True
        assert states["群C"] is True
        assert states["群B"] is False
    finally:
        store.close()


def test_bulk_set_limit(tmp_path: Path) -> None:
    control, engine, _, store, _, path = build(tmp_path)
    try:
        control.bulk_targets(action="set_limit", daily_limit=15)
        assert all(worker.target.daily_limit == 15 for worker in engine.workers.values())
        # 落盘并复读
        reloaded = load_config(path)
        assert all(t.daily_limit == 15 for t in reloaded.targets)
    finally:
        store.close()


def test_bulk_share_limit_divides_evenly(tmp_path: Path) -> None:
    """把总额度平均分给选中的群 —— 多群场景最常用的一步。"""
    control, engine, _, store, _, _ = build(tmp_path)
    try:
        result = control.bulk_targets(action="share_limit", daily_limit=100)
        assert result["count"] == 3
        assert all(worker.target.daily_limit == 33 for worker in engine.workers.values())
    finally:
        store.close()


def test_bulk_share_limit_floor_is_one(tmp_path: Path) -> None:
    """群比额度还多时，每群至少 1 条，不能算出 0。"""
    control, engine, _, store, _, _ = build(tmp_path)
    try:
        control.bulk_targets(action="share_limit", daily_limit=2)
        assert all(worker.target.daily_limit == 1 for worker in engine.workers.values())
    finally:
        store.close()


def test_bulk_set_interval(tmp_path: Path) -> None:
    control, engine, _, store, _, _ = build(tmp_path)
    try:
        result = control.bulk_targets(action="set_interval", interval=[30, 40])
        assert result["count"] == 3
        assert all(worker.target.interval == (30.0, 40.0) for worker in engine.workers.values())
        # 落盘并复读
        reloaded = load_config(control.config_store.path)
        assert all(t.interval == (30.0, 40.0) for t in reloaded.targets)
    finally:
        store.close()


def test_bulk_rejects_unknown_action(tmp_path: Path) -> None:
    control, *_ = build(tmp_path)
    store = _
    with pytest.raises(ControlError, match="不支持"):
        control.bulk_targets(action="explode")


def test_bulk_rejects_unknown_key(tmp_path: Path) -> None:
    control, _, _, store, _, _ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match="找不到目标"):
            control.bulk_targets(action="pause", keys=["不存在的群"])
    finally:
        store.close()


def test_bulk_set_limit_rejects_negative(tmp_path: Path) -> None:
    control, _, _, store, _, _ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match=">= 0"):
            control.bulk_targets(action="set_limit", daily_limit=-5)
    finally:
        store.close()


def test_bulk_unknown_action_wins_over_bad_argument(tmp_path: Path) -> None:
    """先校验 action，再校验参数 —— 否则会给出误导性的错误信息。"""
    control, _, _, store, _, _ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match="不支持"):
            control.bulk_targets(action="explode", daily_limit=-1)
    finally:
        store.close()


def test_bulk_rejects_bad_interval(tmp_path: Path) -> None:
    control, _, _, store, _, _ = build(tmp_path)
    try:
        with pytest.raises(ControlError, match="不合法"):
            control.bulk_targets(action="set_interval", interval=[40, 10])
    finally:
        store.close()


def test_set_target_daily_limit(tmp_path: Path) -> None:
    control, engine, _, store, _, _ = build(tmp_path)
    try:
        result = control.set_target("群A", daily_limit=42)
        assert result["daily_limit"] == 42
        assert engine.find_worker("群A").target.daily_limit == 42
    finally:
        store.close()


# --------------------------------------------------------------------------
# web API
# --------------------------------------------------------------------------


def test_bulk_api(tmp_path: Path) -> None:
    control, engine, _, store, _, _ = build(tmp_path)
    try:
        app = create_app(control, token=TOKEN)
        with TestClient(app) as http:
            auth = {"Authorization": f"Bearer {TOKEN}"}

            response = http.post(
                "/api/targets/bulk", json={"action": "share_limit", "daily_limit": 90}, headers=auth
            )
            assert response.status_code == 200, response.text
            assert response.json()["count"] == 3
            assert all(w.target.daily_limit == 30 for w in engine.workers.values())

            response = http.post("/api/targets/bulk", json={"action": "pause"}, headers=auth)
            assert response.json()["count"] == 3

            bad = http.post("/api/targets/bulk", json={"action": "nope"}, headers=auth)
            assert bad.status_code == 400
    finally:
        store.close()


def test_bulk_route_not_captured_by_key_route(tmp_path: Path) -> None:
    """回归：/api/targets/bulk 不能被 /api/targets/{key} 抢先匹配。"""
    control, _, _, store, _, _ = build(tmp_path)
    try:
        app = create_app(control, token=TOKEN)
        with TestClient(app) as http:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            response = http.post("/api/targets/bulk", json={"action": "pause"}, headers=auth)
            assert response.status_code == 200
            assert response.json()["action"] == "pause"
    finally:
        store.close()


def test_stats_api_includes_quota_fields(tmp_path: Path) -> None:
    control, _, _, store, _, _ = build(tmp_path)
    try:
        app = create_app(control, token=TOKEN)
        with TestClient(app) as http:
            data = http.get("/api/stats", headers={"Authorization": f"Bearer {TOKEN}"}).json()
        for info in data["targets"].values():
            assert {"quota_used", "quota_sent", "quota_limit", "daily_limit"} <= set(info)
    finally:
        store.close()


def test_patch_target_daily_limit_via_api(tmp_path: Path) -> None:
    control, engine, _, store, _, path = build(tmp_path)
    try:
        app = create_app(control, token=TOKEN)
        with TestClient(app) as http:
            response = http.patch(
                "/api/targets/群A",
                json={"daily_limit": 12},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert response.status_code == 200
        assert engine.find_worker("群A").target.daily_limit == 12
        assert "daily_limit: 12" in path.read_text(encoding="utf-8")
    finally:
        store.close()
