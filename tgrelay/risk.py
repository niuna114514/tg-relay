# tg-relay - 用 MTProto 协议号把频道消息转发到多个群
# Copyright (C) 2026 tg-relay contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""发送节奏的风险体检。

为什么要有这个模块（2026-09-12 真实事故）：
    号被 Telegram 反垃圾限制了。复盘数据是：

        5.5 小时内，往**同一个群**发了 **230 次**（间隔 31 秒），全部是同一条素材。

    也就是说，真正触发限制的不是"一天发了几条"，而是
    **同一个群里反复发同样的内容**。而项目原来的限速只照顾到了
    "每秒/每分钟别太快"，完全没有表达"同内容重复"这件事。

    更麻烦的是这个参数没人看得住：`targets[].interval` 在配置里叫"发送间隔"，
    它的默认值 30 秒是为了对齐群慢速模式，看起来很正常 ——
    但它同时也就是"同一个群每 30 秒能收到一条"，一天理论上能塞 2880 条。

所以这里把"什么算危险"写成可执行的检查，在三个地方生效：
    * 启动时打进日志；
    * 面板和 Bot 的状态里当警示显示；
    * `control` 改配置时立刻回显（改了马上就告诉你现在是什么风险等级）。

**只警告、不阻止**：节奏快慢是业务决定，工具不该替用户拍板；
但它有义务把"这么配会发生什么"讲清楚。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .config import AppConfig, Target

# 阈值都是照着那次事故反推的，留了余量
SAFE_PER_GROUP_PER_DAY = 60        # 单群每天超过这个数就该分摊到更多群
SAFE_GROUP_INTERVAL = 120.0        # 同一个群两条之间少于 2 分钟就偏快
VERY_FAST_GROUP_INTERVAL = 60.0    # 少于 1 分钟属于高危
SAFE_MATERIALS = 5                 # 素材少于 5 条时"重复"特征明显
HEAVY_REPEAT_PER_DAY = 30          # 单素材每天重复超过这个次数就够扎眼了


@dataclass(frozen=True)
class Risk:
    """一条风险提示。level: warning = 建议改；danger = 会出事的那种配法。"""

    level: str
    title: str
    detail: str

    def render(self) -> str:
        icon = "危险" if self.level == "danger" else "建议"
        return f"[{icon}] {self.title}：{self.detail}"


def _effective_group_interval(target: Target, config: AppConfig) -> tuple[float, float]:
    interval = target.interval or config.rate.per_target_interval
    return float(interval[0]), float(interval[1])


def assess(config: AppConfig, *, materials: int | None = None) -> list[Risk]:
    """体检当前配置的发送节奏。返回空列表 = 没发现明显风险。

    materials 传"实际会用的素材条数"；不传就从 repost 配置里算。
    """
    risks: list[Risk] = []
    if not config.targets:
        return risks

    if materials is None:
        materials = len(config.repost.message_ids())

    repost_on = bool(config.repost.enabled)
    per_day = config.repost.daily_limit if repost_on else config.rate.daily_cap
    materials = max(1, materials) if repost_on else 1

    # ---- 1. 单群日发送量 ----
    if per_day > 0:
        per_group = per_day
        if per_group > SAFE_PER_GROUP_PER_DAY:
            risks.append(
                Risk(
                    "warning" if per_group <= SAFE_PER_GROUP_PER_DAY * 2 else "danger",
                    "单群日发送量偏高",
                    f"按当前配置每天最多往「每个群」发 {per_group} 条"
                    f"（建议 ≤ {SAFE_PER_GROUP_PER_DAY}）。"
                    "解药是加群分摊，而不是把额度调小又调回来。",
                )
            )

    # ---- 2. 群发送间隔（真正的节流阀）----
    fastest = None
    for target in config.targets:
        low, _high = _effective_group_interval(target, config)
        if fastest is None or low < fastest[0]:
            fastest = (low, target.display)
    if fastest is not None:
        low, who = fastest
        if low <= VERY_FAST_GROUP_INTERVAL:
            risks.append(
                Risk(
                    "danger",
                    "群发送间隔过短",
                    f"{who} 的发送间隔是 {low:.0f}s —— 也就是同一个群不到一分钟就能收到一条，"
                    f"理论上一天能塞 {86400 / max(low, 1):.0f} 条。"
                    f"这就是 2026-09-12 号被限制的那个配法（当时 30s）。"
                    f"建议设成 {SAFE_GROUP_INTERVAL:.0f}s 以上。",
                )
            )
        elif low < SAFE_GROUP_INTERVAL:
            risks.append(
                Risk(
                    "warning",
                    "群发送间隔偏短",
                    f"{who} 的发送间隔是 {low:.0f}s，建议 ≥ {SAFE_GROUP_INTERVAL:.0f}s。"
                    "注意这个值才是真正的节流阀：一轮会把所有素材各发一遍，"
                    "素材越多、实际速率越高，但这个间隔不会自动跟着变。",
                )
            )

    # ---- 3. 素材重复度（那次事故的真正主因）----
    if repost_on:
        if materials < SAFE_MATERIALS:
            risks.append(
                Risk(
                    "warning" if materials > 1 else "danger",
                    "素材太少，重复特征明显",
                    f"当前只有 {materials} 条素材，反垃圾系统最容易识别"
                    "「同一个群反复出现同样的内容」。"
                    f"建议准备 ≥ {SAFE_MATERIALS} 条不同素材并打开 shuffle。",
                )
            )
        if not config.repost.shuffle and materials > 1:
            risks.append(
                Risk(
                    "warning",
                    "素材没有打乱",
                    "shuffle: false 会让每条素材固定按同样顺序、以固定周期出现，"
                    "规律性本身也是特征。建议打开 shuffle。",
                )
            )
        if per_day > 0 and materials >= 1:
            repeats = per_day / materials
            if repeats > HEAVY_REPEAT_PER_DAY:
                risks.append(
                    Risk(
                        "warning",
                        "同一素材重复次数偏多",
                        f"每天每群 {per_day} 条、共 {materials} 条素材 → "
                        f"每条素材每天要在同一个群出现约 {repeats:.0f} 次"
                        f"（建议 ≤ {HEAVY_REPEAT_PER_DAY}）。",
                    )
                )
    return risks


def worst_level(risks: Sequence[Risk]) -> str:
    if any(risk.level == "danger" for risk in risks):
        return "danger"
    if risks:
        return "warning"
    return ""


def summarize(risks: Sequence[Risk]) -> str:
    """一句话给日志/状态栏用。"""
    if not risks:
        return "发送节奏体检：未发现明显风险"
    danger = sum(1 for risk in risks if risk.level == "danger")
    return (
        f"发送节奏体检：{len(risks)} 项需要注意"
        + (f"（其中 {danger} 项高危）" if danger else "")
    )
