"""Strict, operator-friendly CSV parsing for the reconciliation endpoint."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Sequence

from .domain import ResultRecord, TaskRecord

Side = Literal["tasks", "results"]

MAX_DATA_ROWS = 30
MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class CsvIssue:
    row: int | None
    field: str | None
    message: str


class CsvValidationError(ValueError):
    """Raised when a CSV cannot be interpreted safely."""

    def __init__(self, side: Side, message: str, issues: Sequence[CsvIssue]):
        super().__init__(message)
        self.side = side
        self.message = message
        self.issues = tuple(issues)


_COMMON_ALIASES = {
    "sample_code": (
        "sample_code",
        "sample",
        "sample_id",
        "sample_no",
        "样品码",
        "样品编号",
        "样品编码",
    ),
    "item": (
        "item",
        "test_item",
        "analysis_item",
        "检测项目",
        "项目",
        "检测参数",
    ),
}

_TASK_ALIASES = {
    **_COMMON_ALIASES,
    "task_id": (
        "task_id",
        "task_no",
        "task_code",
        "id",
        "unique_id",
        "唯一编号",
        "任务编号",
        "检测任务编号",
    ),
    "planned_time": (
        "planned_time",
        "plan_time",
        "scheduled_time",
        "scheduled_at",
        "计划时间",
        "计划检测时间",
    ),
    "allowed_start": (
        "allowed_start",
        "allowed_from",
        "window_start",
        "interval_start",
        "allowed_test_start",
        "allowed_period_start",
        "permitted_start",
        "test_start",
        "允许检测开始时间",
        "允许检测时段开始",
        "允许检测时段从",
        "允许检测时间从",
        "检测时段开始",
        "检测时段自",
        "时段开始",
    ),
    "allowed_end": (
        "allowed_end",
        "allowed_to",
        "window_end",
        "interval_end",
        "allowed_test_end",
        "allowed_period_end",
        "permitted_end",
        "test_end",
        "允许检测结束时间",
        "允许检测时段结束",
        "允许检测时段至",
        "允许检测时间至",
        "检测时段结束",
        "检测时段至",
        "时段结束",
    ),
    "allowed_interval": (
        "allowed_interval",
        "allowed_window",
        "allowed_period",
        "test_window",
        "允许检测时段",
        "允许检测时间",
        "检测时段",
    ),
}

_RESULT_ALIASES = {
    **_COMMON_ALIASES,
    "result_id": (
        "result_id",
        "result_no",
        "result_code",
        "record_id",
        "id",
        "unique_id",
        "唯一编号",
        "结果编号",
        "检测结果编号",
    ),
    "test_time": (
        "test_time",
        "measured_at",
        "detected_at",
        "tested_at",
        "检测时间",
        "实测时间",
        "分析时间",
    ),
    "value": (
        "value",
        "result_value",
        "measured_value",
        "measurement",
        "数值",
        "检测数值",
        "结果值",
    ),
}


def _alias_map(side: Side) -> dict[str, str]:
    aliases = _TASK_ALIASES if side == "tasks" else _RESULT_ALIASES
    return {alias.strip().casefold(): canonical for canonical, values in aliases.items() for alias in values}


def _decode_csv(data: bytes, side: Side) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise CsvValidationError(
            side,
            f"{side} CSV 超过 {MAX_FILE_BYTES} 字节上限。",
            (CsvIssue(None, None, "文件过大；检测任务与结果各最多 30 行。"),),
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvValidationError(
            side,
            f"{side} CSV 不是有效的 UTF-8 文本。",
            (CsvIssue(None, None, f"UTF-8 解码失败：{exc.reason}（字节偏移 {exc.start}）。"),),
        ) from None


def _detect_delimiter(text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    candidates = (",", ";", "\t")
    best = max(candidates, key=first_line.count)
    return best if first_line.count(best) else ","


def _parse_utc_seconds(raw: str, field: str, issues: list[CsvIssue], row_number: int) -> datetime | None:
    text = raw.strip()
    if not text:
        issues.append(CsvIssue(row_number, field, "不能为空。"))
        return None

    value = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        issues.append(
            CsvIssue(
                row_number,
                field,
                f"不是有效的 ISO 8601 时间：{text!r}。请使用 YYYY-MM-DDTHH:MM:SSZ。",
            )
        )
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        issues.append(CsvIssue(row_number, field, "缺少时区；时间必须显式标注为 UTC。"))
        return None
    if parsed.utcoffset() != timedelta(0):
        issues.append(CsvIssue(row_number, field, "必须使用 UTC（偏移 +00:00 或 Z），不得使用其他时区。"))
        return None
    if parsed.microsecond:
        issues.append(CsvIssue(row_number, field, "必须是整秒，不能包含小数秒。"))
        return None

    return parsed.astimezone(timezone.utc)


def _split_interval(
    raw: str, issues: list[CsvIssue], row_number: int
) -> tuple[datetime | None, datetime | None]:
    text = raw.strip()
    if not text:
        issues.append(CsvIssue(row_number, "allowed_interval", "不能为空。"))
        return None, None

    parts: list[str] | None = None
    for separator in ("/", "~", "|", ".."):
        if separator in text:
            parts = [part.strip() for part in text.split(separator)]
            break
    if parts is None or len(parts) != 2 or not all(parts):
        issues.append(
            CsvIssue(
                row_number,
                "allowed_interval",
                "允许检测时段必须形如 START/END（两个 UTC 整秒时间）。",
            )
        )
        return None, None

    start = _parse_utc_seconds(parts[0], "allowed_interval", issues, row_number)
    end = _parse_utc_seconds(parts[1], "allowed_interval", issues, row_number)
    return start, end


def _nonempty_row(row: Sequence[str]) -> bool:
    return any(cell.strip() for cell in row)


def _read_rows(data: bytes, side: Side) -> tuple[list[str], list[tuple[int, list[str]]]]:
    text = _decode_csv(data, side)
    if not text.strip():
        raise CsvValidationError(side, f"{side} CSV 为空。", (CsvIssue(None, None, "文件为空，缺少表头。"),))

    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

    try:
        header = next(reader)
    except StopIteration:
        raise CsvValidationError(side, f"{side} CSV 为空。", (CsvIssue(None, None, "文件为空，缺少表头。"),)) from None

    if not _nonempty_row(header):
        raise CsvValidationError(side, f"{side} CSV 表头为空。", (CsvIssue(1, None, "第一行必须是表头。"),))

    data_rows: list[tuple[int, list[str]]] = []
    for row in reader:
        if not _nonempty_row(row):
            continue
        if len(data_rows) >= MAX_DATA_ROWS:
            raise CsvValidationError(
                side,
                f"{side} CSV 超过最多 {MAX_DATA_ROWS} 行数据。",
                (CsvIssue(reader.line_num, None, f"只允许最多 {MAX_DATA_ROWS} 行数据（不含表头）。"),),
            )
        data_rows.append((reader.line_num, row))

    return header, data_rows


def _build_column_map(header: list[str], side: Side, issues: list[CsvIssue]) -> dict[str, int]:
    aliases = _alias_map(side)
    columns: dict[str, int] = {}
    for index, raw_name in enumerate(header):
        normalized = raw_name.strip().casefold()
        canonical = aliases.get(normalized)
        if not canonical:
            continue
        if canonical in columns:
            issues.append(
                CsvIssue(
                    1,
                    canonical,
                    f"表头列 {raw_name!r} 与前面的列重复（都映射到 {canonical}）。",
                )
            )
            continue
        columns[canonical] = index
    return columns


def _cell(row: list[str], columns: dict[str, int], name: str) -> str:
    index = columns.get(name)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _require_columns(
    columns: dict[str, int], required: Sequence[str], issues: list[CsvIssue]
) -> None:
    for name in required:
        if name not in columns:
            issues.append(
                CsvIssue(1, name, f"缺少必需列 {name!r}。请查看 OpenAPI 文档中的 CSV 列定义。")
            )


def _check_row_width(
    row_number: int, row: list[str], header_length: int, issues: list[CsvIssue]
) -> None:
    if len(row) > header_length and any(cell.strip() for cell in row[header_length:]):
        issues.append(
            CsvIssue(
                row_number,
                None,
                f"数据行有 {len(row)} 列，多于表头的 {header_length} 列。",
            )
        )


def parse_tasks_csv(data: bytes) -> list[TaskRecord]:
    side: Side = "tasks"
    issues: list[CsvIssue] = []
    header, rows = _read_rows(data, side)
    columns = _build_column_map(header, side, issues)

    _require_columns(columns, ("task_id", "sample_code", "item", "planned_time"), issues)
    has_split_window = "allowed_start" in columns and "allowed_end" in columns
    has_interval = "allowed_interval" in columns
    if not has_split_window and not has_interval:
        issues.append(
            CsvIssue(
                1,
                "allowed_start/allowed_end",
                "缺少允许检测时段：请提供 allowed_start 与 allowed_end 两列，或 allowed_interval 一列。",
            )
        )
    if issues:
        raise CsvValidationError(side, f"{side} CSV 表头不完整。", issues)

    tasks: list[TaskRecord] = []
    seen_ids: dict[str, int] = {}
    for row_number, raw_row in rows:
        row_issues_start = len(issues)
        _check_row_width(row_number, raw_row, len(header), issues)

        task_id = _cell(raw_row, columns, "task_id")
        sample_code = _cell(raw_row, columns, "sample_code")
        item = _cell(raw_row, columns, "item")

        for field, value in (("task_id", task_id), ("sample_code", sample_code), ("item", item)):
            if not value:
                issues.append(CsvIssue(row_number, field, "不能为空。"))

        if task_id:
            if task_id in seen_ids:
                issues.append(
                    CsvIssue(
                        row_number,
                        "task_id",
                        f"任务编号 {task_id!r} 与第 {seen_ids[task_id]} 行重复。",
                    )
                )
            else:
                seen_ids[task_id] = row_number

        planned_time = _parse_utc_seconds(
            _cell(raw_row, columns, "planned_time"), "planned_time", issues, row_number
        )

        start: datetime | None = None
        end: datetime | None = None
        if has_split_window:
            start = _parse_utc_seconds(
                _cell(raw_row, columns, "allowed_start"), "allowed_start", issues, row_number
            )
            end = _parse_utc_seconds(
                _cell(raw_row, columns, "allowed_end"), "allowed_end", issues, row_number
            )
        if (start is None or end is None) and has_interval:
            interval_start, interval_end = _split_interval(
                _cell(raw_row, columns, "allowed_interval"), issues, row_number
            )
            if start is None:
                start = interval_start
            if end is None:
                end = interval_end

        if start is not None and end is not None and start > end:
            issues.append(
                CsvIssue(
                    row_number,
                    "allowed_start",
                    "允许检测时段的开始时间不能晚于结束时间。",
                )
            )

        if len(issues) == row_issues_start and None not in (planned_time, start, end):
            tasks.append(
                TaskRecord(
                    task_id=task_id,
                    sample_code=sample_code,
                    item=item,
                    planned_time=planned_time,
                    allowed_start=start,
                    allowed_end=end,
                )
            )

    if issues:
        raise CsvValidationError(side, f"{side} CSV 数据不合规。", issues)
    return tasks


def parse_results_csv(data: bytes) -> list[ResultRecord]:
    side: Side = "results"
    issues: list[CsvIssue] = []
    header, rows = _read_rows(data, side)
    columns = _build_column_map(header, side, issues)

    _require_columns(columns, ("result_id", "sample_code", "item", "test_time", "value"), issues)
    if issues:
        raise CsvValidationError(side, f"{side} CSV 表头不完整。", issues)

    results: list[ResultRecord] = []
    seen_ids: dict[str, int] = {}
    for row_number, raw_row in rows:
        row_issues_start = len(issues)
        _check_row_width(row_number, raw_row, len(header), issues)

        result_id = _cell(raw_row, columns, "result_id")
        sample_code = _cell(raw_row, columns, "sample_code")
        item = _cell(raw_row, columns, "item")
        value = _cell(raw_row, columns, "value")

        for field, cell_value in (
            ("result_id", result_id),
            ("sample_code", sample_code),
            ("item", item),
            ("value", value),
        ):
            if not cell_value:
                issues.append(CsvIssue(row_number, field, "不能为空。"))

        if result_id:
            if result_id in seen_ids:
                issues.append(
                    CsvIssue(
                        row_number,
                        "result_id",
                        f"结果编号 {result_id!r} 与第 {seen_ids[result_id]} 行重复。",
                    )
                )
            else:
                seen_ids[result_id] = row_number

        test_time = _parse_utc_seconds(
            _cell(raw_row, columns, "test_time"), "test_time", issues, row_number
        )

        if value:
            try:
                numeric_value = Decimal(value)
            except InvalidOperation:
                issues.append(CsvIssue(row_number, "value", f"不是有效数值：{value!r}。"))
            else:
                if not numeric_value.is_finite():
                    issues.append(CsvIssue(row_number, "value", "数值必须是有限数（不能为 NaN 或 Infinity）。"))

        if len(issues) == row_issues_start and test_time is not None:
            results.append(
                ResultRecord(
                    result_id=result_id,
                    sample_code=sample_code,
                    item=item,
                    test_time=test_time,
                    value=value,
                )
            )

    if issues:
        raise CsvValidationError(side, f"{side} CSV 数据不合规。", issues)
    return results
