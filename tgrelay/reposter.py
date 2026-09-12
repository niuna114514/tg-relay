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

"""定时重发（Reposter）：把源频道里指定的一段消息，循环、定时重发到目标群。

与实时转发（engine.py）的区别：
  * 实时转发：源有新消息就跟着转，一轮一次，永不重复；
  * 定时重发：把**固定几条**消息当成素材，按周期反复发，可选顺序/随机。

两条硬约束：
  1. 群慢速模式是硬上限（30s 就是 30s），间隔低于它等于每条都撞墙；
  2. 重发会消耗发送配额，所以单独给 daily_limit，不去挤占实时转发的额度。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .config import AppConfig, RepostConfig, Target
from .db import Store
from .sender import (
    BudgetExhausted,
    ConfigProblem,
    RelayJob,
    Sender,
    SendOutcome,
    SlowModeTooLong,
)

log = logging.getLogger("tgrelay.reposter")

DEFAULT_INTERVAL = 300.0  # 没配 interval 时的默认周期（秒）
MIN_INTERVAL = 31.0       # 群慢速 30s 的硬下限，留 1s 余量


@dataclass
class RepostStats:
    cycles: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    current_index: int = 0
    last_message_id: int | None = None
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycles": self.cycles,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "current_index": self.current_index,
            "last_message_id": self.last_message_id,
            "last_error": self.last_error,
        }


class Reposter:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        sender: Sender,
        *,
        client: Any = None,
    ) -> None:
        self.config = config
        self.store = store
        self.sender = sender
        self.repost_config: RepostConfig = config.repost
        self.client = client
        self.stats = RepostStats()
        self.messages: list[Any] = []
        self.source_peer: Any = None
        self.source_id: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._order: list[int] = []
        # 手动触发（/api/repost/run）和定时循环必须互斥：30s 慢速下两轮交错必然互相撞墙
        self.cycle_lock = asyncio.Lock()
        self._reload_pending = False

    # ---------------- 热更新 ----------------

    async def reload_messages(self) -> int:
        """素材变更后重新拉取（网页/命令改完素材调用）。返回新素材条数。"""
        await self.load_messages()
        log.info("repost 素材已热更新：%s 条", len(self.messages))
        return len(self.messages)

    def request_reload(self) -> None:
        """标记"素材变了"，让常驻循环在下一轮开始时重新拉取。"""
        self._reload_pending = True

    async def _maybe_reload(self) -> None:
        if not self._reload_pending:
            return
        self._reload_pending = False
        try:
            await self.load_messages()
            log.info("repost 素材已热更新：%s 条", len(self.messages))
        except ConfigProblem as exc:
            log.error("repost 素材热更新失败，继续用旧素材：%s", exc)

    # ---------------- 素材加载 ----------------

    async def load_messages(self) -> list[Any]:
        """按配置的 IDs / 范围把素材消息取回来（保序）。"""
        if self.client is None:
            raise ConfigProblem("repost 需要 client 才能拉取源消息")

        source = self.config.source
        entity = await self.client.get_entity(source)
        self.source_peer = await self.client.get_input_entity(entity)
        self.source_id = getattr(entity, "id", None)

        wanted = self.repost_config.message_ids()
        if not wanted:
            raise ConfigProblem("repost.ids / repost.ranges 至少要配一条消息")
        if len(wanted) > 100:
            log.warning("repost 配了 %s 条素材，建议控制在 20 条以内", len(wanted))

        fetched: dict[int, Any] = {}
        try:
            got = await self.client.get_messages(self.source_peer, ids=list(wanted))
        except Exception as exc:
            raise ConfigProblem(f"拉取源消息失败（{source}）：{exc}") from exc
        if got is None:
            got = []
        elif not isinstance(got, (list, tuple)):
            got = [got]
        for message in got:
            if message is not None and getattr(message, "id", None) is not None:
                fetched[int(message.id)] = message

        missing = [msg_id for msg_id in wanted if msg_id not in fetched]
        self.messages = [fetched[msg_id] for msg_id in wanted if msg_id in fetched]
        if missing:
            log.warning(
                "repost：以下消息取不到（可能已删除或 ID 不对），已跳过 -> %s", missing
            )
        if not self.messages:
            raise ConfigProblem("repost：一条素材都没取到，检查 repost.ids / ranges 是否正确")

        self._order = list(range(len(self.messages)))
        if self.repost_config.shuffle:
            random.shuffle(self._order)

        preview = ", ".join(str(getattr(m, "id", "?")) for m in self.messages[:8])
        log.info(
            "repost 素材就绪：%s 条（%s%s）",
            len(self.messages),
            preview,
            " …" if len(self.messages) > 8 else "",
        )
        return self.messages

    # ---------------- 发送 ----------------

    async def post_once(self, target: Target, message: Any) -> SendOutcome:
        """把一条素材发给一个目标。"""
        msg_id = int(getattr(message, "id"))
        # job_id 带毫秒时间戳：每次重发都是独立的一行，投递账本才能如实记录
        # "这条素材今天被发了几次"。用毫秒而不是秒，避免同一秒内的多次重发撞键。
        stamp = int(time.time() * 1000)
        job = RelayJob(
            job_id=f"repost:{self.source_id}:{msg_id}:{stamp}",
            source_id=self.source_id or 0,
            source_peer=self.source_peer,
            msg_ids=(msg_id,),
            kind="repost",
            preview=(getattr(message, "message", None) or "")[:40].replace("\n", " "),
        )
        outcome = await self.sender.send(
            job, target, daily_limit=self.repost_config.daily_limit
        )
        self.store.record_delivery(
            job_id=job.job_id,
            target_id=target.id,
            source_id=job.source_id,
            source_msg_id=msg_id,
            status=outcome.status,
            target_msg_ids=outcome.target_msg_ids,
            error=outcome.error,
        )
        # 只有真的发出去了才计数：被配额/权限/慢速跳过的不算"重发过一次"
        if outcome.status == "sent":
            self.store.bump_repost(msg_id, self.source_id)
        return outcome

    async def run_cycle(self, targets: Sequence[Target] | None = None) -> tuple[int, int]:
        """走完一轮素材。返回 (成功数, 跳过/失败数)。"""
        async with self.cycle_lock:
            return await self._run_cycle_inner(targets)

    async def _run_cycle_inner(self, targets: Sequence[Target] | None = None) -> tuple[int, int]:
        if not self.messages:
            raise ConfigProblem("repost 素材还没加载")

        wanted = self.repost_config.targets
        chosen = [
            target
            for target in (targets or self.config.targets)
            if not wanted or str(target.id) in wanted or target.display in wanted
        ]
        if not chosen:
            log.error("repost：配置的目标都不在 targets 里，检查 repost.targets")
            return (0, 0)

        ok = 0
        bad = 0
        for order_index in self._order:
            if self._stop.is_set():
                break
            message = self.messages[order_index]
            self.stats.current_index = order_index
            self.stats.last_message_id = int(getattr(message, "id", 0))
            for target in chosen:
                if self._stop.is_set():
                    break
                try:
                    outcome = await self.post_once(target, message)
                except (ConfigProblem, SlowModeTooLong) as exc:
                    log.error("repost -> %s 失败：%s", target.display, exc)
                    bad += 1
                    self.stats.last_error = str(exc)
                    continue
                if outcome.status == "sent":
                    ok += 1
                    self.stats.sent += 1
                elif outcome.status == "skipped":
                    bad += 1
                    self.stats.skipped += 1
                    self.stats.last_error = outcome.error
                else:
                    bad += 1
                    self.stats.failed += 1
                    self.stats.last_error = outcome.error
                    if outcome.permanent:
                        log.error(
                            "repost -> %s 永久失败，本轮后续素材跳过该目标：%s",
                            target.display,
                            outcome.error,
                        )
                        chosen = [item for item in chosen if item.id != target.id]
        if ok:
            self.stats.cycles += 1
        return (ok, bad)

    async def run_forever(self) -> None:
        interval = max(MIN_INTERVAL, float(self.repost_config.interval or DEFAULT_INTERVAL))
        if self.repost_config.interval and self.repost_config.interval < MIN_INTERVAL:
            log.warning(
                "repost.interval=%ss 低于群慢速下限 %.0fs，已提升到 %.0fs",
                self.repost_config.interval,
                MIN_INTERVAL,
                interval,
            )
        log.info(
            "repost 循环启动：%s 条素材 / 每轮约 %.0fs / 目标 %s / 每日上限 %s",
            len(self.messages),
            interval,
            [t.display for t in self.config.targets],
            self.repost_config.daily_limit,
        )
        while not self._stop.is_set():
            if self.sender.breaker.tripped:
                log.error("repost 暂停：发送已熔断（%s）", self.sender.breaker.reason)
                return
            await self._maybe_reload()
            ok, bad = await self.run_cycle()
            log.info(
                "repost 第 %s 轮结束：成功 %s，跳过/失败 %s，下轮 %.0fs 后",
                self.stats.cycles,
                ok,
                bad,
                interval,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever(), name="reposter")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except asyncio.TimeoutError:
                self._task.cancel()
                log.warning("repost 任务未在 15s 内退出，已强制取消")
            except asyncio.CancelledError:
                pass
            self._task = None


async def run_once(config: AppConfig, store: Store, sender: Sender, client: Any) -> int:
    """跑一轮就退出（给 --repost-once / --dry-run 验证用）。"""
    reposter = Reposter(config, store, sender, client=client)
    await reposter.load_messages()
    ok, bad = await reposter.run_cycle()
    print(f"repost 单轮完成：成功 {ok}，跳过/失败 {bad}")
    return 0 if ok else 1
