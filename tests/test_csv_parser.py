from __future__ import annotations

from pathlib import Path

import pytest

from app.csv_parser import CsvValidationError, parse_results_csv, parse_tasks_csv

DEMO = Path(__file__).resolve().parent.parent / "demo"


def test_demo_csvs_parse() -> None:
    tasks = parse_tasks_csv((DEMO / "tasks.csv").read_bytes())
    results = parse_results_csv((DEMO / "results.csv").read_bytes())
    assert len(tasks) == 6
    assert len(results) == 7
    assert tasks[0].task_id == "T-001"
    assert tasks[0].sample_code == "S-100"
    assert results[1].value == "7.20"


def test_chinese_headers_combined_interval_and_semicolon() -> None:
    task_csv = (
        "任务编号;样品码;检测项目;计划时间;允许检测时段\n"
        "T-1;S-1;pH;2026-11-11T10:00:00Z;2026-11-11T09:00:00Z/2026-11-11T11:00:00Z\n"
    ).encode("utf-8")
    result_csv = (
        "结果编号,样品码,项目,检测时间,数值\n"
        "R-1,S-1,pH,2026-11-11T10:01:00Z,7.2\n"
    ).encode("utf-8")

    tasks = parse_tasks_csv(task_csv)
    results = parse_results_csv(result_csv)
    assert len(tasks) == 1
    assert len(results) == 1
    assert tasks[0].allowed_start.hour == 9
    assert tasks[0].allowed_end.hour == 11
    assert results[0].value == "7.2"


def test_utf8_bom_is_accepted() -> None:
    content = (
        "task_id,sample_code,item,planned_time,allowed_start,allowed_end\n"
        "T-1,S-1,pH,2026-11-11T10:00:00Z,2026-11-11T09:00:00Z,2026-11-11T11:00:00Z\n"
    ).encode("utf-8-sig")
    assert parse_tasks_csv(content)[0].task_id == "T-1"


def test_timezone_offset_and_fractional_seconds_are_rejected() -> None:
    header = "task_id,sample_code,item,planned_time,allowed_start,allowed_end\n"
    body = (
        "T-1,S-1,pH,2026-11-11T18:00:00+08:00,"
        "2026-11-11T09:00:00Z,2026-11-11T11:00:00Z\n"
        "T-2,S-1,pH,2026-11-11T10:00:00.500Z,"
        "2026-11-11T09:00:00Z,2026-11-11T11:00:00Z\n"
    )
    with pytest.raises(CsvValidationError) as raised:
        parse_tasks_csv((header + body).encode("utf-8"))
    messages = " ".join(issue.message for issue in raised.value.issues)
    assert "必须使用 UTC" in messages
    assert "整秒" in messages


def test_duplicate_ids_are_rejected_and_rows_are_reported() -> None:
    csv_text = (
        "task_id,sample_code,item,planned_time,allowed_start,allowed_end\n"
        "T-1,S-1,pH,2026-11-11T10:00:00Z,2026-11-11T09:00:00Z,2026-11-11T11:00:00Z\n"
        "T-1,S-1,pH,2026-11-11T10:00:00Z,2026-11-11T09:00:00Z,2026-11-11T11:00:00Z\n"
    )
    with pytest.raises(CsvValidationError) as raised:
        parse_tasks_csv(csv_text.encode("utf-8"))
    duplicate = next(issue for issue in raised.value.issues if issue.field == "task_id")
    assert duplicate.row == 3
    assert "重复" in duplicate.message


def test_more_than_thirty_rows_is_rejected() -> None:
    header = "task_id,sample_code,item,planned_time,allowed_start,allowed_end\n"
    row = "T-{index},S-1,pH,2026-11-11T10:00:00Z,2026-11-11T09:00:00Z,2026-11-11T11:00:00Z\n"
    csv_text = header + "".join(row.format(index=index) for index in range(31))
    with pytest.raises(CsvValidationError) as raised:
        parse_tasks_csv(csv_text.encode("utf-8"))
    assert "30" in raised.value.message
    assert raised.value.issues[0].row == 32


def test_value_must_be_finite_number() -> None:
    csv_text = (
        "result_id,sample_code,item,test_time,value\n"
        "R-1,S-1,pH,2026-11-11T10:00:00Z,not-a-number\n"
        "R-2,S-1,pH,2026-11-11T10:00:00Z,NaN\n"
    )
    with pytest.raises(CsvValidationError) as raised:
        parse_results_csv(csv_text.encode("utf-8"))
    fields = {issue.field for issue in raised.value.issues}
    assert "value" in fields


def test_missing_allowance_columns_are_rejected() -> None:
    csv_text = "task_id,sample_code,item,planned_time\nT-1,S-1,pH,2026-11-11T10:00:00Z\n"
    with pytest.raises(CsvValidationError) as raised:
        parse_tasks_csv(csv_text.encode("utf-8"))
    messages = " ".join(issue.message for issue in raised.value.issues)
    assert "允许检测时段" in messages
