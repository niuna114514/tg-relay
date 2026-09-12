"""配置加载与校验测试（覆盖内置极简 YAML 解析器）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgrelay.config import (  # noqa: E402
    ConfigError,
    apply_env,
    load_config,
    load_dotenv,
)

SAMPLE = """
# 顶部注释
sources:
  - "@my_channel"
  - -1001234567890

targets:
  - id: -1001111111111
    interval: [2, 4]
    label: 一群
  - id: -1002222222222

filters:
  keywords: [上新, 优惠]      # 行内注释
  keyword_mode: any
  exclude: []
  regex:
    - "特价.*"
  min_length: 0
  media: [photo, video]

rate:
  per_target_interval: [3, 6]
  cross_target_delay: [5, 15]
  global_per_minute: 20
  daily_cap: 200

premium: false

behavior:
  sync_edits: false
  catch_up_on_start: true
  album_window: 0.6
  queue_size: 200

storage:
  db_path: data/relay.db
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_sources_targets_and_ranges(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SAMPLE))

    assert config.sources == ("@my_channel", -1001234567890)
    assert config.source == "@my_channel"
    assert [t.id for t in config.targets] == [-1001111111111, -1002222222222]
    assert config.targets[0].interval == (2.0, 4.0)
    assert config.targets[0].label == "一群"
    assert config.targets[0].display == "一群"
    assert config.targets[1].display == "-1002222222222"
    assert config.targets[1].interval is None
    assert config.rate.per_target_interval == (3.0, 6.0)
    assert config.rate.global_per_minute == 20
    assert config.behavior.album_window == pytest.approx(0.6)
    assert config.filters.keywords == ("上新", "优惠")
    assert config.filters.regex == ("特价.*",)
    assert config.filters.media == ("photo", "video")
    assert config.db_path == "data/relay.db"
    assert config.premium is False


def test_reversed_range_is_normalized(tmp_path: Path) -> None:
    text = SAMPLE.replace("per_target_interval: [3, 6]", "per_target_interval: [9, 2]")
    config = load_config(write(tmp_path, text))
    assert config.rate.per_target_interval == (2.0, 9.0)


def test_scalar_interval_is_allowed(tmp_path: Path) -> None:
    text = SAMPLE.replace("interval: [2, 4]", "interval: 5")
    config = load_config(write(tmp_path, text))
    assert config.targets[0].interval == (5.0, 5.0)


def test_missing_sources_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="sources"):
        load_config(write(tmp_path, "targets:\n  - -1001\n"))


def test_missing_targets_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="targets"):
        load_config(write(tmp_path, 'sources: ["@a"]\n'))


def test_duplicate_target_fails(tmp_path: Path) -> None:
    text = 'sources: ["@a"]\ntargets:\n  - id: -1001\n  - id: -1001\n'
    with pytest.raises(ConfigError, match="重复"):
        load_config(write(tmp_path, text))


def test_unknown_media_kind_fails(tmp_path: Path) -> None:
    text = 'sources: ["@a"]\ntargets:\n  - id: -1001\nfilters:\n  media: [hologram]\n'
    with pytest.raises(ConfigError, match="media"):
        load_config(write(tmp_path, text))


def test_target_without_id_fails(tmp_path: Path) -> None:
    text = 'sources: ["@a"]\ntargets:\n  - label: 没有id\n'
    with pytest.raises(ConfigError, match="缺少 id"):
        load_config(write(tmp_path, text))


def test_bad_boolean_fails(tmp_path: Path) -> None:
    text = 'sources: ["@a"]\ntargets:\n  - id: -1001\npremium: maybe\n'
    with pytest.raises(ConfigError, match="premium"):
        load_config(write(tmp_path, text))


def test_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到配置文件"):
        load_config(tmp_path / "nope.yaml")


def test_apply_env_sets_credentials_and_premium(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SAMPLE))
    assert config.credentials is None

    updated = apply_env(
        config,
        {
            "TG_API_ID": "12345",
            "TG_API_HASH": "abcdef",
            "TG_SESSION": "data/x.session",
            "TG_PREMIUM": "true",
        },
    )
    assert updated.credentials is not None
    assert updated.credentials.api_id == 12345
    assert updated.credentials.api_hash == "abcdef"
    assert updated.credentials.session_path == "data/x.session"
    assert updated.premium is True


def test_apply_env_requires_both_credentials(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SAMPLE))
    with pytest.raises(ConfigError, match="同时提供"):
        apply_env(config, {"TG_API_ID": "1"})


def test_apply_env_rejects_non_numeric_api_id(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SAMPLE))
    with pytest.raises(ConfigError, match="TG_API_ID"):
        apply_env(config, {"TG_API_ID": "abc", "TG_API_HASH": "x"})


def test_load_dotenv_parses_quotes_and_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# 注释\n"
        "TG_API_ID=123\n"
        'TG_API_HASH="abc#def"\n'
        "export TG_PREMIUM=true\n"
        "\n"
        "BAD_LINE\n",
        encoding="utf-8",
    )
    env = load_dotenv(path)
    assert env["TG_API_ID"] == "123"
    assert env["TG_API_HASH"] == "abc#def"
    assert env["TG_PREMIUM"] == "true"
    assert "BAD_LINE" not in env


def test_load_dotenv_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "none.env") == {}
