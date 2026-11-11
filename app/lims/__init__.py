"""本地模拟 LIMS（实验室信息管理系统）。

行为契约（与投递端的可靠性保证一一对应）
========================================

``POST /lims/results``
- 按 ``delivery_id`` 幂等去重：同一投递 ID 重复提交且内容一致时，
  **不重复入库**，原样重放首次的成功回执。
- 同一 ``delivery_id`` 但内容不同：拒绝并返回 409
  （``code=idempotency_conflict``）。
- 同一 ``task_id`` 只保留**最高修订**：低修订重复到达返回 409
  （``code=stale_revision``），远端修订不会倒退；同修订号但投递 ID
  不同也返回 409（``code=revision_taken``）。
- 可通过请求体 ``simulate`` 字段或 ``X-Simulate-Mode`` 请求头模拟：
  - ``disconnect_after_store``：先入库再断开连接（无回执），
    投递端超时重试后命中幂等重放，不产生重复数据；
  - ``delay``：延迟 ``simulate_delay_seconds`` 秒后返回，
    用于验证超时重试与延迟回执；
  - ``error_500``：返回 500，验证可重试失败路径。
"""

from __future__ import annotations

import os
from pathlib import Path

LIMS_DB_PATH = Path(os.environ.get("LIMS_DB_PATH", "/data/lims.db"))
