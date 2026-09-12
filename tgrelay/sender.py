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

"""发送层：限速、FloodWait 退避、多目标队列、转发（保留来源）。

设计要点
--------
1. 保留来源 = 用 `forward_messages`，不下载不重传，几乎不耗带宽。
2. 每个目标一个独立队列 + 独立 worker，一个群被限流不阻塞其它群。
3. 三层限速：同目标间隔 / 跨目标间隔 / 全局每分钟，全部可配。
4. 错误分级：FloodWait 退避重试，PeerFlood 直接熔断（再发就封号），
   无权限类永久失败并移出队列。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from telethon import errors

from .alerts import AlertHub
from .config import AppConfig, Rate, Target
from .db import Store

log = logging.getLogger("tgrelay.sender")


# --------------------------------------------------------------------------
# 异常分级
# --------------------------------------------------------------------------


class PeerFlood(Exception):
    """账号被判定为群发垃圾信息——必须立刻停手，否则会被限制/封禁。"""


class TargetUnavailable(Exception):
    """对目标群没有发言权限之类，属于永久性失败，不再重试。"""


class BudgetExhausted(Exception):
    """今日发送配额已用完。"""


class SlowModeTooLong(Exception):
    """目标群慢速模式的剩余等待时间超过上限，本条放弃（不重试）。"""


class ConfigProblem(Exception):
    """源/目标解析失败，需要人工处理。"""


# 这些错误退避后重试有意义（不同 Telethon 版本的错误类略有差异，逐个探测）
_TRANSIENT_NAMES = (
    "TimeoutError",
    "ServerError",
    "RpcCallFailError",
    "InterdcCallError",
    "InterdcCallRichError",
    "RpcMcgetFailError",
    "FloodWaitError",
    "SlowModeWaitError",
    "InternalServerError",
    "TimedOutError",
)
TRANSIENT_ERRORS = tuple(
    getattr(errors, name) for name in _TRANSIENT_NAMES if hasattr(errors, name)
)

# 没有权限 / 不可能成功
_PERMANENT_NAMES = (
    "ChatWriteForbiddenError",
    "UserBannedInChannelError",
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "PeerIdInvalidError",
    "ChannelInvalidError",
    "UserNotParticipantError",
    "ChatForwardsRestrictedError",
    "ChatSendMediaForbiddenError",
    "ChatSendPlainForbiddenError",
    "ChatRestrictedError",
    "MessageIdInvalidError",
)
PERMANENT_ERRORS = tuple(
    getattr(errors, name) for name in _PERMANENT_NAMES if hasattr(errors, name)
)

# 「禁止发言」这一类：Telegram 用它表达两件完全不同的事，必须分开对待
#   1) 只有这一个群这样 → 群的问题（群改了权限 / 群把号封了）
#   2) 多个群同时这样   → **账号级限制**（反垃圾把号限制了，@SpamBot 能查到）
# 2026-09-12 真实踩过第 2 种：号被限制后转发报 UserBannedInChannelError，
# 而 `channels.GetParticipant` 显示"普通成员、无 banned_rights"——
# 光看群权限根本看不出来，是被 @SpamBot 点破的。
_WRITE_FORBIDDEN_NAMES = (
    "ChatWriteForbiddenError",
    "UserBannedInChannelError",
)
WRITE_FORBIDDEN_ERRORS = tuple(
    getattr(errors, name) for name in _WRITE_FORBIDDEN_NAMES if hasattr(errors, name)
)

# 这些错误给一句人话解释
_ERROR_HINTS = {
    "ChatForwardsRestrictedError": "该频道开启了『禁止转发』保护。用已开通 Premium 的号，或改用 copy 模式重发内容。",
    "ChatWriteForbiddenError": "该群不让这个号发言。如果**多个群**都这样，就是账号被 Telegram 限制了（用 @SpamBot 查）。",
    "ChatAdminRequiredError": "该操作需要管理员权限。",
    "UserBannedInChannelError": (
        "这个号不能往该群发消息。两种可能：群把号封了／改了权限，"
        "或者**账号被 Telegram 反垃圾限制了**——后者会影响所有群，"
        "用 @SpamBot 查一下就知道。"
    ),
    "UserNotParticipantError": "这个号还没加入该群。",
    "ChannelPrivateError": "该群/频道是私有的，或这个号已被移出。",
    "PeerIdInvalidError": "无法解析该 ID：请先用这个号打开过一次该会话（暖场）。",
    "ChannelInvalidError": "频道 ID 无效，确认是否漏了 -100 前缀。",
}


def _hint(exc: Exception) -> str:
    return _ERROR_HINTS.get(type(exc).__name__, "")


MAX_ATTEMPTS = 3     # 网络类错误的重试次数
MAX_FLOODS = 2       # FloodWait 的额外重试次数（不占用上面的额度）
MAX_SLOW_HITS = 3    # 慢速模式的额外重试次数
RESUME_PROBE_INTERVAL = 600.0  # 暂停中的目标多久自动探测恢复一次（秒）


# --------------------------------------------------------------------------
# 限速器
# --------------------------------------------------------------------------


class Pacer:
    """两层节流：全局每分钟上限 + 目标之间的最小间隔。

    所有 worker 共用一把锁来"预约"发送时刻，避免多个群同时收到同一条消息。
    """

    def __init__(self, rate: Rate, *, premium: bool = False) -> None:
        factor = 4 if premium else 1  # Premium 配额更高，这里按 4 倍放宽
        self.rate = rate
        self.global_per_minute = max(1, rate.global_per_minute * factor)
        # 0 表示不限（完全交给各群的 daily_limit）；否则按 Premium 放宽
        self.daily_cap = 0 if rate.daily_cap <= 0 else max(1, rate.daily_cap * factor)
        self.cross_target_delay = rate.cross_target_delay
        self._min_spacing = 60.0 / self.global_per_minute
        self._lock = asyncio.Lock()
        self._last_send = 0.0

    def interval_for(self, target: Target) -> tuple[float, float]:
        return target.interval or self.rate.per_target_interval

    async def reserve(self, interval: tuple[float, float]) -> float:
        """预约一次发送；返回实际等待的秒数。"""
        async with self._lock:
            now = time.monotonic()
            cross = random.uniform(*self.cross_target_delay)
            wait = max(
                self._min_spacing,
                cross,
                self._last_send + random.uniform(*interval) - now,
            )
            wait = max(0.0, wait)
            self._last_send = now + wait
        if wait > 0:
            await asyncio.sleep(wait)
        return wait

    def observe_flood(self, seconds: float) -> None:
        """被 FloodWait 打过之后自适应收紧：全局速率减半，最小间隔翻倍。"""
        self.global_per_minute = max(2, self.global_per_minute // 2)
        self._min_spacing = 60.0 / self.global_per_minute
        log.warning(
            "自适应降速：全局上限降为 %s 次/分钟（本次 FloodWait %ss）",
            self.global_per_minute,
            int(seconds),
        )

    def describe_limits(self, targets: Sequence[Target]) -> str:
        """给日志用：一句话说明当前额度是怎么分配的。"""
        own = [t for t in targets if t.daily_limit > 0]
        if self.daily_cap > 0:
            base = f"全局总闸 {self.daily_cap} 条/天"
        else:
            base = "全局总闸已关闭（不限总量）"
        if own:
            detail = "，各群单独额度：" + "、".join(
                f"{t.display}={t.daily_limit}" for t in own
            )
        else:
            detail = "，各群未设单独额度"
        return base + detail


class FloodGuard:
    """记录每个目标被 FloodWait 罚站的截止时间。"""

    def __init__(self) -> None:
        self._until: dict[str, float] = {}

    def wait_for(self, target_id: Any) -> float:
        deadline = self._until.get(str(target_id))
        if deadline is None:
            return 0.0
        return max(0.0, deadline - time.monotonic())

    def penalize(self, target_id: Any, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        key = str(target_id)
        if deadline > self._until.get(key, 0.0):
            self._until[key] = deadline

    def active(self) -> dict[str, float]:
        now = time.monotonic()
        return {key: round(value - now, 1) for key, value in self._until.items() if value > now}


class SlowModeGuard:
    """记住某个群处于"群组慢速模式"的窗口。

    慢速模式和 FloodWait 不同：
      * FloodWait 是服务器罚你（通常几秒到几小时），等待期间**任何**发送都会被拒；
      * 慢速模式是群设置（可由群主设为 10s/30s/1min 甚至 15min），
        规则是"距群内上一条消息不足 N 秒就拒"，因此**在发送前预判等待**即可，
        完全不必先撞墙再退避——那样每次都要白等一个窗口。

    这里记录"下次可发送时刻"，供发送前预判；同时会在没人发言的情况下自愈。
    """

    def __init__(self, *, cooldown_cap: float = 180.0) -> None:
        self._ready_at: dict[str, float] = {}
        self._seconds: dict[str, float] = {}
        self.cooldown_cap = cooldown_cap

    def seconds_for(self, target_id: Any) -> float:
        return self._seconds.get(str(target_id), 0.0)

    def remaining(self, target_id: Any) -> float:
        """距离可以发送还剩多少秒（已过期则返回 0）。"""
        key = str(target_id)
        deadline = self._ready_at.get(key)
        if deadline is None:
            return 0.0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # 窗口已过。若期间目标群一直没人发言，就丢弃记录，
            # 避免一条错误的估计把之后所有消息无限延后。
            self._ready_at.pop(key, None)
            self._seconds.pop(key, None)
            return 0.0
        return remaining

    def penalize(self, target_id: Any, seconds: float) -> float:
        """收到 SlowModeWaitError 后，把窗口推进到 now + seconds。"""
        key = str(target_id)
        seconds = max(0.0, float(seconds))
        self._seconds[key] = seconds
        deadline = time.monotonic() + seconds
        if deadline > self._ready_at.get(key, 0.0):
            self._ready_at[key] = deadline
        return seconds

    def active(self) -> dict[str, float]:
        return {
            key: round(self.remaining(key), 1)
            for key in list(self._ready_at)
            if self.remaining(key) > 0
        }

    def windows(self) -> dict[str, float]:
        return {key: value for key, value in self._seconds.items() if self.remaining(key) > 0}


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RelayJob:
    job_id: str
    source_id: int
    source_peer: Any
    msg_ids: tuple[int, ...]
    kind: str = "single"
    preview: str = ""
    is_album: bool = False

    @property
    def source_msg_id(self) -> int:
        return self.msg_ids[0]


@dataclass
class SenderStats:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    flood_waits: int = 0
    slow_waits: int = 0
    last_error: str = ""
    per_target: dict[str, dict[str, int]] = field(default_factory=dict)

    def bump(self, target_id: Any, key: str, amount: int = 1) -> None:
        bucket = self.per_target.setdefault(str(target_id), {"sent": 0, "failed": 0, "skipped": 0})
        bucket[key] = bucket.get(key, 0) + amount


@dataclass(frozen=True)
class SendOutcome:
    status: str  # sent | failed | skipped
    target_msg_ids: tuple[int, ...] = ()
    error: str = ""
    attempts: int = 1
    permanent: bool = False


class PeerFloodBreaker:
    """PeerFlood 熔断器：一旦触发，全局停止发送并等待人工介入。"""

    def __init__(self) -> None:
        self.tripped = False
        self.reason = ""
        self._event = asyncio.Event()

    def trip(self, reason: str) -> None:
        if not self.tripped:
            self.tripped = True
            self.reason = reason
            log.error("熔断：%s。已停止所有发送，请检查账号状态后重启程序。", reason)
        self._event.set()

    def reset(self) -> None:
        self.tripped = False
        self.reason = ""
        self._event.clear()

    @property
    def event(self) -> asyncio.Event:
        return self._event


class WriteForbiddenGuard:
    """把「禁止发言」拆成"这个群的问题"和"账号的问题"，并各自刹车。

    为什么必须有这个东西（真实事故）：
        定时重发是**直接调 `Sender.send()`** 的，不走 TargetWorker 那条队列，
        所以 worker 上那套"永久失败就暂停目标"的保护对它无效。
        结果是号已经被限制、每条都失败，重发循环还每 31 秒再试一次，
        白白叠加负面信号。

    判定规则：
        * 单个目标报这个错 → 只封这个目标一段时间（PER_TARGET_COOLDOWN）；
        * 窗口期内**第二个不同的目标**也报 → 判定为账号级限制，
          返回 account_level=True，交由调用方熔断整个程序。
    """

    ACCOUNT_LEVEL_TARGETS = 2
    ACCOUNT_LEVEL_WINDOW = 900.0    # 15 分钟内两个不同群都出事 = 账号级
    PER_TARGET_COOLDOWN = 1800.0    # 单个目标被禁言后，30 分钟内不再尝试

    def __init__(self) -> None:
        self._recent: dict[str, float] = {}
        self._blocked: dict[str, float] = {}

    def note(self, target_id: Any, *, now: float | None = None) -> tuple[bool, int]:
        """记一次"禁止发言"。返回 (是否账号级, 窗口内出事的不同目标数)。"""
        now = time.monotonic() if now is None else now
        key = str(target_id)
        self._recent = {
            name: when
            for name, when in self._recent.items()
            if now - when <= self.ACCOUNT_LEVEL_WINDOW
        }
        self._recent[key] = now
        self._blocked[key] = now + self.PER_TARGET_COOLDOWN
        return len(self._recent) >= self.ACCOUNT_LEVEL_TARGETS, len(self._recent)

    def blocked_remaining(self, target_id: Any, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self._blocked.get(str(target_id), 0.0) - now)

    def clear(self, target_id: Any) -> None:
        key = str(target_id)
        self._blocked.pop(key, None)
        self._recent.pop(key, None)

    def clear_all(self) -> None:
        self._blocked.clear()
        self._recent.clear()

    def blocked_targets(self, *, now: float | None = None) -> dict[str, float]:
        """还剩多少秒 —— 给面板/日志看。"""
        now = time.monotonic() if now is None else now
        return {
            name: round(until - now)
            for name, until in self._blocked.items()
            if until > now
        }


# --------------------------------------------------------------------------
# 发送器
# --------------------------------------------------------------------------


class Sender:
    def __init__(
        self,
        client: Any,
        config: AppConfig,
        store: Store,
        *,
        breaker: PeerFloodBreaker | None = None,
        dry_run: bool = False,
        alerts: AlertHub | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.store = store
        self.breaker = breaker or PeerFloodBreaker()
        self.dry_run = dry_run
        self.pacer = Pacer(config.rate, premium=config.premium)
        self.flood = FloodGuard()
        self.slow = SlowModeGuard()
        self.write_forbidden = WriteForbiddenGuard()
        self.alerts = alerts or AlertHub()
        self.stats = SenderStats()
        self._budget_lock = asyncio.Lock()
        # 上次问 @SpamBot 的时刻（monotonic）；None = 还没问过
        self._last_account_check: float | None = None
        # 这两个是给测试用的：真实环境下 @SpamBot 一般 1~3 秒回话
        self.account_check_interval = 2.0
        self.account_check_polls = 8

    # ---------- 实体解析 ----------

    async def resolve(self, peer: Any, label: str = "目标") -> Any:
        """把配置里的用户名/数字 ID 解析成可发送的 entity。

        数字 ID 需要账号本地已有该 peer（暖场过），否则会提示先手动打开一次会话。
        """
        try:
            return await self.client.get_input_entity(peer)
        except Exception as first_error:
            try:
                entity = await self.client.get_entity(peer)
                return await self.client.get_input_entity(entity)
            except Exception as second_error:
                raise ConfigProblem(
                    f"{label} {peer!r} 无法解析：{second_error}。"
                    "请确认该账号已在其中，并且先用这个号打开过一次该会话（暖场）。"
                ) from first_error

    # 这些权限位中任意一个为 True，就说明"有某种内容能发出去"
    SEND_RIGHT_FLAGS = (
        "send_messages",
        "send_plain",
        "send_media",
        "send_photos",
        "send_videos",
        "send_audios",
        "send_voices",
        "send_docs",
        "send_gifs",
        "send_games",
        "send_stickers",
        "send_polls",
        "send_roundvideos",
        "send_inline",
    )

    @classmethod
    def _send_rights(cls, entity: Any, permissions: Any) -> tuple[bool, str]:
        """尽力预判"能不能往这里发东西"，并给出人类可读的原因。

        这段逻辑是被真实数据打出来的，说明一下为什么这么写：

        * Telethon 的 `ParticipantPermissions` 根本没有 `send_messages`
          （见 telethon/tl/custom/participantpermissions.py，它只描述管理员权限），
          所以"普通成员能不能发言"读不到，只能看 `default_banned_rights`。
        * `default_banned_rights` 是 **ChatBannedRights**，是一整组开关，
          而且 **True = 禁止**（不是"允许"）。实测目标群
          `send_messages=False, send_photos=True, send_media=True, embed_links=True`：
          它其实是「**只允许发纯文本**」的广告群——文字能发，图片/链接不能。
          正因为如此，一条纯文字转发在那个群里能成功，看图权限位会得出相反结论。
        * 所以只有**连纯文本都不让发、并且所有媒体类型也全禁**时，才判定真的发不出东西。
          其余情况一律放行——宁可让发送时去撞真实错误（错误分级已经很细），
          也不要因为误判把能用的目标拦下来。

        想要 100% 确定的结论，用 `--probe-send` 真发一条试试。
        另外这个函数**读不到账号级限制**：号被 Telegram 限制时，
        群权限依然显示"普通成员、可发言"。那种情况只能靠真发或问 @SpamBot。
        """
        participant = getattr(permissions, "participant", None) if permissions is not None else None

        # 被封禁 / 已退出
        if participant is not None and hasattr(permissions, "is_banned") and permissions.is_banned:
            return False, "账号已被该群封禁"
        if participant is not None and hasattr(permissions, "has_left") and permissions.has_left:
            return False, "账号已退出该群"

        is_admin = bool(
            getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False)
        )

        if is_admin:
            rights = getattr(participant, "admin_rights", None)
            if rights is None:
                return True, ""
            can_post = bool(
                getattr(rights, "post_messages", False)
                or getattr(rights, "send_messages", False)
            )
            if not can_post:
                return False, "你是该群管理员，但没有「发送消息」权限位"
            return True, ""

        default_banned = getattr(entity, "default_banned_rights", None)
        if default_banned is None:
            return True, ""

        # ChatBannedRights 里 True = 禁止。分两组看：文字和媒体。
        text_banned = bool(
            getattr(default_banned, "send_messages", False)
            or getattr(default_banned, "send_plain", False)
        )
        media_flags = [
            flag
            for flag in cls.SEND_RIGHT_FLAGS
            if flag not in ("send_messages", "send_plain") and hasattr(default_banned, flag)
        ]
        media_all_banned = bool(media_flags) and all(
            getattr(default_banned, flag) for flag in media_flags
        )

        if text_banned and media_all_banned:
            return False, (
                "该群默认成员权限里**文字和所有媒体都被禁**，这个号又不是管理员，"
                "确实发不出任何东西"
            )
        # 只禁文字、或只禁某几类媒体：能不能发取决于**素材本身**是文字还是媒体，
        # 这个函数看不到素材，所以放行，让发送时去撞真实错误（错误分级已经足够细）。
        return True, ""

    async def postability(self, target: Target) -> tuple[bool, str]:
        """返回 (能不能发言, 原因)。这是**尽力预判**，不是权威结论。"""
        try:
            entity = await self.resolve(target.id, "目标")
        except ConfigProblem as exc:
            return False, str(exc)
        if self.dry_run:
            return True, ""

        try:
            full = await self.client.get_entity(entity)
        except Exception:
            full = entity

        permissions: Any = None
        try:
            permissions = await self.client.get_permissions(full, None)
        except Exception as exc:
            log.debug("读取 %s 的权限失败：%s", target.display, exc)

        return self._send_rights(full, permissions)

    async def probe_send(self, target: Target) -> tuple[bool, str]:
        """权威判定：真的转发一条源消息，看 Telegram 是接受还是拒绝。

        被动读权限位已被实测证明不可靠（群权限显示"可发言"，
        账号却可能被 Telegram 限制），所以拿不准时用这个。
        **注意：这会真的往目标里发一条消息。**

        探测成功说明"这个号现在确实能往这个群发"，
        于是顺手清掉该目标因「禁止发言」留下的冷板凳。
        """
        source = self.config.source
        try:
            source_entity = await self.client.get_entity(source)
            source_input = await self.client.get_input_entity(source_entity)
            messages = await self.client.get_messages(source_input, limit=1)
        except Exception as exc:
            return False, f"读取源 {source} 失败：{type(exc).__name__}: {exc}"
        if not messages:
            return False, f"源 {source} 里没有消息，无法探测"

        probe = messages[0]
        try:
            peer = await self.resolve(target.id, "目标")
            result = await self.client.forward_messages(
                peer, probe.id, from_peer=source_input
            )
        except Exception as exc:
            note = _hint(exc)
            if isinstance(exc, WRITE_FORBIDDEN_ERRORS):
                self.write_forbidden.note(target.id)
            return False, f"{type(exc).__name__}: {exc}" + (f" —— {note}" if note else "")

        self.write_forbidden.clear(target.id)

        if isinstance(result, (list, tuple)):
            ids = [getattr(item, "id", None) for item in result]
        else:
            ids = [getattr(result, "id", None)]
        return True, f"探测成功，已在 {target.display} 发出消息 id={ids}（源消息 {probe.id}）"

    async def check_write_permission(self, target: Target) -> str:
        """返回 '' 表示可发送，否则返回不可发送的原因。"""
        allowed, reason = await self.postability(target)
        return "" if allowed else reason

    async def probe_slow_mode(self, target: Target) -> int | None:
        """读取群的真实慢速模式设置（秒）。

        慢速模式决定这个目标的**实际吞吐上限**：30s => 最多 2 条/分钟，
        比任何本地限速参数都硬。所以启动时先把它读出来，避免按错误的预期配速。
        """
        if self.dry_run:
            return None
        try:
            entity = await self.resolve(target.id, "目标")
        except ConfigProblem:
            return None
        try:
            full = await self.client.get_entity(entity)
        except Exception:
            full = entity
        seconds = getattr(full, "slowmode_seconds", None)
        if seconds is None:
            return None
        seconds = int(seconds)
        if seconds > 0:
            self.slow.penalize(target.id, seconds)  # 让发送端一上来就按这个窗口配速
        return seconds

    # ---------- 配额 ----------

    @property
    def global_daily_cap(self) -> int:
        """全局每日总闸。0 = 不限（完全交给各群自己的 daily_limit）。"""
        return self.pacer.daily_cap

    async def _take_budget(
        self,
        amount: int,
        *,
        as_repost: bool = False,
        limit: int | None = None,
        target: Target | None = None,
    ) -> None:
        """占用发送额度。

        三本账，互不挤占：
          1. 实时转发总额度   rate.daily_cap        （全局兜底）
          2. 重发总额度       repost.daily_limit    （重发专用）
          3. **每个目标单独的额度** target.daily_limit（多群时必须按群计）

        最终上限 = min(该类别的总额度, 该目标的额度)，目标没配就用 0 表示不限制。

        判定用的是 **checked** 计数（每次检查 +1），不是 sent 计数。
        混用会让每条消息提前一条被拒：sent 在检查前已经被本条累加过，
        于是 `daily_cap=100` 实际只能发出 99 条。
        """
        # 注意：这里**不**把目标额度并进 cap。
        # 全局判定只能用全局上限，目标判定用目标上限 —— 两个限制各自独立成立。
        # 早期版本写成 cap = min(全局, 目标) 再拿全局计数去比，
        # 结果是"某个群的目标额度会消耗掉全局计数、把其它群饿死"（真实踩过）。
        cap = limit if limit is not None else self.pacer.daily_cap
        target_capped = target is not None and target.daily_limit > 0
        # cap == 0 表示"不设全局总闸"，完全按各群自己的 daily_limit 走。
        # 这适合"群多、各自配额明确"的场景；单群时保留全局上限更稳妥。
        global_capped = cap > 0

        async with self._budget_lock:
            target_checked = (
                self.store.target_checked_today(target.id) if target is not None else 0
            )

            # 1) 先判该目标自己的额度
            if target is not None and target.daily_limit > 0:
                if target_checked + amount > target.daily_limit:
                    raise BudgetExhausted(
                        f"今日 {target.display} 的配额已用完：{target_checked}/{target.daily_limit}"
                    )

            # 2) 再判全局总闸（只有配了 > 0 才生效）
            if global_capped:
                global_checked = self.store.checked_today(as_repost=as_repost)
                if global_checked + amount > cap:
                    kind = "重发" if as_repost else "转发"
                    scope = f"（{target.display}）" if target is not None else ""
                    raise BudgetExhausted(
                        f"今日{kind}总配额已用完{scope}：{global_checked}/{cap}"
                    )

            # 检查通过才占位：被拒的消息不消耗计数，
            # 否则错误信息里的数字会一直往上飘（3/2、4/2…）。
            if global_capped:
                self.store.note_checked(amount, as_repost=as_repost)
            if target is not None:
                self.store.note_target_checked(target.id, amount)

    def effective_limit(self, target: Target, *, as_repost: bool = False) -> int:
        """算给界面看：该目标今天最多能发多少条。

        返回 0 表示"不限"（全局和该群都没设上限）。
        """
        if as_repost and self.config.repost.daily_limit:
            cap = self.config.repost.daily_limit
        else:
            cap = self.pacer.daily_cap
        if target.daily_limit > 0:
            cap = target.daily_limit if cap <= 0 else min(cap, target.daily_limit)
        return cap

    async def _release_budget(self, *, as_repost: bool, target: Target | None) -> None:
        """把没送出去的额度占位退回去。

        占位（checked）存在的意义是"让 daily_cap=100 真能发满 100 条"，
        但一条永久失败的消息并没有占用群里的位置，继续占着额度只会让
        一个已经被封的目标把当天的配额吃光。静默失败不致命，所以这里吞异常。
        """
        try:
            self.store.release_checked(1, as_repost=as_repost)
            if target is not None:
                self.store.release_target_checked(target.id, 1)
        except Exception:  # pragma: no cover - 退额度失败不该影响主流程
            log.exception("退还额度占位失败（%s）", target.display if target else "全局")

    async def _handle_write_forbidden(self, target: Target, exc: Exception) -> None:
        """「禁止发言」的统一处置：先封单个目标，再判断是不是账号级限制。"""
        account_level, count = self.write_forbidden.note(target.id)

        # 计数法（多个群同时出事）只在群多的时候有效。
        # 只配了一个群时它永远达不到阈值，所以这里补一次**权威**判定：
        # 问 @SpamBot。半小时最多问一次，避免失败重试时反复打扰。
        if not account_level and self._should_consult_account():
            limited, detail = await self.account_status()
            self._last_account_check = time.monotonic()
            if limited:
                account_level = True
                log.error("@SpamBot 确认：账号当前处于被限制状态")
            else:
                log.info("已问过 @SpamBot：账号目前没有被限制，问题出在这个群本身")

        if account_level:
            self.breaker.trip(
                f"{count} 个目标报「禁止发言」（{type(exc).__name__}）——"
                "这是**账号级**限制，不是单个群的问题"
            )
            await self.alerts(
                "critical",
                "已熔断停止全部发送：这个号被 Telegram 限制了（反垃圾），不是群的问题。\n"
                "先去 @SpamBot 看状态并申诉；解除后重新开启重发即可。\n"
                "限制解除前继续发只会加重处罚。",
            )
            return
        await self.alerts(
            "warning",
            f"{target.display} 报 {type(exc).__name__}：已把这个目标停发 "
            f"{self.write_forbidden.PER_TARGET_COOLDOWN / 60:.0f} 分钟。\n"
            "如果**其它群**也开始这样，那就是账号被限制了（@SpamBot 可查）。",
        )

    def _should_consult_account(self) -> bool:
        if self._last_account_check is None:
            return True
        return (
            time.monotonic() - self._last_account_check
            >= self.write_forbidden.ACCOUNT_LEVEL_WINDOW
        )

    async def account_status(self) -> tuple[bool | None, str]:
        """问 @SpamBot：这个号现在有没有被限制。

        返回 (是否被限制, 原文)。None 表示问不出来（网络/被拒），
        —— 这是唯一能直接读到"账号级限制"的办法：
        `channels.GetParticipant` 在账号被限制时依然显示"普通成员、无限制"。
        **注意这会真的给 @SpamBot 发一条 /start。**
        """
        try:
            bot = await self.client.get_entity("SpamBot")
            sent = await self.client.send_message(bot, "/start")
            reply = None
            for _ in range(self.account_check_polls):
                await asyncio.sleep(self.account_check_interval)
                for msg in await self.client.get_messages(bot, limit=5):
                    if getattr(msg, "id", None) != getattr(sent, "id", None) and getattr(msg, "text", None):
                        reply = msg
                        break
                if reply is not None:
                    break
        except Exception as exc:
            return None, f"问 @SpamBot 失败：{type(exc).__name__}: {exc}"

        if reply is None:
            return None, "15 秒内没收到 @SpamBot 的回复，稍后再试"

        text = reply.text or ""
        if "no limits are currently applied" in text or "free as a bird" in text:
            return False, text
        if "limited" in text.lower() or "sorry" in text.lower():
            return True, text
        return None, f"看不懂 @SpamBot 的回复：{text[:200]}"

    # ---------- 单次转发 ----------

    async def _forward(self, job: RelayJob, target: Target) -> Sequence[Any]:
        peer = await self.resolve(target.id, "目标")
        if job.is_album and self.config.behavior.forward_as_album:
            return await self.client.forward_messages(
                peer, list(job.msg_ids), from_peer=job.source_peer, as_album=True
            )
        return await self.client.forward_messages(
            peer, list(job.msg_ids), from_peer=job.source_peer
        )

    async def _wait_for_slow_mode(self, target: Target) -> float:
        """慢速模式预判等待。返回实际等待秒数；超过上限则抛 SlowModeTooLong。"""
        remaining = self.slow.remaining(target.id)
        if remaining <= 0:
            return 0.0
        cap = self.slow.cooldown_cap
        if remaining > cap:
            raise SlowModeTooLong(
                f"{target.display} 慢速模式还需 {int(remaining)}s，超过上限 {int(cap)}s，本条跳过"
            )
        window = self.slow.seconds_for(target.id)
        log.info(
            "%s 慢速模式生效：等 %.0fs 再发（该群限制 %.0fs/条）",
            target.display,
            remaining,
            window,
        )
        await asyncio.sleep(remaining)
        return remaining

    async def send(
        self,
        job: RelayJob,
        target: Target,
        *,
        daily_limit: int | None = None,
    ) -> SendOutcome:
        """转发一条任务。

        daily_limit 用于定时重发：传了它就走"重发"那本配额账，
        不去消耗实时转发的日额度。
        """
        as_repost = daily_limit is not None

        if self.breaker.tripped:
            return SendOutcome("skipped", error=f"熔断中：{self.breaker.reason}", permanent=True)

        # 这个号刚被这个群拒过「禁止发言」：冷却期内连 API 都不碰。
        # 定时重发是直接调这里的，不走 worker 队列，所以这道闸必须放在最前面。
        blocked = self.write_forbidden.blocked_remaining(target.id)
        if blocked > 0:
            self.stats.skipped += 1
            self.stats.bump(target.id, "skipped")
            return SendOutcome(
                "skipped",
                error=(
                    f"{target.display} 因「禁止发言」停发中，还剩约 {blocked / 60:.0f} 分钟。"
                    "如果别的群也这样，就是账号被限制了（@SpamBot 可查）"
                ),
                permanent=True,
            )

        remaining = self.flood.wait_for(target.id)
        if remaining > 0:
            log.info("%s 仍在 FloodWait 罚站中，剩余 %.0fs，稍后重试", target.display, remaining)
            await asyncio.sleep(min(remaining, 60.0))

        # 慢速模式先预判等待，避免"撞墙 -> 退避 -> 再撞墙"浪费整个窗口
        try:
            await self._wait_for_slow_mode(target)
        except SlowModeTooLong as exc:
            self.stats.skipped += 1
            self.stats.bump(target.id, "skipped")
            log.error("%s", exc)
            return SendOutcome("skipped", error=str(exc), permanent=False)

        interval = self.pacer.interval_for(target)
        waited = await self.pacer.reserve(interval)

        if self.dry_run:
            log.info(
                "[dry-run] 将转发 %s -> %s（%s 条，等待 %.1fs）",
                job.source_id,
                target.display,
                len(job.msg_ids),
                waited,
            )
            self.stats.sent += 1
            self.stats.bump(target.id, "sent")
            return SendOutcome("sent")

        try:
            # 配额按"帖子数"计，不按媒体条数：一个 5 张图的相册只算 1 条
            # 传 target 是为了让该目标自己的 daily_limit 也参与判定
            await self._take_budget(1, as_repost=as_repost, limit=daily_limit, target=target)
        except BudgetExhausted as exc:
            self.stats.skipped += 1
            self.stats.bump(target.id, "skipped")
            log.warning("跳过 %s -> %s：%s", job.source_id, target.display, exc)
            return SendOutcome("skipped", error=str(exc), permanent=True)

        attempts = 0
        floods = 0
        slow_hits = 0
        last_error = ""
        # FloodWait / 慢速模式都不消耗"发送尝试"额度，各自单独给机会，
        # 否则一次慢速模式就能把重试次数耗尽、白白丢掉这条消息
        while attempts < MAX_ATTEMPTS and floods <= MAX_FLOODS:
            attempts += 1
            try:
                messages = await self._forward(job, target)
                if messages is None:
                    ids: tuple[int, ...] = ()
                elif isinstance(messages, (list, tuple)):
                    ids = tuple(int(m.id) for m in messages if getattr(m, "id", None))
                else:
                    ids = (int(messages.id),)
                self.stats.sent += 1
                self.stats.bump(target.id, "sent")
                # 真正发出去了才计入"已发送"（额度占位在 _take_budget 里已经记过）
                self.store.add_sent(1, as_repost=as_repost)
                self.store.add_target_sent(target.id, 1)
                log.info(
                    "已转发 %s -> %s：%s 条%s",
                    job.source_id,
                    target.display,
                    len(job.msg_ids),
                    f"（{job.preview}）" if job.preview else "",
                )
                return SendOutcome("sent", target_msg_ids=ids, attempts=attempts)

            except errors.FloodWaitError as exc:
                seconds = float(getattr(exc, "seconds", 60))
                floods += 1
                self.stats.flood_waits += 1
                self.flood.penalize(target.id, seconds + 5)
                self.pacer.observe_flood(seconds)
                last_error = f"FloodWait {int(seconds)}s"
                log.warning(
                    "FloodWait：%s 需等待 %ss（第 %s 次）",
                    target.display,
                    int(seconds),
                    floods,
                )
                if seconds > 900:
                    log.error("%s 的 FloodWait 超过 15 分钟，放弃本条并暂停该目标", target.display)
                    return SendOutcome("failed", error=last_error, attempts=attempts)
                await asyncio.sleep(min(seconds + 3, 300))
                if floods > MAX_FLOODS:
                    break
                continue

            except errors.SlowModeWaitError as exc:
                # 慢速模式不消耗发送尝试额度，单独限流；预判失败时这里兜底
                seconds = float(getattr(exc, "seconds", 30))
                window = self.slow.penalize(target.id, seconds)
                slow_hits += 1
                self.stats.slow_waits += 1
                last_error = f"SlowModeWait {int(seconds)}s"
                log.warning(
                    "%s 触发慢速模式：该群限制 %ss/条，等待后重试（第 %s 次）",
                    target.display,
                    int(window),
                    slow_hits,
                )
                if slow_hits > MAX_SLOW_HITS:
                    log.error(
                        "%s 连续 %s 次撞上慢速模式，放弃本条（该群配速高于慢速限制）",
                        target.display,
                        slow_hits,
                    )
                    break
                await asyncio.sleep(min(seconds + 2, 300))
                continue

            except errors.PeerFloodError as exc:
                self.breaker.trip(f"收到 PeerFloodError：{exc}")
                await self._release_budget(as_repost=as_repost, target=target)
                await self.alerts(
                    "critical",
                    "熔断：收到 PeerFloodError，已停止全部发送。"
                    "这是账号级的发送限制，先别再发，等冷却后再手动重启程序。",
                )
                return SendOutcome("failed", error="PeerFloodError", attempts=attempts, permanent=True)

            except PERMANENT_ERRORS as exc:
                reason = type(exc).__name__
                note = _hint(exc)
                log.error(
                    "%s 永久失败：%s（%s）%s",
                    target.display,
                    reason,
                    exc,
                    f" —— {note}" if note else "",
                )
                self.stats.failed += 1
                self.stats.bump(target.id, "failed")
                # 这条根本没送出去，把额度占位退回去，别让坏目标吃掉当天配额
                await self._release_budget(as_repost=as_repost, target=target)
                if isinstance(exc, WRITE_FORBIDDEN_ERRORS):
                    await self._handle_write_forbidden(target, exc)
                return SendOutcome(
                    "failed",
                    error=f"{reason}: {exc}" + (f" | {note}" if note else ""),
                    attempts=attempts,
                    permanent=True,
                )

            except TRANSIENT_ERRORS as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                backoff = min(2 ** attempts, 30) + random.uniform(0, 1)
                log.warning(
                    "%s 临时错误：%s，%.1fs 后重试",
                    target.display,
                    last_error,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue

            except ConfigProblem:
                raise

            except Exception as exc:  # 未知错误：记录但不熔断
                last_error = f"{type(exc).__name__}: {exc}"
                log.exception("%s 发送异常：%s", target.display, last_error)
                break

        self.stats.failed += 1
        self.stats.bump(target.id, "failed")
        self.stats.last_error = last_error
        return SendOutcome("failed", error=last_error, attempts=attempts)


# --------------------------------------------------------------------------
# 每目标 worker
# --------------------------------------------------------------------------


@dataclass
class WorkerStats:
    queued: int = 0
    processed: int = 0
    dropped: int = 0
    peak_backlog: int = 0
    last_drop_log: float = 0.0


class TargetWorker:
    """单个目标群的发送 worker：独立队列、独立节奏。"""

    def __init__(
        self,
        target: Target,
        sender: Sender,
        store: Store,
        *,
        queue_size: int = 200,
        drop_on_queue_full: bool = True,
    ) -> None:
        self.target = target
        self.sender = sender
        self.store = store
        self.drop_on_queue_full = drop_on_queue_full
        self.queue: asyncio.Queue[RelayJob | None] = asyncio.Queue(maxsize=queue_size)
        self.stats = WorkerStats()
        self.paused = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # ---------- 队列 ----------

    def submit(self, job: RelayJob) -> bool:
        """入队。返回 False 表示这条没能进入队列。

        队列满时**丢弃最旧的那条**（滑窗），而不是丢弃刚收到的新消息：
        目标群被慢速模式压住时，队列里堆的往往是几分钟前的内容，
        保新弃旧对读者更合理，也能让最新消息尽快转发出去。
        """
        if self.paused:
            return False
        try:
            self.queue.put_nowait(job)
            self._note_backlog()
            self.stats.queued += 1
            return True
        except asyncio.QueueFull:
            pass

        if not self.drop_on_queue_full:
            log.warning("%s 队列已满（%s），本条丢弃", self.target.display, self.queue.maxsize)
            self._note_drop()
            return False

        try:
            evicted = self.queue.get_nowait()
            self.queue.task_done()
        except (asyncio.QueueEmpty, ValueError):
            evicted = None
        if evicted is not None:
            self._note_drop()
            log.warning(
                "%s 队列积压已达上限 %s，丢掉最旧的 %s（源消息 %s）以保住最新内容",
                self.target.display,
                self.queue.maxsize,
                evicted.job_id,
                evicted.source_msg_id,
            )
        try:
            self.queue.put_nowait(job)
            self._note_backlog()
            self.stats.queued += 1
            return True
        except asyncio.QueueFull:  # pragma: no cover - 竞态兜底
            self._note_drop()
            return False

    def _note_drop(self) -> None:
        self.stats.dropped += 1
        # 积压告警只在跨过阈值时打一次，避免刷屏
        if self.stats.dropped == 1 or self.stats.dropped % 25 == 0:
            log.warning(
                "%s 累计丢弃 %s 条（队列 %s，慢速/积压导致）",
                self.target.display,
                self.stats.dropped,
                self.queue.maxsize,
            )

    def _note_backlog(self) -> None:
        size = self.queue.qsize()
        if size > self.stats.peak_backlog:
            self.stats.peak_backlog = size

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"worker:{self.target.display}")

    async def stop(self) -> None:
        self._stop.set()
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=20)
            except asyncio.TimeoutError:
                self._task.cancel()
                log.warning("%s worker 未在 20s 内退出，已强制取消", self.target.display)
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        log.info("%s worker 已启动（队列上限 %s）", self.target.display, self.queue.maxsize)
        while not self._stop.is_set():
            try:
                job = await asyncio.wait_for(self.queue.get(), timeout=RESUME_PROBE_INTERVAL)
            except asyncio.TimeoutError:
                self._probe_resume()
                continue
            except asyncio.CancelledError:
                break
            if job is None:
                self.queue.task_done()
                break
            try:
                await self._handle(job)
            except asyncio.CancelledError:
                self.queue.task_done()
                raise
            except Exception:
                log.exception("%s worker 处理 %s 时未捕获异常", self.target.display, job.job_id)
                self.store.record_delivery(
                    job_id=job.job_id,
                    target_id=self.target.id,
                    source_id=job.source_id,
                    source_msg_id=job.source_msg_id,
                    status="failed",
                    error="worker 内部异常",
                )
            finally:
                self.stats.processed += 1
                self.queue.task_done()

    def _probe_resume(self) -> None:
        """空转时探测一次：暂停中的目标每 RESUME_PROBE_INTERVAL 秒自动解除暂停。

        这样"临时性故障（漏点了一次 / 被短时间限权）"能自愈，
        真正永久性的问题会在下次发送时立刻重新暂停，代价只是一条错误日志。
        """
        if not self.paused:
            return
        self.paused = False
        log.info("%s 已自动解除暂停，重新尝试发送（若问题仍在会再次暂停）", self.target.display)

    async def _handle(self, job: RelayJob) -> None:
        try:
            outcome = await self.sender.send(job, self.target)
        except ConfigProblem as exc:
            log.error("%s 配置/解析问题：%s", self.target.display, exc)
            self.paused = True
            self.store.record_delivery(
                job_id=job.job_id,
                target_id=self.target.id,
                source_id=job.source_id,
                source_msg_id=job.source_msg_id,
                status="failed",
                error=str(exc),
            )
            return

        self.store.record_delivery(
            job_id=job.job_id,
            target_id=self.target.id,
            source_id=job.source_id,
            source_msg_id=job.source_msg_id,
            status=outcome.status,
            target_msg_ids=outcome.target_msg_ids,
            error=outcome.error,
        )
        if outcome.permanent and outcome.status == "failed":
            self.paused = True
            log.error(
                "%s 已暂停接收新任务（永久性失败：%s），修复后重启程序即可恢复",
                self.target.display,
                outcome.error,
            )
