# 运维手册（长期维护）

> 部署信息见 `DEPLOYED.md`。这份是"出问题了怎么办"和"日常要做什么"。

---

## 0. 一句话总结

**最大的风险不是服务崩溃，而是静默失效。**

systemd 只知道进程在不在。下面这些情况进程是 `active`，但转发其实已经废了：

| 静默失效场景 | 表现 | 怎么发现 |
|---|---|---|
| 目标群慢速被群主改成 1 小时 | 每条都要等 1 小时 | 发送记录时间间隔突然变长 |
| 账号被限流 / 掉线 | 转发全部失败 | 失败率上升 |
| 目标群禁言 / 把号踢了 | 永久失败，目标被暂停 | `/status` 显示 ⏸ |
| 会话失效 | 根本连不上 | 日志出现 `AuthKey`/`Unauthorized` |
| 磁盘满 | 写日志/数据库失败 | `df -h` |

所以第一件事是装健康检查（见第 2 节）。

---

## 1. 日常巡检（每周 1 分钟）

```bash
ssh root@<你的服务器> 'cd /opt/tg-relay && .venv/bin/python healthcheck.py'
```

输出示例：

```
[OK  ] 服务状态        tg-relay 正在运行
[OK  ] 磁盘 /          3.2 GB 可用（已用 32%）
[OK  ] TLS 证书        剩余 89 天
[OK  ] 发送活跃        最近一次成功发送在 0.04 小时前
[OK  ] 近 24 小时      成功发送 288 条
[OK  ] 今日重发额度    已用 120/200
[OK  ] 目标群数量      2 个：群A、群B
结论：✅ 一切正常（0 个问题）
```

退出码：`0` 正常、`1` 告警、`2` 严重。可以接进任何监控。

**在手机上更简单的办法**：给 Bot 发 `/status`。

---

## 2. 自动巡检（已配置成定时任务）

每 15 分钟跑一次，发现问题**主动通过 Bot 发消息给你**：

```bash
systemctl status tg-healthcheck.timer     # 看定时器
journalctl -u tg-healthcheck              # 看历史检查结果
systemctl list-timers tg-healthcheck.timer
```

它做的是**被动检查**，不会连接 Telegram（重要：主动连接会和正在跑的服务抢 session，
把账号踢下线）。会话是否有效靠"成功发送记录是否还在更新"来判断——这比连一次更可靠。

### 需要主动验证会话时

```bash
ssh root@<你的服务器>
systemctl stop tg-relay                  # ⚠️ 必须先停服务
cd /opt/tg-relay && .venv/bin/python healthcheck.py --online
systemctl start tg-relay
```

脚本会检测到服务在跑并**拒绝**执行在线检查，防止误操作。

---

## 3. 常见故障与处置

### 3.1 会话失效（最严重，需要手机验证码）

**症状**：日志出现 `AuthKeyDuplicatedError` / `Unauthorized` / `SESSION_REVOKED`；
或健康检查报"发送停滞"且日志全是连接错误。

**原因**（按概率排序）：
1. **同一个 session 在两处使用** —— 最常见。比如本地又跑了一次、或定时任务连了 Telegram
2. 账号在其它设备上手动登出
3. Telegram 风控（高频群发触发）

**处置**：

```bash
ssh root@<你的服务器>
systemctl stop tg-relay
cd /opt/tg-relay
.venv/bin/python login.py          # 交互式：手机号 + 验证码 +（两步密码）
systemctl start tg-relay
```

⚠️ 登录前**确认没有第二个地方在用这个 session**（本地机器上的 `data/relay.session` 必须不存在）。

### 3.2 目标群被禁言 / 把号踢了

**症状**：`/status` 里该群显示 ⏸；日志有 `ChatWriteForbiddenError` / `UserBannedInChannelError`。

**处置**：该群会被自动暂停（不影响其它群），并在 30 分钟内不再尝试发送。修好之后：

```
/on <序号>        # 从 Bot 恢复
```

或者如果这个群不打算再用了：

```
/del <序号>
```

⚠️ **先确认这不是账号级限制**（见 3.2.1）。判断方法：如果**只有这一个群**不行，
就是群的问题；如果**两个以上的群**同时不行，程序会直接熔断并给你发 Bot 通知。

#### 3.2.1 账号被 Telegram 限制（2026-09-12 真实事故）

这是和"群禁言"完全不同的故障，也是最容易被误判的一种。

