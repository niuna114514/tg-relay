"""文字面板的测试。

重点覆盖两件容易翻车的事：
  1. **中文字宽**：中文占 2 列，用 len() 去对齐会让整张表错位；
  2. **面板不许连 Telegram**：一个 session 只能被一个进程持有，
     面板要是自己起 client 会让账号被强制登出。所以这里断言
     `--panel` 走的是面板分支，且面板模块根本不 import telethon。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgrelay import panel  # noqa: E402


# --------------------------------------------------------------------------
# 显示宽度
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("abc", 3),
        ("中文", 4),
        ("中a文b", 6),
        ("", 0),
        ("🙂", 2),          # emoji 按宽字符算
        ("a\u0301", 1),     # 组合字符占 0 列
    ],
)
def test_display_width(text: str, expected: int) -> None:
    assert panel.display_width(text) == expected


def test_truncate_keeps_within_width() -> None:
    text = "全国地区正反 地区 户籍地 地区头 11 北京单地址"
    out = panel.truncate(text, 20)

    assert panel.display_width(out) <= 20
    assert out.endswith("…")


def test_truncate_does_not_cut_chinese_in_half() -> None:
    """宽度是奇数时，宁可少一个字也不能把中文劈成半格。"""
    out = panel.truncate("中文字", 3)
    assert panel.display_width(out) <= 3
    assert out in ("中", "中…", "…")  # 不应出现半个字


def test_truncate_returns_original_when_fits() -> None:
    assert panel.truncate("中文", 4) == "中文"


def test_pad_aligns_by_display_width() -> None:
    assert panel.display_width(panel.pad("中文", 10)) == 10
    assert panel.display_width(panel.pad("中文", 10, "right")) == 10
    assert panel.display_width(panel.pad("中文", 10, "center")) == 10
    assert panel.pad("中文", 10, "right").endswith("中文")
    assert panel.pad("ab", 6, "center").strip() == "ab"


def test_pad_overlong_input_is_truncated_not_overflowed() -> None:
    assert panel.display_width(panel.pad("中" * 20, 8)) == 8


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def make_stats() -> dict:
    return {
        "config": {"sources": ["@src"]},
        "store": {
            "reposted_today": 42,
            "repost_ok_today": 40,
            "sent_today": 0,
            "sent_ok_today": 0,
        },
        "sender": {
            "sent": 230, "failed": 7, "skipped": 903, "slow_waits": 0,
            "slow_mode_windows": {"群甲": 30},
        },
        "repost": {
            "enabled": True, "running": True, "interval": 300, "daily_limit": 60,
            "shuffle": True, "message_ids": [6], "materials": [], "materials_loaded": False,
        },
        "targets": {
            "群甲": {
                "id": "@a", "paused": False, "interval": [300, 300], "daily_limit": 0,
                "quota_used": 42, "quota_limit": 100, "backlog": 0, "dropped": 0,
            },
            "群乙": {
                "id": "@b", "paused": True, "interval": None, "daily_limit": 20,
                "quota_used": 3, "quota_limit": 20, "backlog": 2, "dropped": 1,
            },
        },
        "risk": {
            "level": "danger",
            "items": [{"level": "danger", "title": "素材太少", "detail": "只有 1 条素材"}],
        },
        "account": {
            "breaker": "",
            "write_forbidden": {"群乙": 120},
            "alerts": [{"level": "warning", "text": "群乙 报 UserBanned\n第二行"}],
        },
    }


def make_state(**overrides: object) -> panel.PanelState:
    state = panel.PanelState(
        source="api", reachable=True, fetched_at=1700000000.0,
        service="active", stats=make_stats(),
        logs=["2026-09-12 09:00:00 INFO 启动", "2026-09-12 09:00:01 INFO 自检 OK"],
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_render_lines_are_all_exact_width() -> None:
    """每一行的显示宽度都必须等于面板宽度 —— 否则框线会豁口。"""
    width = 96
    lines = panel.render(make_state(), width=width, height=60)

    assert lines
    for line in lines:
        assert panel.display_width(line) == width, f"这一行宽度不对: {line!r}"


def test_render_shows_key_facts() -> None:
    text = "\n".join(panel.render(make_state(), width=100, height=60))

    assert "tg-relay" in text
    assert "可操作" in text
    assert "定时重发" in text
    assert "群甲" in text and "群乙" in text
    assert "300~300s" in text          # 群自己的间隔
    assert "✅正常" in text and "⏸暂停" in text
    assert "素材太少" in text          # 体检结论
    assert "停发中" in text            # 被停发的目标
    assert "最近日志" in text


def test_render_readonly_hides_actions() -> None:
    state = make_state(source="readonly", reachable=False, error="连不上 127.0.0.1:8123")
    text = "\n".join(panel.render(state, width=100, height=60))

    assert "只读" in text
    assert "--web" in text, "要告诉人怎么变成可操作模式"
    assert "启停重发" not in text, "只读模式不该提示不存在的操作"


def test_render_marks_selected_target() -> None:
    text = "\n".join(panel.render(make_state(), width=100, height=60, selected=1))
    assert "▶" in text


def test_render_height_cap_keeps_header_and_footer() -> None:
    lines = panel.render(make_state(), width=100, height=12)

    assert len(lines) <= 12
    assert "tg-relay" in lines[0]
    assert lines[-1].startswith("└"), "最后一行应该是底边框"


def test_render_survives_empty_stats() -> None:
    """服务刚起来、什么都还没有时不能崩。"""
    state = panel.PanelState(source="api", reachable=True, service="activating")
    lines = panel.render(state, width=80, height=40)

    assert lines
    for line in lines:
        assert panel.display_width(line) == 80


def test_render_status_line_is_shown() -> None:
    text = "\n".join(panel.render(make_state(), width=100, height=60, status_line="已刷新"))
    assert "已刷新" in text


# --------------------------------------------------------------------------
# 只读模式：靠 config.yaml 也要能画出东西
# --------------------------------------------------------------------------


def test_readonly_state_reads_config(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
sources:
  - "@src"
targets:
  - id: "@grp"
    interval: [300, 300]
repost:
  enabled: true
  ids: [6, 7]
  interval: 300
  daily_limit: 60
""",
        encoding="utf-8",
    )

    state = panel.readonly_state(config)

    assert state.source == "readonly"
    assert state.stats["repost"]["enabled"] is True
    assert state.stats["repost"]["message_ids"] == [6, 7]
    assert [name for name, _ in state.targets] == ["@grp"]


