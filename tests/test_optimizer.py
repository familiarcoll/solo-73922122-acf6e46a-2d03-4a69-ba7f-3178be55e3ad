from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.csv_parser import parse_results_csv, parse_tasks_csv
from app.domain import ResultRecord, TaskRecord
from app.optimizer import reconcile

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
DEMO = Path(__file__).resolve().parent.parent / "demo"


def _brute_force(edges: list[tuple[int, int, int]], task_count: int, result_count: int):
    adjacency = {index: [] for index in range(task_count)}
    for task_index, result_index, cost in edges:
        adjacency[task_index].append((result_index, cost))

    best_count = -1
    best_cost = 0
    optimal_matchings: list[tuple[tuple[tuple[int, int], ...], set[int]]] = []

    def visit(task_index: int, used: set[int], pairs: list[tuple[int, int]], cost: int) -> None:
        nonlocal best_count, best_cost, optimal_matchings
        if task_index == task_count:
            count = len(pairs)
            if count > best_count or (count == best_count and cost < best_cost):
                best_count = count
                best_cost = cost
                optimal_matchings = [(tuple(pairs), set(used))]
            elif count == best_count and cost == best_cost:
                optimal_matchings.append((tuple(pairs), set(used)))
            return

        visit(task_index + 1, used, pairs, cost)
        for result_index, edge_cost in adjacency[task_index]:
            if result_index in used:
                continue
            used.add(result_index)
            pairs.append((task_index, result_index))
            visit(task_index + 1, used, pairs, cost + edge_cost)
            pairs.pop()
            used.remove(result_index)

    visit(0, set(), [], 0)

    possible_pairs: set[tuple[int, int]] = set()
    definite_pairs: set[tuple[int, int]] | None = None
    possibly_unpaired_tasks: set[int] = set()
    possibly_unpaired_results: set[int] = set()
    for pairs, used in optimal_matchings:
        pair_set = set(pairs)
        possible_pairs |= pair_set
        definite_pairs = pair_set if definite_pairs is None else definite_pairs & pair_set
        paired_tasks = {task for task, _ in pairs}
        possibly_unpaired_tasks |= set(range(task_count)) - paired_tasks
        possibly_unpaired_results |= set(range(result_count)) - used

    return (
        best_count,
        best_cost,
        possible_pairs,
        definite_pairs or set(),
        possibly_unpaired_tasks,
        possibly_unpaired_results,
    )


def _assert_against_brute(
    tasks: list[TaskRecord], results: list[ResultRecord], edges: list[tuple[int, int, int]]
) -> None:
    decision = reconcile(tasks, results)
    expected = _brute_force(edges, len(tasks), len(results))

    possible = {(pair.task_index, pair.result_index) for pair in decision.definite_pairs}
    for item in decision.ambiguous_tasks:
        possible |= {(item.task_index, pair.result_index) for pair in item.possible_pairs}

    definite = {(pair.task_index, pair.result_index) for pair in decision.definite_pairs}
    unpaired_tasks = {
        item.task_index for item in decision.ambiguous_tasks if item.possibly_unpaired
    }
    unpaired_results = {
        item.result_index for item in decision.ambiguous_results if item.possibly_unpaired
    }

    assert decision.max_pairs == expected[0]
    assert decision.minimum_total_absolute_deviation_seconds == expected[1]
    assert possible == expected[2]
    assert definite == expected[3]
    assert unpaired_tasks == expected[4]
    assert unpaired_results == expected[5]


def test_demo_data_has_expected_ambiguity_and_definite_pairs() -> None:
    tasks = parse_tasks_csv((DEMO / "tasks.csv").read_bytes())
    results = parse_results_csv((DEMO / "results.csv").read_bytes())
    decision = reconcile(tasks, results)

    assert decision.max_pairs == 4
    assert decision.minimum_total_absolute_deviation_seconds == 2700
    assert {
        (tasks[pair.task_index].task_id, results[pair.result_index].result_id)
        for pair in decision.definite_pairs
    } == {("T-004", "R-004"), ("T-005", "R-006")}

    tasks_by_id = {item.task_index: tasks[item.task_index].task_id for item in decision.ambiguous_tasks}
    assert tasks_by_id == {0: "T-001", 1: "T-002", 2: "T-003", 5: "T-006"}
    first_task = next(item for item in decision.ambiguous_tasks if item.task_index == 0)
    assert first_task.possibly_unpaired is False
    assert {results[pair.result_index].result_id for pair in first_task.possible_pairs} == {
        "R-001",
        "R-002",
    }

    competing_task = next(item for item in decision.ambiguous_tasks if item.task_index == 1)
    assert competing_task.possibly_unpaired is True
    assert {
        results[pair.result_index].result_id for pair in competing_task.possible_pairs
    } == {"R-003"}

    results_by_id = {item.result_index: results[item.result_index].result_id for item in decision.ambiguous_results}
    assert results_by_id == {0: "R-001", 1: "R-002", 2: "R-003", 4: "R-005", 6: "R-007"}
    result_003 = next(item for item in decision.ambiguous_results if item.result_index == 2)
    assert result_003.possibly_unpaired is False
    assert {tasks[pair.task_index].task_id for pair in result_003.possible_pairs} == {
        "T-002",
        "T-003",
    }

    for result_index in (0, 1):
        item = next(item for item in decision.ambiguous_results if item.result_index == result_index)
        assert item.possibly_unpaired is True

    for result_index in (4, 6):
        item = next(item for item in decision.ambiguous_results if item.result_index == result_index)
        assert item.possible_pairs == ()
        assert item.possibly_unpaired is True


