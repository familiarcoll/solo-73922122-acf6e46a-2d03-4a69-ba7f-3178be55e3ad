"""结果投递 API（FastAPI 应用）。

路由总览
========
- ``POST   /v1/tasks/{task_id}/results``          提交结果/更正，分配任务内递增修订号
- ``POST   /v1/tasks/{task_id}/revisions/{rev}/retry``  手动重新触发投递
- ``GET    /v1/tasks``                            任务列表与当前同步状态
- ``GET    /v1/tasks/{task_id}``                  当前结果、全部修订与同步状态
- ``GET    /v1/tasks/{task_id}/revisions/{rev}``  单修订详情与事件轨迹（含投递 ID、回执）
- ``GET    /health``                              健康检查
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .. import config as app_config
from . import db
from .schemas import (
    DeliveryEvent,
    ErrorResponse,
    RevisionDetail,
    RevisionTimeline,
    ResultSubmission,
    RetryResponse,
    SubmissionAccepted,
    TaskStatus,
    TaskSummary,
)
from .worker import DeliveryWorker

logger = logging.getLogger("delivery.api")

_DESCRIPTION = """
实验室数据员把**已确认归属**的结果写回 LIMS 的可靠投递服务。

## 核心保证

- **修订快照**：每次提交（含更正）都保存完整快照，并在任务内分配从 1 开始的
  递增修订号；旧修订永不被覆盖。
- **投递 ID 幂等**：每个修订有终身不变的 `delivery_id`，超时、断连或进程退出后
  重试沿用同一 ID 与同一份快照，LIMS 按 ID 去重，断线重试不会重复入库。
- **旧回执不能覆盖新修订**：只有 `delivery_id` 与本地修订完全匹配的成功回执才会
  确认同步；投递途中提交的更正（新修订）不会被旧修订迟到的回执标记为已同步。
- **失败保留数据**：同步失败的修订完整保留，可随时重新触发；重启后 worker
  自动继续，两个 worker 进程并发也不会重复入库或令远端修订倒退。

## 典型流程（可在本页直接 Try it out）

