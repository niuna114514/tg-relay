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

"""调度核心：源消息 -> 过滤 -> 去重 -> 生成任务 -> 分发到各目标队列。"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .config import AppConfig
from .db import Store
from .filters import MessageFilter
from .sender import RelayJob, Sender, TargetWorker

log = logging.getLogger("tgrelay.engine")


@dataclass
class EngineStats:
    received: int = 0
    duplicates: int = 0
    filtered: int = 0
    jobs: int = 0
    dropped: int = 0
    edited: int = 0
    deleted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "duplicates": self.duplicates,
            "filtered": self.filtered,
            "jobs": self.jobs,
            "dropped": self.dropped,
            "edited": self.edited,
            "deleted": self.deleted,
        }


class RelayEngine:
    def __init__(self, config: AppConfig, store: Store, sender: Sender) -> None:
        self.config = config
        self.store = store
        self.sender = sender
        self.filter = MessageFilter(config.filters)
        self.stats = EngineStats()
        self.workers: dict[str, TargetWorker] = {
            str(target.id): TargetWorker(
                target,
                sender,
                store,
                queue_size=config.behavior.queue_size,
                drop_on_queue_full=config.behavior.drop_on_queue_full,
            )
            for target in config.targets
        }
        self._client: Any = None

    def attach(self, client: Any) -> None:
        self._client = client

    async def add_target(self, target: Target, store: Store) -> TargetWorker:
        """运行期新增一个目标群（网页/命令调用）。返回新建的 worker。

        这里**不做**权限校验——调用方（RuntimeControl）负责先校验，
        免得这里偷偷发消息或做重活。
        """
        key = str(target.id)
        if key in self.workers:
            return self.workers[key]
        worker = TargetWorker(
            target,
            self.sender,
            store,
            queue_size=self.config.behavior.queue_size,
            drop_on_queue_full=self.config.behavior.drop_on_queue_full,
        )
        self.workers[key] = worker
        await worker.start()
        log.info("已热加入目标：%s", target.display)
        return worker

    async def remove_target(self, target_id: int | str) -> bool:
        """运行期移除一个目标群。"""
        key = str(target_id)
        worker = self.workers.pop(key, None)
        if worker is None:
            return False
        await worker.stop()
        log.info("已移除目标：%s（积压 %s 条已丢弃）", worker.target.display, worker.queue.qsize())
        return True

    def worker_of(self, target_id: int | str) -> TargetWorker | None:
        return self.workers.get(str(target_id))

    def find_worker(self, key: str) -> TargetWorker | None:
        """按 id 或 label/display 找 worker，方便网页用序号或名字操作。"""
        worker = self.workers.get(key)
        if worker is not None:
            return worker
        for item in self.workers.values():
            if key in (item.target.display, str(item.target.id)):
                return item
        return None

    def pause_all(self, paused: bool) -> int:
        count = 0
        for worker in self.workers.values():
            worker.paused = paused
            count += 1
        log.info("%s 全部目标（%s 个）", "已暂停" if paused else "已恢复", count)
        return count

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        for worker in self.workers.values():
            await worker.start()

    async def stop(self) -> None:
        for worker in self.workers.values():
            await worker.stop()

    # ---------------- 入队 ----------------

    @staticmethod
    def make_job_id(source_id: int, msg_ids: Sequence[int]) -> str:
        first = msg_ids[0]
        if len(msg_ids) > 1:
            return f"{source_id}:{first}:album{len(msg_ids)}"
        return f"{source_id}:{first}"

    def enqueue(self, job: RelayJob) -> int:
        accepted = 0
        for worker in self.workers.values():
            if worker.submit(job):
                accepted += 1
        self.stats.jobs += 1
        if accepted == 0:
            self.stats.dropped += 1
            log.error("任务 %s 没有进入任何目标队列（全部队列满或已暂停）", job.job_id)
        return accepted

    # ---------------- 消息处理 ----------------

    def _preview(self, message: Any) -> str:
        text = (getattr(message, "message", None) or "").replace("\n", " ").strip()
        return text[:40] + ("…" if len(text) > 40 else "")

    async def handle_message(self, message: Any, *, as_album: bool = False, source_peer: Any = None) -> bool:
        """处理一条（或一组）源消息。返回是否真的入队转发了。"""
        self.stats.received += 1
        chat_id = getattr(message, "chat_id", None)
        msg_id = getattr(message, "id", None)
        if chat_id is None or msg_id is None:
            log.debug("跳过没有 chat_id/id 的消息")
            return False

        if not self.store.mark_seen(chat_id, msg_id):
            self.stats.duplicates += 1
            log.debug("重复消息，已跳过：%s/%s", chat_id, msg_id)
            return False

        self.store.set_cursor(chat_id, msg_id)

        decision = self.filter.check(message)
        if not decision.allowed:
            self.stats.filtered += 1
            log.debug("过滤掉 %s/%s：%s", chat_id, msg_id, decision.reason)
            return False

        peer = source_peer if source_peer is not None else await self._source_peer(chat_id)
        job = RelayJob(
            job_id=self.make_job_id(chat_id, (msg_id,)),
            source_id=chat_id,
            source_peer=peer,
            msg_ids=(msg_id,),
            kind="album" if as_album else "single",
            preview=self._preview(message),
            is_album=as_album,
        )
        return self.enqueue(job) > 0

    async def handle_album(self, messages: Iterable[Any], source_peer: Any = None) -> bool:
        """相册整组处理：一次 forward 保留图集形态，避免拆成 N 条砸配额。"""
        items = [m for m in messages if getattr(m, "id", None) is not None]
        if not items:
            return False
        self.stats.received += len(items)

        items.sort(key=lambda m: m.id)
        chat_id = items[0].chat_id
        limit = 10  # Telegram 单次相册上限
        if len(items) > limit:
            log.warning("相册有 %s 张，超过 Telegram 单组上限 %s，只转发前 %s 张", len(items), limit, limit)
            items = items[:limit]

        fresh: list[Any] = []
        for message in items:
            if self.store.mark_seen(chat_id, message.id):
                self.store.set_cursor(chat_id, message.id)
                fresh.append(message)
            else:
                self.stats.duplicates += 1
        if not fresh:
            log.debug("相册 %s 全部为重复消息，已跳过", items[0].id)
            return False

        # 相册的过滤用"整组"判断：只要主消息命中即可
        decision = self.filter.check(fresh[0])
        if not decision.allowed:
            self.stats.filtered += 1
            log.debug("相册 %s/%s 被过滤：%s", chat_id, fresh[0].id, decision.reason)
            return False

        peer = source_peer if source_peer is not None else await self._source_peer(chat_id)
        msg_ids = tuple(m.id for m in fresh)
        job = RelayJob(
            job_id=self.make_job_id(chat_id, msg_ids),
            source_id=chat_id,
            source_peer=peer,
            msg_ids=msg_ids,
            kind="album",
            preview=self._preview(fresh[0]),
            is_album=True,
        )
        log.info("相册任务：%s（%s 张）", job.job_id, len(msg_ids))
        return self.enqueue(job) > 0

    async def _source_peer(self, chat_id: int) -> Any:
        if self._client is None:
            return chat_id
        try:
            return await self._client.get_input_entity(chat_id)
        except Exception as exc:
            log.debug("源 %s 用 chat_id 解析失败，直接用 id 转发：%s", chat_id, exc)
            return chat_id

    # ---------------- 编辑 / 删除同步 ----------------

    async def handle_edit(self, message: Any) -> None:
        """源消息被编辑 -> 同步编辑已转发出去的消息。"""
        if not self.config.behavior.sync_edits or self._client is None:
            return
        if getattr(message, "grouped_id", None):
            return  # 相册编辑按整组处理，见 notes
        chat_id = getattr(message, "chat_id", None)
        msg_id = getattr(message, "id", None)
        if chat_id is None or msg_id is None:
            return
        deliveries = self.store.find_deliveries(chat_id, msg_id)
        if not deliveries:
            return
        text = getattr(message, "message", None) or ""
        edited = 0
        for delivery in deliveries:
            target_msg_id = delivery.first_msg_id
            if not target_msg_id:
                continue
            try:
                target_peer = await self.sender.resolve(delivery.target_id, "目标")
                await self._client.edit_message(target_peer, target_msg_id, text)
                edited += 1
            except Exception as exc:
                log.warning("同步编辑到 %s 失败：%s", delivery.target_id, exc)
        if edited:
            self.stats.edited += 1
            log.info("已同步编辑 %s/%s -> %s 个目标", chat_id, msg_id, edited)

    async def handle_delete(self, event: Any) -> None:
        """源消息被删除 -> 同步删除目标里的转发消息。"""
        if not self.config.behavior.sync_deletes or self._client is None:
            return
        removed = 0
        for chat_id, msg_ids in _deleted_pairs(event, self.config.sources):
            for msg_id in msg_ids:
                for delivery in self.store.find_deliveries(chat_id, msg_id):
                    if not delivery.target_msg_ids:
                        continue
                    try:
                        target_peer = await self.sender.resolve(delivery.target_id, "目标")
                        await self._client.delete_messages(target_peer, list(delivery.target_msg_ids))
                        removed += 1
                    except Exception as exc:
                        log.warning("同步删除 %s 失败：%s", delivery.target_id, exc)
        if removed:
            self.stats.deleted += 1
            log.info("已同步删除 %s 条目标消息", removed)

    # ---------------- 统计 ----------------

    def report(self) -> dict[str, Any]:
        # 一次把按目标的计数取回来，避免在循环里逐个查库
        quota_used = self.store.target_sent_map()
        quota_ok = self.store.sent_ok_by_target()
        return {
            "engine": self.stats.as_dict(),
            "sender": {
                "sent": self.sender.stats.sent,
                "failed": self.sender.stats.failed,
                "skipped": self.sender.stats.skipped,
                "flood_waits": self.sender.stats.flood_waits,
                "slow_waits": self.sender.stats.slow_waits,
                "global_per_minute": self.sender.pacer.global_per_minute,
                "daily_cap": self.sender.pacer.daily_cap,
                "flood_guard": self.sender.flood.active(),
                "slow_mode_remaining": self.sender.slow.active(),
                "slow_mode_windows": self.sender.slow.windows(),
                "breaker": self.sender.breaker.reason if self.sender.breaker.tripped else "",
            },
            "targets": {
                worker.target.display: {
                    "id": str(worker.target.id),
                    "queued": worker.stats.queued,
                    "processed": worker.stats.processed,
                    "dropped": worker.stats.dropped,
                    "peak_backlog": worker.stats.peak_backlog,
                    "paused": worker.paused,
                    "backlog": worker.queue.qsize(),
                    "interval": list(worker.target.interval) if worker.target.interval else None,
                    # 按群的配额：额度占用 vs 实发，以及该群今天的上限
                    "quota_used": quota_used.get(str(worker.target.id), 0),
                    "quota_sent": quota_ok.get(str(worker.target.id), 0),
                    "quota_limit": self.sender.effective_limit(worker.target),
                    "daily_limit": worker.target.daily_limit,
                }
                for worker in self.workers.values()
            },
            "store": self.store.stats(),
        }


def _deleted_pairs(event: Any, sources: Sequence[Any]) -> list[tuple[int, Sequence[int]]]:
    """从 MessageDeleted 事件里取出与配置源相关的 (chat_id, msg_ids)。"""
    pairs: list[tuple[int, Sequence[int]]] = []
    try:
        source_keys = {str(item) for item in sources}
        for chat_id, msg_ids in (getattr(event, "deleted_ids", {}) or {}).items():
            if str(chat_id) in source_keys:
                pairs.append((chat_id, msg_ids))
    except Exception:  # 事件结构变化时不要让删除同步拖垮主流程
        return []
    return pairs