def test_random_dense_graphs_match_brute_force() -> None:
    rng = random.Random(20261111)
    for _ in range(300):
        task_count = rng.randint(0, 5)
        result_count = rng.randint(0, 5)
        planned = [rng.randint(0, 5) for _ in range(task_count)]
        test_times = [rng.randint(0, 5) for _ in range(result_count)]

        tasks = [
            TaskRecord(
                task_id=f"T{index}",
                sample_code="S",
                item="I",
                planned_time=BASE + timedelta(seconds=planned[index]),
                allowed_start=BASE,
                allowed_end=BASE + timedelta(seconds=1000),
            )
            for index in range(task_count)
        ]
        results = [
            ResultRecord(
                result_id=f"R{index}",
                sample_code="S",
                item="I",
                test_time=BASE + timedelta(seconds=test_times[index]),
                value="1",
            )
            for index in range(result_count)
        ]
        edges = [
            (task_index, result_index, abs(test_times[result_index] - planned[task_index]))
            for task_index in range(task_count)
            for result_index in range(result_count)
        ]
        _assert_against_brute(tasks, results, edges)


def test_random_grouped_sparse_graphs_match_brute_force() -> None:
    rng = random.Random(77123)
    for _ in range(300):
        task_count = rng.randint(0, 5)
        result_count = rng.randint(0, 5)
        task_groups = [rng.randint(0, 3) for _ in range(task_count)]
        result_groups = [rng.randint(0, 3) for _ in range(result_count)]
        planned = [rng.randint(0, 5) for _ in range(task_count)]
        test_times = [rng.randint(0, 5) for _ in range(result_count)]

        tasks = [
            TaskRecord(
                task_id=f"T{index}",
                sample_code=f"S{task_groups[index]}",
                item="I",
                planned_time=BASE + timedelta(seconds=planned[index]),
                allowed_start=BASE,
                allowed_end=BASE + timedelta(seconds=1000),
            )
            for index in range(task_count)
        ]
        results = [
            ResultRecord(
                result_id=f"R{index}",
                sample_code=f"S{result_groups[index]}",
                item="I",
                test_time=BASE + timedelta(seconds=test_times[index]),
                value="1",
            )
            for index in range(result_count)
        ]
        edges = [
            (task_index, result_index, abs(test_times[result_index] - planned[task_index]))
            for task_index in range(task_count)
            for result_index in range(result_count)
            if task_groups[task_index] == result_groups[result_index]
        ]
        _assert_against_brute(tasks, results, edges)


def test_closed_interval_and_exact_string_matching() -> None:
    tasks = [
        TaskRecord("T1", "S", "pH", BASE + timedelta(seconds=10), BASE, BASE + timedelta(seconds=10)),
        TaskRecord("T2", "s", "pH", BASE + timedelta(seconds=10), BASE, BASE + timedelta(seconds=10)),
        TaskRecord("T3", "S", "PH", BASE + timedelta(seconds=10), BASE, BASE + timedelta(seconds=10)),
    ]
    results = [ResultRecord("R1", "S", "pH", BASE + timedelta(seconds=10), "7")]
    decision = reconcile(tasks, results)

    assert decision.max_pairs == 1
    assert [(pair.task_index, pair.result_index) for pair in decision.definite_pairs] == [(0, 0)]
    assert {item.task_index for item in decision.ambiguous_tasks} == {1, 2}
    assert all(item.possible_pairs == () and item.possibly_unpaired for item in decision.ambiguous_tasks)


def test_no_records_is_valid() -> None:
    decision = reconcile([], [])
    assert decision.max_pairs == 0
    assert decision.minimum_total_absolute_deviation_seconds == 0
    assert decision.definite_pairs == ()
    assert decision.ambiguous_tasks == ()
    assert decision.ambiguous_results == ()
