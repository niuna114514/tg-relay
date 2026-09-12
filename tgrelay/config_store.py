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

"""把运行期的配置改动写回 config.yaml。

设计取舍：**保留用户手写的注释**。
所以不整体重写文件，而是按"顶层段落"做局部替换——
只重新生成 `targets` 和 `repost` 这两段（它们由网页/命令托管），
其余段落（含注释）原样保留。

写完会立刻用 load_config 复读一遍校验，配置读不回来就不落盘。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .config import AppConfig, RepostConfig, Target, load_config

log = logging.getLogger("tgrelay.config_store")

MANAGED = ("targets", "repost", "rate")


class ConfigStoreError(Exception):
    pass


# YAML 里不能作为"裸标量开头"的字符：
#   @ 和 ` 是保留字符（YAML 规范里明确禁止裸用），
#   ! & * ? | > % 是各种指示符，- : 后面跟空格会变成序列/映射，
#   # 会开始注释，[ ] { } , 是流式结构。
_NEEDS_QUOTE_START = set("@`!&*?|>%#[]{},-:")
# 这些字符出现在值里就必须加引号：
#   : # [ ] { } ,  -> YAML 结构符
#   \ 和 "         -> 在双引号字符串里是转义符，不加引号会被原样保留、加了才安全
#   换行/制表       -> 会破坏单行标量
_NEEDS_QUOTE_ANY = set(":#[]{},\n\r\t\"'\\")


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        # 注意：绝不能用 :g —— 大整数会被写成科学计数法（-1002001 -> -1.002e+06），
        # 解析回来就不是原来那个 ID 了。
        return str(value)
    if isinstance(value, float):
        return f"{value:g}" if value != int(value) else str(int(value))
    text = str(value)
    if not text:
        return '""'
    # 开头是保留字符，或内部含特殊字符，或首尾有空白 —— 一律加引号。
    # 漏掉 @ 会导致 @username 这类目标写回 config.yaml 后解析失败（真实踩过）。
    if (
        text[0] in _NEEDS_QUOTE_START
        or any(ch in _NEEDS_QUOTE_ANY for ch in text)
        or text != text.strip()
    ):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _header_comments(lines: list[str], *, own: tuple[str, ...] = ()) -> list[str]:
    """取出段块里的头部注释（用于保留用户手写说明）。

    `_split_sections` 已经把紧贴键上方的注释放进了这个块的开头。

    ⚠️ 必须**排除本程序自己生成过的注释**，否则每次写回都会把上一次生成的
    注释再抄一遍，文件里会越堆越多重复行（真实踩过：累积到 4 份）。
    """
    header: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if any(marker in line for marker in own):
                continue  # 这是程序自己写的，丢掉
            header.append(line)
        elif stripped == "":
            continue
        else:
            break  # 遇到键行本身
    return header[:5]


def _split_sections(text: str) -> list[tuple[str, list[str]]]:
    """把 yaml 切成 [(顶层键, [行...])]，顶层键为空串表示文件头。

    关键细节：**紧跟在"下一个顶层键"上方的注释行，要归给那个键**，
    而不是留给上一段。否则像这样：

        rate:
          daily_cap: 100
        # ==== repost 的说明 ====
        repost:

    那些说明会被算进 `rate` 段。替换 `repost` 段时它们被留在原地，
    下次生成又加一份 —— 注释就累积了（真实踩过：出现了 4 份）。

    规则：注释/空行先攒着；
      * 后面紧跟一个键 -> 归那个键（这一段的"头部注释"）
      * 后面是普通内容行 -> 归上一段（段内注释）
    """
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = [("", [])]
    pending: list[str] = []

    def flush_to_current() -> None:
        if pending:
            sections[-1][1].extend(pending)
            pending.clear()

    for line in lines:
        match = re.match(r"^([A-Za-z_][\w-]*):", line)
        if match:
            # pending 是给这个键的头部注释，跟着键一起进新段
            sections.append((match.group(1), pending + [line]))
            pending = []
        elif line.strip().startswith("#") or line.strip() == "":
            pending.append(line)
        else:
            flush_to_current()
            sections[-1][1].append(line)

    flush_to_current()
    return sections


# 本程序自己生成的注释里都会带这些标记，写回时用来识别并丢弃，
# 避免"程序注释 → 又被当成用户注释抄一遍"的重复累积
GENERATED_MARKERS = (
    "由程序重写",
    "由网页面板 / 命令维护",
)


def _render_sources(sources: Sequence[Any], original: list[str]) -> list[str]:
    out = _header_comments(original, own=GENERATED_MARKERS)
    out.append("sources:")
    for item in sources:
        out.append(f"  - {_fmt(item)}")
    if not sources:
        out.append("  []")
    return out


def _render_targets(targets: Sequence[Target], original: list[str]) -> list[str]:
    # 保留用户写在 targets 段里的注释，但**必须重新输出 `targets:` 这一行**，
    # 否则整段变成孤立的列表项，解析时就等于没有 targets。
    out = _header_comments(original, own=GENERATED_MARKERS)
    out.append("targets:")
    for target in targets:
        out.append(f"  - id: {_fmt(target.id)}")
        if target.label:
            out.append(f"    label: {_fmt(target.label)}")
        if target.interval:
            lo, hi = target.interval
            out.append(f"    interval: [{_fmt(lo)}, {_fmt(hi)}]")
        if target.daily_limit:
            out.append(f"    daily_limit: {_fmt(target.daily_limit)}")
    if not targets:
        out.append("  []")
    return out


def _render_repost(repost: RepostConfig) -> list[str]:
    out = [
        "# =============================================================",
        "# 定时重发：把源里固定的几条消息，循环重发到目标群",
        "# 本段由程序重写（由网页面板 / 命令维护），其它段落的注释不受影响",
        "#   interval 必须 >= 31 秒：目标群慢速 30s 时低于它会每条都撞墙",
        "# =============================================================",
        "repost:",
        f"  enabled: {_fmt(repost.enabled)}",
    ]
    if repost.ranges:
        out.append("  ranges:")
        for start, end in repost.ranges:
            out.append(f'    - "{start}-{end}"' if start != end else f'    - "{start}"')
    else:
        out.append("  ranges: []")
    if repost.ids:
        out.append("  ids: [" + ", ".join(str(item) for item in repost.ids) + "]")
    else:
        out.append("  ids: []")
    if repost.interval is not None:
        out.append(f"  interval: {_fmt(repost.interval)}")
    out.append(f"  daily_limit: {repost.daily_limit}")
    if repost.targets:
        out.append("  targets: [" + ", ".join(_fmt(item) for item in repost.targets) + "]")
    else:
        out.append("  targets: []")
    out.append(f"  shuffle: {_fmt(repost.shuffle)}")
    out.append(f"  run_mode: {repost.run_mode}")
    return out


class ConfigStore:
    def __init__(self, path: str | Path = "config.yaml") -> None:
        self.path = Path(path)

    # ---------------- 读写 ----------------

    def read(self) -> str:
        if not self.path.exists():
            raise ConfigStoreError(f"找不到配置文件：{self.path}")
        return self.path.read_text(encoding="utf-8")

    def _write_atomic(self, text: str) -> None:
        """先写临时文件再替换：避免写一半崩了导致配置损坏。"""
        directory = self.path.parent or Path(".")
        directory.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".config-", suffix=".yaml")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _replace_sections(self, new_sections: dict[str, list[str]]) -> str:
        """用新内容替换指定顶层段落，其余原样保留。"""
        sections = _split_sections(self.read())
        result: list[list[str]] = []
        seen: set[str] = set()
        for name, lines in sections:
            if name in new_sections:
                result.append(new_sections[name])
                seen.add(name)
            else:
                result.append(lines)
        # 原本不存在的段落，按固定顺序追加
        for name in MANAGED:
            if name in new_sections and name not in seen:
                result.append(new_sections[name])
        text = "\n".join(
            line for block in result for line in block
        )
        if not text.endswith("\n"):
            text += "\n"
        # 连续空行压成一个，避免反复写回后文件越来越松散
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _validate(self, text: str) -> None:
        """写完先自己读一遍，读不回来就不落盘。"""
        handle, tmp_name = tempfile.mkstemp(suffix=".yaml")
        os.close(handle)
        try:
            Path(tmp_name).write_text(text, encoding="utf-8")
            load_config(tmp_name)
        except Exception as exc:
            raise ConfigStoreError(f"改完的配置无法解析，已放弃写入：{exc}") from exc
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def _update(self, new_sections: dict[str, list[str]]) -> str:
        text = self._replace_sections(new_sections)
        self._validate(text)
        self._write_atomic(text)
        return text

    # ---------------- 对外接口 ----------------

    def save_sources(self, sources: Sequence[Any]) -> None:
        original = next(
            (lines for name, lines in _split_sections(self.read()) if name == "sources"),
            [],
        )
        self._update({"sources": _render_sources(sources, original)})
        log.info("已写回 %s：%s 个源", self.path, len(sources))

    def save_targets(self, targets: Sequence[Target]) -> None:
        original = next(
            (lines for name, lines in _split_sections(self.read()) if name == "targets"),
            [],
        )
        self._update({"targets": _render_targets(targets, original)})
        log.info("已写回 %s：%s 个目标", self.path, len(targets))

    def save_repost(self, repost: RepostConfig) -> None:
        self._update({"repost": _render_repost(repost)})
        log.info("已写回 %s：repost 配置", self.path)

    def set_rate(self, **changes: Any) -> None:
        """改 rate 段里的单个键（只替换那一行，保留注释）。"""
        text = self.read()
        for key, value in changes.items():
            if value is None:
                continue
            pattern = re.compile(rf"^(\s*{re.escape(key)}\s*:)([^\n]*)$", re.MULTILINE)

            def _sub(match: re.Match[str], value: Any = value) -> str:
                trailing = ""
                rest = match.group(2)
                if "#" in rest:
                    trailing = "  " + rest[rest.index("#"):]
                return f"{match.group(1)} {_fmt(value)}{trailing}"

            text, count = pattern.subn(_sub, text, count=1)
            if count == 0:
                raise ConfigStoreError(f"config.yaml 里找不到 rate.{key}，请手工添加")
        self._validate(text)
        self._write_atomic(text)
        log.info("已写回 %s：rate 参数 %s", self.path, list(changes))

    def load(self) -> AppConfig:
        return load_config(self.path)
