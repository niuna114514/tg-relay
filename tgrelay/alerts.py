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

"""告警分发。

为什么要有这一层：
    发送层（`tgrelay.sender`）需要在"账号可能被限制了"这种关键时刻
    **主动叫人**，但它不该知道操控 Bot、面板这些东西的存在，
    更不该反向依赖它们（会绕成一团循环导入）。

    所以发送层只往 `AlertHub` 里喊一声，由 `__main__` 决定谁能收到：
    现在接的是日志和操控 Bot，以后要加 webhook / 邮件也只在这里加一个 sink。

用 `__call__` 而不是 `send()`：这样它可以直接当 `alert=` 回调传下去，
调用点写起来就是一句 `await self.alerts("critical", "...")`。
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

log = logging.getLogger("tgrelay.alerts")

# 级别只用两个：warning = 需要你知道；critical = 已经停手了，必须处理
AlertSink = Callable[[str, str], Awaitable[None]]

MAX_HISTORY = 50


class AlertHub:
    """进程内的小广播器，同时留一份历史给面板和测试看。"""

    def __init__(self) -> None:
        self._sinks: list[AlertSink] = []
        self.history: list[tuple[str, str]] = []

    def add(self, sink: AlertSink) -> None:
        self._sinks.append(sink)

    def recent(self, limit: int = 10) -> list[tuple[str, str]]:
        return self.history[-limit:]

    async def __call__(self, level: str, text: str) -> None:
        level = level if level in ("warning", "critical") else "warning"
        self.history.append((level, text))
        if len(self.history) > MAX_HISTORY:
            del self.history[:-MAX_HISTORY]
        if level == "critical":
            log.error("%s", text)
        else:
            log.warning("%s", text)
        for sink in list(self._sinks):
            try:
                await sink(level, text)
            except Exception:  # 告警投递失败绝不能影响转发
                log.exception("告警投递失败（%s）", level)
