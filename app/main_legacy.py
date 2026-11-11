"""FastAPI application for laboratory task/result ownership reconciliation."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from .csv_parser import CsvIssue, CsvValidationError, parse_results_csv, parse_tasks_csv
from .domain import ResultRecord, TaskRecord, format_utc
from .optimizer import (
    AmbiguousResult as OptimizerAmbiguousResult,
    AmbiguousTask as OptimizerAmbiguousTask,
    PairOption,
    ReconciliationDecision,
    reconcile,
)
from .schemas import (
    AmbiguousResult,
    AmbiguousTask,
    CsvErrorResponse,
    DefinitePair,
    PossibleResultMatch,
    PossibleTaskMatch,
    ReconciliationResponse,
    ReconciliationSummary,
)

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"

_DESCRIPTION = """
接收两份 UTF-8 CSV：检测任务与仪器离线结果，每份最多 30 行数据（不含表头）。

**配对规则**
- 仅当 `sample_code`（样品码）与 `item`（检测项目）完全相同，且结果 `test_time` 落在任务
  `[allowed_start, allowed_end]` 闭区间内时才可配对。
- 每条任务和每条结果最多配对一次。
- 先最大化配对数；在配对数相同的方案中，再最小化所有配对
  `|test_time - planned_time|` 的总和（单位秒）。

**输出如何使用**
- `definite_pairs`：所有最优方案共有的配对，可导入实验室系统。
- `ambiguous_tasks` / `ambiguous_results`：其余记录的候选集合，以及它们是否可能在某个最优方案中
  未配对。这里列出的候选**不是确定归属**，不得自动导入；应人工复核或由业务规则进一步判定。

**CSV 时间与编号**
- 所有时间必须是带 UTC 标识的整秒时间，推荐 `YYYY-MM-DDTHH:MM:SSZ`；
  也接受等价的 `+00:00`。拒绝其他时区与小秒。
- `task_id` 与 `result_id` 分别在各自文件内唯一。
- 结果 `value` 必须是有限数值；响应中保留原始文本。

**演示数据（包含确定配对、多候选、可能未配对等歧义）**
- [任务 CSV](/demo/tasks.csv)
- [结果 CSV](/demo/results.csv)

下载上述两个文件后，在下方接口点击 *Try it out* 分别选择文件即可运行。
"""

_TAGS_METADATA = [
    {"name": "核对", "description": "上传两份 CSV，返回确定配对与逐条歧义信息。"},
    {"name": "演示数据", "description": "用于 Swagger UI 试运行的歧义演示 CSV。"},
    {"name": "运维", "description": "服务健康检查与文档入口。"},
]

app = FastAPI(
    title="水质实验室检测结果归属核对 API",
    version="1.0.0",
    description=_DESCRIPTION,
    openapi_tags=_TAGS_METADATA,
    contact={"name": "实验室数据平台"},
    swagger_ui_parameters={"tryItOutEnabled": True, "displayRequestDuration": True},
)


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>检测结果归属核对 API</title>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 820px; margin: 3rem auto; padding: 0 1rem; color: #1f2937; line-height: 1.65; }
    h1 { color: #0f766e; }
    code, pre { background: #f3f4f6; border-radius: 6px; padding: .1rem .3rem; }
    pre { padding: 1rem; overflow-x: auto; }
    a { color: #0f766e; }
    ul { padding-left: 1.25rem; }
  </style>
</head>
<body>
  <h1>水质实验室检测结果归属核对 API</h1>
  <p>上传检测任务 CSV 与仪器离线结果 CSV，获得所有最优方案共有的确定配对，并逐条查看候选归属及可能未配对状态。</p>
  <ul>
    <li><a href="/docs">Swagger UI（可上传文件试运行）</a></li>
    <li><a href="/redoc">ReDoc</a></li>
    <li><a href="/openapi.json">OpenAPI JSON</a></li>
    <li><a href="/demo/tasks.csv">演示任务 CSV</a> / <a href="/demo/results.csv">演示结果 CSV</a></li>
    <li><a href="/health">健康检查</a></li>
  </ul>
  <pre>curl -X POST http://localhost:8000/v1/reconciliations \\
  -F "tasks=@demo/tasks.csv;type=text/csv" \\
  -F "results=@demo/results.csv;type=text/csv"</pre>
  <p>演示数据含对称多候选、任务竞争、唯一最优、始终未配对等情况，适合验证 <code>definite_pairs</code> 与
  <code>ambiguous_tasks</code>/<code>ambiguous_results</code> 的区别。</p>
</body>
</html>"""
    )


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/demo/tasks.csv",
    tags=["演示数据"],
    summary="下载演示任务 CSV",
    response_class=FileResponse,
)
def demo_tasks() -> FileResponse:
    return FileResponse(DEMO_DIR / "tasks.csv", media_type="text/csv; charset=utf-8", filename="tasks.csv")


@app.get(
    "/demo/results.csv",
    tags=["演示数据"],
    summary="下载演示结果 CSV",
    response_class=FileResponse,
)
def demo_results() -> FileResponse:
    return FileResponse(DEMO_DIR / "results.csv", media_type="text/csv; charset=utf-8", filename="results.csv")


def _pair_key(
    pair: PairOption,
    tasks: list[TaskRecord],
    results: list[ResultRecord],
) -> tuple[str, str]:
    return (tasks[pair.task_index].task_id, results[pair.result_index].result_id)


def _definite_pair(
    pair: PairOption,
    tasks: list[TaskRecord],
    results: list[ResultRecord],
) -> DefinitePair:
    task = tasks[pair.task_index]
    result = results[pair.result_index]
    return DefinitePair(
        task_id=task.task_id,
        result_id=result.result_id,
        sample_code=task.sample_code,
        item=task.item,
        planned_time=format_utc(task.planned_time),
        allowed_start=format_utc(task.allowed_start),
        allowed_end=format_utc(task.allowed_end),
        test_time=format_utc(result.test_time),
        value=result.value,
        deviation_seconds=pair.deviation_seconds,
    )


