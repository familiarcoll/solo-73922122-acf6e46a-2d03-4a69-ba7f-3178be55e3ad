# 实验室结果可靠投递服务（含本地模拟 LIMS）

把已确认归属的检测结果写回 LIMS 时，保证**断线重试不重复入库、旧回执不覆盖更正、
超时或进程退出后可沿用同一投递 ID 与快照继续投递**。投递端与模拟 LIMS 均为
FastAPI + SQLite，整个环境可用 Docker Compose 一键启动。

## 快速启动

```bash
docker compose up --build
```

启动后四个容器：

| 服务 | 容器 | 地址 | 职责 |
| --- | --- | --- | --- |
| 投递 API | `api` | <http://localhost:8000> | 接收提交/更正、查询、手动重试（`/docs` 含可调用示例） |
| 投递 worker | `worker1`、`worker2` | 无端口 | 两个独立工作进程，并发轮询共享 SQLite，租约互斥 |
| 模拟 LIMS | `lims` | <http://localhost:8001> | 幂等入库、故障注入、远端查询入口 |

端口占用时：`APP_PORT=8080 LIMS_PORT=8081 docker compose up --build`。

| 入口 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | 投递服务 Swagger UI（更正与重试示例可直接 Try it out） |
| <http://localhost:8000/> | 平台首页与 curl 速览 |
| <http://localhost:8001/docs> | 模拟 LIMS Swagger UI（含故障注入与查询入口） |
| <http://localhost:8001/lims/results> | 模拟 LIMS 全部入库记录 |
| <http://localhost:8001/lims/tasks/{task_id}> | 单任务当前值与修订链 |
| <http://localhost:8001/lims/events> | 入库/幂等重放/拒绝事件流（核对无重复入库） |
| <http://localhost:8000/reconciliation/docs> | 原“归属核对”服务（保留在子应用） |

一键演示脚本（提交、更正、断连重试、冲突、事件流核对）：

```bash
./demo/walkthrough.sh
```

## 可靠性模型

### 1. 修订号与完整快照

`POST /v1/tasks/{task_id}/results` 按任务 ID 接收 `result_id`、`sample_code`、
`item`、`value`。每次调用都：

- 在 SQLite 保存该次提交的**完整快照**（不可变 JSON）；
- 在任务内分配从 1 开始的**递增修订号**（事务内 `MAX(revision)+1`，并发提交不重号）；
- 生成终身不变的**投递 ID** `dlv-...`；
- 初始状态为 `pending`，即使旧修订仍在投递途中，更正也立即入库、互不阻塞。

### 2. 投递 ID 幂等：断线重试不重复入库

worker 投递的请求体始终携带该修订固定的 `delivery_id`。模拟 LIMS：

- 同 `delivery_id` + 同内容 → 不新增数据，重放首次回执（`replayed=true`）；
- 同 `delivery_id` + 异内容 → `409 idempotency_conflict`，投递端置 `failed` 并保留数据。

因此超时、连接重置（含“入库后断开回执”）、worker 崩溃后的重试，在 LIMS 侧都只会
命中幂等重放，**不会产生第二条入库记录**。

### 3. 旧回执不能把新修订标为已同步

只有回执中的 `delivery_id` 与本地修订**完全相等**时才确认同步
（`pending/delivering → synchronized`）。投递途中提交的更正是新修订、新投递 ID，
旧修订迟到的成功回执命中不到新修订，无法影响其同步状态。

### 4. 失败保留数据，可重新触发

| 失败类型 | 处理 |
| --- | --- |
| 超时、连接错误、5xx | 回到 `pending`，保留快照与投递 ID，worker 自动退避重试 |
| 409（同 ID 异内容、修订冲突） | 置 `failed`，数据完整保留，等待人工处理或 `POST .../retry` |
| 已同步 | 不允许重试（`409 already_synchronized`） |

`POST /v1/tasks/{task_id}/revisions/{revision}/retry` 清除租约与错误、回到
`pending`，**沿用原投递 ID 与快照**。

### 5. 重启继续与多 worker 并发

- worker 通过 SQLite 条件 UPDATE 实现行级**租约**（`lease_owner` +
  `lease_expires_at`）：同一修订任一时刻只被一个 worker 认领并发出 HTTP 请求；
- worker 被 kill / 崩溃后租约到期，其他 worker 接管，attempts 累加但投递 ID 不变；
- 投递期间有心跳续租，防止长请求被误接管；
- compose 中 `worker1`、`worker2` 共享同一个 SQLite 卷（WAL 模式）。
  本地不用 Docker 直接 `uvicorn app.main:app` 时，API 进程内置一个 worker，开箱即用。

### 6. 模拟 LIMS 的远端修订规则

- 每个 `task_id` 只保留**最高修订**：低修订迟到 → `409 stale_revision`，远端不倒退；
- 同修订号不同投递 ID → `409 revision_taken`；
- 故障注入（请求体 `simulate` 或请求头 `X-Simulate-Mode`）：
  - `disconnect_after_store`：事务提交后中断响应流，模拟入库后回执丢失；
  - `delay`（配合 `simulate_delay_seconds`）：延迟回执；
  - `error_500`：服务端故障（不入库）。

