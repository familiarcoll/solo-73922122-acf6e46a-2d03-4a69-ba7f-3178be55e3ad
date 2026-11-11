"""独立投递 worker 进程入口（compose 的 worker1 / worker2 使用）。

用法::

    python -m app.worker_main

进程不启动 HTTP 服务，只运行一个 ``DeliveryWorker`` 异步循环。
多个容器副本可同时运行：SQLite 租约保证同一修订同一时刻只被一个
worker 投递；进程退出后租约到期，其他 worker 接管并沿用同一投递 ID。
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .delivery.worker import DeliveryWorker


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = DeliveryWorker()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_stop)

    worker.start()
    try:
        await stop_event.wait()
    finally:
        # 优雅停止：取消正在进行的投递循环（租约到期后可被其他 worker 接管）。
        await worker.stop()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
