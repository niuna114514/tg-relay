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

"""安全写入 .env：校验格式、默认不覆盖、覆盖前自动备份。

用法（PowerShell）：
    python setup_env.py --api-id <你的ID> --api-hash <你的HASH> --session data/relay.session
    python setup_env.py --api-id <你的ID> --api-hash <你的HASH> --premium true --force

api_id / api_hash 从 https://my.telegram.org/apps 申请。
也可以直接手写 .env，格式见 .env.example。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
ALLOWED_KEYS = ("TG_API_ID", "TG_API_HASH", "TG_SESSION", "TG_SESSION_STRING", "TG_PREMIUM")


def validate(api_id: str, api_hash: str) -> list[str]:
    problems: list[str] = []
    if not api_id.isdigit():
        problems.append(f"--api-id 必须是纯数字，实际 {api_id!r}")
    else:
        # Telegram 的 api_id 都是 6~8 位；位数不对通常是抄漏了
        if not (5 <= len(api_id) <= 9):
            problems.append(f"--api-id 位数可疑（{len(api_id)} 位），请核对 my.telegram.org")
    if not API_HASH_RE.match(api_hash):
        problems.append(
            f"--api-hash 应为 32 位十六进制字符，实际 {len(api_hash)} 位：{api_hash[:8]}…"
        )
    return problems


def render(api_id: str, api_hash: str, session: str, session_string: str, premium: bool) -> str:
    lines = [
        "# 由 setup_env.py 生成 —— 本文件等价于账号凭据，不要提交、不要外传",
        f"TG_API_ID={api_id}",
        f"TG_API_HASH={api_hash}",
        f"TG_SESSION={session}",
        "",
        "# 可选：已有 StringSession 时取消注释（填了就忽略上面的 TG_SESSION 文件）",
        f"# TG_SESSION_STRING={session_string}" if session_string else "# TG_SESSION_STRING=",
        "",
        "# 转发用账号是否已开通 Premium（true 时限速自动放宽 4 倍，并能转发禁转频道）",
        f"TG_PREMIUM={'true' if premium else 'false'}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全写入 tg-relay 的 .env")
    parser.add_argument("--api-id", required=True)
    parser.add_argument("--api-hash", required=True)
    parser.add_argument("--session", default="data/relay.session")
    parser.add_argument("--session-string", default="")
    parser.add_argument("--premium", choices=["true", "false"], default="false")
    parser.add_argument("--path", default=".env")
    parser.add_argument("--force", action="store_true", help="已存在时覆盖（会先备份成 .env.bak）")
    parser.add_argument("--show", action="store_true", help="写入后打印内容（哈希会被打码）")
    args = parser.parse_args(argv)

    problems = validate(args.api_id.strip(), args.api_hash.strip())
    if problems:
        for item in problems:
            print(f"[错误] {item}", file=sys.stderr)
        return 2

    target = Path(args.path)
    if target.exists() and not args.force:
        print(
            f"[中止] {target} 已存在。确认要覆盖请加 --force（会先备份为 {target}.bak）",
            file=sys.stderr,
        )
        return 2

    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, backup)
        print(f"[提示] 已备份原文件到 {backup}")

    content = render(
        args.api_id.strip(),
        args.api_hash.strip(),
        args.session,
        args.session_string,
        args.premium == "true",
    )
    target.write_text(content, encoding="utf-8")

    # 尽量收紧权限（Windows 上 chmod 语义有限，但 POSIX 下有效）
    try:
        target.chmod(0o600)
    except OSError:
        pass

    print(f"[完成] 已写入 {target.resolve()}")
    if args.show:
        masked = "\n".join(
            line if line.startswith("#") or "=" not in line else _mask(line) for line in content.splitlines()
        )
        print("\n----- 内容预览（哈希已打码）-----")
        print(masked)

    print("\n下一步：python -m tgrelay --login")
    return 0


def _mask(line: str) -> str:
    key, _, value = line.partition("=")
    if key.strip() in ("TG_API_HASH", "TG_SESSION_STRING") and len(value) > 8:
        return f"{key}={value[:4]}…{value[-4:]}（已打码）"
    return line


if __name__ == "__main__":
    raise SystemExit(main())
