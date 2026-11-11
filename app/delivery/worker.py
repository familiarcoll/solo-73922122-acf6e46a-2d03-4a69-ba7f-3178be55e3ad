"""投递 worker 循环。

可运行两种形态：

1. 独立进程（compose 中的 ``worker1`` / ``worker2``，见 ``app.worker_main``）；
2. API 进程内的 asyncio 后台任务（本地 ``uvicorn`` 直接运行时默认开启）。

互斥保证
========
worker 不做进程间内存锁，完全依赖 SQLite 的条件 UPDATE 租约
（见 ``db.claim_next_pending``）：同一修订在任一时刻只会被一个 worker
认领并发出 HTTP 请求。worker 崩溃或被 kill -9 时租约到期，其他 worker
沿用同一 ``delivery_id`` 与快照重新投递，LIMS 幂等去重，不会重复入库。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from . import config, db
from .lims_client import deliver_once

logger = logging.getLogger("delivery.worker")


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class DeliveryWorker:
    """轮询待投递修订并执行投递的异步 worker。"""

    def __init__(self, identity: str | None = None) -> None:
        self.identity = identity or worker_identity()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name=f"delivery-worker-{self.identity}")
            logger.info("投递 worker 已启动：%s", self.identity)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("投递 worker 已停止：%s", self.identity)

    async def _run(self) -> None:
        # 启动时先确保表存在，独立 worker 容器也能单独拉起。
        db.initialize_db()
        idle_rounds = 0
        while not self._stopping.is_set():
            try:
                revision = await asyncio.to_thread(db.claim_next_pending, self.identity)
                if revision is None:
                    idle_rounds += 1
                    # 空闲时逐步放慢轮询，减少空转写库。
                    wait = min(config.POLL_INTERVAL_SECONDS * (1 + idle_rounds // 10), 5)
                    await asyncio.sleep(wait)
                    continue
                idle_rounds = 0
                # 退避发生在认领之后、发请求之前。此时尚未进入长时间网络调用，
                # 退避时长被限制在租约的一半以内；即便租约在此期间到期被其他
                # worker 接管，LIMS 侧按 delivery_id 幂等去重，也不会重复入库。
                await self._backoff_before_send(revision)
                await self._process(revision)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker 必须吞掉异常持续运行
                logger.exception("worker 循环发生未预期异常")
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

    async def _backoff_before_send(self, revision: dict) -> None:
        attempts = revision["attempts"]
        if attempts <= 1:
            return
        backoff = min(
            config.BACKOFF_BASE_SECONDS * (2 ** (attempts - 2)),
            config.BACKOFF_MAX_SECONDS,
            config.LEASE_SECONDS / 2,
        )
        logger.info(
            "task=%s rev=%s 第 %s 次投递，退避 %.2fs",
            revision["task_id"], revision["revision"], attempts, backoff,
        )
        await asyncio.sleep(backoff)

    async def _process(self, revision: dict) -> None:
        """投递一个修订，投递期间用心跳续租。"""
        delivery_id = revision["delivery_id"]
        logger.info(
            "worker %s 投递 task=%s rev=%s delivery_id=%s attempt=%s",
            self.identity,
            revision["task_id"],
            revision["revision"],
            delivery_id,
            revision["attempts"],
        )
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            updated = await deliver_once(revision)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        logger.info(
            "worker %s 投递结束 task=%s rev=%s -> status=%s",
            self.identity,
            revision["task_id"],
            revision["revision"],
            updated["status"],
        )

    async def _heartbeat_loop(self) -> None:
        """投递期间定期续租，防止长耗时请求导致租约过期被重复接管。"""
        interval = max(1.0, config.LEASE_SECONDS / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(db.heartbeat, self.identity)
            except Exception:  # noqa: BLE001
                logger.exception("续租心跳失败")
