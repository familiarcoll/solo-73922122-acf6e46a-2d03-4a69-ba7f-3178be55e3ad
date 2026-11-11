"""投递服务的运行配置，全部可通过环境变量覆盖。"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# 投递端 SQLite 数据库文件（API 进程与 worker 进程共享）。
DB_PATH = Path(_env("DELIVERY_DB_PATH", "/data/delivery.db"))

# 模拟 LIMS 的投递地址。
LIMS_BASE_URL = _env("LIMS_BASE_URL", "http://lims:8001")

# 一次 HTTP 投递的超时（秒）。
LIMS_TIMEOUT_SECONDS = float(_env("LIMS_TIMEOUT_SECONDS", "5"))

# worker 轮询待投递修订的间隔（秒）。
POLL_INTERVAL_SECONDS = float(_env("POLL_INTERVAL_SECONDS", "0.5"))

# 租约时长（秒）：某个 worker 认领修订后，若此时长内没有更新心跳，
# 其他 worker（或重启后的 worker）可重新认领，沿用同一投递 ID 重试。
LEASE_SECONDS = float(_env("LEASE_SECONDS", "30"))

# 连续失败后两次投递之间的最小退避（秒）。
BACKOFF_BASE_SECONDS = float(_env("BACKOFF_BASE_SECONDS", "1"))
BACKOFF_MAX_SECONDS = float(_env("BACKOFF_MAX_SECONDS", "30"))

# 单个修订最多保留多少次失败记录（事件表只增不减，这里仅用于状态展示截断）。
MAX_FAILURES_IN_STATUS = int(_env("MAX_FAILURES_IN_STATUS", "10"))

# API 进程内是否启动后台 worker（compose 中另有两个独立 worker 进程，
# 容器部署时关闭；本地 `uvicorn` 直接运行时默认开启，保证开箱即用）。
ENABLE_INPROCESS_WORKER = _env("ENABLE_INPROCESS_WORKER", "true").lower() in (
    "1",
    "true",
    "yes",
)
