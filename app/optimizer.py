"""Lexicographic bipartite matching and optimal-solution edge classification.

The optimisation is handled as a min-cost max-flow problem:

1. A maximum-cardinality matching is found first (each flow augmentation adds one
   pair).
2. Among all maximum-cardinality matchings, shortest augmenting paths minimise
   the sum of |test time - planned time|.

Once an optimal flow is known, all other optimal flows are characterised by
zero-cost circulations in the residual network.  The implementation computes a
valid potential for every residual arc, keeps only reduced-cost-zero residual
arcs, and classifies them by strongly connected components.  This avoids
enumerating the potentially enormous set of optimal matchings and lets the API
report both the intersection (definite pairs) and the union (possible pairs).
"""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from typing import Iterable

from .domain import ResultRecord, TaskRecord

_INF = 10**30


@dataclass(frozen=True, slots=True)
class PairOption:
    task_index: int
    result_index: int
    deviation_seconds: int


@dataclass(frozen=True, slots=True)
class AmbiguousTask:
    task_index: int
    possible_pairs: tuple[PairOption, ...]
    possibly_unpaired: bool


@dataclass(frozen=True, slots=True)
class AmbiguousResult:
    result_index: int
    possible_pairs: tuple[PairOption, ...]
    possibly_unpaired: bool


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    max_pairs: int
    minimum_total_absolute_deviation_seconds: int
    eligible_pair_count: int
    definite_pairs: tuple[PairOption, ...]
    ambiguous_tasks: tuple[AmbiguousTask, ...]
    ambiguous_results: tuple[AmbiguousResult, ...]


@dataclass(frozen=True, slots=True)
class _ArcRef:
    source: int
    edge_index: int


@dataclass(slots=True)
class _Edge:
    to: int
    reverse_index: int
    capacity: int
    cost: int


class _Network:
    def __init__(self, node_count: int):
        self.graph: list[list[_Edge]] = [[] for _ in range(node_count)]
        self.node_count = node_count

    def add_edge(self, source: int, target: int, capacity: int, cost: int) -> _ArcRef:
        forward = _Edge(target, len(self.graph[target]), capacity, cost)
        backward = _Edge(source, len(self.graph[source]), 0, -cost)
        self.graph[source].append(forward)
        self.graph[target].append(backward)
        return _ArcRef(source, len(self.graph[source]) - 1)

    def edge(self, ref: _ArcRef) -> _Edge:
        return self.graph[ref.source][ref.edge_index]

    def reverse_edge(self, ref: _ArcRef) -> _Edge:
        edge = self.edge(ref)
        return self.graph[edge.to][edge.reverse_index]

    def max_flow_min_cost(self, source: int, sink: int) -> int:
        """Run successive-shortest-path min-cost max-flow; return the flow value.

        Every forward edge added by this module initially has a non-negative
        cost, so an all-zero potential is valid.  Potentials are maintained after
        each Dijkstra pass, keeping reduced costs non-negative for every node
        that can participate in a future augmentation.
        """

        potentials = [0] * self.node_count
        flow = 0

        while True:
            distances = [_INF] * self.node_count
            previous_node = [-1] * self.node_count
            previous_edge = [-1] * self.node_count
            distances[source] = 0
            queue: list[tuple[int, int]] = [(0, source)]

            while queue:
                distance, node = heappop(queue)
                if distance != distances[node]:
                    continue
                for edge_index, edge in enumerate(self.graph[node]):
                    if edge.capacity <= 0:
                        continue
                    reduced_cost = edge.cost + potentials[node] - potentials[edge.to]
                    candidate = distance + reduced_cost
                    if candidate < distances[edge.to]:
                        distances[edge.to] = candidate
                        previous_node[edge.to] = node
                        previous_edge[edge.to] = edge_index
                        heappush(queue, (candidate, edge.to))

            if distances[sink] == _INF:
                break

            for node, distance in enumerate(distances):
                if distance != _INF:
                    potentials[node] += distance

            increment = _INF
            node = sink
            while node != source:
                incoming = self.graph[previous_node[node]][previous_edge[node]]
                increment = min(increment, incoming.capacity)
                node = previous_node[node]

            node = sink
            while node != source:
                edge = self.graph[previous_node[node]][previous_edge[node]]
                edge.capacity -= increment
                self.graph[node][edge.reverse_index].capacity += increment
                node = previous_node[node]

            flow += increment

        return flow


