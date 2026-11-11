"""模拟 LIMS 的 HTTP 客户端与一次投递的结果判定。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from . import config, db


@dataclass
class DeliveryOutcome:
    """一次投递尝试的判定结果。"""

    success: bool
    retryable: bool
    error: str | None = None
    response_status: int | None = None
    response_body: Any = None
    lims_revision: int | None = None
    receipt: dict[str, Any] | None = None


async def deliver_revision(
    revision: dict[str, Any], *, base_url: str | None = None
) -> DeliveryOutcome:
    """把一个修订按其固定投递 ID 发往 LIMS。

    重试（含进程重启后的重试）发送的请求体与 ``delivery_id`` 完全一致，
    LIMS 据此实现幂等：已入库则原样重放成功回执，不会重复入库。

    判定规则
    --------
    - 200 且回执 ``delivery_id`` 与本修订一致：成功。
    - 409（同 ID 异内容 / 同修订不同投递 ID）：明确拒绝，不可自动重试，
      置 failed 并保留全部数据。
    - 超时、连接错误、5xx：可重试，回到 pending 等待下一轮。
    """
    url = (base_url or config.LIMS_BASE_URL).rstrip("/") + "/lims/results"
    payload = {
        "delivery_id": revision["delivery_id"],
        "task_id": revision["task_id"],
        "revision": revision["revision"],
        "result_id": revision["result_id"],
        "sample_code": revision["sample_code"],
        "item": revision["item"],
        "value": revision["value"],
    }
    try:
        async with httpx.AsyncClient(timeout=config.LIMS_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException:
        return DeliveryOutcome(
            success=False,
            retryable=True,
            error=f"LIMS 请求超时（{config.LIMS_TIMEOUT_SECONDS}s），连接可能已中断；"
            "快照与投递 ID 保留，等待重试。",
        )
    except (httpx.HTTPError, OSError) as exc:
        # 连接被重置（模拟“入库后断开回执”）、连接拒绝等都走可重试路径：
        # LIMS 端按 delivery_id 去重，重试不会造成重复入库。
        # （httpx 通常将底层 OSError 包装为 HTTPError；保留 OSError 兜底。）
        return DeliveryOutcome(
            success=False,
            retryable=True,
            error=f"网络错误：{type(exc).__name__}: {exc}",
        )

    body: Any
    try:
        body = response.json()
    except ValueError:
        body = response.text

    if response.status_code == 200:
        if not isinstance(body, dict) or body.get("delivery_id") != revision["delivery_id"]:
            # 远端回执不属于本修订，绝不确认，按失败处理等待重试。
            return DeliveryOutcome(
                success=False,
                retryable=False,
                error="LIMS 成功回执的 delivery_id 与本地修订不匹配，拒绝确认同步。",
                response_status=response.status_code,
                response_body=body,
            )
        return DeliveryOutcome(
            success=True,
            retryable=False,
            response_status=200,
            response_body=body,
            lims_revision=int(body.get("revision", revision["revision"])),
            receipt=body if isinstance(body, dict) else {"raw": body},
        )

    if response.status_code == 409:
        return DeliveryOutcome(
            success=False,
            retryable=False,
            error=f"LIMS 拒绝（409 冲突）：{body if isinstance(body, dict) else body}",
            response_status=409,
            response_body=body,
        )

    if response.status_code >= 500:
        return DeliveryOutcome(
            success=False,
            retryable=True,
            error=f"LIMS 服务端错误（HTTP {response.status_code}），稍后重试。",
            response_status=response.status_code,
            response_body=body,
        )

    return DeliveryOutcome(
        success=False,
        retryable=True,
        error=f"LIMS 返回未预期状态码 HTTP {response.status_code}，按可重试处理。",
        response_status=response.status_code,
        response_body=body,
    )


async def deliver_once(revision: dict[str, Any]) -> dict[str, Any]:
    """执行一次投递并把结果落库，返回更新后的修订字典。"""
    outcome = await deliver_revision(revision)
    if outcome.success:
        updated = db.mark_synchronized(
            revision["delivery_id"],
            lims_revision=outcome.lims_revision or revision["revision"],
            receipt=outcome.receipt or {},
        )
    else:
        updated = db.mark_failed(
            revision["delivery_id"],
            error=outcome.error or "未知错误",
            retryable=outcome.retryable,
            response_status=outcome.response_status,
            response_body=outcome.response_body,
        )
    return updated or revision
