# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join J1, the code graph to hosts and capacity (rebuild plan 4.1, OMN-22904).

One run reads one graph run's verdicts for the nodes host discovery and placement depend on and gives one typed result
under that run's correlation id. The graph run is the upstream piece; the host stage is the downstream piece and starts
only when every verdict is present.

Golden: the graph run completed and carried a verdict (used or unused) for every required node. Both pieces then emit a
start and a success under the graph run's id, the host stage after the graph run.
Error: the graph run failed, or carried no verdict for a required node. The graph run's chain ends in a typed failure
and the host stage emits a typed ``upstream_`` failure and no start, which is the floor's error shape for a join.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.model_host_reading import EnumHostProbe, ModelProbeReading
from omnimarket.models.model_join_j1 import (
    EnumGraphNodeVerdict,
    ModelGraphRunVerdicts,
    ModelJoinJ1Result,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.overlay import ModelJoinJ1Settings
from omnimarket.nodes.node_host_reading_effect.protocols import (
    ProtocolGraphVerdictSource,
)

UPSTREAM_PREFIX = "upstream_"
GRAPH_RUN_FAILED = "graph_run_failed"
VERDICT_MISSING = "verdict_missing"
_DETAIL_LIMIT = 300


def judge_join_j1(
    settings: ModelJoinJ1Settings, run: ModelGraphRunVerdicts
) -> ModelJoinJ1Result:
    """The join's result for one graph run. Pure."""
    if run.status == "failed":
        return ModelJoinJ1Result(
            join=settings.join,
            correlation_id=run.correlation_id,
            state="failed",
            started=False,
            failure_code=GRAPH_RUN_FAILED,
            failure_detail=run.failure[:_DETAIL_LIMIT],
        )
    carried = {
        v.node: v for v in run.verdicts if v.verdict is not EnumGraphNodeVerdict.MISSING
    }
    missing = tuple(n.name for n in settings.nodes if n.name not in carried)
    verdicts = tuple(carried[n.name] for n in settings.nodes if n.name in carried)
    if missing:
        return ModelJoinJ1Result(
            join=settings.join,
            correlation_id=run.correlation_id,
            state="failed",
            started=False,
            verdicts=verdicts,
            missing=missing,
            failure_code=VERDICT_MISSING,
            failure_detail=f"no verdict for {', '.join(missing)}"[:_DETAIL_LIMIT],
        )
    return ModelJoinJ1Result(
        join=settings.join,
        correlation_id=run.correlation_id,
        state="passed",
        started=True,
        verdicts=verdicts,
    )


def emit_join_chains(
    settings: ModelJoinJ1Settings,
    result: ModelJoinJ1Result,
    sink: ProtocolEmissionSink,
    *,
    backend: str,
    clock: Callable[[], datetime],
) -> None:
    """Publish the two chains of the join under the graph run's correlation id, in the order the floor judges them."""
    graph = ChainEmitter(
        sink,
        node=settings.graph_chain_node,
        piece=settings.from_piece,
        clock=clock,
        correlation_id=result.correlation_id,
        backend=backend,
    )
    graph.start()
    if result.state == "passed":
        graph.success({"verdicts": len(result.verdicts)})
        host = ChainEmitter(
            sink,
            node=settings.host_chain_node,
            piece=settings.to_piece,
            clock=clock,
            correlation_id=result.correlation_id,
            backend=backend,
        )
        host.start()
        host.success({"verdicts": len(result.verdicts)})
        return
    graph.failure(result.failure_code, result.failure_detail)
    sink.emit(
        ModelNodeEmission(
            node=settings.host_chain_node,
            piece=settings.to_piece,
            correlation_id=result.correlation_id,
            phase="failure",
            emitted_at=clock(),
            backend="in_memory" if backend == "in_memory" else "real",
            failure_code=f"{UPSTREAM_PREFIX}{result.failure_code}",
            failure_detail=result.failure_detail,
        )
    )


def _cell(text: str) -> str:
    return text.replace("|", "/").replace(" ", "_") or "none"


def reading_of(result: ModelJoinJ1Result) -> ModelProbeReading:
    """The result as a host reading row: WORKING when the join passed, NOT_WORKING with its typed code when it failed."""
    verdicts = "+".join(f"{v.node}:{v.verdict.value}" for v in result.verdicts)
    cells = {
        "join": result.join,
        "correlation": _cell(result.correlation_id),
        "join_state": result.state,
        "started": "yes" if result.started else "no",
        "verdicts": _cell(verdicts),
        "missing": _cell("+".join(result.missing)),
    }
    if result.state == "passed":
        return ModelProbeReading(
            probe=EnumHostProbe.JOIN_J1,
            verdict=EnumReadingVerdict.WORKING,
            outcome=EnumReadingOutcome.READ,
            reasons=("every-required-verdict-present",),
            cells=cells,
        )
    return ModelProbeReading(
        probe=EnumHostProbe.JOIN_J1,
        verdict=EnumReadingVerdict.NOT_WORKING,
        outcome=EnumReadingOutcome.READ,
        reasons=(f"{UPSTREAM_PREFIX}{result.failure_code}",),
        cells=cells,
    )


def read_join_j1(
    settings: ModelJoinJ1Settings,
    source: ProtocolGraphVerdictSource,
    sink: ProtocolEmissionSink | None,
    *,
    correlation_id: str,
    clock: Callable[[], datetime],
) -> tuple[ModelJoinJ1Result, ModelProbeReading]:
    """Read one graph run, judge the join, publish its chains when a sink is given, and return both forms of the result."""
    run = source.read(correlation_id)
    result = judge_join_j1(settings, run)
    if sink is not None:
        emit_join_chains(
            settings,
            result,
            sink,
            backend=source.backend,
            clock=lambda: clock().astimezone(UTC),
        )
    return result, reading_of(result)
