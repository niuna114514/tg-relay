"""发言权限判定测试。

关键背景（这段是照 Telethon 1.44 源码写的，不是猜的）：

  telethon/tl/custom/participantpermissions.py 里的 `ParticipantPermissions`
  **只有管理员相关属性**：is_admin / is_creator / is_banned / has_left /
  add_admins / ban_users / pin_messages / invite_users …
  **没有 `send_messages`**。

所以"普通成员能不能发言"必须看频道/群的
`default_banned_rights.send_messages`（默认成员权限），而不是权限对象。

历史上这里踩过两次坑：
  1. `getattr(perms, "send_messages", True)`  —— 字段缺失 -> 默认放行 -> 误判成能发言
  2. 把"字段缺失"当成"没有显式权限"再去回落 —— 看上去对，但语义仍是猜的
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import FakeClient  # noqa: E402
from tgrelay.config import AppConfig, Filters, Rate, Target  # noqa: E402
from tgrelay.db import Store  # noqa: E402
from tgrelay.sender import Sender  # noqa: E402


# --------------------------------------------------------------------------
# 复刻 Telethon 的真实对象形状
# --------------------------------------------------------------------------


@dataclass
class BannedRights:
    """对应 tl.types.ChatBannedRights（default_banned_rights）。

    **这些 flag 是反的**（官方 TL 文档原话：
    "the flags are inverted: if set, a flag does not allow a user to do X"，
    见 https://docs.pyrogram.org/telegram/types/chat-banned-rights ）：
        True  = 禁止
        False = 允许
    所以全部 False 才是"什么都能发"的开放群。

    真实目标群的取值（实测）：
        send_messages=False, send_plain=False,      ← 文字**允许**
        send_media=True, send_photos=True, embed_links=True, …  ← 媒体/链接**禁止**
    也就是"只让普通成员发纯文本"的广告群，一条纯文字转发在里面能成功。

    历史坑：这个方向一开始搞反了（把 True 读成"允许"），
    于是"全 False"被当成"全部被禁"，反而会把一个完全开放的群拦下来。
    """

    send_messages: bool = False
    send_plain: bool = False
    send_media: bool = False
    send_photos: bool = False
    send_videos: bool = False
    send_docs: bool = False
    send_gifs: bool = False
    send_stickers: bool = False


def all_banned_rights() -> BannedRights:
    """文字和所有媒体都被禁 —— 这才是真的发不出任何东西。"""
    return BannedRights(
        send_messages=True,
        send_plain=True,
        send_media=True,
        send_photos=True,
        send_videos=True,
        send_docs=True,
        send_gifs=True,
        send_stickers=True,
    )


@dataclass
class AdminRights:
    """对应 tl.types.ChatAdminRights（participant.admin_rights）。"""

    post_messages: bool = False
    send_messages: bool = False


@dataclass
class Participant:
    """对应 tl.types.ChannelParticipant / ChannelParticipantSelf 等。"""

    admin_rights: AdminRights | None = None


@dataclass
class Permissions:
    """复刻 ParticipantPermissions：注意它没有 send_messages。"""

    participant: Participant | None = None
    is_admin: bool = False
    is_creator: bool = False
    is_banned: bool = False
    has_left: bool = False


@dataclass
class Entity:
    id: int = -2001
    title: str = "测试群"
    default_banned_rights: BannedRights | None = None
    slowmode_seconds: int | None = None


class PermClient(FakeClient):
    def __init__(self, *, entity: Entity, permissions: Any) -> None:
        super().__init__()
        self._entity = entity
        self._permissions = permissions

    async def get_entity(self, peer: Any) -> Any:
        return self._entity

    async def get_permissions(self, entity: Any, user: Any = None) -> Any:
        if isinstance(self._permissions, Exception):
            raise self._permissions
        return self._permissions


def make_sender(client: PermClient, tmp_path: Path, *, dry_run: bool = False) -> tuple[Sender, Store]:
    config = AppConfig(
        sources=(-1001,),
        targets=(Target(id=-2001),),
        filters=Filters(),
        rate=Rate(per_target_interval=(0, 0), cross_target_delay=(0, 0), global_per_minute=60, daily_cap=10),
    )
    store = Store(tmp_path / "relay.db")
    return Sender(client, config, store, dry_run=dry_run), store


# --------------------------------------------------------------------------
# 普通成员：只能看 default_banned_rights
# --------------------------------------------------------------------------


async def test_text_only_group_allows_forward(tmp_path: Path) -> None:
    """**核心回归**：真实目标群 —— 只允许文字、禁止媒体和链接。

    这是实测出来的：`send_messages=False`（且 `send_plain=False`）的群代表
    文字**允许**，用 `forward_messages` 转发一条纯文字消息能成功（实测发出过 229 条），
    所以绝不能因为"媒体位是 True"就判定"不能发"。
    """
    client = PermClient(
        entity=Entity(
            default_banned_rights=BannedRights(
                send_media=True, send_photos=True, send_docs=True, send_gifs=True
            )
        ),
        permissions=Permissions(participant=Participant()),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, reason = await sender.postability(Target(id=-2001))
        assert allowed is True, f"只禁媒体的群不应被拦下，实际原因: {reason}"
        assert await sender.check_write_permission(Target(id=-2001)) == ""
    finally:
        store.close()


async def test_everything_banned_is_denied(tmp_path: Path) -> None:
    """文字和所有媒体都被禁，才是真的发不出去。"""
    client = PermClient(
        entity=Entity(default_banned_rights=all_banned_rights()),
        permissions=Permissions(participant=Participant()),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, reason = await sender.postability(Target(id=-2001))
        assert allowed is False
        assert "媒体都被禁" in reason
    finally:
        store.close()


async def test_text_banned_media_ok_still_allowed(tmp_path: Path) -> None:
    """只禁文字（媒体类全开）-> 允许：能不能发取决于素材本身，这里看不到素材。

    宁可放行让发送时去撞真实错误（错误分级很细），也不要误拦一个其实能发媒体的群。
    """
    client = PermClient(
        entity=Entity(default_banned_rights=BannedRights(send_messages=True, send_plain=True)),
        permissions=Permissions(participant=Participant()),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, _ = await sender.postability(Target(id=-2001))
        assert allowed is True
    finally:
        store.close()


async def test_open_group_is_allowed(tmp_path: Path) -> None:
    """普通开放群：所有 flag 都是 False（= 什么都不禁）-> 允许。

    这一条是方向搞反时的重灾区：早期把"全 False"读成"全部被禁"，
    于是完全开放的群反而被判成"发不出任何东西"。
    """
    client = PermClient(
        entity=Entity(default_banned_rights=BannedRights()),
        permissions=Permissions(participant=Participant()),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, reason = await sender.postability(Target(id=-2001))
        assert allowed is True
        assert reason == ""
    finally:
        store.close()


async def test_missing_default_rights_is_treated_as_allowed(tmp_path: Path) -> None:
    """群里没设置默认权限（字段缺失）-> 老群/普通群，按允许处理。"""
    client = PermClient(
        entity=Entity(default_banned_rights=None),
        permissions=Permissions(participant=Participant()),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, _ = await sender.postability(Target(id=-2001))
        assert allowed is True
    finally:
        store.close()


# --------------------------------------------------------------------------
# 管理员：看 admin_rights
# --------------------------------------------------------------------------


async def test_admin_with_post_rights_is_allowed(tmp_path: Path) -> None:
    """管理员且有 post_messages -> 允许，即使群默认禁言。"""
    client = PermClient(
        entity=Entity(default_banned_rights=all_banned_rights()),
        permissions=Permissions(
            participant=Participant(admin_rights=AdminRights(post_messages=True)),
            is_admin=True,
        ),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, _ = await sender.postability(Target(id=-2001))
        assert allowed is True
    finally:
        store.close()


async def test_admin_without_post_rights_is_denied(tmp_path: Path) -> None:
    """管理员但权限位里没有发送消息 -> 不能发。"""
    client = PermClient(
        entity=Entity(default_banned_rights=all_banned_rights()),
        permissions=Permissions(
            participant=Participant(admin_rights=AdminRights(post_messages=False, send_messages=False)),
            is_admin=True,
        ),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, reason = await sender.postability(Target(id=-2001))
        assert allowed is False
        assert "权限位" in reason
    finally:
        store.close()


async def test_creator_counts_as_admin(tmp_path: Path) -> None:
    client = PermClient(
        entity=Entity(default_banned_rights=all_banned_rights()),
        permissions=Permissions(
            participant=Participant(admin_rights=AdminRights(send_messages=True)),
            is_creator=True,
        ),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, _ = await sender.postability(Target(id=-2001))
        assert allowed is True
    finally:
        store.close()


# --------------------------------------------------------------------------
# 异常状态
# --------------------------------------------------------------------------


async def test_banned_is_denied(tmp_path: Path) -> None:
    client = PermClient(
        entity=Entity(default_banned_rights=BannedRights()),
        permissions=Permissions(participant=Participant(), is_banned=True),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, reason = await sender.postability(Target(id=-2001))
        assert allowed is False
        assert "封禁" in reason
    finally:
        store.close()


async def test_left_group_is_denied(tmp_path: Path) -> None:
    client = PermClient(
        entity=Entity(default_banned_rights=BannedRights()),
        permissions=Permissions(participant=Participant(), has_left=True),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, reason = await sender.postability(Target(id=-2001))
        assert allowed is False
        assert "退出" in reason
    finally:
        store.close()


async def test_permission_read_failure_falls_back_to_default_rights(tmp_path: Path) -> None:
    """读不到权限对象时，用 default_banned_rights 兜底，而不是盲目放行。"""
    client = PermClient(
        entity=Entity(default_banned_rights=all_banned_rights()),
        permissions=RuntimeError("读取失败"),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        allowed, _ = await sender.postability(Target(id=-2001))
        assert allowed is False
    finally:
        store.close()


async def test_dry_run_skips_permission_checks(tmp_path: Path) -> None:
    client = PermClient(
        entity=Entity(default_banned_rights=all_banned_rights()),
        permissions=Permissions(participant=Participant()),
    )
    sender, store = make_sender(client, tmp_path, dry_run=True)
    try:
        allowed, _ = await sender.postability(Target(id=-2001))
        assert allowed is True
    finally:
        store.close()


# --------------------------------------------------------------------------
# 慢速模式
# --------------------------------------------------------------------------


async def test_probe_slow_mode_reads_setting(tmp_path: Path) -> None:
    client = PermClient(
        entity=Entity(default_banned_rights=BannedRights(), slowmode_seconds=30),
        permissions=Permissions(participant=Participant()),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        assert await sender.probe_slow_mode(Target(id=-2001)) == 30
        assert sender.slow.seconds_for(-2001) == 30  # 顺手记下窗口用于预判
    finally:
        store.close()


async def test_probe_slow_mode_returns_none_when_absent(tmp_path: Path) -> None:
    client = PermClient(
        entity=Entity(default_banned_rights=BannedRights()),
        permissions=Permissions(participant=Participant()),
    )
    sender, store = make_sender(client, tmp_path)
    try:
        assert await sender.probe_slow_mode(Target(id=-2001)) is None
    finally:
        store.close()
