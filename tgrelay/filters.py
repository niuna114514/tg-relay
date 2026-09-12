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

"""过滤规则：判断一条源消息该不该转发。

规则是"与"关系：所有非空的规则组都必须通过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import Filters

# media 配置值 -> 判定用的归一化类型
_CANONICAL = {
    "photo": "photo",
    "video": "video",
    "document": "document",
    "audio": "audio",
    "voice": "voice",
    "gif": "gif",
    "sticker": "sticker",
    "poll": "poll",
}


def classify(message: Any) -> str:
    """把 Telethon Message 归一化成媒体类型字符串；纯文本返回 'text'。"""
    if getattr(message, "poll", None) is not None:
        return "poll"
    if getattr(message, "photo", None) is not None:
        return "photo"
    document = getattr(message, "document", None)
    if document is not None:
        for attribute in getattr(document, "attributes", ()) or ():
            name = type(attribute).__name__
            if name == "DocumentAttributeSticker":
                return "sticker"
            if name == "DocumentAttributeAnimated":
                return "gif"
            if name == "DocumentAttributeAudio":
                return "voice" if getattr(attribute, "voice", False) else "audio"
            if name == "DocumentAttributeVideo":
                # 圆形视频也算 video
                return "video"
        return "document"
    if getattr(message, "video", None) is not None:
        return "video"
    if getattr(message, "audio", None) is not None:
        return "audio"
    if getattr(message, "voice", None) is not None:
        return "voice"
    if getattr(message, "gif", None) is not None:
        return "gif"
    if getattr(message, "sticker", None) is not None:
        return "sticker"
    if getattr(message, "media", None) is not None:
        return "document"
    return "text"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    kind: str = "text"

    def __bool__(self) -> bool:
        return self.allowed


class MessageFilter:
    def __init__(self, config: Filters) -> None:
        self.config = config
        self._patterns = tuple(self._compile(pattern) for pattern in config.regex)
        self._keywords = tuple(keyword.casefold() for keyword in config.keywords)
        self._exclude = tuple(keyword.casefold() for keyword in config.exclude)
        self._media = frozenset(config.media)

    @staticmethod
    def _compile(pattern: str) -> re.Pattern[str]:
        try:
            return re.compile(pattern, re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            raise ValueError(f"filters.regex 里的正则无效：{pattern!r} -> {exc}") from exc

    @property
    def active(self) -> bool:
        cfg = self.config
        return bool(
            cfg.keywords or cfg.exclude or cfg.regex or cfg.media or cfg.min_length > 0
        )

    def check(self, message: Any) -> Decision:
        text = (getattr(message, "message", None) or "").strip()
        kind = classify(message)
        folded = text.casefold()

        if self._media and kind not in self._media:
            return Decision(False, f"媒体类型 {kind} 不在白名单", kind)

        if self._exclude:
            for keyword in self._exclude:
                if keyword in folded:
                    return Decision(False, f"命中排除词 {keyword!r}", kind)

        if self._keywords:
            hits = [keyword for keyword in self._keywords if keyword in folded]
            if self.config.keyword_mode == "all":
                if len(hits) != len(self._keywords):
                    missing = [k for k in self._keywords if k not in hits]
                    return Decision(False, f"缺少关键词 {missing}", kind)
            elif not hits:
                return Decision(False, "未命中任何关键词", kind)

        if self._patterns:
            if not any(pattern.search(text) for pattern in self._patterns):
                return Decision(False, "未命中任何正则", kind)

        if self.config.min_length > 0 and kind == "text" and len(text) < self.config.min_length:
            return Decision(False, f"文本过短（{len(text)} < {self.config.min_length}）", kind)

        return Decision(True, "", kind)
