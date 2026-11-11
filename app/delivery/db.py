"""投递端持久层：SQLite 快照、修订号、租约与状态流转。

设计要点
--------
- 每次提交保存**完整快照**（result_id / sample_code / item / value 及任务号），
  并在任务内分配递增修订号 ``revision``。
- 每个修订有终身不变的 ``delivery_id``；任何重试（超时、进程退出、worker 崩溃）
  都沿用同一投递 ID 与同一份快照，LIMS 据此幂等去重。
- worker 通过 ``lease_owner`` / ``lease_expires_at`` 行级租约认领修订，
  两个 worker 进程并发也只有一个能真正发出 HTTP 请求。
- 事件表 ``delivery_events`` 只增不减，完整保留每次触发、尝试、成功、失败的轨迹。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

# 修订生命周期状态。
STATUS_PENDING = "pending"          # 已入库，等待投递
STATUS_DELIVERING = "delivering"    # 已被某个 worker 租约认领，投递途中
STATUS_SYNCHRONIZED = "synchronized"  # 收到与本修订匹配的 LIMS 成功回执
STATUS_FAILED = "failed"            # 被 LIMS 明确拒绝（409 等同 ID 异内容等），保留数据待人工处理


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_delivery_id() -> str:
    # 终身不变的投递 ID：rev 前缀便于日志与 LIMS 侧辨认。
    return f"dlv-{uuid.uuid4().hex}"


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """打开一个短连接。

    WAL 模式允许 API 进程与多个 worker 进程同时打开同一数据库文件；
    ``busy_timeout`` 让写锁竞争时等待而不是立即报错。
    """
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    """建表（幂等）。"""
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS result_revisions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id             TEXT NOT NULL,
                revision            INTEGER NOT NULL,
                delivery_id         TEXT NOT NULL UNIQUE,
                result_id           TEXT NOT NULL,
                sample_code         TEXT NOT NULL,
                item                TEXT NOT NULL,
                value               TEXT NOT NULL,
                snapshot_json       TEXT NOT NULL,
                status              TEXT NOT NULL,
                attempts            INTEGER NOT NULL DEFAULT 0,
                lease_owner         TEXT,
                lease_expires_at    TEXT,
                last_error          TEXT,
                lims_revision       INTEGER,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                synchronized_at     TEXT,
                UNIQUE (task_id, revision)
            );

            CREATE INDEX IF NOT EXISTS idx_revisions_task
                ON result_revisions (task_id, revision DESC);
            CREATE INDEX IF NOT EXISTS idx_revisions_status
                ON result_revisions (status, lease_expires_at);

            CREATE TABLE IF NOT EXISTS delivery_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id     TEXT NOT NULL,
                task_id         TEXT NOT NULL,
                revision        INTEGER NOT NULL,
                event_type      TEXT NOT NULL,
                detail_json     TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_delivery
                ON delivery_events (delivery_id, id);
            """
        )


