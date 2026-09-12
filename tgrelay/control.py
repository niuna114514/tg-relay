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

"""运行期控制层：网页面板 / 操控命令的唯一入口。

为什么需要这一层：
  * 网页改配置不能只写文件——要立刻生效（worker 增删、素材热更新）；
  * 改配置、加目标、触发重发都会碰 asyncio 对象，必须在同一个 loop 里串行做；
  * 校验逻辑（能不能发言、群慢速多少）只写一遍，网页和以后的 Bot 都调它。

**重要**：这些操作全部在进程内完成，绝不新建 TelegramClient。
同一个 session 被两个 client 持有会让 auth key 作废、账号被强制登出。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import AppConfig, RepostConfig, Target, load_config
from .config_store import ConfigStore
from .db import Store
from .engine import RelayEngine
from .reposter import Reposter
from .sender import ConfigProblem, Sender

log = logging.getLogger("tgrelay.control")


class ControlError(Exception):
    """可预期的操作失败，信息直接给用户看。"""


def _peer_name(entity: Any, fallback: str) -> str:
    """给实体取一个可读的名字：优先标题，其次用户名，最后回落到输入的 peer。"""
    for attr in ("title", "first_name", "username"):
        value = getattr(entity, attr, None)
        if value:
            return str(value)
    return fallback


class RuntimeControl:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        sender: Sender,
        engine: RelayEngine,
        *,
        config_store: ConfigStore | None = None,
        reposter: Reposter | None = None,
        client: Any = None,
        listener: Any = None,
        log_path: str | Path = "data/relay.log",
        config_path: str | Path = "config.yaml",
    ) -> None:
        self.config = config
        self.store = store
        self.sender = sender
        self.engine = engine
        self.config_store = config_store or ConfigStore(config_path)
        self.reposter = reposter
        self.client = client
        # 监听器（listener.ManagedListener）：换源时用它热更新事件过滤
        self.listener = listener
        self.log_path = Path(log_path)

    # ---------------- 只读：状态 ----------------

    def snapshot(self) -> dict[str, Any]:
        report = self.engine.report()
        report["config"] = {
            "sources": [str(item) for item in self.config.sources],
            "targets": [
                {
                    "id": str(target.id),
                    "label": target.display,
                    "interval": list(target.interval) if target.interval else None,
                    "daily_limit": target.daily_limit,
                }
                for target in self.config.targets
            ],
            "rate": {
                "global_per_minute": self.sender.pacer.global_per_minute,
                "daily_cap": self.sender.pacer.daily_cap,
            },
            "premium": self.config.premium,
            "proxy": getattr(self.config.proxy, "safe_url", None),
        }
        report["repost"] = {
            "enabled": self.config.repost.enabled,
            "interval": self.config.repost.interval,
            "daily_limit": self.config.repost.daily_limit,
            "shuffle": self.config.repost.shuffle,
            "message_ids": list(self.config.repost.message_ids()),
            "materials": [
                {
                    "msg_id": int(getattr(message, "id", 0)),
                    "preview": (getattr(message, "message", None) or "")[:60],
                    "posted": self.store.repost_counts().get(int(getattr(message, "id", 0)), 0),
                }
                for message in (self.reposter.messages if self.reposter else [])
            ],
            "stats": self.reposter.stats.as_dict() if self.reposter else None,
            "running": bool(self.reposter and self.reposter.running),
        }
        # 账号级限制的现场信息：给面板「账号自检」和状态页用
        report["account"] = {
            "breaker": self.sender.breaker.reason if self.sender.breaker.tripped else "",
            "write_forbidden": self.sender.write_forbidden.blocked_targets(),
            "alerts": [
                {"level": level, "text": text}
                for level, text in self.sender.alerts.recent(5)
            ],
        }
        return report

    def logs(self, limit: int = 50) -> list[str]:
        if not self.log_path.exists():
            return []
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ControlError(f"读取日志失败：{exc}") from exc
        return lines[-max(1, min(limit, 500)):]

    # ---------------- 源频道管理 ----------------

    async def add_source(self, peer: str, *, verify: bool = True) -> dict[str, Any]:
        """追加一个源频道（监听会立刻生效，原来的源继续监听）。"""
        peer = (peer or "").strip()
        if not peer:
            raise ControlError("源不能为空")
        if peer in {str(item) for item in self.config.sources}:
            raise ControlError(f"源已存在：{peer}")

        if verify:
            try:
                entity = await self.sender.resolve(peer, "源")
            except Exception as exc:
                raise ControlError(f"无法解析源 {peer}：{exc}") from exc
            name = _peer_name(entity, peer)
        else:
            name = peer

        sources = self.config.sources + (peer,)
        self.config = replace(self.config, sources=sources)
        self._persist_sources()
        if self.listener is not None:
            await self.listener.reload_sources(list(sources))
        log.info("已加入源：%s（%s）", peer, name)
        return {"added": peer, "name": str(name), "sources": [str(s) for s in sources]}

    async def switch_source(self, peer: str, *, verify: bool = True) -> dict[str, Any]:
        """把主源换成另一个频道。

        "主源"的意义：repost 素材、--probe-send 探测、历史补漏游标都用它。
        监听层面是把新源加到列表最前面（旧的仍在列表里，但不再是主源）。

        返回内容里会带上"素材是否还有效"——换源后旧素材的 msg_id 在新源里
        往往不存在（ID 是每个频道独立的），所以要提醒重新设置。
        """
        peer = (peer or "").strip()
        if not peer:
            raise ControlError("源不能为空")

        name: str = peer  # 先给默认值：verify=False 时不会去解析
        if verify:
            try:
                entity = await self.sender.resolve(peer, "源")
            except Exception as exc:
                raise ControlError(f"无法解析源 {peer}：{exc}") from exc
            name = _peer_name(entity, peer)

        old = self.config.source
        # 新源放最前面（成为主源），旧的去重后保留在后面
        rest = tuple(item for item in self.config.sources if str(item) != peer)
        sources = (peer,) + rest
        self.config = replace(self.config, sources=sources)
        self._persist_sources()
        if self.listener is not None:
            await self.listener.reload_sources(list(sources))

        # 换源后旧的 repost 素材基本一定失效（msg_id 是频道内编号）
        materials_ok = False
        materials_note = ""
        if self.reposter is not None:
            try:
                await self.reposter.load_messages()
                materials_ok = bool(self.reposter.messages)
                if not materials_ok:
                    materials_note = (
                        f"素材 {list(self.config.repost.message_ids())} 在新源里取不到，"
                        "请用 /addmat 重新设置"
                    )
            except Exception as exc:
                materials_note = f"素材加载失败：{type(exc).__name__}: {exc}；请用 /addmat 重设"

        log.info("主源已切换：%s -> %s", old, peer)
        return {
            "old": str(old),
            "source": peer,
            "name": str(name),
            "sources": [str(s) for s in sources],
            "materials_ok": materials_ok,
            "materials_note": materials_note,
        }

    async def remove_source(self, peer: str) -> dict[str, Any]:
        """移除一个源。至少保留一个。"""
        peer = (peer or "").strip()
        remaining = tuple(item for item in self.config.sources if str(item) != peer)
        if len(remaining) == len(self.config.sources):
            raise ControlError(f"找不到源：{peer}")
        if not remaining:
            raise ControlError("至少要保留一个源；要换源请用 /source <新源>")

        self.config = replace(self.config, sources=remaining)
        self._persist_sources()
        if self.listener is not None:
            await self.listener.reload_sources(list(remaining))
        log.info("已移除源：%s", peer)
        return {"removed": peer, "sources": [str(s) for s in remaining]}

    def sources_info(self) -> list[dict[str, Any]]:
        return [
            {"peer": str(item), "主源": index == 0}
            for index, item in enumerate(self.config.sources)
        ]

    def _persist_sources(self) -> None:
        try:
            self.config_store.save_sources(self.config.sources)
        except Exception as exc:
            log.error("写回 sources 失败（内存里的改动仍然生效）：%s", exc)

    # ---------------- 目标群管理 ----------------

    async def add_target(self, peer: str, label: str = "", *, verify: bool = True) -> dict[str, Any]:
        """加一个目标群。verify=True 时会探测群慢速（不发消息）。"""
        peer = (peer or "").strip()
        if not peer:
            raise ControlError("目标不能为空")
        for existing in self.config.targets:
            if str(existing.id) == peer or (existing.label and existing.label == label):
                raise ControlError(f"目标已存在：{existing.display}")

        target = Target(id=int(peer) if peer.lstrip("-").isdigit() else peer, label=label)
        if target.id in [item.id for item in self.config.targets]:
            raise ControlError(f"目标已存在：{target.id}")

        slow = None
        if verify:
            allowed, reason = await self.sender.postability(target)
            if not allowed:
                raise ControlError(f"{target.display} 不能作为目标：{reason}")
            slow = await self.sender.probe_slow_mode(target)

        # 群有慢速就自动把该目标的间隔对齐过去，避免一开始就撞墙
        if slow and slow > 0:
            target = replace(target, interval=(float(slow), float(slow + 5)))

        worker = await self.engine.add_target(target, self.store)
        self.config = replace(self.config, targets=self.config.targets + (target,))
        self._persist_targets()
        return {
            "id": str(target.id),
            "label": target.display,
            "slowmode": slow,
            "interval": list(target.interval) if target.interval else None,
            "backlog": worker.queue.qsize(),
        }

    async def remove_target(self, key: str) -> dict[str, Any]:
        worker = self.engine.find_worker(key)
        if worker is None:
            raise ControlError(f"找不到目标：{key}")
        target = worker.target
        await self.engine.remove_target(target.id)
        self.config = replace(
            self.config,
            targets=tuple(item for item in self.config.targets if item.id != target.id),
        )
        self._persist_targets()
        return {"removed": str(target.id), "label": target.display}

    def set_target(self, key: str, **changes: Any) -> dict[str, Any]:
        """改目标的 label / interval / enabled / daily_limit。"""
        worker = self.engine.find_worker(key)
        if worker is None:
            raise ControlError(f"找不到目标：{key}")

        old = worker.target
        new = old
        if "label" in changes and changes["label"] is not None:
            new = replace(new, label=str(changes["label"]).strip())
        if "interval" in changes and changes["interval"] is not None:
            lo, hi = changes["interval"]
            lo, hi = float(lo), float(hi)
            if lo <= 0 or hi < lo:
                raise ControlError(f"间隔不合法：[{lo}, {hi}]")
            new = replace(new, interval=(lo, hi))
        if "daily_limit" in changes and changes["daily_limit"] is not None:
            limit = int(changes["daily_limit"])
            if limit < 0:
                raise ControlError("daily_limit 不能为负（0 = 不限，用全局额度）")
            new = replace(new, daily_limit=limit)
        if "enabled" in changes and changes["enabled"] is not None:
            worker.paused = not bool(changes["enabled"])

        worker.target = new
        self.config = replace(
            self.config,
            targets=tuple(new if item.id == old.id else item for item in self.config.targets),
        )
        self._persist_targets()
        return {
            "id": str(new.id),
            "label": new.display,
            "interval": list(new.interval) if new.interval else None,
            "daily_limit": new.daily_limit,
            "enabled": not worker.paused,
        }

    def bulk_targets(
        self,
        *,
        action: str,
        keys: Sequence[str] | None = None,
        daily_limit: int | None = None,
        interval: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        """批量操作：多群管理的核心。

        action:
          pause / resume   暂停或恢复（keys 为空则全部）
          set_limit        给选中的群设置各自的每日额度
          share_limit      把总额度平均分给选中的群（按群数算每群多少）
          set_interval     统一改发送间隔
        """
        workers = list(self.engine.workers.values())
        if keys:
            selected = []
            for key in keys:
                worker = self.engine.find_worker(key)
                if worker is None:
                    raise ControlError(f"找不到目标：{key}")
                selected.append(worker)
        else:
            selected = workers
        if not selected:
            raise ControlError("没有可操作的目标")

        if action not in ("pause", "resume", "set_limit", "share_limit", "set_interval"):
            # 先校验 action，再校验参数 —— 否则传错 action 时会报出误导性的参数错误
            raise ControlError(f"不支持的批量操作：{action}")

        changed: list[dict[str, Any]] = []

        if action in ("pause", "resume"):
            paused = action == "pause"
            for worker in selected:
                worker.paused = paused
                changed.append({"label": worker.target.display, "paused": paused})

        elif action == "set_limit":
            if daily_limit is None or int(daily_limit) < 0:
                raise ControlError("set_limit 需要 daily_limit >= 0（0 = 不限）")
            for worker in selected:
                worker.target = replace(worker.target, daily_limit=int(daily_limit))
                changed.append({"label": worker.target.display, "daily_limit": int(daily_limit)})
            self._sync_config_from_workers()

        elif action == "share_limit":
            total = int(daily_limit if daily_limit is not None else self.pacer_daily_cap())
            if total <= 0:
                raise ControlError("总额度必须大于 0")
            per = max(1, total // len(selected))
            for worker in selected:
                worker.target = replace(worker.target, daily_limit=per)
                changed.append({"label": worker.target.display, "daily_limit": per})
            self._sync_config_from_workers()

        elif action == "set_interval":
            if not interval or len(interval) != 2:
                raise ControlError("set_interval 需要 interval = [下限, 上限]")
            lo, hi = float(interval[0]), float(interval[1])
            if lo <= 0 or hi < lo:
                raise ControlError(f"间隔不合法：[{lo}, {hi}]")
            for worker in selected:
                worker.target = replace(worker.target, interval=(lo, hi))
                changed.append({"label": worker.target.display, "interval": [lo, hi]})
            self._sync_config_from_workers()

        log.info("批量操作 %s 影响 %s 个目标", action, len(changed))
        return {"action": action, "count": len(changed), "changed": changed}

    def pacer_daily_cap(self) -> int:
        return self.sender.pacer.daily_cap

    def _sync_config_from_workers(self) -> None:
        """worker 的 target 改了之后，把 config.targets 和 config.yaml 一起同步。"""
        order = {str(item.id): index for index, item in enumerate(self.config.targets)}
        updated = []
        for worker in self.engine.workers.values():
            updated.append(worker.target)
        updated.sort(key=lambda item: order.get(str(item.id), 999))
        self.config = replace(self.config, targets=tuple(updated))
        self._persist_targets()

    def set_target_paused(self, key: str, paused: bool) -> dict[str, Any]:
        worker = self.engine.find_worker(key)
        if worker is None:
            raise ControlError(f"找不到目标：{key}")
        worker.paused = paused
        log.info("%s %s", worker.target.display, "已暂停" if paused else "已恢复")
        return {"id": str(worker.target.id), "paused": worker.paused}

    # ---------------- 素材管理 ----------------

    async def set_materials(self, ids: Iterable[int], *, apply: bool = True) -> dict[str, Any]:
        cleaned = tuple(sorted({int(item) for item in ids if int(item) > 0}))
        if not cleaned:
            raise ControlError("素材不能为空（至少要留一条）")

        repost = replace(self.config.repost, ids=cleaned, ranges=(), enabled=True)
        self.config = replace(self.config, repost=repost)
        self._persist_repost()

        loaded = 0
        if self.reposter is not None:
            if apply:
                loaded = await self.reposter.reload_messages()
            else:
                self.reposter.request_reload()
        return {"ids": list(cleaned), "loaded": loaded}

    async def set_repost_options(self, **changes: Any) -> dict[str, Any]:
        repost = self.config.repost
        if changes.get("enabled") is not None:
            repost = replace(repost, enabled=bool(changes["enabled"]))
        if changes.get("interval") is not None:
            interval = float(changes["interval"])
            if interval < 31:
                raise ControlError(
                    f"间隔 {interval:g}s 小于 31s；目标群慢速 30s 时每条都会撞墙，请设为 >= 31"
                )
            repost = replace(repost, interval=interval)
        if changes.get("daily_limit") is not None:
            limit = int(changes["daily_limit"])
            if limit < 1:
                raise ControlError("日额度必须大于 0")
            repost = replace(repost, daily_limit=limit)
        if changes.get("shuffle") is not None:
            repost = replace(repost, shuffle=bool(changes["shuffle"]))
        self.config = replace(self.config, repost=repost)
        self._persist_repost()
        return {
            "enabled": repost.enabled,
            "interval": repost.interval,
            "daily_limit": repost.daily_limit,
            "shuffle": repost.shuffle,
        }

    async def run_repost_once(self) -> dict[str, Any]:
        """立刻跑一轮重发（和定时循环互斥）。"""
        if self.reposter is None:
            raise ControlError("当前进程没有启用重发")
        if not self.reposter.messages:
            await self.reposter.load_messages()
        ok, bad = await self.reposter.run_cycle()
        return {"sent": ok, "skipped_or_failed": bad, "stats": self.reposter.stats.as_dict()}

    async def start_repost(self) -> dict[str, Any]:
        """启动（或**重新**启动）定时重发。

        这是"停止"的对偶操作，必须存在：早期版本只能停不能开，
        面板上点一下"停止重发"就只能重启进程才能恢复。

        这里会顺手清掉熔断和「禁止发言」冷板凳 —— 因为能点这个按钮，
        就意味着人已经确认"账号没问题了"（通常刚查过 @SpamBot 或申诉成功）。
        如果其实还有问题，下一个周期会立刻再次熔断，不会白跑。
        """
        if self.reposter is None:
            raise ControlError("当前进程没有重发组件（启动时没带 --repost/--repost-only）")
        if not self.reposter.messages:
            await self.reposter.load_messages()
        self.sender.breaker.reset()
        self.sender.write_forbidden.clear_all()
        options = await self.set_repost_options(enabled=True)
        self.reposter.start()
        return {"started": True, **options}

    async def check_account(self) -> dict[str, Any]:
        """问 @SpamBot：这个号现在有没有被 Telegram 限制。"""
        limited, detail = await self.sender.account_status()
        if limited is False:
            # 限制确实解除了，把因为限制而留下的刹车松开
            self.sender.breaker.reset()
            self.sender.write_forbidden.clear_all()
        return {
            "limited": limited,
            "detail": detail,
            "message": {
                True: "账号仍在被限制：先申诉并等待，解除前不要发送。",
                False: "账号没有被限制，可以正常发送。",
                None: "没能从 @SpamBot 拿到明确结论，请自行打开 @SpamBot 看。",
            }[limited],
        }

    def stop_repost(self) -> dict[str, Any]:
        if self.reposter is None:
            raise ControlError("当前进程没有启用重发")
        # 连 enabled 一起落盘：否则下次重启（含服务器断电重启）会自己又开始发
        self.config = replace(
            self.config, repost=replace(self.config.repost, enabled=False)
        )
        self._persist_repost()
        self.reposter._stop.set()
        return {"stopping": True, "enabled": False}

    # ---------------- 诊断 ----------------

    async def check_target(self, key: str) -> dict[str, Any]:
        worker = self.engine.find_worker(key)
        target = worker.target if worker else Target(id=key)
        allowed, reason = await self.sender.postability(target)
        slow = await self.sender.probe_slow_mode(target)
        return {
            "id": str(target.id),
            "label": target.display,
            "can_post": allowed,
            "reason": reason,
            "slowmode": slow,
        }

    async def probe_target(self, key: str) -> dict[str, Any]:
        """真发一条，权威判定（会往群里发东西）。"""
        worker = self.engine.find_worker(key)
        target = worker.target if worker else Target(id=key)
        allowed, note = await self.sender.probe_send(target)
        return {"id": str(target.id), "label": target.display, "ok": allowed, "note": note}

    # ---------------- 持久化 ----------------

    def _persist_targets(self) -> None:
        try:
            self.config_store.save_targets(self.config.targets)
        except Exception as exc:
            log.error("写回 config.yaml 失败（内存里的改动仍然生效）：%s", exc)

    def _persist_repost(self) -> None:
        try:
            self.config_store.save_repost(self.config.repost)
        except Exception as exc:
            log.error("写回 config.yaml 失败（内存里的改动仍然生效）：%s", exc)

    def reload_from_disk(self) -> AppConfig:
        """重新读 config.yaml（手工编辑后调用）。不重建 worker。"""
        self.config = load_config(self.config_store.path)
        return self.config
