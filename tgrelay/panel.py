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

"""SSH 里直接看的文字面板（TUI）。

**为什么是"HTTP 客户端"而不是直接操作引擎**

    一个 session 只能被一个进程持有。面板要是自己起一个 TelegramClient，
    两个进程用同一份 auth key 会让它作废、账号被强制登出（这个项目里
    反复强调的红线）。所以面板：

      * 优先走**本机**的面板 API（`--web` 那套，127.0.0.1:8123），
        读 `/api/stats`、`/api/logs`，动作也走它 —— 控制面只有一份，
        网页和文字面板看到的状态永远一致；
      * 拿不到 API（没开 `--web`）就退化成**只读模式**：读 config.yaml、
        SQLite 和 systemd，只显示不给操作，并在界面上说清怎么开启可操作模式。

    宁可用不了按钮，也不能为了一个界面把账号搞掉线。

**关于中文字宽**

    TUI 里最容易翻车的就是对齐：中文字符占 **2 列**，日文/韩文也是，
    组合字符占 0 列。用 `len()` 去 padding，只要一行里有中文，
    后面所有列都会错位。所以这里自己实现了 `display_width` / `pad` /
    `truncate`，一律按"显示宽度"算。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

DEFAULT_PORT = 8123
DEFAULT_HOST = "127.0.0.1"
REFRESH_SECONDS = 5.0
DEFAULT_LOG_LINES = 12


# --------------------------------------------------------------------------
# 显示宽度：中文占 2 列，别用 len()
# --------------------------------------------------------------------------


def char_width(char: str) -> int:
    """单个字符占几列。"""
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    # 控制字符不该出现在界面上；真出现了也当 0 列，免得把排版撑坏
    if ord(char) < 32 or ord(char) == 0x7F:
        return 0
    return 1


def display_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，超长时补省略号（省略号本身也占宽度）。"""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    budget = width - display_width(ellipsis)
    if budget <= 0:
        return ellipsis[:1]
    out = []
    used = 0
    for char in text:
        size = char_width(char)
        if used + size > budget:
            break
        out.append(char)
        used += size
    return "".join(out) + ellipsis


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补齐/截断，保证这一格正好 width 列。"""
    text = truncate(text, width)
    space = width - display_width(text)
    if space <= 0:
        return text
    if align == "right":
        return " " * space + text
    if align == "center":
        left = space // 2
        return " " * left + text + " " * (space - left)
    return text + " " * space


def fit_line(text: str, width: int) -> str:
    """把一行撑满到指定宽度（用于画框）。"""
    return pad(text, width)


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------


@dataclass
class PanelState:
    """面板要显示的一切。两个来源（API / 只读兜底）都归一到这个结构。"""

    source: str = "readonly"          # api | readonly
    reachable: bool = False           # API 是否可用（决定能不能操作）
    error: str = ""
    fetched_at: float = 0.0

    service: str = "unknown"          # active / inactive / failed
    service_uptime: str = ""

    stats: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ---- 取状态的便捷读法（都在这里集中处理缺失字段）----

    @property
    def mode(self) -> str:
        repost = self.stats.get("repost") or {}
        if repost.get("enabled"):
            return "定时重发"
        return "实时转发" if not repost else "空闲（重发关闭）"

    @property
    def repost_running(self) -> bool:
        return bool((self.stats.get("repost") or {}).get("running"))

    @property
    def targets(self) -> list[tuple[str, dict[str, Any]]]:
        return sorted((self.stats.get("targets") or {}).items())

    @property
    def breaker(self) -> str:
        return ((self.stats.get("account") or {}).get("breaker") or "")

    @property
    def blocked(self) -> dict[str, Any]:
        return (self.stats.get("account") or {}).get("write_forbidden") or {}

    @property
    def risks(self) -> list[dict[str, Any]]:
        return (self.stats.get("risk") or {}).get("items") or []

    @property
    def alerts(self) -> list[dict[str, Any]]:
        return (self.stats.get("account") or {}).get("alerts") or []


# --------------------------------------------------------------------------
# 从本机 API 取状态
# --------------------------------------------------------------------------


def http_get(url: str, token: str, timeout: float = 6.0) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "tg-relay-panel",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode(errors="replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:200]
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def http_post(url: str, token: str, payload: dict | None = None, timeout: float = 30.0) -> tuple[int, Any]:
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "tg-relay-panel",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode(errors="replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:300]
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def service_state(name: str = "tg-relay") -> tuple[str, str]:
    """服务状态与运行时长。拿不到就返回 unknown（面板不该因为 systemd 不在就白屏）。"""
    if not shutil.which("systemctl"):
        return "unknown", ""
    try:
        state = subprocess.run(
            ["systemctl", "is-active", name], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        return "unknown", ""
    uptime = ""
    try:
        since = subprocess.run(
            ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", name],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if since and state == "active":
            uptime = since
    except Exception:
        pass
    return state or "unknown", uptime


def fetch_state(
    token: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    log_lines: int = DEFAULT_LOG_LINES,
    service: str = "tg-relay",
) -> PanelState:
    """走本机 API 取状态（可操作模式）。"""
    state = PanelState(source="api", fetched_at=time.time())
    state.service, state.service_uptime = service_state(service)

    base = f"http://{host}:{port}"
    status, stats = http_get(f"{base}/api/stats", token)
    if status != 200 or not isinstance(stats, dict):
        state.reachable = False
        state.error = (
            f"读不到本机面板 API（{base}）：{stats}"
            if status
            else f"连不上 {base} —— {stats}"
        )
        return state

    state.reachable = True
    state.stats = stats

    status, logs = http_get(f"{base}/api/logs?limit={log_lines}", token)
    if status == 200 and isinstance(logs, dict):
        state.logs = list(logs.get("lines") or [])
    elif status == 200 and isinstance(logs, list):
        state.logs = [str(item) for item in logs]
    return state


# --------------------------------------------------------------------------
# 只读兜底：没开 --web 时，靠本地文件也能看出个大概
# --------------------------------------------------------------------------


def readonly_state(
    config_path: Path,
    db_path: Path | None = None,
    *,
    service: str = "tg-relay",
    log_lines: int = DEFAULT_LOG_LINES,
) -> PanelState:
    state = PanelState(source="readonly", fetched_at=time.time())
    state.service, state.service_uptime = service_state(service)
    state.notes.append("只读模式：服务没开 --web，看不到实时状态，也给不了操作。")

    try:
        from .config import load_config

        config = load_config(config_path)
    except Exception as exc:
        state.error = f"读配置失败：{type(exc).__name__}: {exc}"
        return state

    repost = config.repost
    targets = []
    for target in config.targets:
        interval = target.interval or config.rate.per_target_interval
        targets.append(
            (
                target.display,
                {
                    "id": str(target.id),
                    "interval": list(interval) if interval else None,
                    "daily_limit": target.daily_limit,
                    "paused": False,
                    "backlog": 0,
                    "quota_used": 0,
                    "quota_limit": 0,
                },
            )
        )
    state.stats = {
        "config": {"sources": [str(item) for item in config.sources]},
        "repost": {
            "enabled": repost.enabled,
            "running": False,
            "interval": repost.interval,
            "daily_limit": repost.daily_limit,
            "shuffle": repost.shuffle,
            "message_ids": list(repost.message_ids()),
            "materials": [],
            "materials_loaded": False,
        },
        "targets": dict(targets),
        "store": _db_counters(db_path or Path(config.db_path)),
        "sender": {},
        "account": {},
        "risk": {},
    }
    return state


def _db_counters(db_path: Path) -> dict[str, Any]:
    """从 SQLite 里捞几个今天的关键数字（打不开就返回空）。"""
    import sqlite3

    if not db_path.exists():
        return {}
    try:
        from .db import Store

        store = Store(str(db_path))
        try:
            return {
                "sent_today": store.sent_today(),
                "reposted_today": store.reposted_today(),
                "sent_ok_today": store.sent_ok_today(),
                "repost_ok_today": store.sent_ok_today(as_repost=True),
                "stats": store.stats(),
            }
        finally:
            store.close()
    except Exception:
        return {}


# --------------------------------------------------------------------------
# 渲染：纯函数，好测
# --------------------------------------------------------------------------

BOX = {"tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│", "ml": "├", "mr": "┤"}


def _rule(width: int, title: str = "", left: str = "├", right: str = "┤") -> str:
    """`├─ 标题 ─────┤` 这种分隔线。

    宽度必须精确等于 width，否则框线会豁口（第一版就差了 1 列：
    left + 一个 h + label + body + right = width + 1，测试直接抓出来了）。
    所以有标题时 body = width - 3 - label 宽度。
    """
    if not title:
        return left + BOX["h"] * max(0, width - 2) + right
    label = f" {title} "
    body = BOX["h"] * max(0, width - 3 - display_width(label))
    line = f"{left}{BOX['h']}{label}{body}{right}"
    return line if display_width(line) <= width else truncate(line, width)


def render(state: PanelState, *, width: int = 100, height: int = 30, selected: int = 0,
           status_line: str = "") -> list[str]:
    """把一个 PanelState 画成若干行纯文本。不依赖 curses，方便单测。"""
    width = max(60, width)
    lines: list[str] = []

    # ---- 标题栏 ----
    title = "tg-relay"
    if state.reachable:
        tag = "可操作"
    else:
        tag = "只读"
    clock = time.strftime("%H:%M:%S", time.localtime(state.fetched_at or time.time()))
    head = f" {title}  [{tag}] "
    right = f" {clock} "
    lines.append(
        BOX["tl"] + head + BOX["h"] * max(0, width - 2 - display_width(head) - display_width(right))
        + right + BOX["tr"]
    )

    # ---- 概况 ----
    svc = state.service
    svc_flag = "✅" if svc == "active" else ("⏸" if svc == "inactive" else "❌")
    stats = state.stats
    store = stats.get("store") or {}
    sender = stats.get("sender") or {}
    repost = stats.get("repost") or {}

    mode = state.mode
    if repost.get("running"):
        mode += "（循环运行中）"
    elif repost.get("enabled"):
        mode += "（循环停着）"

    account = "正常"
    if state.breaker:
        account = "⚠️ 已熔断"
    elif state.blocked:
        account = f"⚠️ {len(state.blocked)} 个群停发中"

    lines.append(_rule(width, "概况"))
    lines.append(BOX["v"] + fit_line(f" 服务 {svc_flag} {svc}", width - 2) + BOX["v"])
    lines.append(BOX["v"] + fit_line(f" 模式 {mode}", width - 2) + BOX["v"])
    lines.append(BOX["v"] + fit_line(f" 账号 {account}", width - 2) + BOX["v"])

    today = (
        f" 今日重发 额度占用 {store.get('reposted_today', '?')}/{repost.get('daily_limit', '?')}"
        f"  实发 {store.get('repost_ok_today', '?')}"
    )
    lines.append(BOX["v"] + fit_line(today, width - 2) + BOX["v"])
    counters = (
        f" 累计 已发 {sender.get('sent', '?')}  失败 {sender.get('failed', '?')}"
        f"  跳过 {sender.get('skipped', '?')}  慢速等待 {sender.get('slow_waits', '?')}"
    )
    lines.append(BOX["v"] + fit_line(counters, width - 2) + BOX["v"])

    sources = (stats.get("config") or {}).get("sources") or []
    if sources:
        lines.append(BOX["v"] + fit_line(f" 源 {', '.join(str(s) for s in sources)}", width - 2) + BOX["v"])

    # ---- 目标群 ----
    lines.append(_rule(width, "目标群"))
    header = (
        f" {'#':<3}{pad('群', 18)}{pad('状态', 8)}{pad('发送间隔', 12)}"
        f"{pad('慢速', 8)}{pad('今日额度', 14)}{pad('积压', 6)}"
    )
    lines.append(BOX["v"] + fit_line(header, width - 2) + BOX["v"])
    targets = state.targets
    if not targets:
        lines.append(BOX["v"] + fit_line("  （还没有目标群）", width - 2) + BOX["v"])
    for index, (name, info) in enumerate(targets):
        mark = "▶" if index == selected else " "
        paused = bool(info.get("paused"))
        status = "⏸暂停" if paused else "✅正常"
        interval = info.get("interval")
        iv = f"{interval[0]:g}~{interval[1]:g}s" if interval else "—"
        slow = (sender.get("slow_mode_windows") or {}).get(name)
        slow_text = f"{int(slow)}s" if slow else "—"
        own = info.get("daily_limit") or 0
        quota = f"{info.get('quota_used', 0)}/{info.get('quota_limit', 0)}"
        if own:
            quota += f"(限{own})"
        row = (
            f"{mark}{index + 1:<3}{pad(name, 18)}{pad(status, 8)}{pad(iv, 12)}"
            f"{pad(slow_text, 8)}{pad(quota, 14)}{pad(str(info.get('backlog', 0)), 6)}"
        )
        lines.append(BOX["v"] + fit_line(row, width - 2) + BOX["v"])

    # ---- 发送节奏体检 ----
    risks = state.risks
    if risks:
        lines.append(_rule(width, "发送节奏体检"))
        for item in risks[:4]:
            icon = "🔴" if item.get("level") == "danger" else "🟡"
            lines.append(
                BOX["v"] + fit_line(f" {icon} {item.get('title', '')}", width - 2) + BOX["v"]
            )
            lines.append(
                BOX["v"] + fit_line(f"    {item.get('detail', '')}", width - 2) + BOX["v"]
            )

    # ---- 告警 ----
    alerts = state.alerts
    if state.breaker or alerts or state.blocked or state.error:
        lines.append(_rule(width, "告警"))
        if state.error:
            lines.append(BOX["v"] + fit_line(f" ⚠️ {state.error}", width - 2) + BOX["v"])
        if state.breaker:
            lines.append(BOX["v"] + fit_line(f" 🔴 熔断：{state.breaker}", width - 2) + BOX["v"])
        for name, seconds in (state.blocked or {}).items():
            lines.append(
                BOX["v"] + fit_line(f" 🟡 {name} 因「禁止发言」停发中，约 {int(seconds)} 秒后解禁", width - 2)
                + BOX["v"]
            )
        for item in alerts[:3]:
            icon = "🔴" if item.get("level") == "critical" else "🟡"
            first = (item.get("text") or "").splitlines()[0]
            lines.append(BOX["v"] + fit_line(f" {icon} {first}", width - 2) + BOX["v"])

    for note in state.notes:
        lines.append(BOX["v"] + fit_line(f" ℹ️ {note}", width - 2) + BOX["v"])

    # ---- 日志 ----
    if state.logs:
        lines.append(_rule(width, "最近日志"))
        for line in state.logs:
            lines.append(BOX["v"] + fit_line(" " + line, width - 2) + BOX["v"])

    # ---- 底部按键 ----
    lines.append(_rule(width))
    if status_line:
        lines.append(BOX["v"] + fit_line(f" {status_line}", width - 2) + BOX["v"])
    if state.reachable:
        keys = " r 刷新   s 启停重发   1-9 选群   p 暂停/恢复   a 账号自检   l 日志   q 退出"
    else:
        keys = " r 刷新   q 退出          （只读模式：服务加 --web 才能操作）"
    lines.append(BOX["v"] + fit_line(keys, width - 2) + BOX["v"])
    lines.append(BOX["bl"] + BOX["h"] * (width - 2) + BOX["br"])

    # 超出高度时保留头部、丢掉中间的日志（信息量最大的在上面）
    if len(lines) > height:
        keep_head = max(1, height - 1)
        lines = lines[:keep_head] + [lines[-1]]
    return lines


# --------------------------------------------------------------------------
# curses 外壳
# --------------------------------------------------------------------------


def _discover_env(name: str, service: str = "tg-relay", project_dir: Path | None = None) -> str:
    """从 环境变量 / systemd（含 drop-in）/ 项目 .env 里找一个变量。

    为什么必须查 systemd：真实部署里令牌经常是放在
    `/etc/systemd/system/<service>.service.d/*.conf` 里的（用 drop-in 注入密钥
    很常见），`.env` 里反而没有。只查 .env 会得出"没有令牌"的错误结论
    （真踩过：一键管理脚本的账号自检因此一直说"没找到面板令牌"）。
    """
    value = (os.environ.get(name) or "").strip()
    if value:
        return value

    # systemd drop-in（按服务名找）
    for conf in sorted(Path("/etc/systemd/system").glob(f"{service}.service.d/*.conf")):
        try:
            text = conf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(rf"{name}=([^\s\"']+)", text)
        if match:
            return match.group(1).strip('"')

    # systemd 合并后的环境（覆盖 Environment= 直接写在主 unit 里的情况）
    if shutil.which("systemctl"):
        try:
            text = subprocess.run(
                ["systemctl", "show", "-p", "Environment", "--value", service],
                capture_output=True, text=True, timeout=5,
            ).stdout
            match = re.search(rf"{name}=([^\s\"']+)", text)
            if match:
                return match.group(1).strip('"')
        except Exception:
            pass

    if project_dir is not None:
        env_file = project_dir / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _discover_token(explicit: str | None, project_dir: Path, service: str = "tg-relay") -> str:
    """面板令牌：参数 > 环境变量 > systemd > .env。"""
    if explicit and explicit.strip():
        return explicit.strip()
    return _discover_env("TG_WEB_TOKEN", service, project_dir)


def _discover_port(explicit: int | None, service: str = "tg-relay") -> int:
    """没显式给端口就去 systemd 的 ExecStart 里找（服务器上就是这么起的）。"""
    if explicit:
        return explicit
    if shutil.which("systemctl"):
        try:
            text = subprocess.run(
                ["systemctl", "show", "-p", "ExecStart", "--value", service],
                capture_output=True, text=True, timeout=5,
            ).stdout
            match = re.search(r"--web-port[=\s]+(\d+)", text)
            if match:
                return int(match.group(1))
        except Exception:
            pass
    return DEFAULT_PORT


def run_panel(
    *,
    token: str = "",
    port: int | None = None,
    host: str = DEFAULT_HOST,
    config_path: Path = Path("config.yaml"),
    db_path: Path | None = None,
    service: str = "tg-relay",
    refresh: float = REFRESH_SECONDS,
) -> int:
    """curses 主循环。

    两个"环境不配合"的兜底：
      * 没有 curses（Windows 原生 Python 就没有）→ 打印一屏；
      * stdout 不是终端（`ssh host 'python -m tgrelay --panel' > out.txt`、
        或者管道给 less）→ 也打印一屏，不然 curses 会直接报错退出。
    这两种情况都只打印，仍然**不连 Telegram**。
    """
    def print_once() -> int:
        state = _load_state(token, host, port, config_path, db_path, service)
        width = shutil.get_terminal_size((100, 40)).columns
        for line in render(state, width=width, height=shutil.get_terminal_size((100, 40)).lines):
            print(line)
        return 0

    try:
        import curses  # noqa: F401
    except ImportError:
        print_once()
        print("\n（这个 Python 没有 curses，只打印一屏；Linux 上的 Python 才有完整交互）")
        return 0

    if not sys.stdout.isatty():
        return print_once()

    import curses

    log_lines = DEFAULT_LOG_LINES
    selected = 0
    status_line = ""

    def draw(screen: Any) -> None:
        nonlocal selected, status_line
        screen.erase()
        height, width = screen.getmaxyx()
        state = _load_state(token, host, port, config_path, db_path, service, log_lines)
        if selected >= len(state.targets):
            selected = max(0, len(state.targets) - 1)
        for index, line in enumerate(render(state, width=width, height=height - 1,
                                            selected=selected, status_line=status_line)):
            if index >= height - 1:
                break
            try:
                screen.addstr(index, 0, line[: max(0, width - 1)])
            except curses.error:
                pass
        screen.refresh()

    def reload_state() -> PanelState:
        return _load_state(token, host, port, config_path, db_path, service, log_lines)

    def loop(screen: Any) -> int:
        nonlocal log_lines, selected, status_line
        curses.curs_set(0)
        screen.nodelay(True)
        screen.timeout(int(refresh * 1000))
        last = 0.0
        while True:
            now = time.time()
            if now - last >= 0.05:
                draw(screen)
                last = now
            try:
                key = screen.getch()
            except curses.error:
                continue
            if key == -1:
                continue
            char = chr(key) if 0 <= key < 256 else ""
            if char in ("q", "Q", "\x1b"):
                return 0
            if char == "r":
                status_line = "已刷新"
                continue
            if char == "l":
                log_lines = 24 if log_lines == DEFAULT_LOG_LINES else DEFAULT_LOG_LINES
                status_line = f"日志行数 {log_lines}"
                continue
            if char.isdigit() and char != "0":
                selected = int(char) - 1
                continue
            if char == "s":
                state = reload_state()
                if not state.reachable:
                    status_line = "只读模式，操作不了（服务需要带 --web 启动）"
                    continue
                if state.repost_running:
                    code, body = http_post(f"http://{host}:{port}/api/repost/stop", token)
                    status_line = "已停止重发（开关也写盘了）" if code == 200 else f"失败：{body}"
                else:
                    code, body = http_post(f"http://{host}:{port}/api/repost/start", token)
                    status_line = (
                        f"已启动重发" if code == 200 else f"失败：{body}"
                    )
                continue
            if char == "p":
                state = reload_state()
                if not state.reachable or not state.targets:
                    status_line = "只读模式或没有目标群"
                    continue
                name, info = state.targets[min(selected, len(state.targets) - 1)]
                action = "resume" if info.get("paused") else "pause"
                code, body = http_post(f"http://{host}:{port}/api/targets/{name}/{action}", token)
                status_line = f"{name} 已{'恢复' if action == 'resume' else '暂停'}" if code == 200 else f"失败：{body}"
                continue
            if char == "a":
                if not token:
                    status_line = "没有令牌，问不了 @SpamBot"
                    continue
                status_line = "正在问 @SpamBot…（要几秒）"
                draw(screen)
                code, body = http_post(f"http://{host}:{port}/api/account/check", token)
                if code == 200 and isinstance(body, dict):
                    status_line = f"账号自检：{body.get('message', '')}"
                else:
                    status_line = f"账号自检失败：{body}"
                continue
        return 0

    try:
        return int(curses.wrapper(loop) or 0)
    except KeyboardInterrupt:
        return 0


def _load_state(
    token: str,
    host: str,
    port: int | None,
    config_path: Path,
    db_path: Path | None,
    service: str,
    log_lines: int = DEFAULT_LOG_LINES,
) -> PanelState:
    resolved_port = _discover_port(port, service)
    if token:
        state = fetch_state(token, host=host, port=resolved_port, log_lines=log_lines, service=service)
        if state.reachable:
            return state
    else:
        state = None  # type: ignore[assignment]
    fallback = readonly_state(config_path, db_path, service=service, log_lines=log_lines)
    if token and state is not None:
        fallback.error = state.error
        fallback.service = state.service or fallback.service
    return fallback


def main(args: Any) -> int:
    """`python -m tgrelay --panel` 的入口。

    **注意：这里绝不连 Telegram。** 服务器上的服务已经是 session 的唯一持有者，
    面板再建一个 client 会让 auth key 作废、账号被强制登出。
    """
    config_path = Path(getattr(args, "config", "config.yaml"))
    project_dir = config_path.parent if str(config_path.parent) != "" else Path(".")
    service = getattr(args, "service", "tg-relay")
    token = _discover_token(getattr(args, "web_token", None), project_dir, service)
    port = getattr(args, "web_port", None)

    if not token:
        print("提示：没找到面板令牌（TG_WEB_TOKEN / systemd / .env），将进入只读模式。")
        print("      想让面板能操作，服务要带 --web 启动，并让令牌可被读到。")
    return run_panel(
        token=token,
        port=port,
        host=getattr(args, "web_host", DEFAULT_HOST) or DEFAULT_HOST,
        config_path=config_path,
        service=service,
    )
