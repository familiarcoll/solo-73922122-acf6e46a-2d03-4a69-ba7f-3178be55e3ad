"""Pydantic response/error schemas; these drive the operational OpenAPI document."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

_TIME_DESCRIPTION = "UTC 整秒，规范格式为 YYYY-MM-DDTHH:MM:SSZ。"


class ReconciliationSummary(BaseModel):
    task_records: int = Field(description="已接受的任务记录数（最多 30）。")
    result_records: int = Field(description="已接受的仪器结果记录数（最多 30）。")
    eligible_candidate_pairs: int = Field(
        description="满足样品码、检测项目相同且检测时间落在闭区间内的候选配对数。"
    )
    max_pairs: int = Field(description="所有可行方案中的最大配对数（第一优化目标）。")
    minimum_total_absolute_deviation_seconds: int = Field(
        description="在最大配对数下，所有最优方案共有的最小 |检测时间-计划时间| 总和（秒）。"
    )
    definite_pair_count: int = Field(description="所有最优方案共有的确定配对数。")
    ambiguous_task_count: int = Field(description="未落在确定配对中的任务记录数。")
    ambiguous_result_count: int = Field(description="未落在确定配对中的结果记录数。")


class DefinitePair(BaseModel):
    task_id: str = Field(description="任务唯一编号。")
    result_id: str = Field(description="结果唯一编号。")
    sample_code: str = Field(description="样品码（任务与结果必须一致）。")
    item: str = Field(description="检测项目（任务与结果必须一致）。")
    planned_time: str = Field(description=f"计划时间。{_TIME_DESCRIPTION}")
    allowed_start: str = Field(description=f"允许检测时段开始（闭区间）。{_TIME_DESCRIPTION}")
    allowed_end: str = Field(description=f"允许检测时段结束（闭区间）。{_TIME_DESCRIPTION}")
    test_time: str = Field(description=f"检测时间。{_TIME_DESCRIPTION}")
    value: str = Field(description="仪器离线结果数值，保留原始文本形式。")
    deviation_seconds: int = Field(description="|检测时间 - 计划时间|，单位秒。")


class PossibleResultMatch(BaseModel):
    result_id: str = Field(description="在该任务的最优方案中可能对应的结果编号。")
    test_time: str = Field(description=f"检测时间。{_TIME_DESCRIPTION}")
    value: str = Field(description="结果数值。")
    deviation_seconds: int = Field(description="该候选配对的 |检测时间 - 计划时间|，单位秒。")


class PossibleTaskMatch(BaseModel):
    task_id: str = Field(description="在该结果的最优方案中可能对应的任务编号。")
    sample_code: str = Field(description="样品码。")
    item: str = Field(description="检测项目。")
    planned_time: str = Field(description=f"计划时间。{_TIME_DESCRIPTION}")
    allowed_start: str = Field(description=f"允许检测时段开始。{_TIME_DESCRIPTION}")
    allowed_end: str = Field(description=f"允许检测时段结束。{_TIME_DESCRIPTION}")
    deviation_seconds: int = Field(description="该候选配对的 |检测时间 - 计划时间|，单位秒。")


class AmbiguousTask(BaseModel):
    task_id: str
    sample_code: str
    item: str
    planned_time: str
    allowed_start: str
    allowed_end: str
    possible_result_pairs: list[PossibleResultMatch] = Field(
        description="该任务在至少一个最优方案中可能配对的全部结果；空列表表示任何最优方案都不会配对。"
    )
    possibly_unpaired: bool = Field(
        description="是否存在至少一个最优方案使该任务不配对。为 true 时不得仅凭候选列表做自动归属。"
    )


class AmbiguousResult(BaseModel):
    result_id: str
    sample_code: str
    item: str
    test_time: str
    value: str
    possible_task_pairs: list[PossibleTaskMatch] = Field(
        description="该结果在至少一个最优方案中可能配对的全部任务；空列表表示任何最优方案都不会配对。"
    )
    possibly_unpaired: bool = Field(
        description="是否存在至少一个最优方案使该结果不配对。为 true 时不得仅凭候选列表做自动归属。"
    )


class ReconciliationResponse(BaseModel):
    summary: ReconciliationSummary
    definite_pairs: list[DefinitePair] = Field(
        description=(
            "所有最优方案共有的配对，可安全导入实验室系统。"
            "未在此列表中的任何候选都不是确定归属。"
        )
    )
    ambiguous_tasks: list[AmbiguousTask] = Field(
        description="未获得确定配对的任务，逐条给出最优方案中可能的候选及是否可能未配对。"
    )
    ambiguous_results: list[AmbiguousResult] = Field(
        description="未获得确定配对的结果，逐条给出最优方案中可能的候选及是否可能未配对。"
    )


class CsvErrorItem(BaseModel):
    file: Literal["tasks", "results"]
    row: int | None = Field(description="CSV 物理行号（表头为第 1 行）；无法定位时为 null。")
    field: str | None = Field(description="出错的字段名；行级错误时为 null。")
    message: str


class CsvErrorDetail(BaseModel):
    code: str = Field(examples=["csv_validation_error"])
    message: str
    errors: list[CsvErrorItem]


class CsvErrorResponse(BaseModel):
    detail: CsvErrorDetail
