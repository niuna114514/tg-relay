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

"""设置 Bot 的 "/" 命令菜单（BotFather 的 setMyCommands）。

Telegram 客户端的 "/" 菜单就是这份列表 —— 设置之后，
输入 "/" 会弹出所有命令，不用记。这不是"缓存"，是服务端配置。

用法（在服务器上，需要 TG_BOT_TOKEN）：
    python setup_commands.py              # 设置
    python setup_commands.py --clear      # 清空
    python setup_commands.py --show       # 查看当前设置
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 命令菜单：只放最常用的，太多会显得杂乱。
# Telegram 限制每条描述 256 字符、最多 100 条命令。
COMMANDS: list[tuple[str, str]] = [
    # 状态
    ("status", "总览：额度、各群积压、慢速、熔断状态"),
    ("targets", "目标群列表（含今日用量）"),
    ("where", "当前生效的关键参数"),
    ("logs", "最近日志（可加条数，如 /logs 30）"),
    # 源频道
    ("sources", "列出所有源频道（标出主源）"),
    ("source", "换主源：/source @新频道"),
    ("addsource", "追加一个源：/addsource @频道"),
    ("delsource", "移除一个源：/delsource @频道"),
    # 目标群
    ("add", "加入目标群：/add @群名 [备注]"),
    ("del", "移除目标群：/del 序号"),
    ("on", "启用某个群：/on 序号"),
    ("off", "暂停某个群：/off 序号"),
    ("check", "自检某个群（能否发言 + 慢速）"),
    ("probe", "真发一条测试（会往群里发东西）"),
    ("limit", "改某群发送间隔：/limit 序号 30 35"),
    # 批量
    ("onall", "启用全部群"),
    ("offall", "暂停全部群"),
    ("quotas", "给所有群设每日额度：/quotas 50"),
    ("share", "把总额度平均分：/share 100"),
    ("everyone", "统一所有群间隔：/everyone 30 35"),
    # 定时重发
    ("materials", "查看重发素材列表"),
    ("addmat", "设置重发素材：/addmat 6 7 8"),
    ("interval", "改重发轮次间隔（秒）"),
    ("dailylimit", "改重发每日额度"),
    ("run", "立刻跑一轮重发"),
    ("stop", "停止重发循环（并写盘，重启不会自动开）"),
    ("repost", "启动/停止重发循环：/repost on|off"),
    # 账号
    ("account", "问 @SpamBot：这个号有没有被 Telegram 限制"),
    # 其它
    ("help", "显示完整帮助"),
]

MAX_DESC = 256


def api(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise SystemExit(f"[失败] HTTP {exc.code}: {body}") from exc


def get_token() -> str:
    token = (os.environ.get("TG_BOT_TOKEN") or "").strip()
    if token:
        return token
    # 回落到 systemd 配置
    path = Path("/etc/systemd/system/tg-relay.service.d/bot.conf")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "TG_BOT_TOKEN=" in line:
                return line.split("TG_BOT_TOKEN=", 1)[1].strip().strip('"')
    raise SystemExit("[失败] 找不到 bot token（设 TG_BOT_TOKEN 或检查 systemd 配置）")


def validate(commands: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Telegram 对命令名和描述有格式要求，先自查一遍。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, desc in commands:
        if not name.islower() or not name.replace("_", "").isalnum():
            print(f"  [跳过] 命令名不合法：{name!r}（只能小写字母、数字、下划线）")
            continue
        if name in seen:
            print(f"  [跳过] 命令重复：{name}")
            continue
        if len(desc) > MAX_DESC:
            print(f"  [截断] {name} 的描述过长")
            desc = desc[: MAX_DESC - 1] + "…"
        seen.add(name)
        out.append((name, desc))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="设置 Bot 命令菜单")
    parser.add_argument("--clear", action="store_true", help="清空命令菜单")
    parser.add_argument("--show", action="store_true", help="显示当前设置")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于脚本处理）")
    args = parser.parse_args()

    token = get_token()

    if args.clear:
        result = api(token, "deleteMyCommands", {})
        print("[OK] 已清空命令菜单" if result.get("ok") else f"[失败] {result}")
        return 0

    if not args.show and not args.json:
        commands = validate(COMMANDS)
        payload = {
            "commands": [{"command": n, "description": d} for n, d in commands],
            # 默认作用域（私聊、群组都生效）；也可以只对私聊设
            "scope": {"type": "default"},
        }
        result = api(token, "setMyCommands", payload)
        if not result.get("ok"):
            print(f"[失败] {result}")
            return 1
        print(f"[OK] 已设置 {len(commands)} 个命令")

    current = api(token, "getMyCommands", {})
    if args.json:
        print(json.dumps(current, ensure_ascii=False, indent=2))
        return 0
    items = current.get("result") or []
    print(f"\n当前命令菜单（{len(items)} 个）：")
    for item in items:
        print(f"  /{item['command']:<12} {item['description']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