def _valid_potentials(network: _Network) -> list[int]:
    """Return potentials making every residual reduced cost non-negative.

    A virtual source with zero-cost edges to every node is equivalent to
    starting Bellman-Ford with every distance at zero.  The optimal flow has no
    negative residual cycle, so the relaxation terminates within N passes.
    """

    nodes = network.node_count
    distances = [0] * nodes
    residual_arcs = [
        (source, edge.to, edge.cost)
        for source in range(nodes)
        for edge in network.graph[source]
        if edge.capacity > 0
    ]

    for _ in range(nodes):
        changed = False
        for source, target, cost in residual_arcs:
            candidate = distances[source] + cost
            if candidate < distances[target]:
                distances[target] = candidate
                changed = True
        if not changed:
            return distances

    raise RuntimeError("Residual network unexpectedly contains a negative-cost cycle.")


def _strongly_connected_components(node_count: int, adjacency: list[list[int]]) -> list[int]:
    """Tarjan SCC; returns one component id per node."""

    indices = [-1] * node_count
    low_links = [0] * node_count
    on_stack = [False] * node_count
    stack: list[int] = []
    components = [-1] * node_count
    next_index = 0
    next_component = 0

    def visit(node: int) -> None:
        nonlocal next_index, next_component
        indices[node] = low_links[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack[node] = True

        for neighbour in adjacency[node]:
            if indices[neighbour] == -1:
                visit(neighbour)
                low_links[node] = min(low_links[node], low_links[neighbour])
            elif on_stack[neighbour]:
                low_links[node] = min(low_links[node], indices[neighbour])

        if low_links[node] == indices[node]:
            while True:
                member = stack.pop()
                on_stack[member] = False
                components[member] = next_component
                if member == node:
                    break
            next_component += 1

    for node in range(node_count):
        if indices[node] == -1:
            visit(node)

    return components


def _candidate_is_possible(
    network: _Network,
    used: bool,
    potentials: list[int],
    components: list[int],
    arc: _ArcRef,
) -> bool:
    if used:
        # It belongs to the optimal flow we just computed.
        return True

    edge = network.edge(arc)
    if edge.capacity <= 0:
        return False
    task_node = arc.source
    result_node = edge.to
    reduced_cost = edge.cost + potentials[task_node] - potentials[result_node]
    return reduced_cost == 0 and components[task_node] == components[result_node]


def _candidate_is_removable(
    network: _Network,
    potentials: list[int],
    components: list[int],
    arc: _ArcRef,
) -> bool:
    reverse = network.reverse_edge(arc)
    if reverse.capacity <= 0:
        return False
    task_node = arc.source
    result_node = network.edge(arc).to
    reduced_cost = reverse.cost + potentials[result_node] - potentials[task_node]
    return reduced_cost == 0 and components[result_node] == components[task_node]


def _task_can_be_unpaired(
    network: _Network,
    source_arc: _ArcRef,
    potentials: list[int],
    components: list[int],
    source_node: int,
) -> bool:
    edge = network.edge(source_arc)
    task_node = edge.to
    if edge.capacity > 0:
        return True

    reverse = network.reverse_edge(source_arc)
    reduced_cost = reverse.cost + potentials[task_node] - potentials[source_node]
    return reduced_cost == 0 and components[task_node] == components[source_node]


def _result_can_be_unpaired(
    network: _Network,
    sink_arc: _ArcRef,
    potentials: list[int],
    components: list[int],
    sink_node: int,
) -> bool:
    edge = network.edge(sink_arc)
    result_node = sink_arc.source
    if edge.capacity > 0:
        return True

    reverse = network.reverse_edge(sink_arc)
    reduced_cost = reverse.cost + potentials[sink_node] - potentials[result_node]
    return reduced_cost == 0 and components[result_node] == components[sink_node]


def reconcile(
    tasks: Iterable[TaskRecord],
    results: Iterable[ResultRecord],
) -> ReconciliationDecision:
    task_list = list(tasks)
    result_list = list(results)
    task_count = len(task_list)
    result_count = len(result_list)

    source = 0
    first_task = 1
    first_result = first_task + task_count
    sink = first_result + result_count
    network = _Network(sink + 1)

    source_arcs = [
        network.add_edge(source, first_task + index, 1, 0)
        for index in range(task_count)
    ]
    sink_arcs = [
        network.add_edge(first_result + index, sink, 1, 0)
        for index in range(result_count)
    ]

    candidates: list[PairOption] = []
    candidate_arcs: list[_ArcRef] = []
    task_candidates: list[list[int]] = [[] for _ in range(task_count)]
    result_candidates: list[list[int]] = [[] for _ in range(result_count)]

    for task_index, task in enumerate(task_list):
        for result_index, result in enumerate(result_list):
            if task.sample_code != result.sample_code or task.item != result.item:
                continue
            if not (task.allowed_start <= result.test_time <= task.allowed_end):
                continue
            deviation = int(abs((result.test_time - task.planned_time).total_seconds()))
            candidate = PairOption(task_index, result_index, deviation)
            candidate_index = len(candidates)
            candidates.append(candidate)
            candidate_arcs.append(
                network.add_edge(first_task + task_index, first_result + result_index, 1, deviation)
            )
            task_candidates[task_index].append(candidate_index)
            result_candidates[result_index].append(candidate_index)

    flow = network.max_flow_min_cost(source, sink)
    used = [network.edge(arc).capacity == 0 for arc in candidate_arcs]
    total_deviation = sum(
        candidate.deviation_seconds for candidate, is_used in zip(candidates, used) if is_used
    )

    potentials = _valid_potentials(network)

    # Build the reduced-cost-zero residual graph.  Any alternative optimal flow
    # is a circulation made only of these arcs.
    zero_adjacency: list[list[int]] = [[] for _ in range(network.node_count)]
    for node in range(network.node_count):
        for edge in network.graph[node]:
            if edge.capacity <= 0:
                continue
            reduced_cost = edge.cost + potentials[node] - potentials[edge.to]
            if reduced_cost == 0:
                zero_adjacency[node].append(edge.to)

    components = _strongly_connected_components(network.node_count, zero_adjacency)

    possible = [
        _candidate_is_possible(network, is_used, potentials, components, arc)
        for is_used, arc in zip(used, candidate_arcs)
    ]

    definite_pairs: list[PairOption] = []
    definite_task_indices: set[int] = set()
    definite_result_indices: set[int] = set()
    for candidate, is_used, arc in zip(candidates, used, candidate_arcs):
        if not is_used:
            continue
        if not _candidate_is_removable(network, potentials, components, arc):
            definite_pairs.append(candidate)
            definite_task_indices.add(candidate.task_index)
            definite_result_indices.add(candidate.result_index)

    ambiguous_tasks: list[AmbiguousTask] = []
    for task_index in range(task_count):
        if task_index in definite_task_indices:
            continue
        options = tuple(
            candidates[candidate_index]
            for candidate_index in task_candidates[task_index]
            if possible[candidate_index]
        )
        ambiguous_tasks.append(
            AmbiguousTask(
                task_index=task_index,
                possible_pairs=options,
                possibly_unpaired=_task_can_be_unpaired(
                    network, source_arcs[task_index], potentials, components, source
                ),
            )
        )

    ambiguous_results: list[AmbiguousResult] = []
    for result_index in range(result_count):
        if result_index in definite_result_indices:
            continue
        options = tuple(
            candidates[candidate_index]
            for candidate_index in result_candidates[result_index]
            if possible[candidate_index]
        )
        ambiguous_results.append(
            AmbiguousResult(
                result_index=result_index,
                possible_pairs=options,
                possibly_unpaired=_result_can_be_unpaired(
                    network, sink_arcs[result_index], potentials, components, sink
                ),
            )
        )

    return ReconciliationDecision(
        max_pairs=flow,
        minimum_total_absolute_deviation_seconds=total_deviation,
        eligible_pair_count=len(candidates),
        definite_pairs=tuple(definite_pairs),
        ambiguous_tasks=tuple(ambiguous_tasks),
        ambiguous_results=tuple(ambiguous_results),
    )
