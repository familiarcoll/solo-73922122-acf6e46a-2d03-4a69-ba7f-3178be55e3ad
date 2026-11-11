"""FastAPI 应用入口。

- 结果可靠投递 API 即根应用（``/docs``、``/v1/...``、``/health``），
  其生命周期负责初始化 SQLite 与启动进程内 worker；
- 原“检测结果归属核对”服务保留在子应用 ``/reconciliation``
  （文档 ``/reconciliation/docs``）；
- 模拟 LIMS 是独立应用（``app.lims.app``），由独立容器运行在 8001 端口。
"""

from __future__ import annotations

from fastapi.responses import HTMLResponse

from .delivery.api import app as app
from .main_legacy import app as legacy_app

_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>实验室结果可靠投递平台</title>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 880px;
           margin: 3rem auto; padding: 0 1rem; color: #1f2937; line-height: 1.7; }
    h1 { color: #0f766e; }
    code, pre { background: #f3f4f6; border-radius: 6px; padding: .1rem .35rem; }
    pre { padding: 1rem; overflow-x: auto; }
    a { color: #0f766e; }
    li { margin: .25rem 0; }
  </style>
</head>
<body>
  <h1>实验室结果可靠投递平台</h1>
  <p>把已确认归属的结果按“任务内递增修订”可靠写回 LIMS：断线重试不重复入库、
     旧回执不覆盖新修订、超时/退出后沿用同一投递 ID 与快照继续投递。</p>
  <h2>入口</h2>
  <ul>
    <li><a href="/docs">投递服务 Swagger UI（含更正与重试示例，可直接调用）</a></li>
    <li><a href="/redoc">投递服务 ReDoc</a></li>
    <li><a href="/openapi.json">投递服务 OpenAPI JSON</a></li>
    <li><a href="http://localhost:8001/docs">模拟 LIMS Swagger UI（查询入口与故障注入）</a></li>
    <li><a href="http://localhost:8001/lims/results">模拟 LIMS 已入库结果</a></li>
    <li><a href="http://localhost:8001/lims/events">模拟 LIMS 事件流（核对无重复入库）</a></li>
    <li><a href="/reconciliation/docs">原归属核对服务文档</a></li>
    <li><a href="/health">投递服务健康检查</a></li>
  </ul>
  <h2>30 秒上手（curl）</h2>
  <pre># 1. 提交第 1 版结果
curl -s -X POST http://localhost:8000/v1/tasks/T-001/results \\
  -H 'Content-Type: application/json' \\
  -d '{"result_id":"R-001","sample_code":"S-100","item":"pH","value":"7.10"}'

# 2. 查询同步状态（worker 自动投递，pending → delivering → synchronized）
curl -s http://localhost:8000/v1/tasks/T-001

# 3. 提交更正（value 变化 → revision=2，新投递 ID）
curl -s -X POST http://localhost:8000/v1/tasks/T-001/results \\
  -H 'Content-Type: application/json' \\
  -d '{"result_id":"R-001","sample_code":"S-100","item":"pH","value":"7.15"}'

# 4. 查看 LIMS 侧只保留最高修订
curl -s http://localhost:8001/lims/tasks/T-001</pre>
</body>
</html>"""


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def platform_index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


# 原核对服务挂到 /reconciliation（其内部路由仍为 /v1/reconciliations、/health 等）。
app.mount("/reconciliation", legacy_app)
