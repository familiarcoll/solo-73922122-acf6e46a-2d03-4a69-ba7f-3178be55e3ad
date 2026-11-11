"""模拟 LIMS FastAPI 应用。

端口语境：容器内监听 8001；对外映射由 docker-compose 决定（默认 8001）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse

from . import db as lims_db
from .schemas import LimsConflict, LimsReceipt, LimsResultRequest

logger = logging.getLogger("mock-lims")

_DESCRIPTION = """
本地 HTTP 模拟 LIMS，配合结果投递服务验证幂等、更正与重试语义。

## 处理规则（POST /lims/results）

1. **按投递 ID 去重**：相同 `delivery_id` + 相同内容 → 不重复入库，
   返回 `replayed=true` 的同一回执。
2. **同 ID 异内容拒绝**：相同 `delivery_id` 但任一字段不同 → `409
   idempotency_conflict`。
3. **按任务只保留最高修订**：
   - 低于当前修订的请求 → `409 stale_revision`（远端修订不倒退）；
   - 相同修订号但投递 ID 不同 → `409 revision_taken`；
   - 更高修订 → 正常入库，成为该任务的当前值。
4. **故障注入**（请求体 `simulate` 或请求头 `X-Simulate-Mode`）：
   - `disconnect_after_store`：先入库再断开连接、不返回完整回执，
     模拟“入库后断线”；投递端重试后应得到 `replayed=true`；
   - `delay`：延迟 `simulate_delay_seconds` 秒返回，模拟延迟回执；
   - `error_500`：返回 500，模拟服务端临时故障。

## 查询入口

- `GET /lims/results`：全部入库记录（每个任务可见被保留的最高修订及历史行）。
- `GET /lims/tasks/{task_id}`：单任务当前值与修订链。
- `GET /lims/events`：入库、幂等重放、拒绝的完整事件流（核对是否重复入库）。
- `POST /lims/admin/reset`：清空模拟数据，重新演示。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    lims_db.initialize_db()
    yield


app = FastAPI(
    title="本地模拟 LIMS",
    version="1.0.0",
    description=_DESCRIPTION,
    contact={"name": "实验室数据平台"},
    swagger_ui_parameters={"tryItOutEnabled": True, "displayRequestDuration": True},
    lifespan=lifespan,
)


def _receipt(row: dict, *, replayed: bool) -> LimsReceipt:
    return LimsReceipt(
        delivery_id=row["delivery_id"],
        task_id=row["task_id"],
        revision=row["revision"],
        result_id=row["result_id"],
        sample_code=row["sample_code"],
        item=row["item"],
        value=row["value"],
        stored_at=row["stored_at"],
        replayed=replayed,
        receipt_count=row["receipt_count"],
    )


def _conflict(
    code: str,
    message: str,
    *,
    delivery_id: str,
    task_id: str | None,
    existing: dict | None = None,
) -> JSONResponse:
    payload = LimsConflict(
        code=code,  # type: ignore[arg-type]
        message=message,
        delivery_id=delivery_id,
        task_id=task_id,
        existing=existing,
    )
    return JSONResponse(status_code=409, content=payload.model_dump())


def _disconnect_response(receipt: LimsReceipt) -> StreamingResponse:
    """数据已提交后返回一个不完整响应并强制关闭连接。

    客户端（httpx）会看到不完整读取/连接重置，归入可重试错误；
    由于数据已落库，客户端凭同一 delivery_id 重试时命中幂等重放。
    """
    partial = json.dumps(receipt.model_dump(), ensure_ascii=False)[:40]

    async def broken_stream():
        # 先吐出一段响应头/不完整 body，再以连接重置结束，
        # uvicorn 会中止该 HTTP 连接而不是正常结束响应。
        yield partial.encode("utf-8")
        await asyncio.sleep(0)
        raise ConnectionResetError("mock LIMS: disconnect after store")

    logger.warning(
        "simulate=disconnect_after_store：delivery_id=%s 已入库，模拟回执断连。",
        receipt.delivery_id,
    )
    return StreamingResponse(
        broken_stream(),
        status_code=200,
        media_type="application/json",
        headers={"connection": "close"},
    )


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "mock-lims"}


