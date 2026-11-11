"""模拟 LIMS 行为测试：幂等去重、异内容拒绝、最高修订、断连/延迟/500 注入。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.lims import db as lims_db
from app.lims.app import app


@pytest.fixture()
def client(lims_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app import lims as lims_package

    monkeypatch.setattr(lims_package, "LIMS_DB_PATH", lims_db_path, raising=False)
    monkeypatch.setattr(lims_db, "LIMS_DB_PATH", lims_db_path, raising=False)
    return TestClient(app)


def _payload(delivery_id: str = "dlv-1", revision: int = 1, **overrides) -> dict:
    payload = {
        "delivery_id": delivery_id,
        "task_id": "T-1",
        "revision": revision,
        "result_id": "R-1",
        "sample_code": "S-1",
        "item": "pH",
        "value": "7.10",
    }
    payload.update(overrides)
    return payload


def test_first_store_then_idempotent_replay(client: TestClient) -> None:
    r1 = client.post("/lims/results", json=_payload())
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["replayed"] is False
    assert body1["receipt_count"] == 1
    stored_at = body1["stored_at"]

    r2 = client.post("/lims/results", json=_payload())
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["replayed"] is True
    assert body2["receipt_count"] == 2
    # 重放不产生新行、stored_at 不变。
    assert body2["stored_at"] == stored_at

    with lims_db.connect() as conn:
        rows = lims_db.list_results(conn)
        events = lims_db.list_events(conn)
    assert len(rows) == 1
    assert [e["event_type"] for e in events].count("stored") == 1
    assert [e["event_type"] for e in events].count("idempotent_replay") == 1


def test_same_delivery_id_different_content_is_rejected(client: TestClient) -> None:
    assert client.post("/lims/results", json=_payload(value="7.10")).status_code == 200
    r = client.post("/lims/results", json=_payload(value="9.99"))
    assert r.status_code == 409
    assert r.json()["code"] == "idempotency_conflict"
    # 原数据保持不变。
    task = client.get("/lims/tasks/T-1").json()
    assert task["current"]["value"] == "7.10"


def test_stale_revision_rejected_and_higher_revision_advances(client: TestClient) -> None:
    assert client.post("/lims/results", json=_payload("dlv-1", 1)).status_code == 200
    assert client.post("/lims/results", json=_payload("dlv-2", 2, value="7.20")).status_code == 200

    # 旧修订号迟到：拒绝，远端不倒退。
    stale = client.post("/lims/results", json=_payload("dlv-late", 1, value="6.00"))
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"

    # 同修订号但不同投递 ID：拒绝。
    taken = client.post("/lims/results", json=_payload("dlv-other", 2, value="7.20"))
    assert taken.status_code == 409
    assert taken.json()["code"] == "revision_taken"

    task = client.get("/lims/tasks/T-1").json()
    assert task["current"]["revision"] == 2
    assert task["current"]["value"] == "7.20"
    assert len(task["revisions"]) == 2


def test_delay_simulation(client: TestClient) -> None:
    import time

    payload = _payload(simulate="delay", simulate_delay_seconds=0.5)
    start = time.monotonic()
    r = client.post("/lims/results", json=payload)
    elapsed = time.monotonic() - start
    assert r.status_code == 200
    assert elapsed >= 0.45


def test_error_500_simulation(client: TestClient) -> None:
    r = client.post("/lims/results", json=_payload(simulate="error_500"))
    assert r.status_code == 500
    # 500 不入库。
    assert client.get("/lims/results").json()["results"] == []


def test_disconnect_after_store_then_retry_replays_without_duplicate(
    client: TestClient,
) -> None:
    # 第一次请求：入库后连接被掐断，客户端读到不完整响应。
    with pytest.raises(Exception):  # noqa: B017, PT011 - 连接重置/流不完整
        client.post("/lims/results", json=_payload(simulate="disconnect_after_store"))

    # 数据其实已落库。
    with lims_db.connect() as conn:
        assert len(lims_db.list_results(conn)) == 1

    # 客户端凭同一 delivery_id 重试：幂等重放，不重复入库。
    r = client.post("/lims/results", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is True
    with lims_db.connect() as conn:
        rows = lims_db.list_results(conn)
        assert len(rows) == 1
        assert rows[0]["receipt_count"] == 2


def test_simulate_header_alternative(client: TestClient) -> None:
    r = client.post(
        "/lims/results", json=_payload(), headers={"X-Simulate-Mode": "error_500"}
    )
    assert r.status_code == 500


def test_admin_reset(client: TestClient) -> None:
    client.post("/lims/results", json=_payload())
    assert client.post("/lims/admin/reset").status_code == 200
    assert client.get("/lims/results").json()["results"] == []
