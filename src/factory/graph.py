"""Dependency graph algorithms. Pure functions over ``{node: successors}`` mappings."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence


def strongly_connected_components(graph: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative. Nodes are visited in sorted order for deterministic output.

    Edges to nodes that are not keys of ``graph`` are ignored. Each component is sorted;
    the list of components is sorted by first member.
    """
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for start in sorted(graph):
        if start in index_of:
            continue
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            node, child_index = work.pop()
            if child_index == 0:
                index_of[node] = lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            successors = sorted(s for s in set(graph[node]) if s in graph)
            descended = False
            for position in range(child_index, len(successors)):
                successor = successors[position]
                if successor not in index_of:
                    work.append((node, position + 1))
                    work.append((successor, 0))
                    descended = True
                    break
                if successor in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[successor])
            if descended:
                continue
            if lowlink[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

    return sorted(components, key=lambda c: c[0])


def find_cycles(graph: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """Return every cycle-bearing component: size > 1, or a single node with a self-edge."""
    cycles = []
    for component in strongly_connected_components(graph):
        if len(component) > 1 or component[0] in graph[component[0]]:
            cycles.append(component)
    return cycles


def cycle_path(graph: Mapping[str, Sequence[str]], component: Sequence[str]) -> list[str]:
    """Shortest closed path through a cyclic component, starting and ending at its smallest member."""
    members = set(component)
    start = min(component)
    parents: dict[str, str] = {}
    queue: deque[str] = deque()
    for successor in sorted(graph[start]):
        if successor in members and successor not in parents:
            parents[successor] = start
            queue.append(successor)
    while queue:
        node = queue.popleft()
        if node == start:
            break
        for successor in sorted(graph[node]):
            if successor in members and successor not in parents:
                parents[successor] = node
                queue.append(successor)
    backwards = [start]
    node = parents[start]
    while node != start:
        backwards.append(node)
        node = parents[node]
    backwards.append(start)
    return backwards[::-1]
