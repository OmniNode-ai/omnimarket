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
from typing import Literal

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.model_host_reading import EnumHostProbe, ModelProbeReading
from omnimarket.models.model_join_j1 import (
    EnumGraphNodeVerdict,
    ModelGraphRunVerdicts,
    ModelJoinJ1Result,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.overlay import (
    ModelJoinJ1Node,
    ModelJoinJ1Settings,
)
from omnimarket.nodes.node_host_reading_effect.protocols import (
    ProtocolGraphVerdictSource,
)

UPSTREAM_PREFIX = "upstream_"
GRAPH_RUN_FAILED = "graph_run_failed"
VERDICT_MISSING = "verdict_missing"
CONTROL_NOT_BROKEN = "join-j1-control-did-not-break"
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
    backend: Literal["in_memory", "real"],
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
        origin="probe",
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
            origin="probe",
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
            backend=backend,
            origin="probe",
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


def with_control(settings: ModelJoinJ1Settings) -> ModelJoinJ1Settings:
    """The same join with its failing control added as a node the graph run must carry a verdict for.

    The control is a node the graph cannot hold, so no graph run carries its verdict and the join must fail on it.
    """
    assert settings.control_node is not None
    first = settings.nodes[0]
    control = ModelJoinJ1Node(
        name=settings.control_node, repo=first.repo, package_dir=first.package_dir
    )
    return settings.model_copy(update={"nodes": (*settings.nodes, control)})


def read_join_j1(
    settings: ModelJoinJ1Settings,
    source: ProtocolGraphVerdictSource,
    sink: ProtocolEmissionSink | None,
    *,
    correlation_id: str,
    clock: Callable[[], datetime],
    control_correlation_id: str | None = None,
) -> tuple[ModelJoinJ1Result, ModelProbeReading]:
    """Read one graph run, judge the join, publish its chains when a sink is given, and return both forms of the result.

    With ``control_correlation_id`` and a ``control_node`` in the settings, the same graph run is judged once more
    against the failing control under that id and its chains are published too: the join's error side, a real typed
    failure from a real graph run. A control that does not break the join turns the reading red, because a join whose
    control cannot fail has no error side to read.
    """
    run = source.read(correlation_id)
    result = judge_join_j1(settings, run)
    reading = reading_of(result)
    publish = sink is not None
    if publish:
        assert sink is not None
        emit_join_chains(
            settings,
            result,
            sink,
            backend=source.backend,
            clock=lambda: clock().astimezone(UTC),
        )
    if (
        control_correlation_id is not None
        and settings.control_node is not None
        and result.state == "passed"
    ):
        control = judge_join_j1(
            with_control(settings),
            run.model_copy(update={"correlation_id": control_correlation_id}),
        )
        if control.state == "passed":
            return result, ModelProbeReading(
                probe=EnumHostProbe.JOIN_J1,
                verdict=EnumReadingVerdict.NOT_WORKING,
                outcome=EnumReadingOutcome.READ,
                reasons=(CONTROL_NOT_BROKEN,),
                cells=reading.cells,
            )
        if publish:
            assert sink is not None
            emit_join_chains(
                settings,
                control,
                sink,
                backend=source.backend,
                clock=lambda: clock().astimezone(UTC),
            )
    return result, reading
