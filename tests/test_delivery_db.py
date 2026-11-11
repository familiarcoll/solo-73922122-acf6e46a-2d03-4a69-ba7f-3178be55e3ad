"""投递端持久层测试：修订号、快照、租约、状态流转、并发互斥。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.delivery import db


def _submit(db_path: Path, task_id: str = "T-1", value: str = "7.10") -> dict:
    return db.create_revision(
        task_id,
        result_id="R-1",
        sample_code="S-1",
        item="pH",
        value=value,
        db_path=db_path,
    )


def test_revision_numbers_increment_per_task_and_snapshot_is_immutable(
    delivery_db_path: Path,
) -> None:
    rev1 = _submit(delivery_db_path, value="7.10")
    rev2 = _submit(delivery_db_path, value="7.15")
    rev3 = db.create_revision(
        "T-2",
        result_id="R-9",
        sample_code="S-9",
        item="Pb",
        value="0.01",
        db_path=delivery_db_path,
    )

    assert rev1["revision"] == 1
    assert rev2["revision"] == 2
    # 修订号按任务独立计数。
    assert rev3["revision"] == 1
    # 每次修订有不同且终身不变的投递 ID。
    assert rev1["delivery_id"] != rev2["delivery_id"]
    # 完整快照独立保存，更正不覆盖旧修订。
    assert rev1["snapshot"]["value"] == "7.10"
    assert rev2["snapshot"]["value"] == "7.15"
    assert rev1["status"] == db.STATUS_PENDING

    stored_rev1 = db.get_revision(delivery_db_path, "T-1", 1)
    assert stored_rev1 is not None and stored_rev1["snapshot"]["value"] == "7.10"


def test_concurrent_submissions_get_distinct_revisions(delivery_db_path: Path) -> None:
    revisions: list[int] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(20):
                rev = _submit(delivery_db_path)
                revisions.append(rev["revision"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(revisions) == list(range(1, 81))
    assert len(set(revisions)) == 80


def test_lease_claim_is_mutually_exclusive(delivery_db_path: Path) -> None:
    _submit(delivery_db_path)
    _submit(delivery_db_path)

    claimed: list[tuple[str, int]] = []
    barrier = threading.Barrier(4)

    def claim(owner: str) -> None:
        barrier.wait()
        row = db.claim_next_pending(owner, delivery_db_path)
        if row is not None:
            claimed.append((owner, row["revision"]))

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 两个修订各自只能被一个 worker 认领；其余 worker 本轮空手而归。
    assert len(claimed) == 2
    assert {revision for _, revision in claimed} == {1, 2}
    assert len({owner for owner, _ in claimed}) == 2

    # 已被认领（delivering 且租约未过期）的修订不会被再次认领。
    assert db.claim_next_pending("late-worker", delivery_db_path) is None


def test_expired_lease_is_reclaimable_keeping_delivery_id(delivery_db_path: Path) -> None:
    import time

    from app.delivery import config

    rev = _submit(delivery_db_path)
    original_delivery_id = rev["delivery_id"]
    claimed = db.claim_next_pending("worker-a", delivery_db_path)
    assert claimed is not None and claimed["status"] == db.STATUS_DELIVERING

    # 租约未过期：其他 worker 不能抢。
    assert db.claim_next_pending("worker-b", delivery_db_path) is None

    # 把租约改为过去时间，模拟 worker 崩溃/进程退出。
    with db.connect(delivery_db_path) as conn:
        conn.execute(
            "UPDATE result_revisions SET lease_expires_at = '2000-01-01T00:00:00Z'"
        )
    assert config.LEASE_SECONDS > 0
    time.sleep(0.01)

    reclaimed = db.claim_next_pending("worker-b", delivery_db_path)
    assert reclaimed is not None
    # 接管后沿用同一投递 ID 与快照，重试不会被 LIMS 当成新投递。
    assert reclaimed["delivery_id"] == original_delivery_id
    assert reclaimed["attempts"] == 2
    assert reclaimed["lease_owner"] == "worker-b"


def test_mark_synchronized_only_matches_own_delivery_id(delivery_db_path: Path) -> None:
    rev1 = _submit(delivery_db_path, value="7.10")
    rev2 = _submit(delivery_db_path, value="7.15")

    # 旧修订（rev1）迟到的成功回执不能确认新修订（rev2）。
    assert db.mark_synchronized(
        rev1["delivery_id"], lims_revision=1, receipt={"delivery_id": rev1["delivery_id"]},
        db_path=delivery_db_path,
    ) is not None
    rev2_after = db.get_revision(delivery_db_path, "T-1", 2)
    assert rev2_after is not None and rev2_after["status"] == db.STATUS_PENDING

    # 不存在的投递 ID 什么也不确认。
    assert db.mark_synchronized(
        "dlv-does-not-exist", lims_revision=9, receipt={}, db_path=delivery_db_path
    ) is None

    # 正确回执才能确认 rev2。
    confirmed = db.mark_synchronized(
        rev2["delivery_id"], lims_revision=2,
        receipt={"delivery_id": rev2["delivery_id"]}, db_path=delivery_db_path,
    )
    assert confirmed is not None
    assert confirmed["status"] == db.STATUS_SYNCHRONIZED
    assert confirmed["lims_revision"] == 2
    assert confirmed["synchronized_at"] is not None
    assert confirmed["lease_owner"] is None

    # 已同步修订不会再被 worker 认领。
    assert db.claim_next_pending("worker", delivery_db_path) is None


def test_retryable_failure_returns_to_pending_and_keeps_data(delivery_db_path: Path) -> None:
    rev = _submit(delivery_db_path)
    db.claim_next_pending("worker-a", delivery_db_path)
    failed = db.mark_failed(
        rev["delivery_id"], error="connection reset", retryable=True,
        response_status=None, response_body=None, db_path=delivery_db_path,
    )
    assert failed is not None
    assert failed["status"] == db.STATUS_PENDING
    assert failed["last_error"] == "connection reset"
    assert failed["delivery_id"] == rev["delivery_id"]
    assert failed["snapshot"]["value"] == "7.10"

    # 回到 pending 后可被重新认领，投递 ID 不变。
    again = db.claim_next_pending("worker-b", delivery_db_path)
    assert again is not None and again["delivery_id"] == rev["delivery_id"]


def test_hard_failure_stays_failed_until_manual_retrigger(delivery_db_path: Path) -> None:
    rev = _submit(delivery_db_path)
    db.claim_next_pending("worker-a", delivery_db_path)
    hard = db.mark_failed(
        rev["delivery_id"], error="409 conflict", retryable=False,
        response_status=409, response_body={"code": "idempotency_conflict"},
        db_path=delivery_db_path,
    )
    assert hard is not None and hard["status"] == db.STATUS_FAILED

    # failed 也可被 worker 认领重试（人工 retrigger 之外的自动补偿路径）。
    claimed = db.claim_next_pending("worker-b", delivery_db_path)
    assert claimed is not None and claimed["delivery_id"] == rev["delivery_id"]

    # 手动 retrigger 显式回到 pending。
    db.mark_failed(
        rev["delivery_id"], error="409 conflict", retryable=False,
        response_status=409, response_body=None, db_path=delivery_db_path,
    )
    retried = db.retrigger("T-1", 1, delivery_db_path)
    assert retried["status"] == db.STATUS_PENDING
    assert retried["delivery_id"] == rev["delivery_id"]
    assert retried["last_error"] is None

    with pytest.raises(KeyError):
        db.retrigger("T-1", 99, delivery_db_path)


def test_list_tasks_returns_latest_revision_only(delivery_db_path: Path) -> None:
    _submit(delivery_db_path, "T-1", "1")
    _submit(delivery_db_path, "T-1", "2")
    db.create_revision(
        "T-2", result_id="R-2", sample_code="S-2", item="Pb", value="0.1",
        db_path=delivery_db_path,
    )
    tasks = db.list_tasks(delivery_db_path)
    assert [(t["task_id"], t["revision"]) for t in tasks] == [("T-1", 2), ("T-2", 1)]

    timeline = db.list_events("T-1", 1, delivery_db_path)
    assert [e["event_type"] for e in timeline][0] == "submitted"
