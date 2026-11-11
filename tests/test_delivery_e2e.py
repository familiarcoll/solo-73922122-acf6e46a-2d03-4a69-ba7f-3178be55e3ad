"""端到端测试：真实 worker 循环经 ASGI HTTP 调用模拟 LIMS。

覆盖需求中的关键保证：
- 正常投递与同步确认；
- 投递途中提交更正，旧回执不能把新修订标为已同步；
- 模拟“入库后断连”后重试幂等、不重复入库；
- 两个 worker 并发投递不重复入库、远端修订不倒退；
- 重启（worker 停止后重新启动）后沿用投递 ID 继续。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app import lims as lims_package
from app.delivery import config as delivery_config
from app.delivery import db as delivery_db
from app.delivery import lims_client
from app.delivery.worker import DeliveryWorker
from app.lims import db as lims_db
from app.lims.app import app as lims_app

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def setup_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    delivery_path = tmp_path / "delivery-e2e.db"
    lims_path = tmp_path / "lims-e2e.db"
    monkeypatch.setattr(delivery_config, "DB_PATH", delivery_path)
    monkeypatch.setattr(lims_package, "LIMS_DB_PATH", lims_path, raising=False)
    monkeypatch.setattr(lims_db, "LIMS_DB_PATH", lims_path, raising=False)
    monkeypatch.setattr(delivery_config, "LIMS_BASE_URL", "http://lims-asgi")
    monkeypatch.setattr(delivery_config, "POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(delivery_config, "BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(delivery_config, "LEASE_SECONDS", 30)
    delivery_db.initialize_db(delivery_path)
    lims_db.initialize_db(lims_path)
    return delivery_path, lims_path


@pytest.fixture()
def asgi_http(monkeypatch: pytest.MonkeyPatch):
    """把 lims_client 新建的 AsyncClient 绑定到 LIMS 的 ASGI transport。

    行为与真实 HTTP 一致（状态码、JSON、断连注入都由 uvicorn 之外的
    httpx ASGI transport 复现），只是不占用网络端口。
    """
    transport = httpx.ASGITransport(app=lims_app)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self_inner: httpx.AsyncClient, *args, **kwargs) -> None:
        kwargs["transport"] = transport
        kwargs["base_url"] = "http://lims-asgi"
        original_init(self_inner, *args, **kwargs)

    monkeypatch.setattr(lims_client.httpx.AsyncClient, "__init__", patched_init)
    return transport


async def _poll_status(
    task_id: str, revision: int, expected: str, timeout: float = 5.0
) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        row = delivery_db.get_revision(None, task_id, revision)
        assert row is not None
        if row["status"] == expected:
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"revision {revision} 未在 {timeout}s 内达到 {expected}")


async def test_happy_path_delivery_and_confirmation(setup_paths, asgi_http) -> None:
    worker = DeliveryWorker("w1")
    worker.start()
    try:
        rev = delivery_db.create_revision(
            "T-1", result_id="R-1", sample_code="S-1", item="pH", value="7.1"
        )
        await _poll_status("T-1", 1, delivery_db.STATUS_SYNCHRONIZED)

        with lims_db.connect() as conn:
            rows = lims_db.list_results(conn)
        assert len(rows) == 1
        assert rows[0]["delivery_id"] == rev["delivery_id"]
        assert rows[0]["value"] == "7.1"

        local = delivery_db.get_revision(None, "T-1", 1)
        assert local is not None and local["lims_revision"] == 1
    finally:
        await worker.stop()


async def test_correction_in_flight_old_receipt_cannot_sync_new_revision(
    setup_paths, asgi_http
) -> None:
    worker = DeliveryWorker("w1")
    worker.start()
    try:
        delivery_db.create_revision(
            "T-1", result_id="R-1", sample_code="S-1", item="pH", value="7.1"
        )
        await _poll_status("T-1", 1, delivery_db.STATUS_SYNCHRONIZED)

        # 更正：revision 2，新的 delivery_id 与快照。
        rev2 = delivery_db.create_revision(
            "T-1", result_id="R-1", sample_code="S-1", item="pH", value="7.9"
        )
        await _poll_status("T-1", 2, delivery_db.STATUS_SYNCHRONIZED)

        # 旧修订 1 的回执迟到：其 delivery_id 与 rev2 不同，
        # mark_synchronized 只可能命中 rev1，新修订状态不受影响。
        rev1 = delivery_db.get_revision(None, "T-1", 1)
        assert rev1 is not None
        result = delivery_db.mark_synchronized(
            rev1["delivery_id"],
            lims_revision=1,
            receipt={"delivery_id": rev1["delivery_id"]},
        )
        assert result is not None and result["revision"] == 1
        current = delivery_db.get_revision(None, "T-1", 2)
        assert current is not None
        assert current["status"] == delivery_db.STATUS_SYNCHRONIZED
        assert current["delivery_id"] == rev2["delivery_id"]

        with lims_db.connect() as conn:
            latest = lims_db.get_task_latest(conn, "T-1")
        assert latest is not None
        assert latest["revision"] == 2 and latest["value"] == "7.9"
    finally:
        await worker.stop()


async def test_disconnect_after_store_retry_is_idempotent(
    setup_paths, asgi_http
) -> None:
    async with httpx.AsyncClient(
        transport=asgi_http, base_url="http://lims-asgi"
    ) as direct:
        worker = DeliveryWorker("w1")
        worker.start()
        try:
            rev = delivery_db.create_revision(
                "T-9", result_id="R-9", sample_code="S-9", item="Pb", value="0.02"
            )
            # 模拟首次投递“入库后断开回执”：LIMS 已提交数据但响应流中断。
            payload = {
                "delivery_id": rev["delivery_id"],
                "task_id": "T-9",
                "revision": 1,
                "result_id": "R-9",
                "sample_code": "S-9",
                "item": "Pb",
                "value": "0.02",
                "simulate": "disconnect_after_store",
            }
            # ASGI transport 下表现为 ConnectionResetError；真实 uvicorn 下
            # httpx 会将其包装为 ReadError/RemoteProtocolError（同为 OSError 体系）。
            with pytest.raises((httpx.HTTPError, OSError)):
                await direct.post("/lims/results", json=payload)

            with lims_db.connect() as conn:
                assert len(lims_db.list_results(conn)) == 1

            # worker 用同一 delivery_id 重试 → 幂等重放、确认同步、无重复入库。
            await _poll_status("T-9", 1, delivery_db.STATUS_SYNCHRONIZED, timeout=8)
            with lims_db.connect() as conn:
                rows = lims_db.list_results(conn)
                events = lims_db.list_events(conn)
            assert len(rows) == 1
            assert rows[0]["receipt_count"] == 2
            assert sum(1 for e in events if e["event_type"] == "stored") == 1

            row = delivery_db.get_revision(None, "T-9", 1)
            assert row is not None and row["delivery_id"] == rev["delivery_id"]
        finally:
            await worker.stop()


async def test_two_workers_concurrent_no_duplicate_or_regression(
    setup_paths, asgi_http
) -> None:
    workers = [DeliveryWorker(f"w{i}") for i in range(2)]
    for w in workers:
        w.start()
    try:
        # 多任务、多修订洪泛提交，交错产生“投递途中的更正”。
        for task in range(5):
            task_id = f"T-{task}"
            for value in ("1.0", "2.0", "3.0"):
                delivery_db.create_revision(
                    task_id,
                    result_id=f"R-{task}",
                    sample_code=f"S-{task}",
                    item="pH",
                    value=value,
                )
                await asyncio.sleep(0.01)

        for task in range(5):
            for revision in range(1, 4):
                await _poll_status(
                    f"T-{task}", revision,
                    delivery_db.STATUS_SYNCHRONIZED, timeout=15,
                )

        with lims_db.connect() as conn:
            all_rows = lims_db.list_results(conn)
            for task in range(5):
                latest = lims_db.get_task_latest(conn, f"T-{task}")
                assert latest is not None
                # 远端每个任务只保留最高修订 3，不倒退。
                assert latest["revision"] == 3
                assert latest["value"] == "3.0"

        # 每个 (task, revision) 在 LIMS 只有一行：共 15 行，无重复入库。
        assert len(all_rows) == 15
        keys = {(r["task_id"], r["revision"]) for r in all_rows}
        assert len(keys) == 15
    finally:
        for w in workers:
            await w.stop()


async def test_worker_restart_resumes_pending_revisions(
    setup_paths, asgi_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 把租约缩到极短，模拟 worker 认领后立刻崩溃、租约很快到期。
    monkeypatch.setattr(delivery_config, "LEASE_SECONDS", 0.05)
    rev = delivery_db.create_revision(
        "T-restart", result_id="R-1", sample_code="S-1", item="pH", value="5.5"
    )
    delivery_id = rev["delivery_id"]

    # 模拟 worker 认领后进程退出：修订停在 delivering，等待租约到期。
    delivery_db.claim_next_pending("dead-worker")
    current = delivery_db.get_revision(None, "T-restart", 1)
    assert current is not None and current["status"] == delivery_db.STATUS_DELIVERING

    # 新 worker 启动，租约到期后接管（模拟超时/退出后的恢复）。
    worker = DeliveryWorker("new-worker")
    worker.start()
    try:
        await _poll_status("T-restart", 1, delivery_db.STATUS_SYNCHRONIZED, timeout=8)
    finally:
        await worker.stop()

    row = delivery_db.get_revision(None, "T-restart", 1)
    assert row is not None
    # 沿用同一投递 ID 完成投递，尝试次数体现了“崩溃后重试”。
    assert row["delivery_id"] == delivery_id
    assert row["attempts"] >= 2
