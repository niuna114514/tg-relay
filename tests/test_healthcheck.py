"""健康检查的"发送记录"判定。

重点是区分两种"很久没发出去东西"：
  * **重发被关掉了**（账号被限制、人主动停了）→ 预期安静，不能报警；
  * **重发开着却没动静** → 这才该报警。

2026-09-12 之后新增了第一条分支：那会儿为了等申诉把重发关了，
如果不管，停发满 24 小时就会开始每 15 分钟收到一条"发送停滞"的误报，
把真正的告警淹掉。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import healthcheck  # noqa: E402

CRIT, WARN = healthcheck.CRIT, healthcheck.WARN


def make_db(path: Path, rows: list[tuple[str, str]]) -> None:
    """rows = [(status, updated_at)]，updated_at 用 SQLite 的 UTC 字符串。"""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE deliveries (
            job_id TEXT, target_id TEXT, source_id INTEGER, source_msg_id INTEGER,
            target_msg_ids TEXT, status TEXT, error TEXT, attempts INTEGER, updated_at TEXT
        )
        """
    )
    for index, (status, when) in enumerate(rows):
        conn.execute(
            "INSERT INTO deliveries (job_id, status, updated_at) VALUES (?, ?, ?)",
            (f"job{index}", status, when),
        )
    conn.commit()
    conn.close()


def make_config(tmp_path: Path, *, repost_enabled: bool):
    return SimpleNamespace(
        db_path=str(tmp_path / "relay.db"),
        repost=SimpleNamespace(enabled=repost_enabled, interval=300),
    )


def levels(report: healthcheck.Report) -> dict[str, int]:
    return {item.title: item.level for item in report.findings}


def test_quiet_when_repost_disabled_and_nothing_attempted(tmp_path: Path) -> None:
    """重发关着 + 近 24 小时没有任何发送动作 = 预期状态，不该报 WARN/CRIT。"""
    config = make_config(tmp_path, repost_enabled=False)
    make_db(Path(config.db_path), [("sent", "2026-09-10 08:00:00")])

    report = healthcheck.Report()
    healthcheck.check_activity(report, config)

    found = levels(report)
    assert "发送记录" in found
    assert found["发送记录"] == 0, f"不该报警，实际 {found}"
    assert WARN not in found.values() and CRIT not in found.values()


def test_warns_when_repost_disabled_but_still_attempting(tmp_path: Path) -> None:
    """重发关着、但近 24 小时还在尝试（说明有别的东西在发）→ 停顿超过 24h 仍要提醒。"""
    config = make_config(tmp_path, repost_enabled=False)
    make_db(
        Path(config.db_path),
        [
            ("sent", "2026-09-10 08:00:00"),
            ("failed", "2099-01-01 00:00:00"),  # 近期有动作
        ],
    )

    report = healthcheck.Report()
    healthcheck.check_activity(report, config)

    assert levels(report)["发送记录"] == WARN


def test_repost_enabled_stall_is_critical(tmp_path: Path) -> None:
    """重发开着却长时间没成功 → CRIT（这是真的出事）。"""
    config = make_config(tmp_path, repost_enabled=True)
    make_db(Path(config.db_path), [("sent", "2026-09-10 08:00:00")])

    report = healthcheck.Report()
    healthcheck.check_activity(report, config)

    assert levels(report)["发送停滞"] == CRIT


def test_repost_enabled_recent_send_is_fine(tmp_path: Path) -> None:
    config = make_config(tmp_path, repost_enabled=True)
    conn = sqlite3.connect(config.db_path)
    conn.execute(
        """
        CREATE TABLE deliveries (
            job_id TEXT, target_id TEXT, source_id INTEGER, source_msg_id INTEGER,
            target_msg_ids TEXT, status TEXT, error TEXT, attempts INTEGER, updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO deliveries (job_id, status, updated_at) "
        "VALUES ('a', 'sent', datetime('now'))"
    )
    conn.commit()
    conn.close()

    report = healthcheck.Report()
    healthcheck.check_activity(report, config)

    assert levels(report)["发送活跃"] == 0


def test_missing_deliveries_table_is_reported(tmp_path: Path) -> None:
    config = make_config(tmp_path, repost_enabled=False)
    sqlite3.connect(config.db_path).close()

    report = healthcheck.Report()
    healthcheck.check_activity(report, config)

    assert levels(report)["发送记录"] == WARN