**症状**：
- 日志里全是 `UserBannedInChannelError: You're banned from sending messages in supergroups/channels`；
- **所有**目标群一起失败（不是某一个群）；
- 自检却一路报 OK（它检查的是群权限，号被限制时群权限完全正常）；
- 收藏夹/私聊还能正常发消息。

**确认**（唯一靠谱的办法，群权限里看不出来）：

```
/account                 # 从 Bot 查
```

或者面板 → 「账号状态」→ **账号自检**，或者在服务器上（需先停服务）：

```bash
systemctl stop tg-relay
cd /opt/tg-relay && .venv/bin/python -m tgrelay --check-account
systemctl start tg-relay
```

@SpamBot 回 "I'm very sorry…" 就是被限制了；回 "Good news, no limits…" 就是正常。

**处置**：

1. **立刻停发**（程序在检测到多个目标同时报错时会自动熔断，但最好再手动确认一次）：

   ```
   /repost off        # 停止重发，并把开关写盘 —— 重启进程/重启服务器都不会自己再开
   ```

2. 用登录这个号的手机/桌面端打开 **@SpamBot** → `/start` → 点 **"This is a mistake"** 提交申诉。
   这一步只能你本人做（要走人机验证）。
3. **等**。这类限制常见 1 天到 1 周，没有官方保证的解除时间。
   期间**不要**再往任何群发东西 —— 继续发只会加重处罚。
4. 每天查一次：面板「账号自检」，或 `/account`。
5. 解除后重新开：

   ```
   /repost on         # 会顺带清掉熔断和"禁止发言"的暂停状态
   ```

**为什么会被限制**：反垃圾系统对"同一个群里短时间重复发同样内容"最敏感。
2026-09-12 那次是 **5.5 小时里把同一条素材发了 230 次**（间隔 31 秒）。
恢复后务必把节奏放慢，并准备多条不同素材轮换（见第 9 节）。

**程序已经内置的防护**（不会再出现"闷头撞一整晚"）：

| 情况 | 程序行为 |
| --- | --- |
| 单个目标报禁止发言 | 该目标停发 30 分钟，发一条 Bot 提醒 |
| 两个以上目标在 15 分钟内都报 | 判定账号级限制 → **熔断**停止全部发送 + Bot 严重告警 |
| 首次撞上禁止发言（只配了一个群时） | 主动问一次 @SpamBot，确认是不是账号级 |
| 熔断之后 | 所有发送直接跳过（连 API 都不碰），`/status` 显示熔断原因 |
| 发不出去的条 | 退还额度占位，不再白吃当天配额 |
| 配了危险节奏（间隔过短/素材太少/额度太高） | 启动日志 + `/status` + 面板「发送节奏体检」里点名警告 |

#### 3.2.2 「发送节奏体检」会说什么

事故之后加的一道体检（`tgrelay/risk.py`）。它在**启动日志、`/status`、网页面板**三处显示，
只警告不阻止 —— 节奏快慢是你的决定，但工具会把这几个数字摆出来：

| 检查项 | 阈值 | 为什么 |
| --- | --- | --- |
| 群发送间隔 | ≤ 60s 高危；< 120s 提醒 | **这个才是真正的节流阀**。30s 意味着同一个群一天理论上能塞 2880 条 —— 事故当天就是 30s |
| 素材条数 | < 5 条提醒（1 条高危） | 反垃圾最认「同一个群反复出现同样的内容」 |
| `shuffle` | 多素材但没开 → 提醒 | 固定顺序、固定周期本身就是规律特征 |
| 单群日发送量 | > 60 条提醒；> 120 条高危 | 超过就该加群分摊，而不是反复调额度 |
| 单素材日重复次数 | > 30 次提醒 | 一天让同一条素材在同一个群出现 40 次，很扎眼 |

`/status` 里长这样：

```
🚨 发送节奏体检
🟡 素材太少，重复特征明显
   当前只有 1 条素材，反垃圾系统最容易识别「同一个群反复出现同样的内容」。建议 ≥ 5 条并打开 shuffle。
```

### 3.3 慢速被改大（静默失效最典型）

群主把慢速从 30s 改成 10 分钟，程序会等 10 分钟才发一条——**不报错，只是变得极慢**。

**发现**：健康检查报"发送变慢"，或 `/status` 里慢速值变大。

**处置**：

