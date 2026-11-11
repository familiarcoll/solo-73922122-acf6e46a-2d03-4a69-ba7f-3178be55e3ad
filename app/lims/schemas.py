"""模拟 LIMS 的请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SimulateMode = Literal["disconnect_after_store", "delay", "error_500"]


class LimsResultRequest(BaseModel):
    delivery_id: str = Field(
        ..., min_length=1, description="投递端生成的幂等键，重试保持不变。"
    )
    task_id: str = Field(..., min_length=1)
    revision: int = Field(..., ge=1, description="投递端任务内修订号。")
    result_id: str = Field(..., min_length=1)
    sample_code: str = Field(..., min_length=1)
    item: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)
    simulate: SimulateMode | None = Field(
        None,
        description=(
            "故障注入（演示用）：disconnect_after_store=入库后断开回执；"
            "delay=延迟返回；error_500=返回 500。也可用请求头 X-Simulate-Mode。"
        ),
    )
    simulate_delay_seconds: float = Field(
        1.0, ge=0, description="simulate=delay 时的延迟秒数。"
    )


class LimsReceipt(BaseModel):
    """LIMS 成功回执（幂等重放时内容保持一致）。"""

    delivery_id: str
    task_id: str
    revision: int
    result_id: str
    sample_code: str
    item: str
    value: str
    stored_at: str
    replayed: bool = Field(..., description="false=首次入库；true=幂等重放（未重复入库）。")
    receipt_count: int = Field(..., description="该投递 ID 累计收到的请求次数。")


class LimsConflict(BaseModel):
    code: Literal["idempotency_conflict", "stale_revision", "revision_taken"]
    message: str
    delivery_id: str
    task_id: str | None = None
    existing: dict[str, Any] | None = None
