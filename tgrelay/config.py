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

"""配置加载与校验。

优先用 PyYAML；没装则退回到内置的极简 YAML 子集解析器
（支持嵌套映射、`- ` 列表、内联列表、注释），足够本项目使用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """配置有问题，信息里带字段路径，方便直接定位。"""


# --------------------------------------------------------------------------
# 极简 YAML 子集解析器（仅在缺少 PyYAML 时使用）
# --------------------------------------------------------------------------

_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"", "null", "~", "none"}


def _scalar(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        body = text[1:-1].strip()
        if not body:
            return []
        return [_scalar(item) for item in _split_inline(body)]
    lowered = text.lower()
    if lowered in _NULL:
        return None
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _split_inline(body: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == ",":
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return [item for item in (part.strip() for part in items) if item]


def _strip_comment(line: str) -> str:
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _tokenize(text: str) -> list[tuple[int, str]]:
    tokens: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        tokens.append((indent, stripped.strip()))
    return tokens


def _child_indent(tokens: list[tuple[int, str]], index: int, parent: int, path: str) -> int:
    """块内子节点的缩进以实际写入的为准（不假设父级 +1，容忍 2/4 空格）。"""
    if index >= len(tokens):
        return parent + 1
    indent = tokens[index][0]
    if indent <= parent:
        raise ConfigError(f"{path}: 期望缩进的子节点，实际没有缩进")
    return indent


def _parse_block(tokens: list[tuple[int, str]], index: int, indent: int, path: str) -> tuple[Any, int]:
    if index >= len(tokens):
        return None, index
    is_list = tokens[index][1].startswith("- ") or tokens[index][1] == "-"
    container: Any = [] if is_list else {}

    while index < len(tokens):
        line_indent, content = tokens[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ConfigError(f"{path}: 缩进异常 -> {content!r}")

        if is_list:
            if not (content.startswith("- ") or content == "-"):
                raise ConfigError(f"{path}: 列表与映射混用 -> {content!r}")
            item = content[1:].strip()
            index += 1
            if not item:
                child_indent = _child_indent(tokens, index, line_indent, f"{path}[{len(container)}]")
                child, index = _parse_block(tokens, index, child_indent, f"{path}[{len(container)}]")
                container.append(child)
            elif ":" in item and not item.startswith(("'", '"')):
                key, _, rest = item.partition(":")
                entry: dict[str, Any] = {}
                rest = rest.strip()
                if rest:
                    entry[key.strip()] = _scalar(rest)
                # `- id: 123` 后面还可以跟着同级的其它键（缩进比 "-" 更深）
                if index < len(tokens) and tokens[index][0] > line_indent:
                    child_indent = tokens[index][0]
                    extra, index = _parse_block(tokens, index, child_indent, f"{path}[{len(container)}]")
                    if isinstance(extra, dict):
                        entry.update(extra)
                container.append(entry)
            else:
                container.append(_scalar(item))
        else:
            if content.startswith("- "):
                raise ConfigError(f"{path}: 映射与列表混用 -> {content!r}")
            key, sep, rest = content.partition(":")
            if not sep:
                raise ConfigError(f"{path}: 缺少 ':' -> {content!r}")
            key = key.strip()
            child_path = f"{path}.{key}" if path else key
            rest = rest.strip()
            index += 1
            if rest:
                container[key] = _scalar(rest)
            else:
                child_indent = _child_indent(tokens, index, line_indent, child_path)
                child, index = _parse_block(tokens, index, child_indent, child_path)
                container[key] = {} if child is None else child
    return container, index


def load_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data: Any = None
    if not os.environ.get("TGRELAY_FORCE_MINIMAL_YAML"):
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
        except ImportError:
            data = None
    if data is None:
        data, _ = _parse_block(_tokenize(text), 0, 0, "")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: 顶层必须是映射")
    return data


# --------------------------------------------------------------------------
# 配置对象
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    id: int | str
    interval: tuple[float, float] | None = None
    label: str = ""
    # 该目标单独的每日发送上限。0 = 不单独限制，用全局 rate.daily_cap。
    # 多群时必须按群计，否则 N 个群共享一个总额度，会变成
    # "排在前面的群吃饱、后面的群饿着"。
    daily_limit: int = 0

    @property
    def display(self) -> str:
        return self.label or str(self.id)


@dataclass(frozen=True)
class Filters:
    keywords: tuple[str, ...] = ()
    keyword_mode: str = "any"
    exclude: tuple[str, ...] = ()
    regex: tuple[str, ...] = ()
    min_length: int = 0
    media: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rate:
    per_target_interval: tuple[float, float] = (3.0, 6.0)
    cross_target_delay: tuple[float, float] = (5.0, 15.0)
    global_per_minute: int = 20
    daily_cap: int = 200


@dataclass(frozen=True)
class Behavior:
    sync_edits: bool = False
    sync_deletes: bool = False
    catch_up_on_start: bool = True
    catch_up_limit: int = 100
    album_window: float = 0.6
    queue_size: int = 200
    drop_on_queue_full: bool = True
    forward_as_album: bool = True


@dataclass(frozen=True)
class Credentials:
    api_id: int
    api_hash: str
    session_path: str = "data/relay.session"
    session_string: str = ""


@dataclass(frozen=True)
class RepostConfig:
    """定时重发：把源里固定的一段消息循环重发到目标群。"""

    enabled: bool = False
    ids: tuple[int, ...] = ()
    ranges: tuple[tuple[int, int], ...] = ()
    interval: float | None = None      # 两轮之间的间隔（秒）；None = 用默认值
    daily_limit: int = 200             # 重发单独的每日额度，不挤占实时转发
    targets: tuple[str, ...] = ()      # 空 = 所有 targets；否则只发这些（id 或 label）
    shuffle: bool = False              # 每条素材发完就换下一条，还是每轮随机顺序
    run_mode: str = "loop"             # loop = 常驻循环；once = 只跑一轮

    def message_ids(self) -> tuple[int, ...]:
        """展开成完整的消息 ID 列表（按顺序、已去重）。"""
        out: list[int] = []
        for start, end in self.ranges:
            out.extend(range(start, end + 1))
        out.extend(self.ids)
        seen: set[int] = set()
        unique: list[int] = []
        for msg_id in out:
            if msg_id in seen:
                continue
            seen.add(msg_id)
            unique.append(msg_id)
        return tuple(unique)


@dataclass(frozen=True)
class AppConfig:
    sources: tuple[int | str, ...]
    targets: tuple[Target, ...]
    filters: Filters = field(default_factory=Filters)
    rate: Rate = field(default_factory=Rate)
    behavior: Behavior = field(default_factory=Behavior)
    repost: RepostConfig = field(default_factory=RepostConfig)
    premium: bool = False
    db_path: str = "data/relay.db"
    log_path: str = "data/relay.log"
    log_level: str = "INFO"
    credentials: Credentials | None = None
    proxy: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def source(self) -> int | str:
        """主源（用于补漏游标 / 重发素材）。"""
        return self.sources[0]


def parse_range_spec(text: Any, path: str) -> tuple[int, int]:
    """解析 "6-12" / "6..12" / "6~12" -> (6, 12)；单个数字 -> (n, n)。"""
    raw = str(text).strip().replace(" ", "")
    for separator in ("..", "-", "~", "—"):
        if separator in raw[1:]:  # 跳过可能的前导减号
            left, _, right = raw.partition(separator)
            try:
                start, end = int(left), int(right)
            except ValueError as exc:
                raise ConfigError(f"{path}: 无法解析范围 {text!r}，应形如 6-12") from exc
            if start <= 0 or end <= 0:
                raise ConfigError(f"{path}: 消息 ID 必须为正数 -> {text!r}")
            if end < start:
                raise ConfigError(f"{path}: 范围终点小于起点 -> {text!r}")
            return (start, end)
    try:
        single = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{path}: 无法解析 {text!r}，应形如 6 或 6-12") from exc
    if single <= 0:
        raise ConfigError(f"{path}: 消息 ID 必须为正数 -> {text!r}")
    return (single, single)


def _as_peer(value: Any, path: str) -> int | str:
    if isinstance(value, bool) or value is None:
        raise ConfigError(f"{path}: 应为频道/群的用户名或数字 ID")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        raise ConfigError(f"{path}: 不能为空")
    if text.startswith("@"):
        return text
    if text.lstrip("-").isdigit():
        return int(text)
    return text  # t.me 链接里的用户名


def _as_range(value: Any, path: str, *, integer: bool = False) -> tuple[float, float]:
    if value is None:
        raise ConfigError(f"{path}: 缺失")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low = high = float(value)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        low, high = float(value[0]), float(value[1])
    elif isinstance(value, (list, tuple)) and len(value) == 1:
        low = high = float(value[0])
    else:
        raise ConfigError(f"{path}: 应为数字或 [最小值, 最大值]，实际 {value!r}")
    if low < 0 or high < 0:
        raise ConfigError(f"{path}: 不能为负")
    if integer and (low != int(low) or high != int(high)):
        raise ConfigError(f"{path}: 需要整数")
    return (min(low, high), max(low, high))


def _as_str_list(value: Any, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for index, item in enumerate(value):
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            out.append(text)
        return tuple(out)
    raise ConfigError(f"{path}: 应为字符串或字符串列表")


def _as_bool(value: Any, path: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{path}: 应为 true/false，实际 {value!r}")


def _as_int(value: Any, path: str, default: int, *, minimum: int = 0) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: 应为整数，实际 {value!r}") from exc
    if number < minimum:
        raise ConfigError(f"{path}: 不能小于 {minimum}")
    return number


def _as_cap(value: Any, path: str, default: int) -> int:
    """日额度专用：0 = 不限（允许）。用于"完全交给各群自己的 daily_limit"的场景。"""
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: 应为整数（0 = 不限），实际 {value!r}") from exc
    if number < 0:
        raise ConfigError(f"{path}: 不能为负（0 = 不限）")
    return number


def _as_float(value: Any, path: str, default: float, *, minimum: float = 0.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: 应为数字，实际 {value!r}") from exc
    if number < minimum:
        raise ConfigError(f"{path}: 不能小于 {minimum}")
    return number


_MEDIA_KINDS = {"photo", "video", "document", "audio", "voice", "gif", "sticker", "poll"}


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"找不到配置文件：{config_path}")
    data = load_yaml(config_path)

    sources_raw = data.get("sources")
    if not sources_raw:
        raise ConfigError("sources: 至少配置一个源频道")
    if not isinstance(sources_raw, (list, tuple)):
        sources_raw = [sources_raw]
    sources = tuple(_as_peer(item, f"sources[{index}]") for index, item in enumerate(sources_raw))
    if not sources:
        raise ConfigError("sources: 至少配置一个源频道")

    targets_raw = data.get("targets")
    if not targets_raw:
        raise ConfigError("targets: 至少配置一个目标群")
    if not isinstance(targets_raw, (list, tuple)):
        targets_raw = [targets_raw]
    targets: list[Target] = []
    seen_targets: set[str] = set()
    for index, item in enumerate(targets_raw):
        path_prefix = f"targets[{index}]"
        if isinstance(item, dict):
            if "id" not in item:
                raise ConfigError(f"{path_prefix}: 缺少 id")
            target_id = _as_peer(item["id"], f"{path_prefix}.id")
            interval = (
                _as_range(item["interval"], f"{path_prefix}.interval")
                if item.get("interval") is not None
                else None
            )
            label = str(item.get("label") or "").strip()
            daily_limit = _as_int(
                item.get("daily_limit"), f"{path_prefix}.daily_limit", 0, minimum=0
            )
        else:
            target_id = _as_peer(item, path_prefix)
            interval = None
            label = ""
            daily_limit = 0
        key = str(target_id)
        if key in seen_targets:
            raise ConfigError(f"{path_prefix}: 目标重复 -> {target_id}")
        seen_targets.add(key)
        targets.append(
            Target(id=target_id, interval=interval, label=label, daily_limit=daily_limit)
        )
    if not targets:
        raise ConfigError("targets: 至少配置一个目标群")

    raw_filters = data.get("filters") or {}
    if not isinstance(raw_filters, dict):
        raise ConfigError("filters: 应为映射")
    keyword_mode = str(raw_filters.get("keyword_mode") or "any").strip().lower()
    if keyword_mode not in ("any", "all"):
        raise ConfigError("filters.keyword_mode: 只能是 any 或 all")
    media = tuple(item.lower() for item in _as_str_list(raw_filters.get("media"), "filters.media"))
    unknown_media = [item for item in media if item not in _MEDIA_KINDS]
    if unknown_media:
        raise ConfigError(f"filters.media: 不支持的类型 {unknown_media}，可选 {sorted(_MEDIA_KINDS)}")
    filters = Filters(
        keywords=_as_str_list(raw_filters.get("keywords"), "filters.keywords"),
        keyword_mode=keyword_mode,
        exclude=_as_str_list(raw_filters.get("exclude"), "filters.exclude"),
        regex=_as_str_list(raw_filters.get("regex"), "filters.regex"),
        min_length=_as_int(raw_filters.get("min_length"), "filters.min_length", 0),
        media=media,
    )

    raw_rate = data.get("rate") or {}
    if not isinstance(raw_rate, dict):
        raise ConfigError("rate: 应为映射")
    rate = Rate(
        per_target_interval=_as_range(raw_rate.get("per_target_interval", [3, 6]), "rate.per_target_interval"),
        cross_target_delay=_as_range(raw_rate.get("cross_target_delay", [5, 15]), "rate.cross_target_delay"),
        global_per_minute=_as_int(raw_rate.get("global_per_minute"), "rate.global_per_minute", 20, minimum=1),
        daily_cap=_as_cap(raw_rate.get("daily_cap"), "rate.daily_cap", 200),
    )

    raw_behavior = data.get("behavior") or {}
    if not isinstance(raw_behavior, dict):
        raise ConfigError("behavior: 应为映射")
    behavior = Behavior(
        sync_edits=_as_bool(raw_behavior.get("sync_edits"), "behavior.sync_edits", False),
        sync_deletes=_as_bool(raw_behavior.get("sync_deletes"), "behavior.sync_deletes", False),
        catch_up_on_start=_as_bool(raw_behavior.get("catch_up_on_start"), "behavior.catch_up_on_start", True),
        catch_up_limit=_as_int(raw_behavior.get("catch_up_limit"), "behavior.catch_up_limit", 100, minimum=0),
        album_window=_as_float(raw_behavior.get("album_window"), "behavior.album_window", 0.6, minimum=0.0),
        queue_size=_as_int(raw_behavior.get("queue_size"), "behavior.queue_size", 200, minimum=1),
        drop_on_queue_full=_as_bool(raw_behavior.get("drop_on_queue_full"), "behavior.drop_on_queue_full", True),
        forward_as_album=_as_bool(raw_behavior.get("forward_as_album"), "behavior.forward_as_album", True),
    )

    storage = data.get("storage") or {}
    if not isinstance(storage, dict):
        raise ConfigError("storage: 应为映射")

    premium = _as_bool(data.get("premium"), "premium", False)
    repost = _parse_repost(data.get("repost"), premium=premium)

    return AppConfig(
        sources=sources,
        targets=tuple(targets),
        filters=filters,
        rate=rate,
        behavior=behavior,
        repost=repost,
        premium=premium,
        db_path=str(storage.get("db_path") or "data/relay.db"),
        log_path=str(storage.get("log_path") or "data/relay.log"),
        log_level=str(storage.get("log_level") or "INFO"),
        raw=data,
    )


def _parse_repost(raw: Any, *, premium: bool) -> RepostConfig:
    if raw is None:
        return RepostConfig()
    if not isinstance(raw, dict):
        raise ConfigError("repost: 应为映射")

    ranges: list[tuple[int, int]] = []
    for index, item in enumerate(_as_str_list(raw.get("ranges"), "repost.ranges") or ()):
        ranges.append(parse_range_spec(item, f"repost.ranges[{index}]"))

    ids: list[int] = []
    for index, item in enumerate(raw.get("ids") or ()):
        start, end = parse_range_spec(item, f"repost.ids[{index}]")
        if start != end:
            raise ConfigError(
                f"repost.ids[{index}]: ids 里只放单条({item!r})，区间请写进 repost.ranges"
            )
        ids.append(start)

    interval: float | None = None
    if raw.get("interval") is not None:
        interval = float(_as_range(raw["interval"], "repost.interval", integer=True)[0])

    run_mode = str(raw.get("run_mode") or "loop").strip().lower()
    if run_mode not in ("loop", "once"):
        raise ConfigError("repost.run_mode: 只能是 loop 或 once")

    enabled = _as_bool(raw.get("enabled"), "repost.enabled", False)
    per_day = 2880 if not premium else 2880 * 4  # 30s 慢速群的理论上限
    daily_limit = _as_int(raw.get("daily_limit"), "repost.daily_limit", 200, minimum=1)

    if enabled:
        if interval is not None and interval < 31:
            raise ConfigError(
                f"repost.interval={interval:g} 小于 31 秒。目标群慢速模式限制为每条至少间隔 30 秒，"
                "间隔设得比它短只会让每条都撞墙白等。请设为 >= 31"
            )
        if daily_limit > per_day:
            raise ConfigError(
                f"repost.daily_limit={daily_limit} 超过目标群慢速 30s 的理论上限（{per_day} 条/天），"
                "配额永远用不完，请调小"
            )

    targets = _as_str_list(raw.get("targets"), "repost.targets")
    return RepostConfig(
        enabled=enabled,
        ids=tuple(ids),
        ranges=tuple(ranges),
        interval=interval,
        daily_limit=daily_limit,
        targets=targets,
        shuffle=_as_bool(raw.get("shuffle"), "repost.shuffle", False),
        run_mode=run_mode,
    )


def apply_env(config: AppConfig, env: dict[str, str]) -> AppConfig:
    """把 .env / 环境变量合并进来（.env 优先于 config.yaml 的默认值）。"""
    creds = config.credentials
    api_id_raw = (env.get("TG_API_ID") or "").strip()
    api_hash = (env.get("TG_API_HASH") or "").strip()
    session_string = (env.get("TG_SESSION_STRING") or "").strip()
    session_path = (env.get("TG_SESSION") or "data/relay.session").strip() or "data/relay.session"
    premium = config.premium
    premium_raw = (env.get("TG_PREMIUM") or "").strip()
    if premium_raw:
        premium = _as_bool(premium_raw, "TG_PREMIUM", premium)

    from .proxy import parse_proxy  # 局部导入，避免循环依赖

    try:
        proxy = parse_proxy(env)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    if api_id_raw or api_hash:
        if not api_id_raw or not api_hash:
            raise ConfigError("TG_API_ID 与 TG_API_HASH 必须同时提供")
        try:
            api_id = int(api_id_raw)
        except ValueError as exc:
            raise ConfigError(f"TG_API_ID 必须是数字，实际 {api_id_raw!r}") from exc
        creds = Credentials(
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
            session_string=session_string,
        )
    return replace(config, credentials=creds, premium=premium, proxy=proxy)


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """极简 .env 读取（KEY=VALUE，# 注释，可选引号）。不污染 os.environ。"""
    env: dict[str, str] = {}
    dotenv = Path(path)
    if not dotenv.exists():
        return env
    for raw in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            env[key] = value
    return env
