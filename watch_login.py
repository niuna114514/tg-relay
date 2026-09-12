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

"""带看门狗的登录：在后台运行，把每一步需要的东西从文件里读。

背景：后台进程拿不到交互式输入，所以用"轮询文件"代替 input()。

需要时脚本会打印标记并开始等文件：
    PHONE_REQUIRED        -> 等 _login/phone.txt
    PHONE_CODE_REQUIRED   -> 等 _login/code.txt
    TWOFA_REQUIRED        -> 等 _login/password.txt

用法：
    python watch_login.py            # 前台
    # 或作为后台任务
拿到的值会立刻删除文件，避免下次误用同一个验证码。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from telethon.errors import SessionPasswordNeededError  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from tgrelay.config import ConfigError, apply_env, load_config, load_dotenv  # noqa: E402
from tgrelay.listener import build_client  # noqa: E402

BOX = BASE / "_login"
PHONE_FILE = BOX / "phone.txt"
CODE_FILE = BOX / "code.txt"
PASSWORD_FILE = BOX / "password.txt"
LINK_FILE = BOX / "link.txt"

PHONE_WAIT = 900.0
CODE_WAIT = 300.0
PASSWORD_WAIT = 300.0


async def wait_for_file(path: Path, timeout: float, label: str) -> str:
    """等一个文件出现并取出内容（取到后删除）。超时返回空串。"""
    BOX.mkdir(exist_ok=True)
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        if path.exists():
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            try:
                path.unlink()
            except OSError:
                pass
            if value:
                print(f"[拿到] {label}（长度 {len(value)}）", flush=True)
                return value
        if not announced:
            print(f"WAITING_FOR_{(label or 'input').upper()}", flush=True)
            announced = True
        await asyncio.sleep(0.5)
    print(f"[超时] 等 {label} 超过 {int(timeout)}s", flush=True)
    return ""


def describe(entity: object) -> str:
    parts = [
        getattr(entity, "first_name", None) or "",
        getattr(entity, "last_name", None) or "",
    ]
    name = " ".join(part for part in parts if part).strip() or "?"
    tag = f"@{entity.username}" if getattr(entity, "username", None) else ""
    premium = "  [Premium]" if getattr(entity, "premium", False) else ""
    return f"{name} {tag} (id={getattr(entity, 'id', '?')}){premium}"


async def main() -> int:
    BOX.mkdir(exist_ok=True)
    # 清掉上次残留的输入文件，避免误用过期验证码
    for stale in (CODE_FILE, PASSWORD_FILE, LINK_FILE):
        if stale.exists():
            stale.unlink()

    try:
        config = apply_env(load_config("config.yaml"), load_dotenv(".env"))
    except ConfigError as exc:
        print(f"[配置错误] {exc}", flush=True)
        return 2
    if config.credentials is None:
        print("[配置错误] 缺少 TG_API_ID / TG_API_HASH", flush=True)
        return 2

    client = build_client(config.credentials, config.proxy)
    try:
        print("[连接] 正在通过代理连接 Telegram…", flush=True)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"[已登录] {describe(me)}", flush=True)
            return 0

        phone = await wait_for_file(PHONE_FILE, PHONE_WAIT, "phone")
        if not phone:
            print("[失败] 没有拿到手机号", flush=True)
            return 2

        print(f"[1/2] 正在向 {phone} 发送验证码…", flush=True)
        sent = await client.send_code_request(phone)
        print(f"[1/2] 已发送 (code_type={type(sent.type).__name__})", flush=True)

        code = await wait_for_file(CODE_FILE, CODE_WAIT, "code")
        if not code:
            print("[失败] 没有拿到验证码", flush=True)
            return 2
        code = code.replace(" ", "").replace("-", "")
        if ":" in code:
            # 也支持 "12345:hash" 这种带 hash 的写法
            code, _, hash_override = code.partition(":")
            phone_code_hash = hash_override.strip() or sent.phone_code_hash
        else:
            phone_code_hash = sent.phone_code_hash

        print(f"[2/2] 提交验证码（长度 {len(code)}）…", flush=True)
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            print("TWOFA_REQUIRED", flush=True)
            password = await wait_for_file(PASSWORD_FILE, PASSWORD_WAIT, "password")
            if not password:
                print("[失败] 该账号启用了两步验证，必须提供密码", flush=True)
                return 2
            await client.sign_in(password=password)

        me = await client.get_me()
        session_file = Path(config.credentials.session_path)
        print(f"[登录成功] {describe(me)}", flush=True)
        print(f"[会话] {session_file.resolve()}（等价于账号密码，切勿外传）", flush=True)
        if isinstance(client.session, StringSession):
            print("[StringSession] " + client.session.save(), flush=True)
        return 0
    except Exception as exc:
        print(f"[失败] {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
