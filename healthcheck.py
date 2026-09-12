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

"""健康检查：判断"是不是在正常工作"，而不只是"进程还活着"。

为什么需要它：
    systemd 只知道进程在不在。进程活着但**转发已经废了**的情况，
    systemd 认为是健康的 —— 这才是真正的运维风险：
      * 目标群慢速被群主改成 1 小时 -> 每条都要等 1 小时
      * 账号被限流 / 掉线 -> 转发全部失败
      * 目标群禁言 / 被踢 -> 永久失败
      * 会话失效 -> 根本连不上
    本脚本把这些"业务层健康"检查出来。

用法：
    python healthcheck.py                # 人类可读 + 退出码
    python healthcheck.py --json         # 机器可读
    python healthcheck.py --notify       # 通过操控 Bot 发 Telegram 提醒（需要 TG_BOT_TOKEN + 管理员 ID）

退出码：0 = 健康，1 = 告警，2 = 严重
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WARN = 1
CRIT = 2


@dataclass
class Finding:
    level: int
    title: str
    detail: str

    def render(self) -> str:
        icon = {0: "OK  ", WARN: "WARN", CRIT: "CRIT"}[self.level]
        return f"[{icon}] {self.title}\n        {self.detail}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: int, title: str, detail: str) -> None:
        self.findings.append(Finding(level, title, detail))

    @property
    def worst(self) -> int:
        return max((f.level for f in self.findings), default=0)

    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.level > 0]


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------


def check_service(report: Report) -> None:
    """服务是否 active，重启次数是否异常。"""
    import subprocess

    def run(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    state = run(["systemctl", "is-active", "tg-relay"]) or "unknown"
    if state == "active":
        report.add(0, "服务状态", "tg-relay 正在运行")
    else:
        report.add(CRIT, "服务状态", f"tg-relay 状态为 {state}，服务没在运行")

    restarts = run(["systemctl", "show", "-p", "NRestarts", "--value", "tg-relay"]) or "0"
    try:
        count = int(restarts)
    except ValueError:
        count = 0
    if count >= 10:
        report.add(CRIT, "重启次数", f"重启了 {count} 次，说明一直在崩，需要查日志")
    elif count >= 3:
        report.add(WARN, "重启次数", f"重启了 {count} 次，建议查一下原因")

    # 运行时长很短 + 重启次数 > 0，才值得怀疑是崩溃重启。
    # 只看"刚启动"会误报：手动 systemctl restart 之后立刻跑检查就会中招。
    since = run(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", "tg-relay"])
    if since.isdigit() and count > 0:
        try:
            uptime_us = float(open("/proc/uptime").read().split()[0]) - int(since) / 1_000_000
        except Exception:
            uptime_us = -1
        if 0 < uptime_us < 180:
            report.add(
                WARN,
                "疑似崩溃重启",
                f"刚启动 {uptime_us:.0f} 秒，且服务累计重启过 {count} 次 —— 建议查日志",
            )
        else:
            report.add(0, "运行时长", f"{uptime_us / 60:.0f} 分钟（重启累计 {count} 次）")


def connect_ro(db_path: Path):
    """只读连接数据库。

    注意：只读连接**不能执行 ALTER TABLE**，所以这里不做迁移。
    老库可能缺新加的列（checked / repost_checked），下面用 columns() 自适应。
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def columns(conn, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def check_activity(report: Report, config: object) -> None:
    """最近有没有成功发送 —— 这是"业务健康"的核心指标。"""
    db_path = Path(getattr(config, "db_path", "data/relay.db"))
    if not db_path.exists():
        report.add(CRIT, "数据库", f"找不到 {db_path}")
        return

    conn = connect_ro(db_path)
    try:
        if not table_exists(conn, "deliveries"):
            report.add(WARN, "发送记录", "deliveries 表还不存在（还没发过东西？）")
            return

        # 最近一次成功发送
        row = conn.execute(
            "SELECT MAX(updated_at) AS last_ok FROM deliveries WHERE status = 'sent'"
        ).fetchone()
        last_ok = row["last_ok"] if row else None

        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM deliveries
            WHERE status = 'sent' AND updated_at >= datetime('now', '-1 day')
            """
        ).fetchone()
        sent_24h = int(row["n"]) if row else 0

        repost = getattr(config, "repost", None)
        repost_enabled = bool(getattr(repost, "enabled", False))
        interval = getattr(repost, "interval", None) or 300

        # 近 24 小时有没有**任何**发送动作（不管成功失败）。
        # 用来区分"重发被关掉了（预期安静）"和"程序卡死了（该报警）"。
        attempts_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM deliveries
            WHERE updated_at >= datetime('now', '-1 day')
            """
        ).fetchone()
        attempts_24h = int(attempts_row["n"]) if attempts_row else 0

        if last_ok is None:
            report.add(WARN, "发送记录", "数据库里还没有任何成功发送记录")
        else:
            age_row = conn.execute(
                "SELECT (strftime('%s','now') - strftime('%s', ?)) AS age", (last_ok,)
            ).fetchone()
            age = int(age_row["age"]) if age_row and age_row["age"] is not None else 0
            hours = age / 3600
            if repost_enabled:
                expected = max(1.0, interval * 3 / 3600)  # 间隔×3 算正常波动
                if hours > expected * 2:
                    report.add(
                        CRIT,
                        "发送停滞",
                        f"最近一次成功发送在 {hours:.1f} 小时前，"
                        f"而重发间隔是 {interval:.0f} 秒 —— 明显不正常",
                    )
                elif hours > expected:
                    report.add(
                        WARN, "发送变慢",
                        f"最近一次成功发送在 {hours:.1f} 小时前（重发间隔 {interval:.0f} 秒）",
                    )
                else:
                    report.add(0, "发送活跃", f"最近一次成功发送在 {hours:.2f} 小时前")
            elif attempts_24h == 0:
                # 重发关着 + 近 24 小时一次都没试过 = **人主动停的**
                # （账号被限制、在等申诉解除）。这是预期状态，只报信息。
                # 不加这个分支的话，停发超过 24 小时就会天天收到"发送停滞"的误报，
                # 把真正的告警淹掉。
                report.add(
                    0,
                    "发送记录",
                    f"重发已关闭，近 24 小时没有发送动作（预期）；"
                    f"上次成功发送在 {hours:.1f} 小时前",
                )
            elif hours > 24:
                report.add(WARN, "发送记录", f"最近一次成功发送在 {hours:.1f} 小时前")

        report.add(0, "近 24 小时", f"成功发送 {sent_24h} 条")

        # 失败 / 跳过比例
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM deliveries
            WHERE updated_at >= datetime('now', '-1 day') GROUP BY status
            """
        ).fetchall()
        counts = {r["status"]: int(r["n"]) for r in rows}
        total = sum(counts.values())
        if total:
            failed = counts.get("failed", 0)
            skipped = counts.get("skipped", 0)
            # 要求最小失败数：偶发一两条多半是重启/网络抖动造成的，
            # 每次都报警会变成噪音（真的踩过：重启时有一条在途就报警了）
            if failed >= 3 and failed / total > 0.3:
                report.add(CRIT, "失败率", f"近 24 小时失败 {failed}/{total}，超过 30%")
            elif failed >= 3:
                report.add(WARN, "失败率", f"近 24 小时失败 {failed}/{total}")
            elif failed:
                report.add(0, "失败率", f"近 24 小时失败 {failed}/{total}（偶发，忽略）")
            if skipped >= 5 and skipped / total > 0.5:
                report.add(
                    WARN, "跳过率", f"近 24 小时跳过 {skipped}/{total}（多半是配额或慢速限制）"
                )
    finally:
        conn.close()


def check_quota(report: Report, config: object) -> None:
    """配额是否被打满 —— 打满意味着有内容没发出去。"""
    db_path = Path(getattr(config, "db_path", "data/relay.db"))
    if not db_path.exists():
        return

    conn = connect_ro(db_path)
    try:
        if not table_exists(conn, "daily_counter"):
            report.add(0, "今日配额", "还没有配额记录")
            return

        cols = columns(conn, "daily_counter")
        # checked 是后加的列；老库没有就回落到 sent（语义接近，够用来判断"是否打满"）
        checked_col = "checked" if "checked" in cols else "sent"
        repost_col = "repost_checked" if "repost_checked" in cols else "repost"
        if checked_col != "checked" or repost_col != "repost_checked":
            report.add(WARN, "数据库结构", "daily_counter 缺新列（服务启动时会自动补，可忽略）")

        today = time.strftime("%Y-%m-%d")
        row = conn.execute(
            f"SELECT {checked_col} AS c, {repost_col} AS r FROM daily_counter WHERE day = ?",
            (today,),
        ).fetchone()
        if row is None:
            report.add(0, "今日配额", "今天还没有发送记录")
            return

        rate = getattr(config, "rate", None)
        repost = getattr(config, "repost", None)
        cap = getattr(rate, "daily_cap", 0) or 0
        repost_cap = getattr(repost, "daily_limit", 0) or 0

        def describe(used: int, limit: int, label: str) -> None:
            if limit <= 0:
                report.add(0, label, f"已用 {used}（未设上限，0 = 不限）")
                return
            ratio = used / limit
            if ratio >= 1:
                report.add(WARN, label, f"已打满 {used}/{limit}，之后的消息会被跳过")
            elif ratio >= 0.9:
                report.add(0, label, f"接近上限 {used}/{limit}")
            else:
                report.add(0, label, f"已用 {used}/{limit}")

        describe(int(row["r"]), repost_cap, "今日重发额度")
        describe(int(row["c"]), cap, "今日转发额度")
    finally:
        conn.close()


def check_targets(report: Report, config: object) -> None:
    """目标群的慢速是否被改大 —— 这是最常见的"静默失效"。"""
    targets = getattr(config, "targets", ())
    if not targets:
        report.add(CRIT, "目标群", "配置里没有任何目标群")
        return
    report.add(0, "目标群数量", f"{len(targets)} 个：" + "、".join(t.display for t in targets))
    for target in targets:
        if target.daily_limit == 0 and (getattr(config.rate, "daily_cap", 0) or 0) == 0:
            report.add(
                WARN,
                f"{target.display} 额度",
                "该群没有单独额度，全局总闸也关了 —— 今天可以无限发，注意风控",
            )


async def check_tg_alive(config: object, report: Report, timeout: float = 25.0) -> None:
    """真正连一次 Telegram，确认会话还有效。

    ⚠️ **默认不跑这个检查**（要显式加 --online）。
    原因：同一个 session 不能被两个进程同时持有，
    如果定时任务里也连一次 Telegram，会让正在跑的服务被踢下线
    （AuthKeyDuplicatedError，账号会被强制登出）。
    所以在线检查只在手动排查时用，而且应该先停服务。

    "会话是否有效"平时靠 check_activity() 的发送记录来判断就够了 ——
    会话失效的话，成功发送记录会立刻停止更新。
    """
    credentials = getattr(config, "credentials", None)
    if credentials is None:
        report.add(WARN, "会话检查", "读不到协议号凭据，跳过在线检查")
        return

    # 检测服务是否在跑：在跑就拒绝连（避免把服务踢下线）
    import subprocess

    try:
        state = subprocess.run(
            ["systemctl", "is-active", "tg-relay"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        state = ""
    if state == "active":
        report.add(
            CRIT,
            "在线检查被拒绝",
            "tg-relay 服务正在运行，此时再连一次 Telegram 会让服务掉线。"
            "先 systemctl stop tg-relay 再跑 --online。",
        )
        return

    try:
        from tgrelay.listener import build_client
    except Exception as exc:
        report.add(WARN, "会话检查", f"无法导入客户端：{exc}")
        return

    client = build_client(credentials, getattr(config, "proxy", None))
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        if await client.is_user_authorized():
            me = await client.get_me()
            report.add(0, "会话在线", f"账号有效：{getattr(me, 'first_name', '?')} (id={me.id})")
        else:
            report.add(
                CRIT,
                "会话失效",
                "session 未授权！需要用 login.py 重新登录（要手机验证码）",
            )
    except asyncio.TimeoutError:
        report.add(CRIT, "连接超时", f"{timeout:.0f} 秒内连不上 Telegram，检查网络/代理")
    except Exception as exc:
        name = type(exc).__name__
        if "AuthKey" in name or "Unauthorized" in name:
            report.add(CRIT, "会话被作废", f"{name}：session 已失效，需要重新登录")
        else:
            report.add(WARN, "连接异常", f"{name}: {exc}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def check_accounting(report: Report, config: object) -> None:
    """账目自检：额度计数 与 实际投递记录 是否对得上。

    为什么需要：如果这两者漂移，要么"有额度却发不出去"，要么"额度用完了还在发"。
    正常情况：额度占用(checked) >= 实发(sent)，
    差额 = 预占了额度但最终失败/跳过的次数（这不正常但要能解释）。
    """
    db_path = Path(getattr(config, "db_path", "data/relay.db"))
    if not db_path.exists():
        return

    conn = connect_ro(db_path)
    try:
        if not (table_exists(conn, "daily_counter") and table_exists(conn, "deliveries")):
            return

        cols = columns(conn, "daily_counter")
        repost_col = "repost_checked" if "repost_checked" in cols else "repost"
        checked_col = "checked" if "checked" in cols else "sent"

        row = conn.execute(
            f"SELECT {checked_col} AS c, {repost_col} AS r FROM daily_counter WHERE day = date('now','localtime')"
        ).fetchone()
        if row is None:
            return
        quota_repost = int(row["r"])
        quota_relay = int(row["c"])

        # 今天真实成功的条数（按 job_id 前缀区分转发/重发）
        sent_repost = conn.execute(
            """
            SELECT COUNT(*) n FROM deliveries
            WHERE status='sent' AND job_id LIKE 'repost:%'
              AND date(updated_at,'localtime') = date('now','localtime')
            """
        ).fetchone()["n"]
        sent_relay = conn.execute(
            """
            SELECT COUNT(*) n FROM deliveries
            WHERE status='sent' AND job_id NOT LIKE 'repost:%'
              AND date(updated_at,'localtime') = date('now','localtime')
            """
        ).fetchone()["n"]

        # 预占了但没成功发出去的（探测失败、被拒、重试耗尽都算）
        wasted = (quota_repost - sent_repost) + (quota_relay - sent_relay)
        total_quota = quota_repost + quota_relay
        total_sent = sent_repost + sent_relay

        detail = (
            f"额度占用：转发 {quota_relay}、重发 {quota_repost}｜"
            f"实发：转发 {sent_relay}、重发 {sent_repost}｜"
            f"预占未发出 {wasted}"
        )

        if total_quota < total_sent:
            report.add(
                CRIT,
                "账目异常",
                f"实发数({total_sent}) 大于额度占用({total_quota})！"
                f"说明额度没记上 —— 可能会超发。{detail}",
            )
        elif total_quota and wasted / total_quota > 0.5:
            report.add(
                WARN,
                "额度浪费偏多",
                f"一半以上的额度预占了却没发出去（多半是被拒/失败）。{detail}",
            )
        else:
            report.add(0, "账目核对", detail)
    except Exception as exc:
        report.add(WARN, "账目核对", f"检查失败：{type(exc).__name__}: {exc}")
    finally:
        conn.close()


def check_disk(report: Report) -> None:
    import shutil

    for path in ("/", "/opt/tg-relay"):
        try:
            total, used, free = shutil.disk_usage(path)
        except OSError:
            continue
        pct = used / total * 100
        detail = f"{free // 1024 // 1024} MB 可用（已用 {pct:.0f}%）"
        if pct >= 90:
            report.add(CRIT, f"磁盘 {path}", detail)
        elif pct >= 80:
            report.add(WARN, f"磁盘 {path}", detail)
        else:
            report.add(0, f"磁盘 {path}", detail)


def check_certificate(report: Report, host: str | None = None) -> None:
    """证书剩余天数（只在装了 certbot 的机器上有意义）。

    主机名从 TG_HEALTH_HOST 环境变量取（部署时设置），
    避免把具体域名写死在源码里。
    """
    host = host or os.environ.get("TG_HEALTH_HOST") or ""
    if not host:
        return
    cert = Path(f"/etc/letsencrypt/live/{host}/cert.pem")
    if not cert.exists():
        return
    import subprocess

    try:
        out = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", str(cert)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        raw = out.split("=", 1)[1]
        expiry = time.mktime(time.strptime(raw, "%b %d %H:%M:%S %Y %Z"))
    except Exception:
        return
    days = int((expiry - time.time()) / 86400)
    if days < 0:
        report.add(CRIT, "TLS 证书", f"已过期 {abs(days)} 天！面板会报证书错误")
    elif days < 7:
        report.add(CRIT, "TLS 证书", f"只剩 {days} 天，自动续期可能失败了，手动跑 certbot renew")
    elif days < 20:
        report.add(WARN, "TLS 证书", f"只剩 {days} 天")
    else:
        report.add(0, "TLS 证书", f"剩余 {days} 天")


def check_web(report: Report, url: str | None = None) -> None:
    """网页面板是否可达。

    URL 从 TG_HEALTH_URL 环境变量取（部署时设置），
    避免把服务器地址写死在源码里。
    """
    url = url or os.environ.get("TG_HEALTH_URL") or ""
    if not url:
        return
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            code = response.status
        if code == 200:
            report.add(0, "网页面板", f"{url} 返回 200")
        elif code in (401, 403):
            report.add(WARN, "网页面板", f"返回 {code}（认证被拦，可能 normal）")
        else:
            report.add(WARN, "网页面板", f"返回 HTTP {code}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            report.add(0, "网页面板", f"可达（{exc.code} 是预期的认证挑战）")
        else:
            report.add(WARN, "网页面板", f"HTTP {exc.code}")
    except Exception as exc:
        report.add(WARN, "网页面板", f"访问失败：{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------


def notify_bot(text: str) -> None:
    """通过 Bot API 直接发消息给管理员（不需要起 Telethon，不会碰 session）。"""
    token = (os.environ.get("TG_BOT_TOKEN") or "").strip()
    admins = (os.environ.get("TG_BOT_ADMINS") or "").strip()
    if not admins:
        # 回落到服务写的管理员文件
        admin_file = Path(os.environ.get("TG_BOT_ADMIN_FILE") or "data/bot_admin.txt")
        if admin_file.exists():
            ids = [line.strip() for line in admin_file.read_text(encoding="utf-8").splitlines()]
            admins = ",".join(item for item in ids if item)
    if not token or not admins:
        print("（--notify 需要 TG_BOT_TOKEN 与管理员 ID；已跳过）")
        return
    chat_id = admins.split(",")[0].strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status == 200:
                print("（已通过 Bot 发送提醒）")
            else:
                print(f"（发送提醒失败：HTTP {response.status}）")
    except Exception as exc:
        print(f"（发送提醒失败：{type(exc).__name__}: {exc}）")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    from tgrelay.config import apply_env, load_config, load_dotenv

    report = Report()
    try:
        config = apply_env(load_config(args.config), load_dotenv(args.env))
    except Exception as exc:
        report.add(CRIT, "配置读取", f"{type(exc).__name__}: {exc}")
        config = None

    check_service(report)
    check_disk(report)
    check_certificate(report)
    if not args.no_web:
        check_web(report)
    if config is not None:
        check_activity(report, config)
        check_quota(report, config)
        check_accounting(report, config)
        check_targets(report, config)
        if args.online:
            await check_tg_alive(config, report)

    if args.json:
        print(json.dumps(
            {
                "level": report.worst,
                "ok": report.worst == 0,
                "findings": [
                    {"level": f.level, "title": f.title, "detail": f.detail}
                    for f in report.findings
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("=" * 62)
        print("  tg-relay 健康检查")
        print("=" * 62)
        for finding in report.findings:
            print(finding.render())
        print("-" * 62)
        verdict = {0: "✅ 一切正常", WARN: "⚠️ 有告警，建议处理", CRIT: "❌ 有严重问题"}[report.worst]
        print(f"结论：{verdict}（{len(report.problems())} 个问题）")

    # 什么级别才值得推一条到手机上：
    #   正常情况：有 WARN 就推（黄灯），有 CRIT 更要推。
    #   重发关着的时候（账号被限制、在等申诉，见 MAINTENANCE 3.2.1）：
    #     只推 CRIT。因为"停发"会让昨天的失败率/跳过率/额度浪费在 24 小时窗口里
    #     残留十几个小时，每 15 分钟推一次的黄灯纯属噪音，会把真正的报警淹掉。
    #     服务挂了、磁盘满、证书过期这些仍然是 CRIT，照样会叫人。
    notify_from = WARN
    if config is not None and not bool(getattr(getattr(config, "repost", None), "enabled", False)):
        notify_from = CRIT
    if args.notify and report.worst >= notify_from:
        lines = ["<b>⚠️ tg-relay 健康检查报警</b>"]
        for finding in report.problems():
            if finding.level < notify_from:
                continue
            icon = "🔴" if finding.level == CRIT else "🟡"
            lines.append(f"{icon} <b>{finding.title}</b>\n{finding.detail}")
        if len(lines) > 1:
            notify_bot("\n\n".join(lines))

    return report.worst


def main() -> int:
    parser = argparse.ArgumentParser(description="tg-relay 健康检查")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--notify", action="store_true", help="有问题时通过 Bot 发提醒")
    parser.add_argument(
        "--online",
        action="store_true",
        help="额外连一次 Telegram 验证会话（⚠️ 必须先停服务，否则会把服务踢下线）",
    )
    parser.add_argument("--no-web", action="store_true", help="跳过网页面板检查")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