def _row_to_revision(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["snapshot"] = json.loads(data.pop("snapshot_json"))
    return data


def add_event(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    task_id: str,
    revision: int,
    event_type: str,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO delivery_events
            (delivery_id, task_id, revision, event_type, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            delivery_id,
            task_id,
            revision,
            event_type,
            json.dumps(detail, ensure_ascii=False, sort_keys=True),
            utc_now_iso(),
        ),
    )


def create_revision(
    task_id: str,
    *,
    result_id: str,
    sample_code: str,
    item: str,
    value: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """提交一次结果（含更正），在任务内分配下一个递增修订号。

    修订号分配与快照写入在同一事务内完成，并发提交也不会重号。
    新修订一律从 ``pending`` 开始；即使同一任务的旧修订仍在投递途中，
    也不阻塞新修订入库。
    """
    snapshot = {
        "task_id": task_id,
        "result_id": result_id,
        "sample_code": sample_code,
        "item": item,
        "value": value,
    }
    delivery_id = _new_delivery_id()
    now = utc_now_iso()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS max_revision "
            "FROM result_revisions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        revision = row["max_revision"] + 1
        conn.execute(
            """
            INSERT INTO result_revisions (
                task_id, revision, delivery_id, result_id, sample_code, item,
                value, snapshot_json, status, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                task_id,
                revision,
                delivery_id,
                result_id,
                sample_code,
                item,
                value,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                STATUS_PENDING,
                now,
                now,
            ),
        )
        add_event(
            conn,
            delivery_id=delivery_id,
            task_id=task_id,
            revision=revision,
            event_type="submitted",
            detail={"snapshot": snapshot},
        )
        conn.execute("COMMIT")
        created = get_revision(conn, task_id, revision)
    assert created is not None
    return created


def get_revision(
    conn_or_path: sqlite3.Connection | Path | None,
    task_id: str,
    revision: int,
) -> dict[str, Any] | None:
    if isinstance(conn_or_path, sqlite3.Connection):
        row = conn_or_path.execute(
            "SELECT * FROM result_revisions WHERE task_id = ? AND revision = ?",
            (task_id, revision),
        ).fetchone()
        return _row_to_revision(row) if row else None
    with connect(conn_or_path) as conn:
        return get_revision(conn, task_id, revision)


def get_revision_by_delivery_id(
    conn: sqlite3.Connection, delivery_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM result_revisions WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    return _row_to_revision(row) if row else None


def list_task_revisions(task_id: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM result_revisions WHERE task_id = ? ORDER BY revision",
            (task_id,),
        ).fetchall()
        return [_row_to_revision(row) for row in rows]


def list_tasks(db_path: Path | None = None) -> list[dict[str, Any]]:
    """返回每个任务的当前（最高）修订与同步状态摘要。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.*
            FROM result_revisions r
            JOIN (
                SELECT task_id, MAX(revision) AS max_revision
                FROM result_revisions GROUP BY task_id
            ) latest
              ON latest.task_id = r.task_id AND latest.max_revision = r.revision
            ORDER BY r.task_id
            """
        ).fetchall()
        return [_row_to_revision(row) for row in rows]


def list_events(
    task_id: str, revision: int, db_path: Path | None = None
) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM delivery_events
            WHERE task_id = ? AND revision = ?
            ORDER BY id
            """,
            (task_id, revision),
        ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


def claim_next_pending(owner: str, db_path: Path | None = None) -> dict[str, Any] | None:
    """认领一个可投递的修订。

    可投递 = ``pending`` / ``failed``（可被重新触发）/ 租约已过期的
    ``delivering``（上个 worker 超时或进程退出）。
    已同步的修订永不会被再次认领。认领通过条件更新保证跨进程互斥：
    两个 worker 同时轮询时只有一个 UPDATE 能命中该行。
    """
    now = utc_now_iso()
    lease_until = (
        (datetime.now(timezone.utc).timestamp() + config.LEASE_SECONDS)
    )
    lease_expires_iso = (
        datetime.fromtimestamp(lease_until, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM result_revisions
            WHERE status IN (?, ?)
               OR (status = ? AND lease_expires_at IS NOT NULL
                   AND lease_expires_at < ?)
            ORDER BY task_id, revision
            LIMIT 1
            """,
            (STATUS_PENDING, STATUS_FAILED, STATUS_DELIVERING, now),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE result_revisions
            SET status = ?, lease_owner = ?, lease_expires_at = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE id = ?
            """,
            (STATUS_DELIVERING, owner, lease_expires_iso, now, row["id"]),
        )
        add_event(
            conn,
            delivery_id=row["delivery_id"],
            task_id=row["task_id"],
            revision=row["revision"],
            event_type="delivery_attempt",
            detail={
                "owner": owner,
                "attempt": row["attempts"] + 1,
                "delivery_id": row["delivery_id"],
            },
        )
        conn.execute("COMMIT")
        return get_revision(conn, row["task_id"], row["revision"])


def heartbeat(owner: str, db_path: Path | None = None) -> int:
    """续租：worker 投递期间定期调用，防止长请求导致租约被他人抢走。"""
    lease_expires_iso = (
        datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + config.LEASE_SECONDS,
            tz=timezone.utc,
        )
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE result_revisions
            SET lease_expires_at = ?
            WHERE lease_owner = ? AND status = ?
            """,
            (lease_expires_iso, owner, STATUS_DELIVERING),
        )
        return cur.rowcount


def mark_synchronized(
    delivery_id: str,
    *,
    lims_revision: int,
    receipt: dict[str, Any],
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """收到成功回执后确认同步。

    只有回执中的 ``delivery_id`` 与本地修订完全一致才会确认；
    旧修订迟到的成功回执命中不到新修订的 delivery_id，无法把新修订
    标记为已同步。
    """
    now = utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM result_revisions WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE result_revisions
            SET status = ?, synchronized_at = ?, lims_revision = ?,
                lease_owner = NULL, lease_expires_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (STATUS_SYNCHRONIZED, now, lims_revision, now, row["id"]),
        )
        add_event(
            conn,
            delivery_id=delivery_id,
            task_id=row["task_id"],
            revision=row["revision"],
            event_type="synchronized",
            detail={"receipt": receipt, "lims_revision": lims_revision},
        )
        return get_revision(conn, row["task_id"], row["revision"])


def mark_failed(
    delivery_id: str,
    *,
    error: str,
    retryable: bool,
    response_status: int | None = None,
    response_body: Any = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """投递失败处理。

    - ``retryable=True``（超时、断连、5xx、网络错误）：回到 ``pending``，
      保留快照与投递 ID，等待重新触发（worker 自动轮询或手动 retry）。
    - ``retryable=False``（409 同 ID 异内容等明确拒绝）：置 ``failed``，
      数据完整保留，需人工介入；可通过 retry 接口重新触发。
    """
    now = utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM result_revisions WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            return None
        new_status = STATUS_PENDING if retryable else STATUS_FAILED
        conn.execute(
            """
            UPDATE result_revisions
            SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_status, error, now, row["id"]),
        )
        add_event(
            conn,
            delivery_id=delivery_id,
            task_id=row["task_id"],
            revision=row["revision"],
            event_type="delivery_failed",
            detail={
                "error": error,
                "retryable": retryable,
                "response_status": response_status,
                "response_body": response_body,
                "next_status": new_status,
            },
        )
        return get_revision(conn, row["task_id"], row["revision"])


def retrigger(task_id: str, revision: int, db_path: Path | None = None) -> dict[str, Any]:
    """手动重新触发投递。

    任何未同步修订都可重新触发：清除租约与错误，回到 ``pending``，
    投递 ID 与快照保持不变。已同步修订返回 409（由路由层判断）。
    """
    now = utc_now_iso()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM result_revisions WHERE task_id = ? AND revision = ?",
            (task_id, revision),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            raise KeyError((task_id, revision))
        conn.execute(
            """
            UPDATE result_revisions
            SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (STATUS_PENDING, now, row["id"]),
        )
        add_event(
            conn,
            delivery_id=row["delivery_id"],
            task_id=task_id,
            revision=revision,
            event_type="retriggered",
            detail={"by": "api"},
        )
        conn.execute("COMMIT")
        result = get_revision(conn, task_id, revision)
    assert result is not None
    return result


def stale_delivering_count(db_path: Path | None = None) -> int:
    """统计租约过期但仍处于 delivering 的修订（供健康检查/测试使用）。"""
    now = utc_now_iso()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM result_revisions "
            "WHERE status = ? AND lease_expires_at < ?",
            (STATUS_DELIVERING, now),
        ).fetchone()
        return int(row["n"])
