#!/bin/bash
# tg-relay 每日加密备份
#
# ⚠️ 备份包里含 relay.session —— 它等价于账号密码，
#    所以必须加密存放，密码单独保存，别和备份包放在一起。
#
# 恢复方式（在目标机器上）：
#     gpg -d tgrelay-XXXX.tar.gz.gpg | tar -xzf - -C /
# 包内是**绝对路径去掉开头的 /**，所以 -C / 解包就能回到原位：
#     opt/tg-relay/.env          -> /opt/tg-relay/.env
#     etc/systemd/system/...     -> /etc/systemd/system/...
#
# 用法：bash backup.sh

set -euo pipefail

PROJECT=/opt/tg-relay
BACKUP_DIR=/root/backups
PASS_FILE=/root/.tgrelay-backup-pass
KEEP=14
STAMP=$(date +%Y%m%d-%H%M%S)

# 备份包权限紧一点
umask 077

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# 加密密码：第一次运行自动生成
if [ ! -f "$PASS_FILE" ]; then
    head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32 > "$PASS_FILE"
    chmod 600 "$PASS_FILE"
    echo "[!] 首次运行：已生成备份密码 -> $PASS_FILE"
    echo "    请把内容单独存一份（换机器恢复时要用），但别和备份包放一起。"
fi

TARGET="$BACKUP_DIR/tgrelay-$STAMP.tar.gz.gpg"
TMP_TAR="$BACKUP_DIR/.tmp-$STAMP.tar.gz"

# 用 -C / 保证归档内的相对路径 = 绝对路径去掉开头的 /，
# 这样恢复时 tar -xzf - -C / 就能精确回到原位。
cd /

INCLUDE=()
for rel in \
    "opt/tg-relay/.env" \
    "opt/tg-relay/config.yaml" \
    "opt/tg-relay/requirements.lock.txt" \
    "opt/tg-relay/data/relay.db" \
    "opt/tg-relay/data/relay.session" \
    "opt/tg-relay/data/bot_admin.txt" \
    "etc/systemd/system/tg-relay.service" \
    "etc/systemd/system/tg-healthcheck.service" \
    "etc/systemd/system/tg-healthcheck.timer" \
    "etc/systemd/system/tg-relay.service.d" \
    "etc/nginx/sites-available/tgrelay" \
    "etc/nginx/.htpasswd-tgrelay" \
    "etc/tgrelay-health.env" \
; do
    [ -e "/$rel" ] && INCLUDE+=("$rel")
done

if [ ${#INCLUDE[@]} -eq 0 ]; then
    echo "[失败] 没有找到任何要备份的文件"
    exit 1
fi

tar -czf "$TMP_TAR" "${INCLUDE[@]}" 2>/dev/null
gpg --batch --yes --quiet --symmetric --cipher-algo AES256 \
    --passphrase-file "$PASS_FILE" -o "$TARGET" "$TMP_TAR"
rm -f "$TMP_TAR"

SIZE=$(du -h "$TARGET" | cut -f1)
FILES=${#INCLUDE[@]}
echo "[OK] 备份完成：$TARGET（$SIZE，$FILES 项）"

# 保留最近 N 份
if [ "$(ls -1 "$BACKUP_DIR"/tgrelay-*.tar.gz.gpg 2>/dev/null | wc -l)" -gt "$KEEP" ]; then
    ls -1t "$BACKUP_DIR"/tgrelay-*.tar.gz.gpg | tail -n +$((KEEP + 1)) | while read -r old; do
        rm -f "$old"
        echo "[..] 清理旧备份：$(basename "$old")"
    done
fi
echo "[..] 当前保留 $(ls -1 "$BACKUP_DIR"/tgrelay-*.tar.gz.gpg 2>/dev/null | wc -l) 份（上限 $KEEP）"
