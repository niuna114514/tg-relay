"""配置写回测试：重点是 YAML 转义（曾经因为漏掉 @ 导致写回失败）。

背景（真实踩过）：
    目标写成 `- id: @example_group` 时，YAML 解析报
    "found character '@' that cannot start any token"
    → 写回被放弃，改动只留在内存里，服务器一重启就丢。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgrelay.config import Target, load_config  # noqa: E402
from tgrelay.config_store import ConfigStore, ConfigStoreError, _fmt  # noqa: E402

BASE = """\
# 保留我手写的注释
sources:
  - "@src"

targets:
  - id: -1002001
    label: 群A

rate:
  per_target_interval: [5, 10]
  cross_target_delay: [5, 15]
  global_per_minute: 10    # 手写尾注
  daily_cap: 100

behavior:
  queue_size: 10

repost:
  enabled: true
  ids: [6]
  interval: 300
  daily_limit: 200
"""


def make(tmp_path: Path) -> tuple[ConfigStore, Path]:
    path = tmp_path / "config.yaml"
    path.write_text(BASE, encoding="utf-8")
    return ConfigStore(path), path


# --------------------------------------------------------------------------
# _fmt 转义规则
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "@example_group",          # 真实踩过的坑：YAML 保留字符
        "@my_channel",
        "-1002001abc",
        "-1002001",
        "!important",
        "&anchor",
        "*alias",
        "?key",
        "|pipe",
        ">fold",
        "%pct",
        "#hash",
        "[list",
        "{map",
        "a: b",
        "a, b",
        " leading",
        "trailing ",
        "",
        "has'quote",
        'has"quote',
        "back\\slash",
    ],
)
def test_special_values_are_quoted(value: str) -> None:
    rendered = _fmt(value)
    assert rendered.startswith('"'), f"{value!r} 应该被加引号，实际 {rendered!r}"
    assert rendered.endswith('"')
    # 引号内容必须能还原
    body = rendered[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    assert body == value


@pytest.mark.parametrize(
    "value",
    ["example_group", "群A", "一本正经的备注", "abc123", "a_b", "a-b", "a.b", "a/b"],
)
def test_plain_values_are_not_quoted(value: str) -> None:
    assert _fmt(value) == value


def test_backslash_round_trips(tmp_path: Path) -> None:
    """反斜杠在 YAML 双引号里是转义符：必须加引号并转义。

    如果只加引号不转义，`a\\b` 会被 YAML 解成别的字符；
    如果不加引号，又会在某些位置被当成转义开头。两头都要管。
    """
    value = "back\\slash"
    rendered = _fmt(value)
    assert rendered == '"back\\\\slash"'

    # 落到文件里再读回来，必须一模一样
    path = tmp_path / "config.yaml"
    path.write_text(
        'sources: ["@src"]\ntargets:\n'
        + f"  - id: {rendered}\n"
        + "rate:\n  global_per_minute: 10\n  daily_cap: 100\n",
        encoding="utf-8",
    )
    assert load_config(path).targets[0].id == value



def test_booleans_and_numbers() -> None:
    assert _fmt(True) == "true"
    assert _fmt(False) == "false"
    assert _fmt(-1002001) == "-1002001"      # 不能变成科学计数法
    assert _fmt(1234567890123456789) == "1234567890123456789"
    assert _fmt(30) == "30"
    assert _fmt(30.0) == "30"
    assert _fmt(30.5) == "30.5"


def test_big_negative_id_is_not_scientific_notation() -> None:
    """:g 会把 -1002001 写成 -1.002e+06，解析回来就不是那个 ID 了。"""
    assert "e+" not in _fmt(-1002001).lower()
    assert "e-" not in _fmt(-1002001).lower()


# --------------------------------------------------------------------------
# 写回 config.yaml
# --------------------------------------------------------------------------


def test_username_target_round_trips(tmp_path: Path) -> None:
    """核心回归：@username 作为目标 ID 必须能写回并重新解析。"""
    store, path = make(tmp_path)
    store.save_targets((Target(id="@example_group", label="example_group"),))

    text = path.read_text(encoding="utf-8")
    assert '@example_group' in text
    # 必须是被引号包起来的
    assert '- id: "@example_group"' in text, text

    reloaded = load_config(path)
    assert reloaded.targets[0].id == "@example_group"
    assert reloaded.targets[0].label == "example_group"


def test_mixed_numeric_and_username_targets(tmp_path: Path) -> None:
    store, path = make(tmp_path)
    store.save_targets(
        (
            Target(id=-1002001, label="数字群"),
            Target(id="@user_group", label="用户名群", interval=(30.0, 35.0)),
        )
    )
    reloaded = load_config(path)
    assert [t.id for t in reloaded.targets] == [-1002001, "@user_group"]
    assert reloaded.targets[1].interval == (30.0, 35.0)


def test_user_comments_survive(tmp_path: Path) -> None:
    store, path = make(tmp_path)
    store.save_targets((Target(id=-1002001),))
    text = path.read_text(encoding="utf-8")
    assert "保留我手写的注释" in text
    assert "# 手写尾注" in text


def test_rate_update_keeps_trailing_comment(tmp_path: Path) -> None:
    store, path = make(tmp_path)
    store.set_rate(global_per_minute=25, daily_cap=300)
    text = path.read_text(encoding="utf-8")
    assert "global_per_minute: 25" in text
    assert "# 手写尾注" in text
    reloaded = load_config(path)
    assert reloaded.rate.global_per_minute == 25
    assert reloaded.rate.daily_cap == 300


def test_repost_round_trip(tmp_path: Path) -> None:
    from dataclasses import replace

    store, path = make(tmp_path)
    config = load_config(path)
    store.save_repost(replace(config.repost, ids=(7, 9), interval=600.0, shuffle=True))
    reloaded = load_config(path)
    assert reloaded.repost.ids == (7, 9)
    assert reloaded.repost.interval == 600.0
    assert reloaded.repost.shuffle is True


def test_invalid_result_is_not_written(tmp_path: Path) -> None:
    """改完解析不回来就放弃写入，不能把配置写坏。"""
    store, path = make(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigStoreError):
        store._update({"targets": ["targets:", "  - id:"]})
    assert path.read_text(encoding="utf-8") == before


def test_write_is_atomic_no_temp_left(tmp_path: Path) -> None:
    store, path = make(tmp_path)
    store.save_targets((Target(id=-1002001),))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".config-")]
    assert leftovers == []


# --------------------------------------------------------------------------
# 注释保留与去重（真实踩过的坑）
# --------------------------------------------------------------------------


def test_user_comment_before_targets_is_preserved(tmp_path: Path) -> None:
    """紧贴 targets 段头部的用户注释必须保留。"""
    path = tmp_path / "c.yaml"
    path.write_text(
        'sources: ["@s"]\n'
        "# 我自己写的说明，别弄丢\n"
        "targets:\n  - id: -1001\n"
        "rate:\n  global_per_minute: 10\n  daily_cap: 100\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    store.save_targets((Target(id=-1001, label="群A"),))
    text = path.read_text(encoding="utf-8")
    assert "我自己写的说明，别弄丢" in text
    assert load_config(path).targets[0].label == "群A"


def test_repeated_writes_do_not_accumulate_comments(tmp_path: Path) -> None:
    """连续写回多次，注释不能累积。

    曾经的 bug：程序生成的注释在下次写回时被当成"用户注释"再抄一遍，
    文件里出现 3 行相同的注释，越写越乱。
    """
    path = tmp_path / "c.yaml"
    path.write_text(
        'sources: ["@s"]\ntargets:\n  - id: -1001\n'
        "rate:\n  global_per_minute: 10\n  daily_cap: 100\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    for _ in range(5):
        store.save_targets((Target(id=-1001, label="群A"),))

    text = path.read_text(encoding="utf-8")
    assert text.count("targets:") == 1
    # 程序自己生成的注释必须被丢弃，不能累积
    assert text.count("# 目标") <= 1
    assert load_config(path).targets[0].label == "群A"


def test_mixed_user_and_generated_comments(tmp_path: Path) -> None:
    """用户注释保留、程序生成的注释丢弃，两者混在一起也要分得清。"""
    path = tmp_path / "c.yaml"
    path.write_text(
        'sources: ["@s"]\n'
        "# 用户写的：这三个群都是我的\n"
        "# 本段由程序重写（由网页面板 / 命令维护）   <- 程序生成的格式，应被丢弃\n"
        "targets:\n  - id: -1001\n"
        "rate:\n  global_per_minute: 10\n  daily_cap: 100\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    for _ in range(3):
        store.save_targets((Target(id=-1001, label="群A"),))

    text = path.read_text(encoding="utf-8")
    assert text.count("用户写的：这三个群都是我的") == 1
    assert "由程序重写" not in text, "程序生成的注释应该被丢掉，不能累积"
    assert text.count("targets:") == 1
    assert load_config(path).targets[0].label == "群A"


def test_generated_repost_header_does_not_accumulate(tmp_path: Path) -> None:
    """repost 段的程序注释反复写回也不能累积。"""
    from dataclasses import replace

    store, path = make(tmp_path)
    config = load_config(path)
    for i in range(4):
        store.save_repost(replace(config.repost, ids=(6 + i,), interval=300.0))

    text = path.read_text(encoding="utf-8")
    assert text.count("本段由程序重写") == 1
    assert text.count("repost:") == 1
    reloaded = load_config(path)
    assert reloaded.repost.ids == (9,)


def test_repost_section_keeps_user_comments_outside(tmp_path: Path) -> None:
    """repost 段被重写，但其它段的注释不受影响。"""
    from dataclasses import replace

    store, path = make(tmp_path)
    config = load_config(path)
    store.save_repost(replace(config.repost, ids=(9,), interval=400.0))
    text = path.read_text(encoding="utf-8")
    assert "保留我手写的注释" in text
    assert "# 手写尾注" in text
    assert "interval: 400" in text