```
/check <序号>                    # 看当前真实慢速
/everyone <慢速值> <慢速值+5>     # 把所有群间隔对齐过去
```

### 3.4 配额打满

**症状**：健康检查报"已打满"；日志出现"今日配额已用完"。

**处置**：调整额度。群多的时候用：

```
/share 500        # 500 条平均分给所有群
```

或者彻底关掉全局总闸、只按各群额度走（见 `DEPLOYED.md` 第六章）。

### 3.5 磁盘满

```bash
df -h /
journalctl --disk-usage
```

清理顺序：

```bash
# 1) 日志（最大头，已限制在 200MB，见第 5 节）
journalctl --vacuum-size=100M

# 2) 应用日志
: > /opt/tg-relay/data/relay.log

# 3) 数据库瘦身（deliveries 只增不减，长期会涨）
cd /opt/tg-relay
.venv/bin/python - <<'PY'
import sqlite3
conn = sqlite3.connect("data/relay.db")
# 只留最近 30 天的投递记录
conn.execute("DELETE FROM deliveries WHERE updated_at < datetime('now','-30 days')")
conn.commit()
print("清理后 VACUUM 中…")
conn.execute("VACUUM")
conn.close()
PY
```

> `VACUUM` 会锁库，建议在低峰期做，或者先 `systemctl stop tg-relay`。

### 3.6 网页面板打不开

```bash
systemctl is-active nginx
nginx -t
curl -sI https://<你的域名>/health     # 本机自测
tail -20 /var/log/nginx/error.log
```

排查顺序：
1. `nginx` 是否 active
2. 端口：`ss -tln | grep -E ':(80|443|8123)'`
3. 自己被 fail2ban 封了？`fail2ban-client status tgrelay`
4. 浏览器缓存 → `Ctrl+Shift+R`（已加 `no-store`，正常不会再发生）

### 3.7 证书续期失败

```bash
certbot renew --dry-run          # 试探
tail -50 /var/log/letsencrypt/letsencrypt.log
```

常见原因：80 端口被占（续期走 HTTP-01）。确认 `nginx` 在跑且 80 放行。

---

## 4. 备份与恢复

### 4.1 什么必须备份（按重要性）

| 文件 | 丢了会怎样 | 能否重建 |
|---|---|---|
| `data/relay.session` | **账号要重新登录**（要手机验证码）| ❌ 不能 |
| `.env` | 要重新填 api_id/hash | 可从 my.telegram.org 重取 |
| `data/relay.db` | 去重表丢失 → **重放的消息会重复转发**；配额/统计归零 | ❌ 不能 |
| `config.yaml` | 配置要重写 | 能（但麻烦） |
| `/etc/systemd/system/tg-relay.service.d/*.conf` | 令牌要重新生成 | 能 |
| `/etc/nginx/.htpasswd-tgrelay` | Basic Auth 密码要重设 | 能 |

### 4.2 备份命令

```bash
# 在服务器上
mkdir -p /root/backups
cd /opt/tg-relay
tar -czf /root/backups/tgrelay-$(date +%Y%m%d).tar.gz \
    .env config.yaml data/relay.db data/relay.session data/bot_admin.txt \
    /etc/systemd/system/tg-relay.service.d/
```

⚠️ **这个压缩包等价于你的账号密码**——不要传到网盘、不要贴到聊天里。建议加密：

```bash
gpg -c /root/backups/tgrelay-$(date +%Y%m%d).tar.gz     # 会提示设密码
rm /root/backups/tgrelay-$(date +%Y%m%d).tar.gz          # 删掉未加密的
```

### 4.3 恢复

```bash
# 1) 停服务
systemctl stop tg-relay

# 2) 解包（注意 session 权限）
cd /opt/tg-relay
tar -xzf /root/backups/tgrelay-YYYYMMDD.tar.gz .env config.yaml data/
chmod 600 .env data/relay.session

# 3) 起服务并验证
systemctl start tg-relay
journalctl -u tg-relay -n 20
.venv/bin/python healthcheck.py
```

### 4.4 自动备份（已配置 ✅）

**每天 6:25 自动加密备份**，保留最近 14 份：

```bash
ls -lh /root/backups/                    # 看备份
cat /var/log/tgrelay-backup.log          # 看备份日志
bash /opt/tg-relay/backup.sh             # 手动立刻备份
```

