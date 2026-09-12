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

"""客户端构建、事件注册、相册聚合、离线补漏。

相册很关键：频道发 5 张图会来 5 个 NewMessage 更新，
逐条转发会把图集拆开、并触发 5 倍风控。这里用 grouped_id 聚合后再整组转发。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from .config import AppConfig, Credentials
from .engine import RelayEngine
from .sender import ConfigProblem
from .db import Store

log = logging.getLogger("tgrelay.listener")


def build_client(credentials: Credentials, proxy: Any = None) -> TelegramClient:
    """用 StringSession（环境变量）或会话文件创建客户端。

    注意两点：
      * TelegramClient 必须在事件循环内创建，否则 Telethon 1.x 会报
        "no running event loop"；
      * Telethon **不读系统代理**，要走代理必须显式传 proxy（见 proxy.py）。
    """
    if credentials.session_string:
        session: Any = StringSession(credentials.session_string)
    else:
        session_path = Path(credentials.session_path)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session = str(session_path)
    kwargs: dict[str, Any] = {}
    if proxy is not None:
        kwargs.update(proxy.as_telethon_kwargs())
        log.info("使用代理：%s", proxy.safe_url)
    return TelegramClient(session, credentials.api_id, credentials.api_hash, **kwargs)


class AlbumCollector:
    """按 grouped_id 缓冲消息，等窗口结束再整组处理。

    同时兜住两种情况：
      * `events.Album` 正常触发
      * 补漏/重放时只有零散的 `NewMessage`（也带 grouped_id）
    """

    def __init__(
        self,
        callback: Callable[[list[Any], Any], Awaitable[Any]],
        *,
        window: float = 0.6,
    ) -> None:
        self.callback = callback
        self.window = window
        self._groups: dict[tuple[int, int], list[Any]] = {}
        self._timers: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._source_peers: dict[tuple[int, int], Any] = {}

    def add(self, message: Any, source_peer: Any = None) -> None:
        grouped_id = getattr(message, "grouped_id", None)
        chat_id = getattr(message, "chat_id", None)
        if grouped_id is None or chat_id is None:
            return
        key = (chat_id, grouped_id)
        self._groups.setdefault(key, []).append(message)
        if source_peer is not None:
            self._source_peers[key] = source_peer

        timer = self._timers.get(key)
        if timer is not None and not timer.done():
            timer.cancel()
        self._timers[key] = asyncio.create_task(self._flush_later(key))

    async def _flush_later(self, key: tuple[int, int]) -> None:
        try:
            await asyncio.sleep(self.window)
        except asyncio.CancelledError:
            return
        await self.flush_key(key)

    async def flush_key(self, key: tuple[int, int]) -> None:
        messages = self._groups.pop(key, [])
        self._timers.pop(key, None)
        peer = self._source_peers.pop(key, None)
        if not messages:
            return
        messages.sort(key=lambda m: getattr(m, "id", 0))
        log.debug("相册聚合完成：chat=%s grouped=%s 共 %s 条", key[0], key[1], len(messages))
        await self.callback(messages, peer)

    async def flush_all(self) -> None:
        """退出前把缓冲区里的相册补发出去，避免丢图集尾巴。"""
        for key in list(self._groups.keys()):
            timer = self._timers.pop(key, None)
            if timer is not None and not timer.done():
                timer.cancel()
            await self.flush_key(key)


class ManagedListener:
    """监听器的封装，支持运行期换源。

    为什么需要它：`client.on(events.NewMessage(chats=[...]))` 的 chats 是
    **注册时**就固定下来的，之后改 config.sources 不会影响已注册的处理器。
    所以换源时必须把旧处理器摘掉、用新列表重新注册。

    本类做的就是这件事：记录所有被注册的 handler，换源时全部移除再重装。
    """

    def __init__(
        self,
        client: TelegramClient,
        engine: RelayEngine,
        config: AppConfig,
        *,
        on_edit: Callable[[Any], Awaitable[None]] | None = None,
        on_delete: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.engine = engine
        self.config = config
        self.on_edit = on_edit
        self.on_delete = on_delete
        self.collector: AlbumCollector | None = None
        self.sources: list[Any] = list(config.sources)
        self._installed: list[Any] = []

    def install(self) -> AlbumCollector:
        """按当前 sources 注册事件处理器。"""
        self.uninstall()
        self.collector = install_handlers(
            self.client,
            self.engine,
            self.config,
            sources=self.sources,
            on_edit=self.on_edit,
            on_delete=self.on_delete,
            _record=self._installed,
        )
        log.info("已监听 %s 个源：%s", len(self.sources), self.sources)
        return self.collector

    def uninstall(self) -> None:
        for handler in self._installed:
            try:
                self.client.remove_event_handler(handler)
            except Exception as exc:  # pragma: no cover - 摘不掉也不该阻塞换源
                log.warning("移除事件处理器失败：%s", exc)
        self._installed.clear()

    async def reload_sources(self, sources: list[Any]) -> None:
        """运行期换源：重新注册事件处理器。"""
        if [str(item) for item in sources] == [str(item) for item in self.sources]:
            log.debug("源列表没变化，跳过重载")
            return
        self.sources = list(sources)
        self.install()
        log.info("源列表已热更新为 %s", self.sources)

    async def flush(self) -> None:
        """退出前把相册缓冲冲刷掉。"""
        if self.collector is not None:
            await self.collector.flush_all()


def install_handlers(
    client: TelegramClient,
    engine: RelayEngine,
    config: AppConfig,
    *,
    sources: Sequence[Any] | None = None,
    on_edit: Callable[[Any], Awaitable[None]] | None = None,
    on_delete: Callable[[Any], Awaitable[None]] | None = None,
    _record: list[Any] | None = None,
) -> AlbumCollector:
    """注册事件处理器。

    sources 可以显式传入（换源时用），默认取 config.sources。
    _record 传一个列表进来时，会把注册的 handler 记进去，便于之后移除。
    """
    watch = list(sources) if sources is not None else list(config.sources)
    collector = AlbumCollector(engine.handle_album, window=config.behavior.album_window)

    def own(handler: Any) -> Any:
        if _record is not None:
            _record.append(handler)
        return handler

    @own
    @client.on(events.Album(chats=watch))
    async def _album(event: Any) -> None:
        try:
            peer = await event.get_input_chat()
        except Exception:
            peer = None
        await engine.handle_album(list(event.messages), peer)

    @own
    @client.on(events.NewMessage(chats=watch))
    async def _message(event: Any) -> None:
        message = event.message
        if getattr(message, "grouped_id", None):
            collector.add(message, getattr(event, "input_chat", None))
            return
        try:
            peer = await event.get_input_chat()
        except Exception:
            peer = None
        await engine.handle_message(message, source_peer=peer)

    if config.behavior.sync_edits:

        @own
        @client.on(events.MessageEdited(chats=watch))
        async def _edited(event: Any) -> None:
            handler = on_edit or engine.handle_edit
            try:
                await handler(event.message)
            except Exception:
                log.exception("同步编辑失败")

    if config.behavior.sync_deletes:

        @own
        @client.on(events.MessageDeleted(chats=watch))
        async def _deleted(event: Any) -> None:
            handler = on_delete or engine.handle_delete
            try:
                await handler(event)
            except Exception:
                log.exception("同步删除失败")

    return collector


async def catch_up(
    client: TelegramClient,
    engine: RelayEngine,
    store: Store,
    config: AppConfig,
) -> int:
    """启动时补齐离线期间漏掉的消息。

    比单纯依赖 Telethon 的 catch_up 更可靠：直接按 msg_id 增量拉取，
    并且覆盖到"程序没运行"的那段时间。
    """
    if not config.behavior.catch_up_on_start or config.behavior.catch_up_limit <= 0:
        return 0

    total = 0
    for source in config.sources:
        try:
            entity = await client.get_entity(source)
            input_peer = await client.get_input_entity(entity)
        except Exception as exc:
            raise ConfigProblem(
                f"源 {source!r} 无法解析：{exc}。"
                "请确认该协议号已经加入这个频道，并且先用它打开过一次（暖场）。"
            ) from exc

        chat_id = getattr(entity, "id", None)
        if chat_id is None:
            continue
        last_id = store.get_cursor(chat_id)
        if last_id == 0:
            log.info("源 %s 首次运行，不做补漏（从下次收到的新消息开始）", source)
            continue

        try:
            messages = await client.get_messages(
                input_peer,
                min_id=last_id,
                limit=config.behavior.catch_up_limit,
            )
        except Exception as exc:
            log.warning("补漏读取 %s 失败：%s", source, exc)
            continue

        if not messages:
            log.info("源 %s 无遗漏消息", source)
            continue

        messages = list(reversed(messages))  # 按时间正序处理
        log.info("源 %s 补漏 %s 条（min_id=%s）", source, len(messages), last_id)
        for item in _group_albums(messages):
            if isinstance(item, list):
                await engine.handle_album(item, input_peer)
            else:
                await engine.handle_message(item, source_peer=input_peer)
            total += 1
        store.prune_seen()
    return total


def _group_albums(messages: Iterable[Any]) -> list[Any]:
    """把连续且 grouped_id 相同的消息合成一组，其余保持单条。"""
    grouped: list[Any] = []
    buffer: list[Any] = []
    current: int | None = None
    for message in messages:
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is not None and grouped_id == current and buffer:
            buffer.append(message)
            continue
        if buffer:
            grouped.append(buffer if len(buffer) > 1 else buffer[0])
        buffer = [message]
        current = grouped_id
    if buffer:
        grouped.append(buffer if len(buffer) > 1 else buffer[0])
    return grouped
