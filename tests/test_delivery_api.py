"""投递 API 测试：提交、更正、查询、重试、404/409、事件轨迹。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.delivery import config as delivery_config
from app.delivery import db as delivery_db
from app.delivery.api import app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "delivery-api.db"
    monkeypatch.setattr(delivery_config, "DB_PATH", db_path)
    delivery_db.initialize_db(db_path)
    # 关闭进程内 worker，测试中手动控制投递时机。
    monkeypatch.setattr("app.config.ENABLE_INPROCESS_WORKER", False)
    with TestClient(app) as client:
        yield client


def _body(value: str = "7.10") -> dict:
    return {
        "result_id": "R-1",
        "sample_code": "S-100",
        "item": "pH",
        "value": value,
    }


def test_submit_creates_revision_and_docs_are_available(client: TestClient) -> None:
    r = client.post("/v1/tasks/T-001/results", json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["revision"]["task_id"] == "T-001"
    assert data["revision"]["revision"] == 1
    assert data["revision"]["status"] == "pending"
    assert data["revision"]["delivery_id"].startswith("dlv-")
    assert data["revision"]["snapshot"] == {"task_id": "T-001", **_body()}

    # OpenAPI 与首页可访问。
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/health").json()["service"] == "result-delivery"


def test_correction_creates_new_revision_without_overwriting(client: TestClient) -> None:
    client.post("/v1/tasks/T-001/results", json=_body("7.10"))
    r2 = client.post("/v1/tasks/T-001/results", json=_body("7.20"))
    assert r2.status_code == 200
    assert r2.json()["revision"]["revision"] == 2

    task = client.get("/v1/tasks/T-001").json()
    assert task["current_revision"] == 2
    assert task["latest"]["value"] == "7.20"
    assert [rev["revision"] for rev in task["revisions"]] == [1, 2]
    assert task["revisions"][0]["value"] == "7.10"

    # 任务列表只显示每个任务的最高修订。
    listing = client.get("/v1/tasks").json()
    assert len(listing) == 1 and listing[0]["current_revision"] == 2


def test_timeline_records_lifecycle_events(client: TestClient) -> None:
    created = client.post("/v1/tasks/T-001/results", json=_body()).json()
    delivery_id = created["revision"]["delivery_id"]

    # 模拟一次成功投递。
    delivery_db.mark_synchronized(delivery_id, lims_revision=1, receipt={"ok": True})

    timeline = client.get("/v1/tasks/T-001/revisions/1").json()
    types = [e["event_type"] for e in timeline["events"]]
    assert types == ["submitted", "synchronized"]
    assert timeline["revision"]["status"] == "synchronized"


def test_retry_endpoint_states_and_conflicts(client: TestClient) -> None:
    created = client.post("/v1/tasks/T-001/results", json=_body()).json()
    delivery_id = created["revision"]["delivery_id"]

    # 未同步修订可重试，沿用投递 ID。
    r = client.post("/v1/tasks/T-001/revisions/1/retry")
    assert r.status_code == 200
    assert r.json()["revision"]["delivery_id"] == delivery_id
    assert r.json()["revision"]["status"] == "pending"

    # 同步后重试被拒。
    delivery_db.mark_synchronized(delivery_id, lims_revision=1, receipt={})
    conflict = client.post("/v1/tasks/T-001/revisions/1/retry")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "already_synchronized"

    # 不存在的修订/任务。
    assert client.post("/v1/tasks/T-001/revisions/99/retry").status_code == 404
    assert client.get("/v1/tasks/NOPE").status_code == 404
    assert client.get("/v1/tasks/NOPE/revisions/1").status_code == 404


def test_validation_error_on_empty_fields(client: TestClient) -> None:
    r = client.post(
        "/v1/tasks/T-001/results",
        json={"result_id": "", "sample_code": "S-1", "item": "pH", "value": "1"},
    )
    assert r.status_code == 422


def test_legacy_reconciliation_app_still_mounted(client: TestClient) -> None:
    assert client.get("/reconciliation/health").status_code == 200
    assert client.get("/reconciliation/docs").status_code == 200