备份包含 14 项：`.env`、`config.yaml`、`data/relay.session`、`data/relay.db`、
`data/bot_admin.txt`、systemd 单元与 override（含 bot token / 面板令牌）、
nginx 站点与 Basic Auth 密码、健康检查环境变量。

**包是 GPG 加密的**（AES256），密码在 `/root/.tgrelay-backup-pass`（**单独保存一份**，
换机器恢复时要用，但别和备份包放一起）。

> 归档内的路径是"绝对路径去掉开头的 `/`"，所以恢复时 `tar -x -C /` 就能精确回到原位。

### 4.5 恢复（用 `restore.py`，有回滚保护）

```bash
# 1) 离线校验备份包内容（服务不用停）
python restore.py /root/backups/tgrelay-XXXX.tar.gz.gpg --dry-run

# 2) 正式恢复
systemctl stop tg-relay
python restore.py /root/backups/tgrelay-XXXX.tar.gz.gpg
systemctl daemon-reload
systemctl start tg-relay
journalctl -u tg-relay -n 30
python healthcheck.py
```

`restore.py` 会做这些事：

| 行为 | 说明 |
|---|---|
| 检测服务是否在跑 | 在跑就**拒绝恢复**（除非 `--force`），避免覆盖正在用的 session |
| 恢复前自动回滚点 | 现有文件先拷到 `/root/restore-rollback-<时间戳>/` |
| 按清单恢复权限 | `session`/`.env`/token 恢复成 600，配置文件 644 |
| 未知文件也恢复 | 不在清单里的文件照样解出来，只提示 |

**手动恢复**（不用脚本时）：

```bash
systemctl stop tg-relay
gpg -d /root/backups/tgrelay-XXXX.tar.gz.gpg | tar -xzf - -C /
chmod 600 /opt/tg-relay/.env /opt/tg-relay/data/relay.session
systemctl start tg-relay
```

---

## 5. 资源上限（已配置）

| 项 | 上限 | 在哪配 |
|---|---|---|
| journald 日志 | 200MB | `/etc/systemd/journald.conf` 的 `SystemMaxUse` |
| 应用日志 | 5MB × 5 份轮转 | `/etc/logrotate.d/tg-relay` |
| 投递记录 | 保留 30 天 | 需要手动清理，见 3.5 |

检查：

```bash
journalctl --disk-usage
cat /etc/logrotate.d/tg-relay
```

---

## 6. 升级代码的正确流程

### 6.1 两台机器的关系

| | 本地 `D:\dsh\tg-relay` | 服务器 `/opt/tg-relay` |
|---|---|---|
| 角色 | **开发副本**（改代码、跑测试）| **运行副本**（systemd 跑的就是它）|
| `tgrelay/*.py` | 源码 | **应该完全一致** |
| `tests/*.py` | 测试 | 一致 |
| `config.yaml` | **模板**（占位符）| **实盘配置**（真实源/目标/素材）|
| `.env` / `data/` | 无（已删，避免 session 冲突）| 真实凭据与会话 |

**⚠️ 绝对不要把本地的 `config.yaml` 同步到服务器** —— 那会覆盖掉你的实际配置。
同理 `.env`、`data/`、`DEPLOYED.md`。

### 6.2 一条命令同步 + 验证（推荐）

```powershell
cd D:\dsh\tg-relay

# 先看差异（不上传）
powershell -ExecutionPolicy Bypass -File .\sync.ps1 -Check

# 同步差异文件，并在服务器上跑测试
powershell -ExecutionPolicy Bypass -File .\sync.ps1

# 同步 + 自动重启 + 健康检查
powershell -ExecutionPolicy Bypass -File .\sync.ps1 -Deploy
```

它会：

1. 逐文件算 SHA256，找出"内容不同"和"只在本地有"的文件
2. **保险丝**：如果差异里出现 `config.yaml` / `.env` / `data` / `DEPLOYED.md`，**直接中止**
3. 只上传差异文件（不整目录覆盖，快且安全）
4. 在服务器上跑全量测试
5. `-Deploy` 时自动重启并跑健康检查

> 注：脚本以 UTF-8 **带 BOM** 保存。Windows PowerShell 5.1 读无 BOM 的 `.ps1`
> 会按 GBK 解码，中文/emoji 会破坏引号配对导致语法错误（踩过）。
> 如果你编辑了这个脚本，记得保留 BOM。

### 6.3 手动同步（不用脚本时）