1. `POST /v1/tasks/{task_id}/results` 提交结果，拿到 `revision=1` 与 `delivery_id`。
2. 轮询 `GET /v1/tasks/{task_id}` 观察 `pending → delivering → synchronized`。
3. 在 LIMS 侧打开
   [`GET /lims/tasks/{task_id}`](http://localhost:8001/lims/tasks/{task_id})
   查看远端只保留最高修订。
4. 再次调用提交接口、**value 改为更正值**，生成 `revision=2`；重复步骤 2/3，
   观察远端修订前进、修订 1 的旧回执无法影响修订 2。
5. 需要重试时调用 `POST .../retry`；模拟断连/延迟见
   [模拟 LIMS 文档](http://localhost:8001/docs)。
"""

_TAGS = [
    {"name": "结果投递", "description": "提交结果/更正、查询修订与同步状态、重新触发。"},
    {"name": "运维", "description": "健康检查。"},
]


def _detail(revision: dict) -> RevisionDetail:
    return RevisionDetail(**revision)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.initialize_db()
    worker: DeliveryWorker | None = None
    if app_config.ENABLE_INPROCESS_WORKER:
        worker = DeliveryWorker()
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()


app = FastAPI(
    title="实验室结果可靠投递 API",
    version="1.0.0",
    description=_DESCRIPTION,
    openapi_tags=_TAGS,
    contact={"name": "实验室数据平台"},
    swagger_ui_parameters={"tryItOutEnabled": True, "displayRequestDuration": True},
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "result-delivery"}


@app.post(
    "/v1/tasks/{task_id}/results",
    response_model=SubmissionAccepted,
    tags=["结果投递"],
    summary="提交结果或更正（分配任务内递增修订号）",
    description=(
        "按任务 ID 接收结果 ID、样品码、检测项目与值。每次调用都生成一个新修订，"
        "完整快照落 SQLite 并分配任务内递增修订号与终身不变的投递 ID。"
        "即使旧修订仍在投递途中，更正也会立即入库，互不阻塞。"
    ),
    responses={422: {"model": ErrorResponse, "description": "请求体字段缺失或为空。"}},
)
def submit_result(task_id: str, payload: ResultSubmission) -> SubmissionAccepted:
    revision = db.create_revision(
        task_id,
        result_id=payload.result_id,
        sample_code=payload.sample_code,
        item=payload.item,
        value=payload.value,
    )
    return SubmissionAccepted(
        message=(
            f"已接收任务 {task_id} 的第 {revision['revision']} 版结果，"
            "等待 worker 投递。"
        ),
        revision=_detail(revision),
    )


@app.post(
    "/v1/tasks/{task_id}/revisions/{revision}/retry",
    response_model=RetryResponse,
    tags=["结果投递"],
    summary="重新触发指定修订的投递",
    description=(
        "把未同步修订重新置为待投递，**沿用原投递 ID 与快照**。"
        "已同步的修订不可重试（返回 409），修订不存在返回 404。"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "任务或修订不存在。"},
        409: {"model": ErrorResponse, "description": "修订已同步，无需重试。"},
    },
)
def retry_revision(task_id: str, revision: int) -> RetryResponse:
    existing = db.get_revision(None, task_id, revision)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "revision_not_found", "task_id": task_id, "revision": revision},
        )
    if existing["status"] == db.STATUS_SYNCHRONIZED:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_synchronized",
                "message": "该修订已收到匹配的 LIMS 成功回执，不能重复投递。",
                "task_id": task_id,
                "revision": revision,
                "delivery_id": existing["delivery_id"],
            },
        )
    updated = db.retrigger(task_id, revision)
    return RetryResponse(
        message=(
            f"已重新触发任务 {task_id} 修订 {revision}，"
            f"沿用投递 ID {updated['delivery_id']}，等待 worker 投递。"
        ),
        revision=_detail(updated),
    )


@app.get(
    "/v1/tasks",
    response_model=list[TaskSummary],
    tags=["结果投递"],
    summary="查询所有任务的当前结果与同步状态",
)
def list_tasks() -> list[TaskSummary]:
    summaries: list[TaskSummary] = []
    for row in db.list_tasks():
        summaries.append(
            TaskSummary(
                task_id=row["task_id"],
                current_revision=row["revision"],
                status=row["status"],
                synchronized=row["status"] == db.STATUS_SYNCHRONIZED,
                delivery_id=row["delivery_id"],
                result_id=row["result_id"],
                sample_code=row["sample_code"],
                item=row["item"],
                value=row["value"],
                attempts=row["attempts"],
                last_error=row["last_error"],
                synchronized_at=row["synchronized_at"],
            )
        )
    return summaries


@app.get(
    "/v1/tasks/{task_id}",
    response_model=TaskStatus,
    tags=["结果投递"],
    summary="查询任务当前结果、全部修订及同步状态",
    responses={404: {"model": ErrorResponse, "description": "任务不存在。"}},
)
def get_task(task_id: str) -> TaskStatus:
    revisions = db.list_task_revisions(task_id)
    if not revisions:
        raise HTTPException(
            status_code=404,
            detail={"code": "task_not_found", "task_id": task_id},
        )
    latest = revisions[-1]
    return TaskStatus(
        task_id=task_id,
        current_revision=latest["revision"],
        status=latest["status"],
        synchronized=latest["status"] == db.STATUS_SYNCHRONIZED,
        latest=_detail(latest),
        revisions=[_detail(row) for row in revisions],
    )


@app.get(
    "/v1/tasks/{task_id}/revisions/{revision}",
    response_model=RevisionTimeline,
    tags=["结果投递"],
    summary="查询单个修订的详情与投递事件轨迹",
    responses={404: {"model": ErrorResponse, "description": "任务或修订不存在。"}},
)
def get_revision_timeline(task_id: str, revision: int) -> RevisionTimeline:
    existing = db.get_revision(None, task_id, revision)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "revision_not_found", "task_id": task_id, "revision": revision},
        )
    events = [DeliveryEvent(**event) for event in db.list_events(task_id, revision)]
    return RevisionTimeline(revision=_detail(existing), events=events)
