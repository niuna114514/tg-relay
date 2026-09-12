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

"""程序入口：登录、自检、启动转发、优雅退出。

用法：
    python -m tgrelay --login          # 首次登录，保存会话
    python -m tgrelay --check          # 只做自检（源/目标解析 + 发言权限），不转发
    python -m tgrelay                  # 正式运行
    python -m tgrelay --dry-run        # 只打日志不真发，用来验证过滤规则
    python -m tgrelay --stats          # 打印统计后退出
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import signal
import sys
from html import escape as html_escape
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from . import logger as logsetup
from .alerts import AlertHub
from .config import AppConfig, ConfigError, apply_env, load_config, load_dotenv
from .db import Store
from .engine import RelayEngine
from .listener import ManagedListener, build_client, catch_up
from .reposter import Reposter
from .risk import assess, summarize
from .sender import ConfigProblem, PeerFloodBreaker, Sender

log = logsetup.get("tgrelay.main")

STATS_INTERVAL = 300.0
PRUNE_INTERVAL = 3600.0


# --------------------------------------------------------------------------
# 参数与准备
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgrelay",
        description="协议号：把频道消息转发到多个群（保留来源）",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--env", default=".env", help="环境变量文件路径")
    parser.add_argument("--login", action="store_true", help="交互式登录并保存会话")
    parser.add_argument("--check", action="store_true", help="只做自检后退出")
    parser.add_argument("--dry-run", action="store_true", help="只记录不真发")
    parser.add_argument("--stats", action="store_true", help="打印统计后退出")
    parser.add_argument("--print-session", action="store_true", help="登录后打印 StringSession")
    parser.add_argument(
        "--premium",
        choices=["true", "false"],
        default=None,
        help="覆盖 Premium 判定（升级会员后可显式打开，放宽限速）",
    )
    parser.add_argument(
        "--reset-cursor",
        action="store_true",
        help="清空补漏游标（配合 catch_up 可重新补发一批历史消息）",
    )
    parser.add_argument(
        "--repost",
        action="store_true",
        help="强制开启定时重发（覆盖 repost.enabled）",
    )
    parser.add_argument(
        "--repost-once",
        action="store_true",
        help="重发只跑一轮就退出（用来验证素材和间隔，不会常驻）",
    )
    parser.add_argument(
        "--repost-only",
        action="store_true",
        help="只做定时重发，不启动实时转发监听",
    )
    parser.add_argument(
        "--probe-send",
        action="store_true",
        help="权威判定目标能不能发：真的转发一条源消息（会往目标里发东西），然后退出",
    )
    parser.add_argument(
        "--check-account",
        action="store_true",
        help="问 @SpamBot：这个号有没有被 Telegram 限制（群权限查不出来），然后退出",
    )
    parser.add_argument("--web", action="store_true", help="启动网页面板（与转发同进程）")
    parser.add_argument("--web-host", default="127.0.0.1", help="面板监听地址（默认只绑本机）")
    parser.add_argument("--web-port", type=int, default=8000, help="面板端口")
    parser.add_argument(
        "--web-token",
        default=None,
        help="面板访问令牌；不给就随机生成并打印（也可以用 TG_WEB_TOKEN 环境变量）",
    )
    parser.add_argument(
        "--bot",
        action="store_true",
        help="启动 Telegram 操控 Bot（与转发同进程，用 bot token 认证，不占用协议号会话）",
    )
    parser.add_argument(
        "--bot-token",
        default=None,
        help="BotFather 给的 bot token（也可用 TG_BOT_TOKEN 环境变量）",
    )
    parser.add_argument("--log-level", default=None, help="覆盖日志级别")
    parser.add_argument(
        "--panel",
        action="store_true",
        help="在 SSH 里打开文字面板（★ 不连接 Telegram，只读本机 API / SQLite）",
    )
    parser.add_argument(
        "--service",
        default="tg-relay",
        help="systemd 服务名：面板用它查服务状态、并从 ExecStart 里推断面板端口",
    )
    return parser


def prepare(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)
    env = load_dotenv(args.env)
    if not env:
        log.warning("没找到 %s，将只能使用交互式登录（无法自动读取 api_id/api_hash）", args.env)
    config = apply_env(config, env)
    if args.premium is not None:
        from dataclasses import replace

        config = replace(config, premium=args.premium == "true")
    if args.log_level:
        from dataclasses import replace

        config = replace(config, log_level=args.log_level)
    if config.credentials is None:
        raise ConfigError(
            f"缺少协议号凭据：请在 {args.env} 里配置 TG_API_ID 与 TG_API_HASH"
            "（可从 https://my.telegram.org/apps 获取）"
        )
    return config


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------


async def ensure_login(client: TelegramClient, config: AppConfig, *, print_session: bool) -> None:
    """确保有可用会话；没有则走一次登录流程。"""
    if not client.is_connected():
        await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        log.info("会话有效：%s (id=%s)%s", _name(me), getattr(me, "id", "?"), _premium_note(me))
        if print_session:
            print("\nTG_SESSION_STRING=" + _string_session(client))
        return

    log.info("未登录，开始登录协议号（首次运行需要）")
    phone = input("手机号（含国家码，如 +8613800138000）: ").strip()
    if not phone:
        raise ConfigError("手机号不能为空")
    await client.send_code_request(phone)
    code = input("Telegram 收到的登录验证码: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        password = input("这个号开了两步验证，请输入密码: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    log.info("登录成功：%s (id=%s)%s", _name(me), getattr(me, "id", "?"), _premium_note(me))
    session_path = Path(config.credentials.session_path) if config.credentials else None
    if session_path:
        log.info("会话已保存到 %s（等价于账号密码，切勿外传）", session_path)
    if print_session:
        print("\nTG_SESSION_STRING=" + _string_session(client))


def _name(entity: Any) -> str:
    return " ".join(
        part for part in [getattr(entity, "first_name", None), getattr(entity, "last_name", None)] if part
    ) or getattr(entity, "username", None) or str(getattr(entity, "id", "?"))


def _premium_note(entity: Any) -> str:
    return "  [Premium]" if getattr(entity, "premium", False) else ""


def _string_session(client: TelegramClient) -> str:
    try:
        from telethon.sessions import StringSession

        session = client.session
        if isinstance(session, StringSession):
            return session.save()
    except Exception:  # pragma: no cover - 仅影响便利输出
        pass
    return "(当前使用会话文件，未启用 StringSession)"


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------


async def self_check(sender: Sender, config: AppConfig) -> tuple[list[str], list[str]]:
    """解析所有源和目标、检查发言权限、读取慢速模式。返回 (ok_messages, problems)。"""
    ok: list[str] = []
    problems: list[str] = []

    for source in config.sources:
        try:
            entity = await sender.resolve(source, "源")
            ok.append(f"源 {source} -> OK（{_name(entity)}）")
        except ConfigProblem as exc:
            problems.append(str(exc))

    for target in config.targets:
        try:
            entity = await sender.resolve(target.id, "目标")
        except ConfigProblem as exc:
            problems.append(str(exc))
            continue
        reason = await sender.check_write_permission(target)
        if reason:
            problems.append(f"目标 {target.display} 不可发送：{reason}")
        else:
            ok.append(f"目标 {target.display} -> OK（{_name(entity)}）")

        # 慢速模式决定实际吞吐上限，必须显式告知并按它核对配置
        slow = await sender.probe_slow_mode(target)
        interval = sender.pacer.interval_for(target)
        if slow:
            ok.append(
                f"目标 {target.display} 慢速模式 {slow}s -> 实际上限约 {60 / slow:.1f} 条/分钟"
            )
            effective = max(slow, interval[0])
            if effective > interval[1]:
                problems.append(
                    f"目标 {target.display} 的本地限速下限 {interval[0]:.0f}s 低于群慢速 {slow}s："
                    f"实际会按 {slow}s 一条（{60 / slow:.1f} 条/分钟）发送。"
                    f"建议把该目标的 interval 设为 [{slow}, {slow + 5}] 以免每次都撞慢速模式"
                )
            _warn_throughput(config, target.display, slow)
    if ok and not problems:
        # 说清楚这个自检的边界：它读的是**群**的权限，
        # 而账号被 Telegram 限制时群权限一切正常（2026-09-12 就是这样：
        # 自检报 OK，实际一条都发不出去）。别让人误以为 OK 就等于能发。
        if config.targets:
            log.info(
                "注意：以上是**群权限**的预判。账号级限制（反垃圾把号限制了）在群权限里"
                "完全看不出来，只有真发一条或问 @SpamBot 才知道 —— 分别用 "
                "--probe-send 和 --check-account"
            )
    return ok, problems


def _warn_throughput(config: AppConfig, display: str, slow_seconds: int) -> None:
    """按慢速模式核对该目标的日配额是否够用。"""
    per_minute = 60 / slow_seconds
    per_day_possible = per_minute * 60 * 24
    if config.rate.daily_cap > per_day_possible:
        log.warning(
            "目标 %s 慢速 %ss：一天最多只能发 %.0f 条，而 rate.daily_cap=%s 永远用不满，"
            "可把 daily_cap 下调到 %.0f 左右",
            display,
            slow_seconds,
            per_day_possible,
            config.rate.daily_cap,
            per_day_possible,
        )
    log.info(
        "目标 %s 的吞吐预期：慢速 %ss -> %.1f 条/分钟，队列上限 %s 条最多缓冲 %.0f 分钟",
        display,
        slow_seconds,
        per_minute,
        config.behavior.queue_size,
        config.behavior.queue_size * slow_seconds / 60,
    )


# --------------------------------------------------------------------------
# 运行
# --------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    config = prepare(args)
    logsetup.setup(config.log_level, config.log_path)
    log.info("启动 tgrelay：源 %s 个 / 目标 %s 个 / Premium=%s",
             len(config.sources), len(config.targets), config.premium)
    if config.premium:
        log.info("Premium 已启用：全局每分钟上限 %s 次（未升级时按 1/4）",
                 config.rate.global_per_minute * 4)

    store = Store(config.db_path)
    log.info("数据库：%s（%s）", config.db_path, store.stats())

    if args.reset_cursor:
        for source in config.sources:
            store.set_cursor(source, 0)
        log.info("已清空补漏游标")

    client = build_client(config.credentials, config.proxy)  # type: ignore[arg-type]
    breaker = PeerFloodBreaker()
    # 告警中枢：发送层往这里喊，__main__ 决定谁能收到（日志 + 操控 Bot）
    alerts = AlertHub()
    sender = Sender(
        client, config, store, breaker=breaker, dry_run=args.dry_run, alerts=alerts
    )
    engine = RelayEngine(config, store, sender)
    engine.attach(client)

    listener: ManagedListener | None = None
    reposter: Reposter | None = None
    try:
        await ensure_login(client, config, print_session=args.print_session)

        if args.stats:
            print(json.dumps(engine.report(), ensure_ascii=False, indent=2))
            return 0

        # 源/目标暖场：把 entity 灌进 Telethon 的缓存，数字 ID 才能用
        try:
            await client.get_dialogs(limit=None)
            log.info("已载入会话列表（暖场完成）")
        except Exception as exc:
            log.warning("载入会话列表失败（继续尝试）：%s", exc)

        log.info("额度分配：%s", sender.pacer.describe_limits(config.targets))

        # 发送节奏体检：把"什么配法会再次被限制"讲在启动日志里
        risks = assess(config)
        log.info(summarize(risks))
        for risk in risks:
            if risk.level == "danger":
                log.warning("%s", risk.render())
            else:
                log.info("%s", risk.render())

        ok, problems = await self_check(sender, config)
        for line in ok:
            log.info("自检 %s", line)
        for line in problems:
            log.error("自检失败 %s", line)
        if problems and not args.dry_run:
            log.error("自检未通过，先修好上面 %s 个问题再运行（--dry-run 可忽略）", len(problems))
            return 2

        if args.check:
            log.info("自检结束：%s 项正常，%s 项异常", len(ok), len(problems))
            return 0 if not problems else 2

        if args.probe_send:
            exit_code = 0
            for target in config.targets:
                allowed, note = await sender.probe_send(target)
                if allowed:
                    log.info("探测 %s -> 能发：%s", target.display, note)
                else:
                    log.error("探测 %s -> 不能发：%s", target.display, note)
                    exit_code = 2
            return exit_code

        if args.check_account:
            limited, detail = await sender.account_status()
            if limited is True:
                print("账号被限制：Telegram 反垃圾限制了这个号，先申诉并等解除。")
                print(detail)
                return 2
            if limited is False:
                print("账号正常：没有被限制。")
                print(detail)
                return 0
            print("没能得到明确结论，请自行打开 @SpamBot 查看。")
            print(detail)
            return 1

        await engine.start()
        if not args.repost_only:
            listener = ManagedListener(client, engine, config)
            listener.install()
        else:
            log.info("--repost-only：跳过实时转发监听，只做定时重发")

        # ---- 定时重发（可选）----
        # 关键点：Reposter 对象**总是**构造出来，哪怕重发当前是关的。
        # 因为面板/Bot 需要它才能「启动重发」，否则停了就只能重启进程（真实踩过）。
        repost_wanted = bool(args.repost or config.repost.enabled)
        repost_once = args.repost_once or config.repost.run_mode == "once"
        reposter = Reposter(config, store, sender, client=client)
        if repost_wanted or repost_once:
            await reposter.load_messages()
        if repost_once:
            ok_count, bad = await reposter.run_cycle()
            print(f"repost 单轮完成：成功 {ok_count}，跳过/失败 {bad}")
            print(json.dumps(reposter.stats.as_dict(), ensure_ascii=False, indent=2))
            return 0 if ok_count else 1
        if repost_wanted:
            reposter.start()
        else:
            # 以前这里直接 return 2，导致 `--repost-only` + 重发关闭 =
            # systemd 无限重启（每次都连一次 Telegram）。现在改成"空转待命"：
            # 面板和 Bot 照常在线，人在面板里点一下「启动重发」就能开始。
            log.warning(
                "定时重发当前是关闭的（repost.enabled=false）：不发任何消息，"
                "只保持面板/Bot 在线。要开始发送，在面板点「启动重发」或给 bot 发 /repost on"
            )

        if not args.dry_run and not args.repost_only:
            sent = await catch_up(client, engine, store, config)
            if sent:
                log.info("补漏完成：%s 条", sent)

        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)

        # ---- 网页面板 / 操控 Bot（可选，与转发同进程）----
        web_task: asyncio.Task[Any] | None = None
        bot_task: asyncio.Task[Any] | None = None
        if args.web or args.bot:
            control = _make_control(
                args, config, store, sender, engine, reposter, client, listener
            )
            if args.web:
                web_task = asyncio.create_task(_serve_web(args, config, store, sender, engine, reposter, client, control))
            if args.bot:
                bot_task = asyncio.create_task(
                    _serve_bot(args, config, control, alerts=alerts)
                )

        stats_task = asyncio.create_task(_stats_loop(engine, store, stop_event, reposter))
        # 注意用 reposter.running 而不是 reposter 是不是 None ——
        # Reposter 现在总是会构造出来（面板要能随时启动它），
        # 拿对象在不在判断会打出"定时重发已开启"这种自相矛盾的话。
        log.info(
            "开始监听 %s%s（Ctrl+C 退出）",
            "、".join(str(item) for item in config.sources),
            "；定时重发运行中" if reposter is not None and reposter.running else "；定时重发未运行",
        )

        listen_task = asyncio.create_task(client.run_until_disconnected())
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {listen_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            if task is listen_task and task.exception():
                log.error("监听连接中断：%s（将会重连，若反复出现请检查网络/代理）", task.exception())

        if breaker.tripped:
            log.error("运行期间触发熔断：%s", breaker.reason)

        log.info("正在退出：冲刷相册缓冲、停止 worker…")
        if bot_task is not None:
            bot_task.cancel()
        if web_task is not None:
            web_task.cancel()
        if reposter is not None:
            await reposter.stop()
        if listener is not None:
            await listener.flush()
        await engine.stop()
        stats_task.cancel()
        report = engine.report()
        if reposter is not None:
            report["repost"] = reposter.stats.as_dict()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        log.info("最终统计：%s", store.stats())
        return 0
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        store.close()


def _make_control(
    args: argparse.Namespace,
    config: AppConfig,
    store: Store,
    sender: Sender,
    engine: RelayEngine,
    reposter: Reposter | None,
    client: Any,
    listener: Any = None,
) -> "Any":
    """构造运行期控制层。网页面板和操控 Bot 共用同一个实例。"""
    from .control import RuntimeControl

    return RuntimeControl(
        config,
        store,
        sender,
        engine,
        reposter=reposter,
        client=client,
        listener=listener,
        log_path=config.log_path,
        config_path=args.config,
    )


async def _serve_bot(
    args: argparse.Namespace,
    config: AppConfig,
    control: "Any",
    *,
    alerts: AlertHub | None = None,
) -> None:
    """启动操控 Bot。

    注意：bot 用的是 **bot token 认证**，不是 relay.session，
    所以它不会和协议号抢占同一个 session（不会 AuthKeyDuplicatedError）。
    但它必须跑在同一个进程里，否则操作不了引擎。
    """
    try:
        from .bot import BotController
    except ImportError as exc:  # pragma: no cover
        log.error("操控 Bot 需要 telethon（%s）", exc)
        return

    token = (
        args.bot_token
        or os.environ.get("TG_BOT_TOKEN")
        or ""
    ).strip()
    if not token:
        log.error(
            "启用了 --bot 但没有 token。去 @BotFather 发 /newbot 拿到 token，"
            "填到 .env 的 TG_BOT_TOKEN 或 systemd 的 Environment 里。"
        )
        return

    admins: list[int] = []
    for chunk in (os.environ.get("TG_BOT_ADMINS") or "").replace(",", " ").split():
        if chunk.lstrip("-").isdigit():
            admins.append(int(chunk))

    controller = BotController(
        control,
        token=token,
        admins=admins,
        admin_file=Path(config.db_path).parent / "bot_admin.txt",
        proxy=config.proxy,
    )
    try:
        await controller.start()
    except Exception as exc:
        log.error("操控 Bot 启动失败：%s: %s", type(exc).__name__, exc)
        log.error("常见原因：token 写错、被 @BotFather 撤销、或网络/代理不通")
        return

    # 把 Bot 接成告警出口：熔断、账号被限制这类事会直接推到你手机上。
    # 必须放在 start() 之后 —— 之前 client 还是 None，推不出去。
    if alerts is not None:
        async def _push_to_admins(level: str, text: str) -> None:
            prefix = "🚨 <b>严重</b>" if level == "critical" else "⚠️ <b>提醒</b>"
            await controller.notify_admins(f"{prefix}\n{html_escape(text)}")

        alerts.add(_push_to_admins)

    # 一直挂在这里，直到进程退出
    await controller._task  # noqa: SLF001 - 保持引用直到退出


async def _serve_web(
    args: argparse.Namespace,
    config: AppConfig,
    store: Store,
    sender: Sender,
    engine: RelayEngine,
    reposter: Reposter | None,
    client: Any,
    control: "Any",
) -> None:
    """在**当前进程内**启动 FastAPI（uvicorn 共用同一个 asyncio loop）。

    绝不在这里新建 TelegramClient：这个进程已经是 session 的唯一持有者，
    再加一个 client 会让 auth key 作废、账号被强制登出。
    """
    try:
        import uvicorn

        from .webapp import LogBuffer, create_app
    except ImportError as exc:  # pragma: no cover
        log.error("网页面板需要 fastapi/uvicorn：pip install fastapi 'uvicorn[standard]'（%s）", exc)
        return

    token = args.web_token or os.environ.get("TG_WEB_TOKEN") or secrets.token_urlsafe(24)
    buffer = LogBuffer()
    logging.getLogger().addHandler(buffer)

    app = create_app(control, token=token, log_buffer=buffer)
    server_config = uvicorn.Config(
        app,
        host=args.web_host,
        port=args.web_port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(server_config)

    shown = args.web_host if args.web_host != "0.0.0.0" else "127.0.0.1"
    log.info("网页面板已启动：http://%s:%s/?token=%s", shown, args.web_port, token)
    if args.web_host == "0.0.0.0":
        log.warning(
            "面板绑定在 0.0.0.0，任何能访问该端口的人都能控制这个协议号。"
            "更安全的做法是保持 127.0.0.1 并用 SSH 隧道："
            "ssh -L %s:127.0.0.1:%s 用户@服务器",
            args.web_port,
            args.web_port,
        )
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise


async def _stats_loop(
    engine: RelayEngine,
    store: Store,
    stop: asyncio.Event,
    reposter: Reposter | None = None,
) -> None:
    ticks = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=STATS_INTERVAL)
            return
        except asyncio.TimeoutError:
            pass
        ticks += 1
        report = engine.report()
        log.info(
            "统计：收到 %s / 去重 %s / 过滤 %s / 任务 %s | 已发 %s 失败 %s 跳过 %s | "
            "今日转发 额度 %s/%s、实发 %s | 重发 额度 %s/%s、实发 %s",
            report["engine"]["received"],
            report["engine"]["duplicates"],
            report["engine"]["filtered"],
            report["engine"]["jobs"],
            report["sender"]["sent"],
            report["sender"]["failed"],
            report["sender"]["skipped"],
            report["store"]["sent_today"],
            report["sender"]["daily_cap"],
            report["store"]["sent_ok_today"],
            report["store"]["reposted_today"],
            reposter.repost_config.daily_limit if reposter else 0,
            report["store"]["repost_ok_today"],
        )
        if reposter is not None:
            log.info(
                "重发进度：已跑 %s 轮，成功 %s，跳过/失败 %s，当前素材 %s",
                reposter.stats.cycles,
                reposter.stats.sent,
                reposter.stats.skipped + reposter.stats.failed,
                reposter.stats.last_message_id,
            )
        backlog = {name: item["backlog"] for name, item in report["targets"].items() if item["backlog"]}
        if backlog:
            log.warning("目标积压：%s", backlog)
        slow = report["sender"].get("slow_mode_remaining") or {}
        if slow:
            log.info("慢速模式等待中：%s", slow)
        if report["sender"]["breaker"]:
            log.error("熔断状态：%s", report["sender"]["breaker"])
        if ticks % 12 == 0:  # 每小时清理一次去重表
            removed = store.prune_seen()
            if removed:
                log.info("清理去重记录 %s 条", removed)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    def _handler(*_: Any) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except (NotImplementedError, AttributeError, ValueError):
            # Windows 的 ProactorEventLoop 不支持 add_signal_handler
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ★ 文字面板必须在**创建 TelegramClient 之前**分流出去。
    # 服务进程已经是 relay.session 的唯一持有者；面板要是也去连一次，
    # auth key 会作废、账号被强制登出（这个项目最大的红线）。
    # 面板只读本机面板 API 和 SQLite，一行 Telegram 代码都不碰。
    if getattr(args, "panel", False):
        from .panel import main as panel_main

        return panel_main(args)

    try:
        return asyncio.run(run(args))
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
