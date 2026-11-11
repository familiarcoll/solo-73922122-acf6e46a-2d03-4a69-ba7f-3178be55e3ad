"""Domain records shared by the CSV parser, optimiser, and API layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """A laboratory test task as supplied by the scheduling system."""

    task_id: str
    sample_code: str
    item: str
    planned_time: datetime
    allowed_start: datetime
    allowed_end: datetime


@dataclass(frozen=True, slots=True)
class ResultRecord:
    """One offline instrument result."""

    result_id: str
    sample_code: str
    item: str
    test_time: datetime
    value: str


def format_utc(value: datetime) -> str:
    """Render an aware UTC datetime in the canonical, second-precision form."""

    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
