"""发送节奏体检测试。

背景（2026-09-12）：号被 Telegram 反垃圾限制了。复盘发现真正的触发条件是
「同一个群 5.5 小时里收到 230 次同一条素材」，而不是"总发送量太大"。
这些用例把"什么配法算危险"钉住，免得以后又靠手感调参数。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgrelay.config import AppConfig, Filters, Rate, RepostConfig, Target  # noqa: E402
from tgrelay.risk import (  # noqa: E402
    SAFE_GROUP_INTERVAL,
    SAFE_PER_GROUP_PER_DAY,
    Risk,
    assess,
    summarize,
    worst_level,
)


def make_config(
    *,
    interval: tuple[float, float] = (300.0, 300.0),
    repost_enabled: bool = True,
    ids: tuple[int, ...] = (6, 7, 8, 9, 10),
    daily_limit: int = 60,
    shuffle: bool = True,
    daily_cap: int = 100,
    targets: int = 1,
) -> AppConfig:
    return AppConfig(
        sources=(-1001,),
        targets=tuple(Target(id=-2000 - i, interval=interval) for i in range(1, targets + 1)),
        filters=Filters(),
        rate=Rate(
            per_target_interval=interval,
            cross_target_delay=(0.0, 0.0),
            global_per_minute=10,
            daily_cap=daily_cap,
        ),
        repost=RepostConfig(
            enabled=repost_enabled,
            ids=ids,
            interval=300,
            daily_limit=daily_limit,
            shuffle=shuffle,
        ),
    )


def titles(risks: list[Risk]) -> set[str]:
    return {risk.title for risk in risks}


def has(risks: list[Risk], keyword: str) -> bool:
    """按关键词找风险 —— 标题措辞以后微调了，测试也不该跟着碎。"""
    return any(keyword in risk.title for risk in risks)


# --------------------------------------------------------------------------
# 安全配置
# --------------------------------------------------------------------------


def test_safe_cadence_has_no_risks() -> None:
    """间隔 300s、每日 60 条、5 条素材、开了 shuffle —— 这是事故后定的目标配置。"""
    risks = assess(make_config())
    assert risks == [], [risk.render() for risk in risks]
    assert worst_level(risks) == ""
    assert "未发现明显风险" in summarize(risks)


def test_no_targets_means_nothing_to_assess() -> None:
    config = make_config()
    empty = AppConfig(
        sources=config.sources, targets=(), filters=Filters(), rate=config.rate, repost=config.repost
    )
    assert assess(empty) == []


# --------------------------------------------------------------------------
# 群发送间隔：真正的节流阀
# --------------------------------------------------------------------------


def test_30s_group_interval_is_danger() -> None:
    """事故当天就是这个值：同一个群不到一分钟就能收到一条。"""
    risks = assess(make_config(interval=(30.0, 30.0)))

    assert worst_level(risks) == "danger"
    danger = [risk for risk in risks if risk.level == "danger"]
    assert has(danger, "群发送间隔过短")
    detail = next(risk.detail for risk in danger if "群发送间隔过短" in risk.title)
    assert "30s" in detail
    assert "2880" in detail, "应该把「一天理论上能塞多少条」算出来给他看"


def test_sixty_seconds_is_still_danger() -> None:
    """60s = 同一个群每分钟一条，一天理论上 1440 条 —— 还在高危区。"""
    risks = assess(make_config(interval=(60.0, 60.0)))
    assert has(risks, "群发送间隔过短")


def test_sixty_one_seconds_drops_to_warning() -> None:
    risks = assess(make_config(interval=(61.0, 61.0)))
    assert not has(risks, "群发送间隔过短")
    assert has(risks, "群发送间隔偏短")


def test_ninety_seconds_is_warning_not_danger() -> None:
    risks = assess(make_config(interval=(90.0, 90.0)))

    assert has(risks, "群发送间隔偏短")
    assert not any(risk.level == "danger" for risk in risks)
    assert worst_level(risks) == "warning"


def test_interval_exactly_at_threshold_is_clean() -> None:
    risks = assess(make_config(interval=(SAFE_GROUP_INTERVAL, SAFE_GROUP_INTERVAL)))
    assert not has(risks, "群发送间隔偏短")
    assert not has(risks, "群发送间隔过短")


def test_target_interval_falls_back_to_global() -> None:
    """目标没配 interval 时用全局的 —— 别漏判。"""
    config = make_config(interval=(30.0, 30.0))
    config = AppConfig(
        sources=config.sources,
        targets=(Target(id=-2001, interval=None),),
        filters=Filters(),
        rate=config.rate,
        repost=config.repost,
    )
    assert has(assess(config), "群发送间隔过短")


# --------------------------------------------------------------------------
# 素材重复度：那次事故的真正主因
# --------------------------------------------------------------------------


def test_single_material_is_danger() -> None:
    risks = assess(make_config(ids=(6,)))

    assert worst_level(risks) == "danger"
    assert has(risks, "素材太少")


def test_three_materials_is_warning() -> None:
    risks = assess(make_config(ids=(6, 7, 8)))
    assert has(risks, "素材太少")
    assert all(risk.level != "danger" for risk in risks if "素材太少" in risk.title)


def test_five_materials_is_clean() -> None:
    risks = assess(make_config(ids=(6, 7, 8, 9, 10)))
    assert not has(risks, "素材太少")


def test_shuffle_off_with_multiple_materials_is_warned() -> None:
    risks = assess(make_config(ids=(6, 7, 8, 9, 10), shuffle=False))
    assert has(risks, "素材没有打乱")


def test_shuffle_warning_not_raised_for_single_material() -> None:
    """只有一条素材时"顺序"没有意义，不该再多报一条。"""
    risks = assess(make_config(ids=(6,), shuffle=False))
    assert not has(risks, "素材没有打乱")


def test_heavy_repeat_per_material_is_warned() -> None:
    """每天 200 条 / 5 条素材 = 每条每天重复 40 次，偏多。"""
    risks = assess(make_config(ids=(6, 7, 8, 9, 10), daily_limit=200))
    assert has(risks, "同一素材重复次数偏多")


# --------------------------------------------------------------------------
# 日发送量
# --------------------------------------------------------------------------


def test_high_daily_limit_is_warned() -> None:
    risks = assess(make_config(daily_limit=100))
    assert has(risks, "单群日发送量偏高")


def test_two_hundred_daily_limit_is_danger() -> None:
    risks = assess(make_config(ids=(6, 7, 8, 9, 10), daily_limit=200))
    risk = next(r for r in risks if "单群日发送量偏高" in r.title)
    assert risk.level == "danger"


def test_limit_at_safe_threshold_is_clean() -> None:
    risks = assess(make_config(daily_limit=SAFE_PER_GROUP_PER_DAY))
    assert not has(risks, "单群日发送量偏高")


def test_unlimited_daily_limit_skips_that_check() -> None:
    """0 = 不限量。这种情况没法算"每天最多几条"，不该硬报。"""
    risks = assess(make_config(daily_limit=0))
    assert not has(risks, "单群日发送量偏高")


# --------------------------------------------------------------------------
# 重发关闭时不该乱报素材相关的风险
# --------------------------------------------------------------------------


def test_repost_disabled_skips_material_checks() -> None:
    config = make_config(repost_enabled=False, ids=(6,), shuffle=False, daily_limit=60)
    risks = assess(config)

    assert not has(risks, "素材太少")
    assert not has(risks, "素材没有打乱")
    assert not has(risks, "同一素材重复次数偏多")
    # 但间隔检查照样要生效
    assert assess(make_config(repost_enabled=False, interval=(30.0, 30.0))) != []


def test_repost_disabled_uses_relay_daily_cap() -> None:
    """重发关了就看实时转发的日上限。"""
    config = make_config(repost_enabled=False, daily_cap=500)
    assert has(assess(config), "单群日发送量偏高")


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("levels", "expected"),
    [([], ""), (["warning"], "warning"), (["warning", "danger"], "danger")],
)
def test_worst_level(levels: list[str], expected: str) -> None:
    risks = [Risk(level, "t", "d") for level in levels]
    assert worst_level(risks) == expected


def test_summarize_counts_danger() -> None:
    risks = [Risk("danger", "a", "b"), Risk("warning", "c", "d")]
    text = summarize(risks)
    assert "2 项" in text and "1 项高危" in text


def test_risk_render_is_plain_text() -> None:
    """状态栏走的是 Telegram HTML / 面板 esc()，正文里不能留 markdown 标记。"""
    for risk in assess(make_config(interval=(30.0, 30.0), ids=(6,), daily_limit=200)):
        assert "**" not in risk.detail
        assert "**" not in risk.title
        assert risk.render()
