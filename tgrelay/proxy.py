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

"""代理配置：Telethon 不读系统代理，必须显式传 proxy。

支持 socks5 / socks4 / http（http 需要 python-socks，socks 需要 PySocks）。
从环境变量读取，写在 .env 里即可：

    TG_PROXY_TYPE=socks5
    TG_PROXY_HOST=127.0.0.1
    TG_PROXY_PORT=7897
    # TG_PROXY_USER=
    # TG_PROXY_PASS=

也可以直接写一行 url：TG_PROXY=socks5://127.0.0.1:7897
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

VALID_TYPES = ("socks5", "socks4", "http")


@dataclass(frozen=True)
class ProxyConfig:
    type: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    rdns: bool = True

    @property
    def url(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.type}://{auth}{self.host}:{self.port}"

    @property
    def safe_url(self) -> str:
        """日志里用，带账号时打码。"""
        auth = f"{self.username}:***@" if self.username else ""
        return f"{self.type}://{auth}{self.host}:{self.port}"

    def as_telethon_kwargs(self) -> dict[str, Any]:
        """翻成 Telethon 的 proxy 参数。"""
        try:
            if self.type == "http":
                import python_socks  # noqa: F401

                return {
                    "proxy": (
                        python_socks.ProxyType.HTTP,
                        self.host,
                        self.port,
                        self.rdns,
                        self.username or None,
                        self.password or None,
                    )
                }
            import socks  # PySocks

            kind = socks.SOCKS5 if self.type == "socks5" else socks.SOCKS4
            return {
                "proxy": (
                    kind,
                    self.host,
                    self.port,
                    self.rdns,
                    self.username or None,
                    self.password or None,
                )
            }
        except ImportError as exc:
            need = "python-socks" if self.type == "http" else "PySocks"
            raise RuntimeError(
                f"要用 {self.type} 代理需要先安装 {need}：pip install {need}"
                + ("[asyncio]" if self.type == "http" else "")
            ) from exc


def parse_proxy(env: dict[str, str]) -> ProxyConfig | None:
    """从环境变量解析代理；没配返回 None。"""
    url = (env.get("TG_PROXY") or "").strip()
    if url:
        parsed = urlparse(url if "://" in url else f"socks5://{url}")
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"TG_PROXY 解析失败（需要 host:port）: {url!r}")
        kind = (parsed.scheme or "socks5").lower()
        if kind not in VALID_TYPES:
            raise ValueError(f"TG_PROXY 协议不支持 {kind!r}，可选 {VALID_TYPES}")
        return ProxyConfig(
            type=kind,
            host=parsed.hostname,
            port=int(parsed.port),
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
        )

    host = (env.get("TG_PROXY_HOST") or "").strip()
    port_raw = (env.get("TG_PROXY_PORT") or "").strip()
    if not host and not port_raw:
        return None
    if not host or not port_raw:
        raise ValueError("TG_PROXY_HOST 与 TG_PROXY_PORT 必须同时提供")
    kind = (env.get("TG_PROXY_TYPE") or "socks5").strip().lower()
    if kind not in VALID_TYPES:
        raise ValueError(f"TG_PROXY_TYPE 不支持 {kind!r}，可选 {VALID_TYPES}")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"TG_PROXY_PORT 必须是数字，实际 {port_raw!r}") from exc
    return ProxyConfig(
        type=kind,
        host=host,
        port=port,
        username=(env.get("TG_PROXY_USER") or "").strip(),
        password=(env.get("TG_PROXY_PASS") or "").strip(),
        rdns=(env.get("TG_PROXY_RDNS") or "true").strip().lower() not in ("false", "0", "no"),
    )
