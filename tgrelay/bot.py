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

"""Telegram 操控 Bot：在**同一个进程里**再挂一个 bot 客户端，用来跟你对话。

为什么必须是同进程：
    * 这个进程已经是 relay.session 的唯一持有者；
    * 但 **bot 走的是另一套认证（bot token），不是 MTProto 用户会话**，
      所以它是一个独立 client、独立 session 文件，不会和协议号冲突
      （不会触发 AuthKeyDuplicatedError）。
    * 反过来，如果 bot 单独起一个进程，它就没法操作引擎了（跨进程没共享内存）。

命令全部复用 control.RuntimeControl，和网页面板是同一套逻辑，
所以面板上有的操作（加/删目标、暂停、改素材、跑一轮、看日志）这里都有。

安全：默认**只响应白名单里的用户**。名单为空时，第一个发消息的人会被自动
认成管理员并写进 data/bot_admin.txt —— 所以建完 bot 后请你自己先发一句。
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from pathlib import Path
from typing import Any, Iterable

from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from .config import Target
from .control import ControlError, RuntimeControl

log = logging.getLogger("tgrelay.bot")

HELP_TEXT = """<b>tg-relay 控制台</b>

<b>状态</b>
/status — 总览（额度、各群积压、慢速、熔断）
/targets — 目标群列表（带序号）
/logs [条数] — 最近日志
/where — 当前生效的关键参数

<b>源频道</b>
/sources — 列出所有源（标出主源）
/source &lt;@频道 或 -100...&gt; — 换主源（repost 素材/探测/补漏都以主源为准）
/addsource &lt;@频道&gt; — 追加一个源（多源同时监听）
/delsource &lt;@频道&gt; — 移除某个源

<b>目标群</b>
/targets — 目标群列表（含各群今日用量与额度）
/add &lt;@群名 或 -100...&gt; [备注] — 加入目标
/del &lt;序号 或 @群名&gt; — 移除目标
/on &lt;序号&gt; / /off &lt;序号&gt; — 启用 / 暂停
/limit &lt;序号&gt; &lt;下限&gt; &lt;上限&gt; — 改该群的**发送间隔**（两次发消息之间至少隔多久）
/check &lt;序号&gt; — 探测该群能否发言 + 慢速设置
/probe &lt;序号&gt; — 真发一条测试（会往群里发东西）

<b>批量操作（多群管理）</b>
/onall / /offall — 一键启用 / 暂停全部群
/quotas &lt;每群条数&gt; — 给所有群设置各自的每日额度
/share &lt;总条数&gt; — 把总额度平均分给所有群
/everyone &lt;下限&gt; &lt;上限&gt; — 统一改所有群的发送间隔

<b>定时重发</b>
/materials — 素材列表（带序号和已发次数）
/addmat &lt;消息ID...&gt; — 设置素材（覆盖）
/interval &lt;秒&gt; — 改**轮次间隔**：一轮跑完等多久跑下一轮（必须 ≥ 31）
/dailylimit &lt;条数&gt; — 改重发日额度
/run — 立刻跑一轮
/stop — 停止重发循环（并写盘，重启不会自动开）
/repost on|off — 重新启动 / 停止重发循环

<i>注意别把两个"间隔"搞混：
  /limit 改的是「同一个群两次发消息之间至少隔多久」；
  /interval 改的是「重发循环多久跑一轮」。
  一轮要把所有素材各发一遍，所以轮次间隔最好 &gt; 素材数 × 群发送间隔。</i>

<b>账号</b>
/account — 问 @SpamBot：这个号有没有被 Telegram 限制（唯一靠谱的查法）

