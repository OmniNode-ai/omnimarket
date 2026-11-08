# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDependencyGateCompute: the report and the frozen baseline in, PASS or RED out (OMN-23136).

Pure: no file, clock, network or environment; the caller reads the baseline file and hands it in.
The count is the occurrences of the frozen cut's relations in the run plus the residual back
edges, the back-edge occurrences a depth-first walk finds once those relations are removed
(a new relation that closes a cycle the frozen cut does not break). The gate reads RED when
the count, any frozen relation, the size of the largest component or the residual rose above
the frozen value, or when the baseline's own hash no longer matches its content.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping

import yaml
from pydantic import ValidationError

from omnimarket.models.import_relations import (
    EnumDependencyRiseKind,
    ModelDependencyBaseline,
    ModelDependencyGateRequest,
    ModelDependencyGateResult,
    ModelDependencyRise,
)

Pair = tuple[str, str]


def baseline_from_text(text: str) -> ModelDependencyBaseline:
    """The baseline file's text, as the caller read it, parsed into the typed baseline."""
    return ModelDependencyBaseline.model_validate(yaml.safe_load(text))


def residual_back_edges(found: dict[Pair, int], cut: set[Pair]) -> int:
    """Occurrences on the back edges of a sorted depth-first walk of the graph less the cut."""
    graph: dict[str, list[str]] = defaultdict(list)
    for (source, target), occurrences in sorted(found.items()):
        if (source, target) not in cut and occurrences > 0:
            graph[source].append(target)
    nodes = sorted({*graph, *(t for targets in graph.values() for t in targets)})
    state: dict[str, int] = {}
    total = 0
    for root in nodes:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(graph.get(root, ())))]
        while stack:
            node, successors = stack[-1]
            for nxt in successors:
                seen = state.get(nxt)
                if seen is None:
                    state[nxt] = 1
                    stack.append((nxt, iter(graph.get(nxt, ()))))
                    break
                if seen == 1:
                    total += found[(node, nxt)]
            else:
                state[node] = 2
                stack.pop()
    return total


class HandlerDependencyGateCompute:
    """The continuing gate over the frozen cross-dependency baseline."""

    def handle(self, request: ModelDependencyGateRequest) -> ModelDependencyGateResult:
        baseline = request.baseline
        found: dict[Pair, int] = {
            (r.key.source, r.key.target): r.occurrences
            for r in request.report.relations
        }
        frozen = {
            (f.key.source, f.key.target): f.occurrences for f in baseline.relations
        }
        rises: list[ModelDependencyRise] = []
        intact = baseline.hash_matches()
        if not intact:
            rises.append(
                ModelDependencyRise(
                    kind=EnumDependencyRiseKind.BASELINE_HASH_MISMATCH,
                    detail=(
                        f"the baseline file holds {baseline.sha256}; its content "
                        f"hashes to {baseline.body_digest()}"
                    ),
                )
            )
        for pair in sorted(frozen):
            now = found.get(pair, 0)
            if now > frozen[pair]:
                rises.append(
                    ModelDependencyRise(
                        kind=EnumDependencyRiseKind.RELATION_ROSE,
                        detail=f"{pair[0]} to {pair[1]}",
                        frozen=frozen[pair],
                        current=now,
                    )
                )
        residual = residual_back_edges(found, set(frozen))
        if residual > 0:
            rises.append(
                ModelDependencyRise(
                    kind=EnumDependencyRiseKind.RESIDUAL_BACK_EDGE,
                    detail="a relation outside the frozen cut closes a cycle",
                    frozen=0,
                    current=residual,
                )
            )
        subsystems = max((len(c.labels) for c in request.report.cycles), default=0)
        if subsystems > baseline.subsystems:
            rises.append(
                ModelDependencyRise(
                    kind=EnumDependencyRiseKind.SUBSYSTEMS_ROSE,
                    detail="the largest component spans more subsystems",
                    frozen=baseline.subsystems,
                    current=subsystems,
                )
            )
        current = sum(found.get(pair, 0) for pair in frozen) + residual
        if current > baseline.total_occurrences:
            rises.append(
                ModelDependencyRise(
                    kind=EnumDependencyRiseKind.COUNT_ROSE,
                    detail="cut occurrences plus residual back edges",
                    frozen=baseline.total_occurrences,
                    current=current,
                )
            )
        return ModelDependencyGateResult(
            correlation_id=request.correlation_id,
            state="RED" if rises else "PASS",
            current_count=current,
            frozen_count=baseline.total_occurrences,
            residual_occurrences=residual,
            current_subsystems=subsystems,
            frozen_subsystems=baseline.subsystems,
            baseline_sha256=baseline.sha256,
            baseline_intact=intact,
            rises=tuple(rises),
        )


def answer_gate_command(
    payload: bytes, handler: HandlerDependencyGateCompute, topics: Mapping[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, its event out. A payload that is no request is a typed failure.

    ``topics`` is handed in from the contract: ``computed`` for a verdict (PASS or RED) and
    ``failed`` for a request that could not be read.
    """
    try:
        request = ModelDependencyGateRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {
            "correlation_id": "unknown",
            "failure": f"bad-payload:{fields}",
        }
        return [(topics["failed"], json.dumps(failure).encode())]
    result = handler.handle(request)
    return [(topics["computed"], result.model_dump_json().encode())]