## 接口一览（投递服务，前缀 `/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/tasks/{task_id}/results` | 提交结果/更正，返回新修订与投递 ID |
| POST | `/v1/tasks/{task_id}/revisions/{revision}/retry` | 重新触发（沿用投递 ID 与快照） |
| GET | `/v1/tasks` | 所有任务当前（最高）修订与同步状态 |
| GET | `/v1/tasks/{task_id}` | 当前结果、全部修订、同步状态 |
| GET | `/v1/tasks/{task_id}/revisions/{revision}` | 单修订详情与事件轨迹 |
| GET | `/health` | 健康检查 |

状态取值：`pending`（待投递）、`delivering`（租约投递中）、
`synchronized`（已收到匹配回执）、`failed`（明确拒绝，保留数据）。

### curl 示例

```bash
# 提交
curl -s -X POST http://localhost:8000/v1/tasks/T-001/results \
  -H 'Content-Type: application/json' \
  -d '{"result_id":"R-001","sample_code":"S-100","item":"pH","value":"7.10"}'

# 查询（观察 pending → delivering → synchronized）
curl -s http://localhost:8000/v1/tasks/T-001

# 更正（生成 revision=2）
curl -s -X POST http://localhost:8000/v1/tasks/T-001/results \
  -H 'Content-Type: application/json' \
  -d '{"result_id":"R-001","sample_code":"S-100","item":"pH","value":"7.15"}'

# 模拟 LIMS 侧查询
curl -s http://localhost:8001/lims/tasks/T-001
```

## 数据存储

两个独立 SQLite 文件，分别挂载在独立 Docker 卷：

- 投递端 `delivery.db`（卷 `delivery-data`）：
  - `result_revisions`：修订、快照 JSON、投递 ID、状态、租约、尝试次数、错误、回执修订；
  - `delivery_events`：`submitted / delivery_attempt / delivery_failed /
    synchronized / retriggered` 只增事件轨迹。
- 模拟 LIMS `lims.db`（卷 `lims-data`）：
  - `lims_results`：`delivery_id` 唯一、`(task_id, revision)` 唯一；
  - `lims_events`：`stored / idempotent_replay / rejected`。

清空演示数据：`POST http://localhost:8001/lims/admin/reset`；
清空投递端历史：删除卷 `docker compose down -v` 后重启。

## 本地开发与测试

Python 3.11+：

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试（45 个）覆盖：

- 修订号递增、快照不可变、并发提交不重号、租约互斥与过期接管；
- 仅匹配投递 ID 的回执才能确认同步、可重试失败与硬失败、手动重试；
- 模拟 LIMS 幂等重放、同 ID 异内容 409、过期修订/修订占用 409、延迟、500、断连；
- API 提交/更正/查询/重试/404/409/事件轨迹；
- 端到端（真实 worker 循环 + ASGI HTTP）：正常投递、投递途中更正、
  断连后幂等重试、**两个 worker 并发洪泛无重复入库且远端不倒退**、worker 重启恢复。

不用 Docker 的本地运行方式（三个进程）：

```bash
# 1) 模拟 LIMS
LIMS_DB_PATH=./data/lims.db uvicorn app.lims.app:app --port 8001
# 2) 投递 API（内置 worker；如需独立 worker 见下）
DELIVERY_DB_PATH=./data/delivery.db LIMS_BASE_URL=http://127.0.0.1:8001 \
  uvicorn app.main:app --port 8000
# 3) 可选：独立 worker（可启动多个副本）
DELIVERY_DB_PATH=./data/delivery.db LIMS_BASE_URL=http://127.0.0.1:8001 \
  ENABLE_INPROCESS_WORKER=false python -m app.worker_main
```

## 文件结构

```
app/
  config.py              环境变量开关（进程内 worker 等）
  main.py                根应用（投递服务）+ 挂载 /reconciliation 旧服务
  main_legacy.py         原归属核对服务
  worker_main.py         独立 worker 进程入口
  delivery/
    config.py            投递端可调参数（DB 路径、超时、租约、退避）
    db.py                SQLite 修订/快照/租约/状态流转/事件
    schemas.py           Pydantic 模型（/docs 示例来源）
    lims_client.py       LIMS HTTP 客户端与回执判定
    worker.py            异步 worker 循环、心跳、退避
    api.py               投递 FastAPI 路由与生命周期
  lims/
    db.py                模拟 LIMS SQLite（唯一约束、事件）
    schemas.py           LIMS 请求/回执/冲突模型
    app.py               LIMS 路由、幂等规则、故障注入
demo/
  walkthrough.sh         端到端演示脚本
  tasks.csv / results.csv 原核对服务演示数据
tests/                   单元与端到端测试
docker-compose.yml       api + worker1 + worker2 + lims
Dockerfile               统一镜像（通过 command 区分角色）
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DELIVERY_DB_PATH` | `/data/delivery.db` | 投递端 SQLite 路径 |
| `LIMS_DB_PATH` | `/data/lims.db` | 模拟 LIMS SQLite 路径 |
| `LIMS_BASE_URL` | `http://lims:8001` | LIMS 地址 |
| `LIMS_TIMEOUT_SECONDS` | `5` | 单次投递 HTTP 超时 |
| `LEASE_SECONDS` | `30` | worker 租约时长 |
| `POLL_INTERVAL_SECONDS` | `0.5` | worker 轮询间隔 |
| `BACKOFF_BASE_SECONDS` / `BACKOFF_MAX_SECONDS` | `1` / `30` | 失败退避 |
| `ENABLE_INPROCESS_WORKER` | `true` | API 进程内 worker（compose 中关闭，交给两个独立 worker） |