@app.post(
    "/lims/results",
    response_model=LimsReceipt,
    summary="接收一次结果投递（幂等）",
    responses={
        409: {"model": LimsConflict, "description": "幂等冲突、过期修订或修订号被占用。"},
        500: {"description": "simulate=error_500 模拟的服务端故障。"},
    },
)
async def receive_result(
    payload: LimsResultRequest,
    x_simulate_mode: str | None = Header(default=None),
):
    simulate = payload.simulate or x_simulate_mode
    data = payload.model_dump(exclude={"simulate", "simulate_delay_seconds"})

    if simulate == "delay":
        await asyncio.sleep(payload.simulate_delay_seconds)

    if simulate == "error_500":
        with lims_db.connect() as conn:
            lims_db.record_rejected(
                conn,
                delivery_id=payload.delivery_id,
                task_id=payload.task_id,
                code="simulated_error_500",
                detail={"revision": payload.revision},
            )
        return JSONResponse(
            status_code=500,
            content={"code": "simulated_error", "message": "模拟 LIMS 500 故障。"},
        )

    with lims_db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = lims_db.get_by_delivery_id(conn, payload.delivery_id)
        if existing is not None:
            # 规则 1/2：投递 ID 已存在 —— 内容一致则幂等重放，不一致则 409。
            same = all(
                existing["payload"].get(key) == data.get(key)
                for key in (
                    "task_id",
                    "revision",
                    "result_id",
                    "sample_code",
                    "item",
                    "value",
                )
            )
            if not same:
                lims_db.record_rejected(
                    conn,
                    delivery_id=payload.delivery_id,
                    task_id=payload.task_id,
                    code="idempotency_conflict",
                    detail={"incoming": data, "existing": existing["payload"]},
                )
                conn.execute("COMMIT")
                return _conflict(
                    "idempotency_conflict",
                    "同一 delivery_id 已关联不同的内容，拒绝写入。",
                    delivery_id=payload.delivery_id,
                    task_id=payload.task_id,
                    existing=existing["payload"],
                )
            lims_db.record_replay(conn, payload.delivery_id)
            conn.execute("COMMIT")
            refreshed = lims_db.get_by_delivery_id(conn, payload.delivery_id)
            assert refreshed is not None
            receipt = _receipt(refreshed, replayed=True)
            if simulate == "disconnect_after_store":
                return _disconnect_response(receipt)
            return JSONResponse(content=receipt.model_dump())

        # 新 delivery_id：检查任务修订链。
        latest = lims_db.get_task_latest(conn, payload.task_id)
        if latest is not None:
            if payload.revision < latest["revision"]:
                lims_db.record_rejected(
                    conn,
                    delivery_id=payload.delivery_id,
                    task_id=payload.task_id,
                    code="stale_revision",
                    detail={
                        "incoming_revision": payload.revision,
                        "current_revision": latest["revision"],
                    },
                )
                conn.execute("COMMIT")
                return _conflict(
                    "stale_revision",
                    f"任务 {payload.task_id} 当前修订为 {latest['revision']}，"
                    f"拒绝过期修订 {payload.revision}，远端修订不倒退。",
                    delivery_id=payload.delivery_id,
                    task_id=payload.task_id,
                    existing=latest["payload"],
                )
            same_rev = lims_db.get_task_revision(conn, payload.task_id, payload.revision)
            if same_rev is not None:
                lims_db.record_rejected(
                    conn,
                    delivery_id=payload.delivery_id,
                    task_id=payload.task_id,
                    code="revision_taken",
                    detail={
                        "revision": payload.revision,
                        "existing_delivery_id": same_rev["delivery_id"],
                    },
                )
                conn.execute("COMMIT")
                return _conflict(
                    "revision_taken",
                    f"修订 {payload.revision} 已被投递 {same_rev['delivery_id']} 占用。",
                    delivery_id=payload.delivery_id,
                    task_id=payload.task_id,
                    existing=same_rev["payload"],
                )

        stored = lims_db.store_result(conn, data)
        conn.execute("COMMIT")

    receipt = _receipt(stored, replayed=False)
    if simulate == "disconnect_after_store":
        return _disconnect_response(receipt)
    return JSONResponse(content=receipt.model_dump())


@app.get("/lims/results", summary="查询全部入库记录")
def list_all_results() -> dict[str, object]:
    with lims_db.connect() as conn:
        return {"results": lims_db.list_results(conn)}


@app.get("/lims/tasks/{task_id}", summary="按任务查询当前值与修订链")
def get_task(task_id: str):
    with lims_db.connect() as conn:
        latest = lims_db.get_task_latest(conn, task_id)
        if latest is None:
            return JSONResponse(
                status_code=404,
                content={"code": "task_not_found", "task_id": task_id},
            )
        revisions = [
            row for row in lims_db.list_results(conn) if row["task_id"] == task_id
        ]
    return {
        "task_id": task_id,
        "current": {
            "revision": latest["revision"],
            "value": latest["value"],
            "delivery_id": latest["delivery_id"],
            "stored_at": latest["stored_at"],
            "receipt_count": latest["receipt_count"],
        },
        "revisions": revisions,
    }


@app.get("/lims/events", summary="查询入库/重放/拒绝事件流")
def get_events() -> dict[str, object]:
    with lims_db.connect() as conn:
        return {"events": lims_db.list_events(conn)}


@app.post("/lims/admin/reset", summary="清空模拟 LIMS 数据（演示重置）")
def reset_all() -> dict[str, str]:
    lims_db.reset()
    return {"status": "reset"}