def test_readonly_state_handles_missing_config(tmp_path: Path) -> None:
    state = panel.readonly_state(tmp_path / "nope.yaml")
    assert state.error
    assert state.stats == {}


# --------------------------------------------------------------------------
# 红线：面板绝不能连 Telegram
# --------------------------------------------------------------------------


def test_panel_module_does_not_import_telethon() -> None:
    """面板要是 import 了 telethon，就有可能顺手建 client —— 那是自杀操作。"""
    source = Path(panel.__file__).read_text(encoding="utf-8")
    code_lines = [
        line for line in source.splitlines()
        if line.strip().startswith(("import ", "from ")) and not line.strip().startswith("#")
    ]
    assert not any("telethon" in line for line in code_lines), code_lines


def test_panel_branch_returns_before_touching_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--panel` 必须在创建 TelegramClient 之前返回。"""
    from tgrelay import __main__ as entry

    called: dict[str, object] = {}

    def fake_panel_main(args: object) -> int:
        called["args"] = args
        return 0

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("--panel 不该走到 run()，那会连 Telegram")

    monkeypatch.setattr("tgrelay.panel.main", fake_panel_main)
    monkeypatch.setattr(entry, "run", explode)

    code = entry.main(["--panel"])

    assert code == 0
    assert called["args"] is not None


def test_real_main_parser_accepts_panel_flag() -> None:
    from tgrelay.__main__ import build_parser

    args = build_parser().parse_args(["--panel", "--service", "tg-relay"])
    assert args.panel is True
    assert args.service == "tg-relay"


# --------------------------------------------------------------------------
# 令牌/端口发现
# --------------------------------------------------------------------------


def test_discover_token_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TG_WEB_TOKEN", "from-env")
    assert panel._discover_token("explicit", tmp_path) == "explicit"
    assert panel._discover_token(None, tmp_path) == "from-env"

    monkeypatch.delenv("TG_WEB_TOKEN", raising=False)
    (tmp_path / ".env").write_text('TG_WEB_TOKEN="from-dotenv"\n', encoding="utf-8")
    assert panel._discover_token(None, tmp_path) == "from-dotenv"


def test_discover_token_empty_when_nothing_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TG_WEB_TOKEN", raising=False)
    monkeypatch.setattr(panel.Path, "glob", lambda self, pattern: iter(()))
    assert panel._discover_token(None, tmp_path) == ""


def test_discover_port_explicit_wins() -> None:
    assert panel._discover_port(9999) == 9999