```powershell
# 1) 本地跑测试
cd D:\dsh\tg-relay; python -m pytest

# 2) 只传代码（不要传 config.yaml / .env）
scp tgrelay\*.py root@<你的服务器>:/opt/tg-relay/tgrelay/

# 3) 服务器上验证
ssh root@<你的服务器> 'cd /opt/tg-relay && .venv/bin/python -m pytest -q'

# 4) 重启并看日志
ssh root@<你的服务器> 'systemctl restart tg-relay && sleep 15 && journalctl -u tg-relay -n 25'

# 5) 健康检查
ssh root@<你的服务器> 'cd /opt/tg-relay && .venv/bin/python healthcheck.py'
```

**回滚**：`restore.py` 或从 `/root/backups/` 里解出旧版本。

### 6.4 怎么确认两边一致

```powershell
powershell -ExecutionPolicy Bypass -File .\sync.ps1 -Check
```

期望输出：

```
  相同       : 48 个
  内容不同   : 0 个
  只在本地   : 0 个
  只在服务器 : 0 个

✅ 两边已经完全一致
```

（`config.yaml` 不参与比对 —— 两边本来就该不同。）

---

## 7. 依赖版本（已锁定）

`requirements.lock.txt` 记录了**当前实际可用**的确切版本。
`requirements.txt` 里是范围约束（如 `telethon>=1.36,<2`），**重建环境时可能装到不兼容的新版**。

重建环境用锁定文件：

```bash
.venv/bin/pip install -r requirements.lock.txt
```

升级依赖的流程：

```bash
# 1) 在测试环境装新版
.venv/bin/pip install -U telethon
# 2) 跑全量测试
.venv/bin/python -m pytest
# 3) 通过后更新锁定文件
.venv/bin/pip freeze > requirements.lock.txt
```

> 注意：Debian 11 的 apt 源已过期（`bullseye-security` 返回 404），
> 所以**不要用 apt 装 Python 包**。Python 3.12 是用 `uv` 独立安装的，不受影响。

---

## 8. 你现在可以忘掉的事

这些是自动的，不用管：

- ✅ **证书续期** —— `certbot.timer` 每天两次检查，到期前 30 天自动续
- ✅ **服务崩溃重启** —— systemd `Restart=always`，15 秒后自动拉起
- ✅ **断线重连** —— Telethon 自己重连；重发循环有熔断保护
- ✅ **慢速模式预判** —— 发送前等窗口，不会去撞墙
- ✅ **FloodWait 自适应降速** —— 被限流会自动把速率减半
- ✅ **配额跨重启累计** —— 存在 SQLite 里，重启不会超发
- ✅ **健康巡检** —— 每 15 分钟一次，有问题主动发 Telegram 提醒
- ✅ **账号被限制的刹车** —— 多个群同时报"禁止发言"时自动熔断，并发 Bot 告警给你

## 9. 需要你留意的事

只有四件：

1. **手机验证码** —— 会话一旦失效只能重新登录，没有别的办法。所以**别在第二个地方用同一个 session**。
2. **群里的慢速/权限变化** —— 群主改设置不会通知你。健康检查能发现"发送变慢"，但最好偶尔 `/status` 看一眼。
3. **`DEPLOYED.md` 和备份包** —— 里面是能接管账号的凭据。别提交到 git、别传网盘。
4. **发送节奏** —— Telegram 的反垃圾对"同一个群反复发同样内容"最敏感。
   2026-09-12 的号被限制就是这么来的：**5.5 小时里同一条素材发了 230 次**。
   建议的上限：

   | 项目 | 建议值 | 说明 |
   | --- | --- | --- |
   | 单群日发送量 | ≤ 60 条 | 超过就该考虑加群分摊 |
   | 同一个群两条之间的间隔 | ≥ 5 分钟 | 也就是 `targets[].interval = [300, 300]` |
   | 素材条数 | ≥ 5 条并开 `shuffle` | 同样的内容别连着刷 |

   ⚠️ 两个"间隔"不要搞混：`targets[].interval`（群发送间隔）才是真正的节流阀，
   `repost.interval`（轮次间隔）只是一轮跑完之后等多久再跑下一轮。
   素材变成 N 条之后，**每轮会发 N 条**，所以真正决定"一小时发几条"的是群发送间隔。
   设成 `[300, 300]` 就是"每个群最多 5 分钟一条"，跟有几种素材无关。
