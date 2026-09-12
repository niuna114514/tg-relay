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

"""存储层：消息去重、推送账本、每日配额、补漏游标。

用 SQLite（标准库 sqlite3），所有写入用 `INSERT OR IGNORE` 保证幂等，
因为断线重连后 Telethon 会重放错过的更新——去重必须落库，不能只放内存。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source_id   TEXT NOT NULL,
    msg_id      INTEGER NOT NULL,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, msg_id)
);

CREATE TABLE IF NOT EXISTS cursor (
    source_id    TEXT PRIMARY KEY,
    last_msg_id  INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deliveries (
    job_id        TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    source_msg_id INTEGER NOT NULL,
    target_msg_ids TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    error         TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (job_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_source ON deliveries (source_id, source_msg_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries (status);

CREATE TABLE IF NOT EXISTS daily_counter (
    day    TEXT PRIMARY KEY,
    sent   INTEGER NOT NULL DEFAULT 0,
    repost INTEGER NOT NULL DEFAULT 0,
    -- checked = 今天"检查过额度"的次数，用于硬上限判定。
    -- 必须和 sent 分开：如果拿 sent 当检查依据，会因为它已经被累加过
    -- 而让每条消息提前一条被拒（daily_cap=100 实际只能发 99 条）。
    checked        INTEGER NOT NULL DEFAULT 0,
    repost_checked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reposts (
    source_id   TEXT NOT NULL,
    msg_id      INTEGER NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    last_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, msg_id)
);

-- 每个目标每天的发送量：多群场景下必须按群计数，
-- 否则 N 个群共享一个总额度，排在后面的群永远发不出去。
CREATE TABLE IF NOT EXISTS target_daily (
    day        TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    checked    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, target_id)
);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给旧库补上新列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。"""
    wanted = {
        "daily_counter": [
            ("repost", "INTEGER NOT NULL DEFAULT 0"),
            ("checked", "INTEGER NOT NULL DEFAULT 0"),
            ("repost_checked", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "target_daily": [
            ("checked", "INTEGER NOT NULL DEFAULT 0"),
        ],
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, spec in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


@dataclass(frozen=True)
class Delivery:
    job_id: str
    target_id: str
    source_id: str
    source_msg_id: int
    target_msg_ids: tuple[int, ...]
    status: str
    error: str
    attempts: int

    @property
    def first_msg_id(self) -> int | None:
        return self.target_msg_ids[0] if self.target_msg_ids else None


class Store:
    """线程安全的 SQLite 封装。写操作串行化，读操作走同一连接避免锁竞争。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._default_source: str | None = None
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            _ensure_columns(self._conn)
            self._conn.commit()

    # ---------------- 去重 ----------------

    def mark_seen(self, source_id: int | str, msg_id: int) -> bool:
        """记录一条源消息。返回 True 表示第一次见到（应当处理），False 表示重复。"""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO seen (source_id, msg_id) VALUES (?, ?)",
                (str(source_id), int(msg_id)),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def is_seen(self, source_id: int | str, msg_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen WHERE source_id = ? AND msg_id = ?",
                (str(source_id), int(msg_id)),
            ).fetchone()
            return row is not None

    def forget_seen(self, source_id: int | str, msg_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM seen WHERE source_id = ? AND msg_id = ?",
                (str(source_id), int(msg_id)),
            )
            self._conn.commit()

    # ---------------- 补漏游标 ----------------

    def get_cursor(self, source_id: int | str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_msg_id FROM cursor WHERE source_id = ?",
                (str(source_id),),
            ).fetchone()
            return int(row["last_msg_id"]) if row else 0

    def set_cursor(self, source_id: int | str, msg_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cursor (source_id, last_msg_id, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(source_id) DO UPDATE SET
                    last_msg_id = MAX(cursor.last_msg_id, excluded.last_msg_id),
                    updated_at  = excluded.updated_at
                """,
                (str(source_id), int(msg_id)),
            )
            self._conn.commit()

    # ---------------- 每日配额 ----------------

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def sent_today(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT sent FROM daily_counter WHERE day = ?",
                (self._today(),),
            ).fetchone()
            return int(row["sent"]) if row else 0

    def reposted_today(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT repost FROM daily_counter WHERE day = ?",
                (self._today(),),
            ).fetchone()
            return int(row["repost"]) if row else 0

    def add_sent(self, count: int = 1, *, as_repost: bool = False) -> int:
        """累加今日**已发送**数（展示用），返回累加后的值。跨重启有效。

        注意：额度判定用的是 checked 那组计数（见 note_checked），
        不是这个 —— 混用会导致每条消息提前一条被拒。
        """
        column = "repost" if as_repost else "sent"
        with self._lock:
            self._conn.execute(
                f"""
                INSERT INTO daily_counter (day, {column}) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET {column} = {column} + excluded.{column}
                """,
                (self._today(), int(count)),
            )
            self._conn.commit()
            return self.reposted_today() if as_repost else self.sent_today()

    # ---------------- 额度判定用的计数（checked） ----------------

    def checked_today(self, *, as_repost: bool = False) -> int:
        """今天已经"检查通过"的次数 —— 额度判定的依据。"""
        column = "repost_checked" if as_repost else "checked"
        with self._lock:
            row = self._conn.execute(
                f"SELECT {column} AS n FROM daily_counter WHERE day = ?",
                (self._today(),),
            ).fetchone()
            return int(row["n"]) if row else 0

    def note_checked(self, count: int = 1, *, as_repost: bool = False) -> int:
        """记一次额度占位，返回累计值。

        用独立计数而不是读 sent，是为了让 `daily_cap=100` 真的能发满 100 条。
        """
        column = "repost_checked" if as_repost else "checked"
        with self._lock:
            self._conn.execute(
                f"""
                INSERT INTO daily_counter (day, {column}) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET {column} = {column} + excluded.{column}
                """,
                (self._today(), int(count)),
            )
            self._conn.commit()
            return self.checked_today(as_repost=as_repost)

    def target_checked_today(self, target_id: int | str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT checked FROM target_daily WHERE day = ? AND target_id = ?",
                (self._today(), str(target_id)),
            ).fetchone()
            return int(row["checked"]) if row else 0

    def note_target_checked(self, target_id: int | str, count: int = 1) -> int:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO target_daily (day, target_id, checked) VALUES (?, ?, ?)
                ON CONFLICT(day, target_id) DO UPDATE SET checked = checked + excluded.checked
                """,
                (self._today(), str(target_id), int(count)),
            )
            self._conn.commit()
            return self.target_checked_today(target_id)

    # ---------------- 按目标的每日计数 ----------------

    def target_sent_today(self, target_id: int | str) -> int:
        """该目标今天已发送（占用额度）的条数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT sent FROM target_daily WHERE day = ? AND target_id = ?",
                (self._today(), str(target_id)),
            ).fetchone()
            return int(row["sent"]) if row else 0

    def target_sent_map(self) -> dict[str, int]:
        """今天所有目标的已用额度，便于一次取回（不要在循环里逐个查）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT target_id, sent FROM target_daily WHERE day = ?",
                (self._today(),),
            ).fetchall()
        return {row["target_id"]: int(row["sent"]) for row in rows}

    def add_target_sent(self, target_id: int | str, count: int = 1) -> int:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO target_daily (day, target_id, sent) VALUES (?, ?, ?)
                ON CONFLICT(day, target_id) DO UPDATE SET sent = sent + excluded.sent
                """,
                (self._today(), str(target_id), int(count)),
            )
            self._conn.commit()
            return self.target_sent_today(target_id)

    def sent_ok_by_target(self) -> dict[str, int]:
        """今天每个目标**真正发成功**的条数（数投递账本，不是额度占用）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT target_id, COUNT(*) AS n FROM deliveries
                WHERE status = 'sent'
                  AND date(updated_at, 'localtime') = date('now', 'localtime')
                GROUP BY target_id
                """
            ).fetchall()
        return {row["target_id"]: int(row["n"]) for row in rows}

    def sent_ok_today(self, *, as_repost: bool | None = None) -> int:
        """今天**真正发成功**的条数（和配额预占不同）。

        配额是在发送前预占的（防止重试无限消耗额度），所以 daily_counter 里的
        数字是"额度占用"，会包含最终失败/跳过的那几次。
        对外报数一律用这个：直接数投递账本里 status='sent' 的行。

        日期边界用 SQLite 的 localtime，和 Python 的 date.today() 保持一致，
        否则在 UTC+8 这类时区会横跨一天算错。
        """
        prefix = "repost:"
        sql = """
            SELECT COUNT(*) AS n FROM deliveries
            WHERE status = 'sent' AND date(updated_at, 'localtime') = date('now', 'localtime')
        """
        params: tuple[Any, ...] = ()
        if as_repost is True:
            sql += " AND job_id LIKE ?"
            params = (prefix + "%",)
        elif as_repost is False:
            sql += " AND job_id NOT LIKE ?"
            params = (prefix + "%",)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def bump_repost(self, msg_id: int, source_id: int | str | None = None) -> int:
        """记录某条素材被重发的次数（跨重启累计）。

        source_id 建议显式传入；未传时用 set_default_source 设定的值，
        再没有就用 'unknown'，避免静默地记到一个错误的分组下。
        """
        if source_id is None:
            source_id = self._default_source
        if source_id is None:
            logging.getLogger("tgrelay.db").debug("bump_repost 未指定 source_id，记为 unknown")
            source_id = "unknown"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reposts (source_id, msg_id, count, last_at)
                VALUES (?, ?, 1, datetime('now'))
                ON CONFLICT(source_id, msg_id) DO UPDATE SET
                    count = reposts.count + 1,
                    last_at = excluded.last_at
                """,
                (str(source_id), int(msg_id)),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT count FROM reposts WHERE source_id = ? AND msg_id = ?",
                (str(source_id), int(msg_id)),
            ).fetchone()
            return int(row["count"]) if row else 0

    def repost_counts(self) -> dict[int, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT msg_id, count FROM reposts ORDER BY msg_id"
            ).fetchall()
        return {int(row["msg_id"]): int(row["count"]) for row in rows}

    # ---------------- 投递账本 ----------------

    def record_delivery(
        self,
        *,
        job_id: str,
        target_id: int | str,
        source_id: int | str,
        source_msg_id: int,
        status: str,
        target_msg_ids: Sequence[int] = (),
        error: str = "",
    ) -> None:
        joined = "\n".join(str(item) for item in target_msg_ids)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO deliveries
                    (job_id, target_id, source_id, source_msg_id, target_msg_ids, status, error, attempts, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
                ON CONFLICT(job_id, target_id) DO UPDATE SET
                    target_msg_ids = CASE
                        WHEN excluded.target_msg_ids <> '' THEN excluded.target_msg_ids
                        ELSE deliveries.target_msg_ids END,
                    status   = excluded.status,
                    error    = excluded.error,
                    attempts = deliveries.attempts + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    str(target_id),
                    str(source_id),
                    int(source_msg_id),
                    joined,
                    status,
                    error[:500],
                ),
            )
            self._conn.commit()

    def find_deliveries(self, source_id: int | str, source_msg_id: int) -> list[Delivery]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM deliveries
                WHERE source_id = ? AND source_msg_id = ? AND status = 'sent'
                """,
                (str(source_id), int(source_msg_id)),
            ).fetchall()
        return [self._row_to_delivery(row) for row in rows]

    def find_delivery(self, job_id: str, target_id: int | str) -> Delivery | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deliveries WHERE job_id = ? AND target_id = ?",
                (job_id, str(target_id)),
            ).fetchone()
        return self._row_to_delivery(row) if row else None

    def failed_deliveries(self, limit: int = 100) -> list[Delivery]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM deliveries WHERE status <> 'sent'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_delivery(row) for row in rows]

    @staticmethod
    def _row_to_delivery(row: sqlite3.Row) -> Delivery:
        raw_ids = (row["target_msg_ids"] or "").strip()
        ids = tuple(int(part) for part in raw_ids.splitlines() if part.strip()) if raw_ids else ()
        return Delivery(
            job_id=row["job_id"],
            target_id=row["target_id"],
            source_id=row["source_id"],
            source_msg_id=int(row["source_msg_id"]),
            target_msg_ids=ids,
            status=row["status"],
            error=row["error"] or "",
            attempts=int(row["attempts"]),
        )

    # ---------------- 统计 / 维护 ----------------

    def stats(self) -> dict[str, int]:
        with self._lock:
            seen = self._conn.execute("SELECT COUNT(*) AS n FROM seen").fetchone()["n"]
            sent = self._conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status = 'sent'"
            ).fetchone()["n"]
            failed = self._conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status <> 'sent'"
            ).fetchone()["n"]
        return {
            "seen": int(seen),
            "sent": int(sent),
            "failed": int(failed),
            "sent_today": self.sent_today(),
            "reposted_today": self.reposted_today(),
            "sent_ok_today": self.sent_ok_today(),
            "repost_ok_today": self.sent_ok_today(as_repost=True),
        }

    def prune_seen(self, keep_per_source: int = 20000) -> int:
        """只保留每个源最近的 N 条去重记录，避免库无限增长。"""
        removed = 0
        with self._lock:
            sources = [
                row["source_id"]
                for row in self._conn.execute("SELECT DISTINCT source_id FROM seen").fetchall()
            ]
            for source_id in sources:
                cursor = self._conn.execute(
                    """
                    DELETE FROM seen WHERE source_id = ? AND msg_id NOT IN (
                        SELECT msg_id FROM seen WHERE source_id = ?
                        ORDER BY msg_id DESC LIMIT ?
                    )
                    """,
                    (source_id, source_id, int(keep_per_source)),
                )
                removed += cursor.rowcount
            self._conn.commit()
        return removed

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def reset(store: Store, source_id: int | str, msg_id: int) -> None:
    """把某条消息标记为"未处理"，下次补漏或重放时会重新转发（用于手动重试）。"""
    store.forget_seen(source_id, msg_id)


def reset_many(store: Store, pairs: Iterable[tuple[int | str, int]]) -> int:
    count = 0
    for source_id, msg_id in pairs:
        store.forget_seen(source_id, msg_id)
        count += 1
    return count