<b>其它</b>
/help — 显示这条帮助
/id — 显示你的 Telegram ID（用于白名单）
"""


class BotError(Exception):
    """可以直接回给用户的错误。"""


class BotController:
    def __init__(
        self,
        control: RuntimeControl,
        *,
        token: str,
        admins: Iterable[int] = (),
        admin_file: str | Path = "data/bot_admin.txt",
        proxy: Any = None,
    ) -> None:
        self.control = control
        self.token = token
        self.admin_file = Path(admin_file)
        self.proxy = proxy
        self.allowed: set[int] = {int(item) for item in admins}
        self._load_admins()
        self.client: TelegramClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._meta: dict[int, Any] = {}  # 序号 -> 目标，供 /on /off /del 用
        self._meta_at: float = 0.0

    # ---------------- 白名单 ----------------

    def _load_admins(self) -> None:
        if self.admin_file.exists():
            for line in self.admin_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.isdigit() or (line.startswith("-") and line[1:].isdigit()):
                    self.allowed.add(int(line))

    def _remember_admin(self, user_id: int) -> None:
        if user_id in self.allowed:
            return
        self.allowed.add(user_id)
        try:
            self.admin_file.parent.mkdir(parents=True, exist_ok=True)
            with self.admin_file.open("a", encoding="utf-8") as stream:
                stream.write(f"{user_id}\n")
        except OSError as exc:
            log.warning("写入管理员文件失败：%s", exc)
        log.warning(
            "已把 %s 记为 bot 管理员（白名单原本为空）。如果不是你本人，"
            "请立刻到 @BotFather 撤销这个 bot 的 token 并重建。",
            user_id,
        )

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        """启动 bot 客户端并注册命令处理器。

        bot 用 **MemorySession**：bot token 本身就是长期凭据，
        每次启动用 token 重新认证即可，不需要（也不该）落一份会话文件。
        """
        client = TelegramClient(
            MemorySession(),
            self._api_id(),
            self._api_hash(),
            **self._proxy_kwargs(),
        )
        await client.start(bot_token=self.token)
        me = await client.get_me()
        log.info(
            "操控 Bot 已启动：@%s (id=%s)",
            getattr(me, "username", "?"),
            getattr(me, "id", "?"),
        )
        if not self.allowed:
            log.warning(
                "Bot 白名单为空——第一个给 bot 发消息的人会被设为管理员。"
                "请自己先发一句 /start。"
            )
        self.client = client

        @client.on(events.NewMessage(incoming=True))
        async def _on_message(event: Any) -> None:  # pragma: no cover - 需要真实 bot
            try:
                await self.handle(event)
            except Exception:
                log.exception("处理 bot 消息失败")

        self._task = asyncio.create_task(client.run_until_disconnected(), name="bot-client")
        log.info("操控 Bot 就绪，等待你的消息")

    def _api_id(self) -> int:
        credentials = self.control.config.credentials
        return credentials.api_id if credentials else 1

    def _api_hash(self) -> str:
        credentials = self.control.config.credentials
        return credentials.api_hash if credentials else ""

    def _proxy_kwargs(self) -> dict[str, Any]:
        return self.proxy.as_telethon_kwargs() if self.proxy is not None else {}

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    # ---------------- 消息处理 ----------------

    async def handle(self, event: Any) -> None:
        sender_id = event.sender_id or 0
        text = (event.raw_text or "").strip()

        if not self.allowed:
            # 白名单为空：第一个说话的人被认成管理员
            self._remember_admin(sender_id)
            await event.reply(
                "已把你设为管理员。\n\n"
                "如果这不是你本人操作，请立刻到 @BotFather 用 /revoke 撤销 token。\n\n"
                + HELP_TEXT,
                parse_mode="html",
            )
            return

        if sender_id not in self.allowed:
            log.warning("忽略来自 %s 的消息（不在白名单）", sender_id)
            return

        if not text:
            return
        if not text.startswith("/"):
            await event.reply("我是命令式的，发 /help 看有哪些命令。", parse_mode="html")
            return

        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip()

        try:
            reply = await self.dispatch(command, argument)
        except BotError as exc:
            reply = f"❌ {html.escape(str(exc))}"
        except ControlError as exc:
            reply = f"❌ {html.escape(str(exc))}"
        except Exception as exc:  # pragma: no cover
            log.exception("命令 %s 执行失败", command)
            reply = f"❌ 内部错误：{html.escape(type(exc).__name__)}: {html.escape(str(exc))}"
        await event.reply(reply, parse_mode="html")

    async def dispatch(self, command: str, argument: str) -> str:
        handler = getattr(self, f"_cmd_{command.lstrip('/')}", None)
        if handler is None:
            return f"没有这个命令：<code>{html.escape(command)}</code>\n\n" + HELP_TEXT
        return await handler(argument)

    # ---------------- 命令实现 ----------------

    async def _cmd_help(self, _: str) -> str:
        return HELP_TEXT

    async def _cmd_start(self, _: str) -> str:
        return HELP_TEXT

    async def _cmd_id(self, _: str) -> str:
        return "你的 Telegram ID 已记入白名单。要手动管理，见 data/bot_admin.txt"

    async def _cmd_status(self, _: str) -> str:
        snapshot = self.control.snapshot()
        engine = snapshot["engine"]
        sender = snapshot["sender"]
        store = snapshot["store"]
        repost = snapshot["repost"]

        lines = [
            "<b>📊 运行状态</b>",
            f"模式：{'定时重发' if repost['enabled'] else '实时转发'}",
            f"熔断：{'⚠️ ' + html.escape(sender['breaker']) if sender['breaker'] else '正常'}",
            "",
            f"今日转发：额度占用 {store['sent_today']}/{sender['daily_cap']}，实发 {store['sent_ok_today']}",
            f"今日重发：额度占用 {store['reposted_today']}/{repost['daily_limit']}，实发 {store['repost_ok_today']}",
            f"累计：已发 {sender['sent']}，失败 {sender['failed']}，慢速等待 {sender['slow_waits']}",
            "",
            "<b>目标群</b>",
        ]
        self._meta.clear()
        self._meta_at = time.time()
        global_interval = self.control.config.rate.per_target_interval
        for index, (name, info) in enumerate(snapshot["targets"].items(), start=1):
            self._meta[index] = name
            state = "⏸" if info["paused"] else "✅"

            # 发送间隔：该群自己配了就用它，否则用全局的（标出来，避免"改了没反应"的困惑）
            interval = info.get("interval")
            if interval:
                interval_text = f"{interval[0]:g}~{interval[1]:g}s"
            else:
                interval_text = f"{global_interval[0]:g}~{global_interval[1]:g}s*"  # * = 用全局值

            # 群慢速（实测值，来自发送时的 SlowModeWaitError；没有说明没触发过）
            slow = sender.get("slow_mode_windows", {}).get(name)
            slow_text = f"慢速{int(slow)}s" if slow else "无慢速"

            # 按群的额度：已用/上限
            used = info.get("quota_used", 0)
            limit = info.get("quota_limit", 0)
            own = info.get("daily_limit", 0)
            quota = f"今日 {used}/{limit}"
            if own:
                quota += f"（本群限 {own}）"

            lines.append(
                f"{index}. {state} <code>{html.escape(name)}</code>\n"
                f"    发送间隔 {interval_text}｜{slow_text}｜{quota}｜积压 {info['backlog']}"
            )
        if not snapshot["targets"]:
            lines.append("（还没有目标群，用 /add 添加）")
        elif any(not info.get("interval") for info in snapshot["targets"].values()):
            lines.append("\n<i>* 表示该群没单独配发送间隔，用全局值</i>")

        if repost["enabled"]:
            stats = repost["stats"] or {}
            repost_interval = repost["interval"]
            interval_text = f"{repost_interval:g}s" if repost_interval else "默认"

            # 用"实际发帖速度"表达，比干巴巴的秒数直观得多
            rate_text = ""
            if repost_interval:
                per_hour = 3600 / repost_interval
                rate_text = f"≈ 每小时 {per_hour:.0f} 条"
                if per_hour >= 60:
                    rate_text += " ⚠️ 偏快"
                elif per_hour >= 30:
                    rate_text += " ⚠️"

            material_count = len(repost.get("materials", []))
            # 一轮要把所有素材各发一遍，算出"一轮至少多久"
            min_cycle = None
            intervals = [
                item.get("interval") for item in snapshot["targets"].values() if item.get("interval")
            ]
            if material_count and intervals:
                min_cycle = material_count * max(iv[0] for iv in intervals)

            lines += [
                "",
                "<b>定时重发</b>",
                f"素材 {material_count} 条｜<b>轮次间隔</b> {interval_text}"
                + (f"（{rate_text}）" if rate_text else ""),
                f"已跑 {stats.get('cycles', 0)} 轮｜成功 {stats.get('sent', 0)}｜"
                f"跳过 {stats.get('skipped', 0)}｜失败 {stats.get('failed', 0)}",
            ]
            if min_cycle is not None:
                lines.append(
                    f"<i>一轮要把 {material_count} 条素材各发一遍，至少需 {min_cycle:g}s</i>"
                )
            if min_cycle is not None and repost_interval and repost_interval < min_cycle:
                lines.append(
                    "⚠️ <i>轮次间隔比「一轮最少耗时」还短，会一轮接一轮不停发；"
                    "建议调到 ≥ 一轮最少耗时</i>"
                )

            # 按当前速度推算日额度还能撑多久 —— 这才是偏快节奏下真正的瓶颈。
            # 注意用 store["repost_ok_today"]（真实成功数）而不是 daily_counter.repost：
            # 后者是"预占+成功"各记一次的历史累积，数字会虚高（踩过）。
            limit = repost.get("daily_limit") or 0
            used = snapshot["store"].get("repost_ok_today", 0)
            if limit and repost_interval and used < limit:
                per_hour = 3600 / repost_interval
                if per_hour > 0:
                    hours_left = (limit - used) / per_hour
                    lines.append(
                        f"<i>剩 {limit - used} 条额度，按当前速度约 {hours_left:.1f} 小时后用满"
                        f"（用满后当天自动停止，不会超发）</i>"
                    )
            elif limit and used >= limit:
                lines.append("⚠️ <i>今日重发额度已用满，今天不会再发</i>")
        return "\n".join(lines)

    async def _cmd_targets(self, _: str) -> str:
        return await self._cmd_status("")

    async def _cmd_logs(self, argument: str) -> str:
        try:
            limit = max(1, min(int(argument or 15), 40))
        except ValueError:
            limit = 15
        lines = self.control.logs(limit)
        if not lines:
            return "日志为空"
        body = "\n".join(html.escape(line) for line in lines)
        return f"<b>最近 {len(lines)} 行</b>\n<pre>{body}</pre>"

    async def _cmd_where(self, _: str) -> str:
        config = self.control.config
        rate = config.rate
        snapshot = self.control.snapshot()
        own_limits = [
            f"{t.display}={t.daily_limit}" for t in config.targets if t.daily_limit
        ]
        return (
            "<b>关键参数</b>\n"
            f"源：<code>{html.escape(str(config.source))}</code>\n"
            f"目标：{len(config.targets)} 个\n"
            f"同群间隔：{rate.per_target_interval[0]:g}~{rate.per_target_interval[1]:g}s\n"
            f"跨群间隔：{rate.cross_target_delay[0]:g}~{rate.cross_target_delay[1]:g}s\n"
            f"全局上限：{self.control.sender.pacer.global_per_minute} 次/分钟\n"
            f"转发日总额度：{self.control.sender.pacer.daily_cap}\n"
            f"各群单独额度：{html.escape(', '.join(own_limits)) if own_limits else '（未设置，都用全局）'}\n"
            f"重发：{'开' if config.repost.enabled else '关'}，"
            f"间隔 {config.repost.interval}s，日额度 {config.repost.daily_limit}\n"
            f"素材：{list(config.repost.message_ids())}\n"
            f"Premium：{'是' if config.premium else '否'}"
        )

    # ---------------- 源频道 ----------------

    async def _cmd_sources(self, _: str) -> str:
        rows = self.control.sources_info()
        lines = ["<b>源频道</b>"]
        for index, item in enumerate(rows, start=1):
            mark = " ⭐主源" if item["主源"] else ""
            lines.append(f"{index}. <code>{html.escape(item['peer'])}</code>{mark}")
        lines.append(
            "\n主源用于：repost 素材、真发探测、历史补漏游标\n"
            "换源：<code>/source @新频道</code>"
        )
        return "\n".join(lines)

    async def _cmd_source(self, argument: str) -> str:
        if not argument:
            raise BotError("用法：/source @新频道（把主源换成它）；查看当前源用 /sources")
        result = await self.control.switch_source(argument)
        lines = [
            "✅ 主源已切换",
            f"  旧：<code>{html.escape(result['old'])}</code>",
            f"  新：<code>{html.escape(result['source'])}</code>（{html.escape(result['name'])}）",
        ]
        if result.get("materials_note"):
            lines.append(f"\n⚠️ {html.escape(result['materials_note'])}")
        elif result.get("materials_ok"):
            lines.append("\n（repost 素材在新源里仍然有效）")
        lines.append(
            "\n提示：换源后旧源仍在监听列表里。不需要了可以 "
            "<code>/delsource 旧源</code>；用 <code>/sources</code> 查看全部。"
        )
        return "\n".join(lines)

    async def _cmd_addsource(self, argument: str) -> str:
        if not argument:
            raise BotError("用法：/addsource @频道（多个源同时监听）")
        result = await self.control.add_source(argument)
        return (
            f"✅ 已加入源 <code>{html.escape(result['added'])}</code>"
            f"（{html.escape(result['name'])}）\n"
            f"当前共监听 {len(result['sources'])} 个源"
        )

    async def _cmd_delsource(self, argument: str) -> str:
        if not argument:
            raise BotError("用法：/delsource @频道")
        result = await self.control.remove_source(argument)
        return (
            f"✅ 已移除源 <code>{html.escape(result['removed'])}</code>\n"
            f"剩余 {len(result['sources'])} 个源"
        )

    async def _cmd_add(self, argument: str) -> str:
        if not argument:
            raise BotError("用法：/add @群用户名 [备注]")
        parts = argument.split(maxsplit=1)
        peer = parts[0]
        label = parts[1] if len(parts) > 1 else ""
        result = await self.control.add_target(peer, label)
        self._meta.clear()
        slow = result.get("slowmode")
        if slow:
            return (
                f"✅ 已加入 <code>{html.escape(result['label'])}</code>\n"
                f"该群慢速 {slow}s，发送间隔已自动对齐"
            )
        return f"✅ 已加入 <code>{html.escape(result['label'])}</code>（无慢速限制）"

    async def _resolve(self, argument: str) -> str:
        """把「序号」或「群名」解析成目标 key。序号过期时自动刷新一次。"""
        argument = (argument or "").strip()
        if not argument:
            raise BotError("请给出序号或目标名，先用 /targets 看列表")
        if not argument.isdigit():
            return argument

        index = int(argument)
        if index not in self._meta or time.time() - self._meta_at > 300:
            await self._cmd_status("")  # 刷新序号表
        if index not in self._meta:
            raise BotError(f"没有序号 {index}，共 {len(self._meta)} 个目标")
        return str(self._meta[index])

    async def _cmd_del(self, argument: str) -> str:
        key = await self._resolve(argument)
        result = await self.control.remove_target(key)
        self._meta.clear()
        return f"✅ 已移除 <code>{html.escape(result['label'])}</code>"

    async def _cmd_on(self, argument: str) -> str:
        key = await self._resolve(argument)
        self.control.set_target_paused(key, False)
        return f"▶️ <code>{html.escape(key)}</code> 已恢复"

    async def _cmd_off(self, argument: str) -> str:
        key = await self._resolve(argument)
        self.control.set_target_paused(key, True)
        return f"⏸ <code>{html.escape(key)}</code> 已暂停"

    async def _cmd_limit(self, argument: str) -> str:
        parts = argument.split()
        if len(parts) != 3:
            raise BotError("用法：/limit <序号> <秒下限> <秒上限>")
        key = await self._resolve(parts[0])
        try:
            low, high = float(parts[1]), float(parts[2])
        except ValueError as exc:
            raise BotError("间隔必须是数字") from exc
        self.control.set_target(key, interval=[low, high])
        return f"✅ <code>{html.escape(key)}</code> 间隔改为 {low:g}~{high:g}s"

    async def _cmd_check(self, argument: str) -> str:
        key = await self._resolve(argument)
        result = await self.control.check_target(key)
        slow = result.get("slowmode")
        return (
            f"<b>{html.escape(result['label'])}</b>\n"
            f"能否发言：{'✅ 可以' if result['can_post'] else '❌ 不行'}\n"
            f"原因：{html.escape(result['reason']) or '—'}\n"
            f"慢速模式：{f'{slow}s' if slow else '无'}"
        )

    async def _cmd_probe(self, argument: str) -> str:
        key = await self._resolve(argument)
        result = await self.control.probe_target(key)
        return (
            f"{'✅' if result['ok'] else '❌'} <code>{html.escape(result['label'])}</code>\n"
            f"{html.escape(result['note'])}"
        )

    # ---------------- 批量操作（多群管理） ----------------

    async def _cmd_onall(self, _: str) -> str:
        result = self.control.bulk_targets(action="resume")
        return f"▶️ 已恢复全部 {result['count']} 个目标"

    async def _cmd_offall(self, _: str) -> str:
        result = self.control.bulk_targets(action="pause")
        return f"⏸ 已暂停全部 {result['count']} 个目标"

    async def _cmd_quotas(self, argument: str) -> str:
        if not argument.strip().isdigit():
            raise BotError("用法：/quotas 50（给每个群各设 50 条/天；0 = 不限，用全局额度）")
        limit = int(argument)
        result = self.control.bulk_targets(action="set_limit", daily_limit=limit)
        if limit == 0:
            return f"✅ 已取消 {result['count']} 个群的单独额度，改用全局上限"
        return f"✅ 已给 {result['count']} 个群各设 {limit} 条/天"

    async def _cmd_share(self, argument: str) -> str:
        if not argument.strip().isdigit():
            raise BotError(
                "用法：/share 100（把 100 条平均分给所有群）\n"
                "适合「总额度固定、要公平分配」的场景"
            )
        total = int(argument)
        result = self.control.bulk_targets(action="share_limit", daily_limit=total)
        per = result["changed"][0]["daily_limit"] if result["changed"] else 0
        return f"✅ 总额度 {total} 已平均分给 {result['count']} 个群，每群 {per} 条/天"

    async def _cmd_everyone(self, argument: str) -> str:
        parts = argument.split()
        if len(parts) != 2:
            raise BotError("用法：/everyone 30 35（统一所有群的发送间隔为 30~35 秒）")
        try:
            low, high = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise BotError("间隔必须是数字") from exc
        result = self.control.bulk_targets(action="set_interval", interval=[low, high])
        return f"✅ 已把 {result['count']} 个群的发送间隔统一为 {low:g}~{high:g}s"

    async def _cmd_materials(self, _: str) -> str:
        repost = self.control.snapshot()["repost"]
        materials = repost.get("materials", [])
        if not materials:
            return "还没有素材，用 /addmat 6 7 8 添加"
        lines = ["<b>重发素材</b>"]
        for index, item in enumerate(materials, start=1):
            preview = html.escape(item["preview"] or "（非文本）")
            lines.append(f"{index}. <code>{item['msg_id']}</code> 已发 {item['posted']} 次\n   {preview}")
        lines.append(f"\n间隔 {repost['interval']}s｜日额度 {repost['daily_limit']}")
        return "\n".join(lines)

    async def _cmd_addmat(self, argument: str) -> str:
        ids = [int(token) for token in argument.replace(",", " ").split() if token.isdigit()]
        if not ids:
            raise BotError("用法：/addmat 6 7 8（覆盖式设置素材）")
        result = await self.control.set_materials(ids)
        return f"✅ 素材已设为 {result['ids']}（成功加载 {result['loaded']} 条）"

    async def _cmd_interval(self, argument: str) -> str:
        if not argument.strip().isdigit():
            raise BotError("用法：/interval 300（秒，必须 ≥ 31）")
        result = await self.control.set_repost_options(interval=float(argument))
        return f"✅ 重发间隔改为 {result['interval']:g}s"

    async def _cmd_dailylimit(self, argument: str) -> str:
        if not argument.strip().isdigit():
            raise BotError("用法：/dailylimit 200")
        result = await self.control.set_repost_options(daily_limit=int(argument))
        return f"✅ 重发日额度改为 {result['daily_limit']}"

    async def _cmd_run(self, _: str) -> str:
        result = await self.control.run_repost_once()
        return f"✅ 跑完一轮：成功 {result['sent']}，跳过/失败 {result['skipped_or_failed']}"

    async def _cmd_stop(self, _: str) -> str:
        self.control.stop_repost()
        return "⏹ 已停止重发循环，并把重发开关写成「关」——重启进程/重启服务器都不会自己又开始发。\n用 /repost on 可以重新启动。"

    async def _cmd_repost(self, argument: str) -> str:
        """开关定时重发。/stop 的逆操作。"""
        word = argument.strip().lower()
        if word in ("on", "start", "开", "启动"):
            result = await self.control.start_repost()
            return (
                f"▶️ 重发已启动：每轮间隔 {result['interval']:g}s，日额度 {result['daily_limit']}\n"
                "（已顺带清掉熔断和「禁止发言」的暂停状态；如果账号其实还在被限制，很快会再次熔断）"
            )
        if word in ("off", "stop", "关", "停止"):
            self.control.stop_repost()
            return "⏹ 重发已停止，开关已写盘（重启也不会自动开）"
        state = self.control.snapshot()["repost"]
        return (
            f"重发现在：{'运行中' if state['running'] else '已停止'}"
            f"（开关 {'开' if state['enabled'] else '关'}）\n"
            "用法：/repost on 启动，/repost off 停止"
        )

    async def _cmd_account(self, _: str) -> str:
        """问 @SpamBot：这个号有没有被 Telegram 限制。"""
        result = await self.control.check_account()
        icon = {True: "🚫", False: "✅", None: "❓"}[result["limited"]]
        detail = html.escape((result["detail"] or "")[:600])
        return f"{icon} <b>账号自检</b>\n{html.escape(result['message'])}\n\n<blockquote>{detail}</blockquote>"

    # ---------------- 主动通知 ----------------

    async def notify_admins(self, text: str) -> None:
        """把告警推给所有管理员（熔断、账号被限制这类事必须让人马上知道）。

        没有 client（bot 没启动）或没有管理员时静默返回：
        告警本身已经进了日志，不能因为推不出去就抛异常。
        """
        if self.client is None or not self.allowed:
            return
        for user_id in sorted(self.allowed):
            try:
                await self.client.send_message(user_id, text, parse_mode="html")
            except Exception as exc:
                log.warning("给 %s 推送告警失败：%s", user_id, exc)
