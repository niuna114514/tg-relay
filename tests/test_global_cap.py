"""全局总闸（rate.daily_cap）的行为测试。

设计取舍：
  * 全局总闸是"跨所有群的总安全阀"——20 个群各 50 条 = 1000 条，靠它兜底；
  * 但有些人想完全按各群自己的 daily_limit 走，不设总量上限，
    所以 daily_cap 支持 **0 = 不限**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import FakeClient  # noqa: E402
from tgrelay.config import (  # noqa: E402
    AppConfig,
    ConfigError,
    Filters,
    Rate,
    Target,
    load_config,
)
from tgrelay.db import Store  # noqa: E402
from tgrelay.sender import BudgetExhausted, Pacer, Sender  # noqa: E402


def make(tmp_path: Path, rate: Rate, targets: list[Target]) -> tuple[Sender, Store]:
    config = AppConfig(
        sources=(-1001,),
        targets=tuple(targets),
        filters=Filters(),
        rate=rate,
    )
    store = Store(tmp_path / "relay.db")
    return Sender(FakeClient(), config, store), store


# --------------------------------------------------------------------------
# 配置解析
# --------------------------------------------------------------------------


def test_daily_cap_zero_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'sources: ["@src"]\ntargets:\n  - id: -1001\n'
        "rate:\n  global_per_minute: 10\n  daily_cap: 0\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.rate.daily_cap == 0


def test_daily_cap_negative_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'sources: ["@src"]\ntargets:\n  - id: -1001\n'
        "rate:\n  global_per_minute: 10\n  daily_cap: -5\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="不能为负"):
        load_config(path)


def test_pacer_keeps_zero_as_unlimited() -> None:
    """0 不能像以前那样被 max(1, ...) 变成 1。"""
    pacer = Pacer(Rate(global_per_minute=10, daily_cap=0))
    assert pacer.daily_cap == 0
    premium = Pacer(Rate(global_per_minute=10, daily_cap=0), premium=True)
    assert premium.daily_cap == 0  # Premium 也不该把 0 变成 4


def test_pacer_still_scales_nonzero_cap_for_premium() -> None:
    pacer = Pacer(Rate(global_per_minute=10, daily_cap=100), premium=True)
    assert pacer.daily_cap == 400


# --------------------------------------------------------------------------
# 发送时的行为
# --------------------------------------------------------------------------


async def test_no_global_cap_means_only_target_limit_applies(tmp_path: Path) -> None:
    """总闸关闭 + 群A 限 2：群A 发 2 条后被自己的额度挡住，群B 不受总量影响。"""
    sender, store = make(
        tmp_path,
        Rate(per_target_interval=(0, 0), cross_target_delay=(0, 0),
             global_per_minute=6000, daily_cap=0),
        [Target(id=-1001, label="群A", daily_limit=2), Target(id=-1002, label="群B", daily_limit=3)],
    )
    try:
        a = Target(id=-1001, label="群A", daily_limit=2)
        b = Target(id=-1002, label="群B", daily_limit=3)
        for _ in range(2):
            await sender._take_budget(1, target=a)
        for _ in range(3):
            await sender._take_budget(1, target=b)

        with pytest.raises(BudgetExhausted, match="群A"):
            await sender._take_budget(1, target=a)
        with pytest.raises(BudgetExhausted, match="群B"):
            await sender._take_budget(1, target=b)

        assert store.target_checked_today(-1001) == 2
        assert store.target_checked_today(-1002) == 3
        # 总闸关闭时不记全局占位（记了也没人看，还容易误导）
        assert store.checked_today() == 0
    finally:
        store.close()


async def test_global_cap_still_enforced_when_set(tmp_path: Path) -> None:
    """总闸开着就仍然是硬限制：跨目标合计不能超。"""
    sender, store = make(
        tmp_path,
        Rate(per_target_interval=(0, 0), cross_target_delay=(0, 0),
             global_per_minute=6000, daily_cap=3),
        [Target(id=-1001, label="群A"), Target(id=-1002, label="群B")],
    )
    try:
        a = Target(id=-1001, label="群A")
        b = Target(id=-1002, label="群B")
        for _ in range(3):
            await sender._take_budget(1, target=a)
        with pytest.raises(BudgetExhausted, match="总配额"):
            await sender._take_budget(1, target=b)
        assert store.checked_today() == 3
    finally:
        store.close()


async def test_no_cap_at_all_when_both_disabled(tmp_path: Path) -> None:
    """总闸关 + 群也没限额 = 完全不限条数（只受每分钟与风控限制）。"""
    sender, store = make(
        tmp_path,
        Rate(per_target_interval=(0, 0), cross_target_delay=(0, 0),
             global_per_minute=6000, daily_cap=0),
        [Target(id=-1001, label="群A")],
    )
    try:
        target = Target(id=-1001, label="群A")
        for _ in range(50):
            await sender._take_budget(1, target=target)
        assert store.target_checked_today(-1001) == 50
    finally:
        store.close()


async def test_effective_limit_reports_zero_as_unlimited(tmp_path: Path) -> None:
    sender, store = make(
        tmp_path,
        Rate(per_target_interval=(0, 0), cross_target_delay=(0, 0),
             global_per_minute=6000, daily_cap=0),
        [Target(id=-1001, label="不限"), Target(id=-1002, label="限5", daily_limit=5)],
    )
    try:
        assert sender.effective_limit(Target(id=-1001)) == 0      # 0 = 不限
        assert sender.effective_limit(Target(id=-1002, daily_limit=5)) == 5
    finally:
        store.close()


async def test_effective_limit_takes_min_when_both_set(tmp_path: Path) -> None:
    sender, store = make(
        tmp_path,
        Rate(per_target_interval=(0, 0), cross_target_delay=(0, 0),
             global_per_minute=6000, daily_cap=100),
        [Target(id=-1001, daily_limit=30), Target(id=-1002, daily_limit=500)],
    )
    try:
        assert sender.effective_limit(Target(id=-1001, daily_limit=30)) == 30
        assert sender.effective_limit(Target(id=-1002, daily_limit=500)) == 100
    finally:
        store.close()


def test_describe_limits_mentions_both(tmp_path: Path) -> None:
    """启动日志要能一眼看出额度是怎么分配的。"""
    sender, store = make(
        tmp_path,
        Rate(per_target_interval=(0, 0), cross_target_delay=(0, 0),
             global_per_minute=10, daily_cap=100),
        [Target(id=-1001, label="群A", daily_limit=30), Target(id=-1002, label="群B")],
    )
    try:
        text = sender.pacer.describe_limits(
            [Target(id=-1001, label="群A", daily_limit=30), Target(id=-1002, label="群B")]
        )
        assert "100" in text
        assert "群A=30" in text
    finally:
        store.close()


def test_describe_limits_when_cap_disabled(tmp_path: Path) -> None:
    sender, store = make(
        tmp_path,
        Rate(per_target_interval=(0, 0), cross_target_delay=(0, 0),
             global_per_minute=10, daily_cap=0),
        [Target(id=-1001, label="群A", daily_limit=30)],
    )
    try:
        text = sender.pacer.describe_limits([Target(id=-1001, label="群A", daily_limit=30)])
        assert "关闭" in text
        assert "群A=30" in text
    finally:
        store.close()
