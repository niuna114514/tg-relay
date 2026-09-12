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

"""恢复备份：从加密的备份包里还原整个 tg-relay 部署。

    python restore.py /root/backups/tgrelay-20260911-110639.tar.gz.gpg
    python restore.py <包> --dry-run     # 只列出会恢复什么，不动文件

⚠️ 恢复会覆盖现有的 session / 数据库 / 配置。
   如果当前服务在跑，脚本会先要求你停掉它。
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

# 恢复清单：归档内路径 -> 说明 -> 恢复后的权限
MANIFEST: dict[str, tuple[str, int]] = {
    "opt/tg-relay/.env": ("协议号凭据", 0o600),
    "opt/tg-relay/config.yaml": ("转发配置", 0o644),
    "opt/tg-relay/data/relay.session": ("会话文件（等价于账号密码）", 0o600),
    "opt/tg-relay/data/relay.db": ("去重表 + 配额 + 投递账本", 0o644),
    "opt/tg-relay/data/bot_admin.txt": ("Bot 管理员白名单", 0o644),
    "opt/tg-relay/requirements.lock.txt": ("依赖锁定", 0o644),
    "etc/systemd/system/tg-relay.service": ("服务单元", 0o644),
    "etc/systemd/system/tg-relay.service.d/bot.conf": ("Bot token", 0o600),
    "etc/systemd/system/tg-relay.service.d/token.conf": ("面板令牌", 0o600),
    "etc/systemd/system/tg-healthcheck.service": ("健康检查单元", 0o644),
    "etc/systemd/system/tg-healthcheck.timer": ("健康检查定时器", 0o644),
    "etc/nginx/sites-available/tgrelay": ("nginx 站点", 0o644),
    "etc/nginx/.htpasswd-tgrelay": ("Basic Auth 密码", 0o644),
    "etc/tgrelay-health.env": ("健康检查环境变量", 0o600),
}


def service_running() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "is-active", "tg-relay"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out == "active"
    except Exception:
        return False


def decrypt(path: Path, pass_file: Path) -> bytes:
    if not pass_file.exists():
        raise SystemExit(f"[失败] 找不到备份密码文件 {pass_file}；恢复需要它")
    result = subprocess.run(
        [
            "gpg", "--batch", "--quiet", "--decrypt",
            "--passphrase-file", str(pass_file),
            str(path),
        ],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise SystemExit(f"[失败] 解密失败：{message}\n（密码对不对？看 {pass_file}）")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="从加密备份恢复 tg-relay")
    parser.add_argument("archive", help="备份包路径（.tar.gz.gpg）")
    parser.add_argument("--pass-file", default="/root/.tgrelay-backup-pass")
    parser.add_argument("--dry-run", action="store_true", help="只列出内容，不写文件")
    parser.add_argument(
        "--check",
        action="store_true",
        help="连服务运行检查也跳过（离线校验备份包内容）",
    )
    parser.add_argument("--force", action="store_true", help="服务在跑也强行恢复")
    args = parser.parse_args()

    archive = Path(args.archive)
    if not archive.exists():
        raise SystemExit(f"[失败] 找不到备份包 {archive}")

    if args.dry_run or args.check:
        pass  # 只读校验，不碰服务
    elif service_running() and not args.force:
        raise SystemExit(
            "[中止] tg-relay 正在运行。恢复会覆盖 session/数据库，\n"
            "       请先 systemctl stop tg-relay，或加 --force（不推荐）"
        )

    print(f"解密 {archive.name} …")
    raw = decrypt(archive, Path(args.pass_file))

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        members = {m.name.lstrip("./"): m for m in tar.getmembers() if m.isfile()}

        print(f"\n包内文件（{len(members)} 个）：")
        known, unknown = [], []
        for name in sorted(members):
            if name in MANIFEST:
                desc, _ = MANIFEST[name]
                print(f"  ✓ {name}\n      {desc}")
                known.append(name)
            else:
                print(f"  ? {name}（不在清单里，也会恢复）")
                unknown.append(name)

        if args.dry_run or args.check:
            print(f"\n[dry-run] 会恢复 {len(known) + len(unknown)} 个文件，未做任何改动")
            return 0

        # 备份当前状态，便于回滚
        stamp = subprocess.run(["date", "+%Y%m%d-%H%M%S"], capture_output=True, text=True).stdout.strip()
        rollback = Path(f"/root/restore-rollback-{stamp}")
        rollback.mkdir(parents=True, exist_ok=True)
        print(f"\n先把现有文件备份到 {rollback}（万一要回滚）")
        for name in list(members):
            target = Path("/") / name
            if target.exists():
                keep = rollback / name
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, keep)

        print("\n开始恢复：")
        for name, member in members.items():
            target = Path("/") / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            target.write_bytes(source.read())
            mode = MANIFEST.get(name, ("", 0o644))[1]
            os.chmod(target, mode)
            print(f"  → {target}")

    print("\n完成。接下来：")
    print("  systemctl daemon-reload")
    print("  systemctl start tg-relay")
    print("  journalctl -u tg-relay -n 30")
    print(f"  回滚目录：{rollback}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
