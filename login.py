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

"""登录脚本：非交互式读取手机号 / 验证码 / 两步密码。

为什么不用 `python -m tgrelay --login`：那个走 input()，没法在后面挂一个进程给它送输入。
这个脚本从 stdin 逐行读，可以这样用：

    "`+8613800138000`n12345`npwd" | python login.py

或者每行一个参数（手机号可省略，会话文件存在时会自动跳过）：
    echo "12345" | python login.py

登录成功后会打印账号信息和 StringSession 提示，并把会话写进 config 指定的路径。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telethon import TelegramClient  # noqa: E402
from telethon.errors import SessionPasswordNeededError  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from tgrelay.config import ConfigError, apply_env, load_config, load_dotenv  # noqa: E402
from tgrelay.listener import build_client  # noqa: E402


def read_line(prompt: str) -> str:
    """从 stdin 读一行；没有输入就返回空串（不阻塞、不抛异常）。"""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    return line.strip()


async def main() -> int:
    try:
        config = apply_env(load_config("config.yaml"), load_dotenv(".env"))
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    if config.credentials is None:
        print("[配置错误] 缺少 TG_API_ID / TG_API_HASH")
        return 2
    if not config.credentials.session_string:
        session_path = Path(config.credentials.session_path)
        session_path.parent.mkdir(parents=True, exist_ok=True)

    client = build_client(config.credentials, config.proxy)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"[已登录] {_name(me)} (id={getattr(me, 'id', '?')}){_premium(me)}")
            _print_session(client)
            return 0

        phone = read_line("手机号(含国家码): ")
        if not phone:
            phone = (_prompt_phone_from_env() or "")
        if not phone:
            print("[失败] 没有拿到手机号")
            return 2

        print(f"[1/2] 正在向 {phone} 发送验证码…")
        sent = await client.send_code_request(phone)
        print(f"[1/2] 已发送（code_type={type(sent.type).__name__}），请查看 Telegram / 短信")
        print("PHONE_CODE_REQUIRED", flush=True)

        code = read_line("验证码: ")
        if not code:
            print("[失败] 没有拿到验证码")
            return 2
        code = code.replace(" ", "").replace("-", "")

        print(f"[2/2] 提交验证码（长度 {len(code)}）…")
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            print("TWOFA_REQUIRED", flush=True)
            password = read_line("两步验证密码: ")
            if not password:
                print("[失败] 该账号启用了两步验证，必须提供密码")
                return 2
            await client.sign_in(password=password)

        me = await client.get_me()
        print(f"[登录成功] {_name(me)} (id={getattr(me, 'id', '?')}){_premium(me)}")
        if not config.credentials.session_string:
            print(f"[会话] 已写入 {config.credentials.session_path}（等价于账号密码，切勿外传）")
        _print_session(client)
        return 0
    except Exception as exc:
        print(f"[失败] {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _prompt_phone_from_env() -> str:
    import os

    return (os.environ.get("TG_PHONE") or "").strip()


def _name(entity: object) -> str:
    parts = [
        getattr(entity, "first_name", None) or "",
        getattr(entity, "last_name", None) or "",
    ]
    joined = " ".join(part for part in parts if part).strip()
    return joined or getattr(entity, "username", None) or "?"


def _premium(entity: object) -> str:
    return "  [Premium]" if getattr(entity, "premium", False) else ""


def _print_session(client: TelegramClient) -> None:
    session = client.session
    if isinstance(session, StringSession):
        print("[StringSession] " + session.save())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