def _ambiguous_task(
    item: OptimizerAmbiguousTask,
    tasks: list[TaskRecord],
    results: list[ResultRecord],
) -> AmbiguousTask:
    task = tasks[item.task_index]
    options = sorted(item.possible_pairs, key=lambda pair: results[pair.result_index].result_id)
    return AmbiguousTask(
        task_id=task.task_id,
        sample_code=task.sample_code,
        item=task.item,
        planned_time=format_utc(task.planned_time),
        allowed_start=format_utc(task.allowed_start),
        allowed_end=format_utc(task.allowed_end),
        possible_result_pairs=[
            PossibleResultMatch(
                result_id=results[pair.result_index].result_id,
                test_time=format_utc(results[pair.result_index].test_time),
                value=results[pair.result_index].value,
                deviation_seconds=pair.deviation_seconds,
            )
            for pair in options
        ],
        possibly_unpaired=item.possibly_unpaired,
    )


def _ambiguous_result(
    item: OptimizerAmbiguousResult,
    tasks: list[TaskRecord],
    results: list[ResultRecord],
) -> AmbiguousResult:
    result = results[item.result_index]
    options = sorted(item.possible_pairs, key=lambda pair: tasks[pair.task_index].task_id)
    return AmbiguousResult(
        result_id=result.result_id,
        sample_code=result.sample_code,
        item=result.item,
        test_time=format_utc(result.test_time),
        value=result.value,
        possible_task_pairs=[
            PossibleTaskMatch(
                task_id=tasks[pair.task_index].task_id,
                sample_code=tasks[pair.task_index].sample_code,
                item=tasks[pair.task_index].item,
                planned_time=format_utc(tasks[pair.task_index].planned_time),
                allowed_start=format_utc(tasks[pair.task_index].allowed_start),
                allowed_end=format_utc(tasks[pair.task_index].allowed_end),
                deviation_seconds=pair.deviation_seconds,
            )
            for pair in options
        ],
        possibly_unpaired=item.possibly_unpaired,
    )


def _build_response(
    decision: ReconciliationDecision,
    tasks: list[TaskRecord],
    results: list[ResultRecord],
) -> ReconciliationResponse:
    definite = sorted(decision.definite_pairs, key=lambda pair: _pair_key(pair, tasks, results))
    ambiguous_tasks = sorted(decision.ambiguous_tasks, key=lambda item: tasks[item.task_index].task_id)
    ambiguous_results = sorted(
        decision.ambiguous_results, key=lambda item: results[item.result_index].result_id
    )
    return ReconciliationResponse(
        summary=ReconciliationSummary(
            task_records=len(tasks),
            result_records=len(results),
            eligible_candidate_pairs=decision.eligible_pair_count,
            max_pairs=decision.max_pairs,
            minimum_total_absolute_deviation_seconds=decision.minimum_total_absolute_deviation_seconds,
            definite_pair_count=len(definite),
            ambiguous_task_count=len(ambiguous_tasks),
            ambiguous_result_count=len(ambiguous_results),
        ),
        definite_pairs=[_definite_pair(pair, tasks, results) for pair in definite],
        ambiguous_tasks=[_ambiguous_task(item, tasks, results) for item in ambiguous_tasks],
        ambiguous_results=[_ambiguous_result(item, tasks, results) for item in ambiguous_results],
    )


def _error_item(side: str, issue: CsvIssue) -> dict:
    return {
        "file": side,
        "row": issue.row,
        "field": issue.field,
        "message": issue.message,
    }


@app.post(
    "/v1/reconciliations",
    response_model=ReconciliationResponse,
    tags=["核对"],
    summary="核对任务与离线结果的归属",
    description=(
        "上传 `tasks` 和 `results` 两份 UTF-8 CSV（各最多 30 行数据）。"
        "返回所有最优方案共有的确定配对，以及其余记录的逐条候选和可能未配对标记。"
        "演示文件可从 [`/demo/tasks.csv`](/demo/tasks.csv) 与 "
        "[`/demo/results.csv`](/demo/results.csv) 下载。"
    ),
    responses={
        422: {
            "model": CsvErrorResponse,
            "description": "CSV 表头/数据不合规，或缺少上传文件。",
        }
    },
)
async def reconcile_endpoint(
    tasks: UploadFile = File(
        ...,
        description=(
            "检测任务 CSV。必需列：task_id, sample_code, item, planned_time, "
            "allowed_start, allowed_end；也可用 allowed_interval（START/END）代替后两列。"
        ),
    ),
    results: UploadFile = File(
        ...,
        description=(
            "仪器离线结果 CSV。必需列：result_id, sample_code, item, test_time, value。"
        ),
    ),
) -> ReconciliationResponse:
    task_records: list[TaskRecord] = []
    result_records: list[ResultRecord] = []
    errors: list[dict] = []
    messages: list[str] = []

    try:
        task_records = parse_tasks_csv(await tasks.read())
    except CsvValidationError as exc:
        messages.append(exc.message)
        errors.extend(_error_item("tasks", issue) for issue in exc.issues)

    try:
        result_records = parse_results_csv(await results.read())
    except CsvValidationError as exc:
        messages.append(exc.message)
        errors.extend(_error_item("results", issue) for issue in exc.issues)

    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "csv_validation_error",
                "message": " ".join(messages) or "CSV 校验失败。",
                "errors": errors,
            },
        )

    decision = reconcile(task_records, result_records)
    return _build_response(decision, task_records, result_records)
