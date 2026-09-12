"""存储层测试：去重、游标、日配额、投递账本。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgrelay.db import Store  # noqa: E402


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "relay.db")


def test_mark_seen_is_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        assert store.mark_seen(-1001, 10) is True
        assert store.mark_seen(-1001, 10) is False  # 重放同一条消息
        assert store.mark_seen(-1001, 11) is True
        assert store.mark_seen(-1002, 10) is True  # 不同源互不影响
        assert store.is_seen(-1001, 10)
        assert not store.is_seen(-1001, 12)
    finally:
        store.close()


def test_forget_seen_allows_replay(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.mark_seen(-1001, 10)
        store.forget_seen(-1001, 10)
        assert store.mark_seen(-1001, 10) is True
    finally:
        store.close()


def test_cursor_is_monotonic(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        assert store.get_cursor(-1001) == 0
        store.set_cursor(-1001, 50)
        assert store.get_cursor(-1001) == 50
        store.set_cursor(-1001, 30)  # 不允许回退
        assert store.get_cursor(-1001) == 50
        store.set_cursor(-1001, 80)
        assert store.get_cursor(-1001) == 80
    finally:
        store.close()


def test_daily_counter_accumulates(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        assert store.sent_today() == 0
        assert store.add_sent(1) == 1
        assert store.add_sent(5) == 6
        assert store.sent_today() == 6
    finally:
        store.close()


def test_daily_counter_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "relay.db"
    store = Store(path)
    store.add_sent(3)
    store.close()

    reopened = Store(path)
    try:
        assert reopened.sent_today() == 3
    finally:
        reopened.close()


def test_record_delivery_upsert(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.record_delivery(
            job_id="-1001:10",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=10,
            status="failed",
            error="FloodWait 60s",
        )
        failed = store.find_delivery("-1001:10", -2001)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.attempts == 1

        store.record_delivery(
            job_id="-1001:10",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=10,
            status="sent",
            target_msg_ids=[777, 778],
        )
        sent = store.find_delivery("-1001:10", -2001)
        assert sent is not None
        assert sent.status == "sent"
        assert sent.target_msg_ids == (777, 778)
        assert sent.first_msg_id == 777
        assert sent.attempts == 2  # 同一投递被重试过
    finally:
        store.close()


def test_find_deliveries_only_returns_sent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.record_delivery(
            job_id="-1001:10",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=10,
            status="sent",
            target_msg_ids=[1],
        )
        store.record_delivery(
            job_id="-1001:10",
            target_id=-2002,
            source_id=-1001,
            source_msg_id=10,
            status="failed",
            error="无权限",
        )
        found = store.find_deliveries(-1001, 10)
        assert len(found) == 1
        assert found[0].target_id == "-2001"
        assert len(store.failed_deliveries()) == 1
    finally:
        store.close()


def test_error_message_is_truncated(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.record_delivery(
            job_id="j",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=1,
            status="failed",
            error="x" * 900,
        )
        delivery = store.find_delivery("j", -2001)
        assert delivery is not None
        assert len(delivery.error) == 500
    finally:
        store.close()


def test_prune_seen_keeps_recent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        for msg_id in range(1, 21):
            store.mark_seen(-1001, msg_id)
        removed = store.prune_seen(keep_per_source=5)
        assert removed == 15
        assert store.is_seen(-1001, 20)
        assert not store.is_seen(-1001, 1)
        assert store.stats()["seen"] == 5
    finally:
        store.close()


def test_sent_ok_today_separates_repost_and_relay(tmp_path: Path) -> None:
    """实发计数要能把"实时转发"和"定时重发"分开，且只数成功的那部分。"""
    store = make_store(tmp_path)
    try:
        store.record_delivery(
            job_id="-1001:1",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=1,
            status="sent",
            target_msg_ids=[10],
        )
        store.record_delivery(
            job_id="repost:-1001:6:1700000000000",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=6,
            status="sent",
            target_msg_ids=[11],
        )
        store.record_delivery(
            job_id="repost:-1001:7:1700000000001",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=7,
            status="failed",
            error="无权限",
        )

        assert store.sent_ok_today() == 2
        assert store.sent_ok_today(as_repost=True) == 1
        assert store.sent_ok_today(as_repost=False) == 1
    finally:
        store.close()


def test_sent_ok_today_ignores_previous_days(tmp_path: Path) -> None:
    """昨天发出的不计入今天。"""
    store = make_store(tmp_path)
    try:
        store.record_delivery(
            job_id="old",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=1,
            status="sent",
            target_msg_ids=[10],
        )
        with store._lock:
            store._conn.execute(
                "UPDATE deliveries SET updated_at = datetime('now', '-2 days') WHERE job_id = 'old'"
            )
            store._conn.commit()
        assert store.sent_ok_today() == 0
    finally:
        store.close()


def test_stats_counts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    try:
        store.mark_seen(-1001, 1)
        store.record_delivery(
            job_id="j1",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=1,
            status="sent",
            target_msg_ids=[9],
        )
        store.record_delivery(
            job_id="j2",
            target_id=-2001,
            source_id=-1001,
            source_msg_id=2,
            status="failed",
            error="e",
        )
        stats = store.stats()
        assert stats["seen"] == 1
        assert stats["sent"] == 1
        assert stats["failed"] == 1
        assert stats["sent_today"] == 0
    finally:
        store.close()
