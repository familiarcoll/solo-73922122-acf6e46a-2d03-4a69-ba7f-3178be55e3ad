"""原归属核对服务的 API 测试（现挂载在 /reconciliation 子应用）。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main_legacy import app

DEMO = Path(__file__).resolve().parent.parent / "demo"
client = TestClient(app)


def _demo_files():
    return {
        "tasks": ("tasks.csv", (DEMO / "tasks.csv").read_bytes(), "text/csv"),
        "results": ("results.csv", (DEMO / "results.csv").read_bytes(), "text/csv"),
    }


def test_reconcile_demo_response() -> None:
    response = client.post("/v1/reconciliations", files=_demo_files())
    assert response.status_code == 200
    payload = response.json()

    assert payload["summary"] == {
        "task_records": 6,
        "result_records": 7,
        "eligible_candidate_pairs": 7,
        "max_pairs": 4,
        "minimum_total_absolute_deviation_seconds": 2700,
        "definite_pair_count": 2,
        "ambiguous_task_count": 4,
        "ambiguous_result_count": 5,
    }
    assert [(pair["task_id"], pair["result_id"]) for pair in payload["definite_pairs"]] == [
        ("T-004", "R-004"),
        ("T-005", "R-006"),
    ]
    assert payload["definite_pairs"][0]["planned_time"].endswith("Z")
    assert payload["definite_pairs"][0]["deviation_seconds"] == 600

    ambiguous_tasks = {item["task_id"]: item for item in payload["ambiguous_tasks"]}
    assert ambiguous_tasks["T-001"]["possibly_unpaired"] is False
    assert [pair["result_id"] for pair in ambiguous_tasks["T-001"]["possible_result_pairs"]] == [
        "R-001",
        "R-002",
    ]
    assert ambiguous_tasks["T-002"]["possibly_unpaired"] is True
    assert ambiguous_tasks["T-006"]["possible_result_pairs"] == []

    ambiguous_results = {item["result_id"]: item for item in payload["ambiguous_results"]}
    assert ambiguous_results["R-003"]["possibly_unpaired"] is False
    assert [pair["task_id"] for pair in ambiguous_results["R-003"]["possible_task_pairs"]] == [
        "T-002",
        "T-003",
    ]
    assert ambiguous_results["R-005"]["possible_task_pairs"] == []
    assert ambiguous_results["R-005"]["possibly_unpaired"] is True


def test_invalid_csv_returns_structured_422() -> None:
    tasks_content = (
        "task_id,sample_code,item,planned_time\n"
        "T-1,S-1,pH,2026-11-11T10:00:00Z\n"
    )
    response = client.post(
        "/v1/reconciliations",
        files={
            "tasks": ("tasks.csv", tasks_content, "text/csv"),
            "results": ("results.csv", (DEMO / "results.csv").read_bytes(), "text/csv"),
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "csv_validation_error"
    assert detail["errors"][0]["file"] == "tasks"
    assert detail["errors"][0]["row"] == 1


def test_both_invalid_files_are_reported_together() -> None:
    response = client.post(
        "/v1/reconciliations",
        files={
            "tasks": ("tasks.csv", b"", "text/csv"),
            "results": ("results.csv", b"", "text/csv"),
        },
    )
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert {item["file"] for item in errors} == {"tasks", "results"}


def test_docs_and_demo_files_are_available() -> None:
    assert client.get("/docs").status_code == 200
    openapi = client.get("/openapi.json").json()
    assert "/v1/reconciliations" in openapi["paths"]
    assert "multipart/form-data" in openapi["paths"]["/v1/reconciliations"]["post"]["requestBody"][
        "content"
    ]
    assert client.get("/demo/tasks.csv").status_code == 200
    assert client.get("/demo/results.csv").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200


def test_legacy_app_mounted_under_reconciliation() -> None:
    from app.main import app as root_app

    root_client = TestClient(root_app)
    assert root_client.get("/reconciliation/health").status_code == 200
    assert root_client.get("/reconciliation/docs").status_code == 200
