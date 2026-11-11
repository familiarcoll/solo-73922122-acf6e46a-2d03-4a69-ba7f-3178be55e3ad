"""投递服务的 Pydantic 模型（同时是 /docs 中可调用示例的来源）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# 与 db.py 中的状态常量保持一致。
RevisionStatus = Literal["pending", "delivering", "synchronized", "failed"]


class ResultSubmission(BaseModel):
    """按任务 ID 写回一条已确认归属的结果。"""

    result_id: str = Field(
        ...,
        min_length=1,
        description="结果 ID（仪器结果编号），例如 R-004。",
        examples=["R-004"],
    )
    sample_code: str = Field(
        ..., min_length=1, description="样品码，例如 S-300。", examples=["S-300"]
    )
    item: str = Field(
        ..., min_length=1, description="检测项目，例如 Pb。", examples=["Pb"]
    )
    value: str = Field(
        ...,
        min_length=1,
        description=(
            "结果值，按字符串保存完整快照（保留原始文本，如 0.012、<0.01）。"
        ),
        examples=["0.012"],
    )


class RevisionDetail(BaseModel):
    task_id: str = Field(..., description="任务 ID。")
    revision: int = Field(..., description="任务内递增修订号，从 1 开始。")
    delivery_id: str = Field(
        ..., description="本修订终身不变的投递 ID，LIMS 按此幂等去重。"
    )
    result_id: str
    sample_code: str
    item: str
    value: str
    snapshot: dict[str, Any] = Field(..., description="提交时保存的完整快照。")
    status: RevisionStatus = Field(..., description="修订同步状态。")
    attempts: int = Field(..., description="累计投递尝试次数（含重试）。")
    lease_owner: str | None = Field(
        None, description="当前持有投递租约的 worker 标识；空闲时为 null。"
    )
    lease_expires_at: str | None = None
    last_error: str | None = Field(
        None, description="最近一次投递失败原因；成功确认后清空。"
    )
    lims_revision: int | None = Field(
        None, description="LIMS 侧确认时记录的远端修订号。"
    )
    created_at: str
    updated_at: str
    synchronized_at: str | None = None


class SubmissionAccepted(BaseModel):
    """提交成功响应：新修订立即入库，投递由 worker 异步执行。"""

    message: str
    revision: RevisionDetail


class TaskStatus(BaseModel):
    """单个任务的当前结果、修订与同步状态。"""

    task_id: str
    current_revision: int = Field(..., description="该任务当前最高修订号。")
    status: RevisionStatus = Field(..., description="当前最高修订的同步状态。")
    synchronized: bool = Field(
        ..., description="当前最高修订是否已收到匹配的 LIMS 成功回执。"
    )
    latest: RevisionDetail = Field(..., description="当前最高修订详情。")
    revisions: list[RevisionDetail] = Field(
        ..., description="该任务全部修订（含历次更正），按修订号升序。"
    )


class TaskSummary(BaseModel):
    """任务列表条目：每个任务只展示当前（最高）修订。"""

    task_id: str
    current_revision: int
    status: RevisionStatus
    synchronized: bool
    delivery_id: str
    result_id: str
    sample_code: str
    item: str
    value: str
    attempts: int
    last_error: str | None
    synchronized_at: str | None


class DeliveryEvent(BaseModel):
    event_type: str = Field(
        ...,
        description=(
            "事件类型：submitted / delivery_attempt / delivery_failed / "
            "synchronized / retriggered。"
        ),
    )
    detail: dict[str, Any]
    created_at: str


class RevisionTimeline(BaseModel):
    revision: RevisionDetail
    events: list[DeliveryEvent] = Field(
        ..., description="该修订从提交到同步（或失败）的完整事件轨迹。"
    )


class RetryResponse(BaseModel):
    message: str
    revision: RevisionDetail


class ErrorResponse(BaseModel):
    detail: dict[str, Any]
