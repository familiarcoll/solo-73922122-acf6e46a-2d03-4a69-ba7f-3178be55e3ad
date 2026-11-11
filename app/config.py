"""应用级共享配置（环境变量入口）。

投递服务的可调参数默认值定义在 ``app.delivery.config``，
这里仅转发，供 API 生命周期与 worker 入口共同使用。
"""

from __future__ import annotations

from .delivery.config import ENABLE_INPROCESS_WORKER

__all__ = ["ENABLE_INPROCESS_WORKER"]
