"""模拟 LIMS 的 SQLite 持久层。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import LIMS_DB_PATH


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or LIMS_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS lims_results (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id      TEXT NOT NULL UNIQUE,
                task_id          TEXT NOT NULL,
                revision         INTEGER NOT NULL,
                result_id        TEXT NOT NULL,
                sample_code      TEXT NOT NULL,
                item             TEXT NOT NULL,
                value            TEXT NOT NULL,
                payload_json     TEXT NOT NULL,
                stored_at        TEXT NOT NULL,
                receipt_count    INTEGER NOT NULL DEFAULT 0,
                last_receipt_at  TEXT,
                UNIQUE (task_id, revision)
            );
            CREATE INDEX IF NOT EXISTS idx_lims_task ON lims_results (task_id, revision);

            CREATE TABLE IF NOT EXISTS lims_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id  TEXT NOT NULL,
                task_id      TEXT,
                event_type   TEXT NOT NULL,
                detail_json  TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            """
        )


def add_event(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    event_type: str,
    detail: dict[str, Any],
    task_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO lims_events (delivery_id, task_id, event_type, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            delivery_id,
            task_id,
            event_type,
            json.dumps(detail, ensure_ascii=False, sort_keys=True),
            utc_now_iso(),
        ),
    )


def get_by_delivery_id(
    conn: sqlite3.Connection, delivery_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM lims_results WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    return data


def get_task_latest(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM lims_results WHERE task_id = ? ORDER BY revision DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    return data


def get_task_revision(
    conn: sqlite3.Connection, task_id: str, revision: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM lims_results WHERE task_id = ? AND revision = ?",
        (task_id, revision),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    return data


def store_result(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO lims_results (
            delivery_id, task_id, revision, result_id, sample_code, item,
            value, payload_json, stored_at, receipt_count, last_receipt_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            payload["delivery_id"],
            payload["task_id"],
            payload["revision"],
            payload["result_id"],
            payload["sample_code"],
            payload["item"],
            payload["value"],
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    add_event(
        conn,
        delivery_id=payload["delivery_id"],
        task_id=payload["task_id"],
        event_type="stored",
        detail={"revision": payload["revision"], "value": payload["value"]},
    )
    return get_by_delivery_id(conn, payload["delivery_id"])  # type: ignore[return-value]


def record_replay(conn: sqlite3.Connection, delivery_id: str) -> None:
    """幂等重放：累计回执次数，不新增数据行。"""
    conn.execute(
        "UPDATE lims_results SET receipt_count = receipt_count + 1, last_receipt_at = ? "
        "WHERE delivery_id = ?",
        (utc_now_iso(), delivery_id),
    )
    add_event(
        conn,
        delivery_id=delivery_id,
        event_type="idempotent_replay",
        detail={},
    )


def record_rejected(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    task_id: str | None,
    code: str,
    detail: dict[str, Any],
) -> None:
    add_event(
        conn,
        delivery_id=delivery_id,
        task_id=task_id,
        event_type="rejected",
        detail={"code": code, **detail},
    )


def list_results(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM lims_results ORDER BY task_id, revision"
    ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        result.append(data)
    return result


def list_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, delivery_id, task_id, event_type, detail_json, created_at "
        "FROM lims_events ORDER BY id"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "delivery_id": row["delivery_id"],
            "task_id": row["task_id"],
            "event_type": row["event_type"],
            "detail": json.loads(row["detail_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def reset(db_path: Path | None = None) -> None:
    """清空模拟 LIMS 数据（仅用于演示/测试）。"""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM lims_results")
        conn.execute("DELETE FROM lims_events")
