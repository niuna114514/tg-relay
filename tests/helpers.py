"""测试用假对象：不连 Telegram 也能跑通整条转发链路。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from telethon.tl import types as tl


def _document(*attributes: Any) -> SimpleNamespace:
    return SimpleNamespace(attributes=list(attributes))


def fake_message(
    *,
    msg_id: int = 1,
    chat_id: int = -1001,
    text: str = "",
    kind: str = "text",
    grouped_id: int | None = None,
) -> SimpleNamespace:
    """构造一条假的源消息，kind 取值见 filters._CANONICAL。"""
    message = SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        message=text,
        grouped_id=grouped_id,
        media=None,
        photo=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
        gif=None,
        sticker=None,
        poll=None,
    )
    if kind == "text":
        return message
    if kind == "poll":
        message.poll = SimpleNamespace(question="q")
        return message
    if kind == "photo":
        message.media = object()
        message.photo = object()
        return message
    if kind == "video":
        message.media = object()
        message.document = _document(
            tl.DocumentAttributeVideo(duration=10, w=1920, h=1080)
        )
        return message
    if kind == "gif":
        message.media = object()
        message.document = _document(tl.DocumentAttributeAnimated())
        return message
    if kind == "sticker":
        message.media = object()
        message.document = _document(
            tl.DocumentAttributeSticker(alt="🙂", stickerset=tl.InputStickerSetEmpty())
        )
        return message
    if kind == "voice":
        message.media = object()
        message.document = _document(
            tl.DocumentAttributeAudio(duration=3, voice=True)
        )
        return message
    if kind == "audio":
        message.media = object()
        message.document = _document(
            tl.DocumentAttributeAudio(duration=180, title="t", performer="p")
        )
        return message
    message.media = object()
    message.document = _document(tl.DocumentAttributeFilename(file_name="x.pdf"))
    return message


@dataclass
class FakeSentMessage:
    id: int


@dataclass
class FakeClient:
    """记录 forward_messages 调用，并可按需抛异常。"""

    sent: list[tuple[Any, tuple[int, ...], Any]] = field(default_factory=list)
    fail_with: list[Exception] = field(default_factory=list)
    next_id: int = 5000
    as_album_flags: list[bool] = field(default_factory=list)
    connected: bool = True

    async def get_input_entity(self, peer: Any) -> Any:
        return peer

    async def get_entity(self, peer: Any) -> Any:
        return SimpleNamespace(id=peer if isinstance(peer, int) else 999, first_name="Fake")

    async def get_permissions(self, entity: Any, user: Any = None) -> Any:
        return SimpleNamespace(is_banned=False, send_messages=True)

    async def forward_messages(
        self,
        entity: Any,
        messages: Any,
        from_peer: Any = None,
        **kwargs: Any,
    ) -> Any:
        if self.fail_with:
            raise self.fail_with.pop(0)
        ids = messages if isinstance(messages, (list, tuple)) else [messages]
        self.as_album_flags.append(bool(kwargs.get("as_album")))
        self.sent.append((entity, tuple(int(i) for i in ids), from_peer))
        out = []
        for _ in ids:
            self.next_id += 1
            out.append(FakeSentMessage(self.next_id))
        return out if isinstance(messages, (list, tuple)) else out[0]

    async def edit_message(self, entity: Any, message: Any, text: str) -> Any:  # pragma: no cover
        self.sent.append((entity, (int(message),), "edit"))
        return FakeSentMessage(int(message))

    async def delete_messages(self, entity: Any, ids: Any) -> None:  # pragma: no cover
        self.sent.append((entity, tuple(int(i) for i in ids), "delete"))

    async def connect(self) -> None:
        self.connected = True


async def drain(queue: asyncio.Queue[Any]) -> None:
    """等队列里的任务全部处理完。"""
    await queue.join()
