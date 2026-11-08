# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join J1, the code graph to hosts and capacity (OMN-22904): golden and error chain event tests on both backends.

A graph run that carries the used-or-unused verdict of every node host discovery and placement depend on gives one typed
result under the run's correlation id, and both pieces emit a start and a success under it. A graph run whose verdict is
missing gives a typed failure and the host stage never starts: the graph run ends in a typed failure and the host stage
emits a typed ``upstream_`` failure and no start. The floor's own receipt fold then reads the emissions as the golden and
the error side of join J1.

The in-memory backend answers from a fixed run; the real backend builds the graph from real git clones and takes the
verdict from it. Each named break is applied the same way on both, and the last block shows the join's reading turn red:
a host stage that starts on a missing verdict is named by the floor as a break.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    JsonlEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.models.model_host_reading import (
    EnumHostProbe,
    ModelHostReadingCommand,
    ModelProbeReading,
)
from omnimarket.models.model_join_j1 import (
    EnumGraphNodeVerdict,
    ModelGraphNodeVerdict,
    ModelGraphRunVerdicts,
    ModelJoinJ1Result,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    GitRepoSource,
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import GraphRules
from omnimarket.nodes.node_code_graph_effect.protocols import ProtocolRepoSource
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_host_reading_effect.adapters.graph_verdict_source import (
    GraphBuildingVerdictSource,
    InMemoryGraphVerdictSource,
)
from omnimarket.nodes.node_host_reading_effect.adapters.host_files import (
    InMemoryHostFiles,
)
from omnimarket.nodes.node_host_reading_effect.handlers.handler_host_reading import (
    HandlerHostReading,
)
from omnimarket.nodes.node_host_reading_effect.handlers.probe_join_j1 import (
    GRAPH_RUN_FAILED,
    UPSTREAM_PREFIX,
    VERDICT_MISSING,
    judge_join_j1,
    read_join_j1,
)
from omnimarket.nodes.node_host_reading_effect.overlay import (
    ModelHostReadingOverlay,
    ModelJoinJ1Settings,
)
from omnimarket.nodes.node_host_reading_effect.protocols import (
    ProtocolGraphVerdictSource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    REF,
    make_clone,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    overlay as graph_overlay,
)
from tests.nodes.node_host_reading_effect.host_reading_support import RUN_AT, overlay

pytestmark = pytest.mark.unit

DISCOVERY = "node_lane_host_discovery_compute"
PLACEMENT = "node_lab_placement_compute"
CORRELATION = "graph-run-golden-1"
BACKENDS = ("memory", "real")
JOIN_HOST = "example-join-host"


def _clock() -> Callable[[], datetime]:
    ticks = iter(range(10_000))
    return lambda: RUN_AT + timedelta(seconds=next(ticks))


def settings() -> ModelJoinJ1Settings:
    found = overlay().join_j1
    assert found is not None
    return found


def _node_files(node: str, *, linked: bool) -> dict[str, str]:
    """The node's package; a script at a root imports its handler when ``linked``, so the graph reaches it."""
    package = f"src/example_app/nodes/{node}"
    files = {
        f"{package}/__init__.py": "",
        f"{package}/handler.py": "class Handler:\n    pass\n",
    }
    if linked:
        files[f"scripts/{node}.py"] = (
            f"from example_app.nodes.{node}.handler import Handler\n\nHANDLER = Handler\n"
        )
    return files


def app_files(*, discovery: str = "used", placement: str = "used") -> dict[str, str]:
    """The example app with the two nodes the join names: ``used`` (linked from a root), ``unused`` or ``missing``."""
    files = dict(APP)
    for node, state in ((DISCOVERY, discovery), (PLACEMENT, placement)):
        if state != "missing":
            files.update(_node_files(node, linked=state == "used"))
    return files


class JoinWorld:
    """The example app on either backend; a test breaks one thing by name."""

    def __init__(self, backend: str, base: Path) -> None:
        self.backend = backend
        self.base = base
        self.app = app_files()
        self.unreadable = False

    def break_remove_package(self, node: str) -> None:
        for path in [
            p for p in self.app if f"/nodes/{node}/" in p or p.endswith(f"/{node}.py")
        ]:
            del self.app[path]

    def break_unlink_contract(self, node: str) -> None:
        """The node stays in the repository and nothing at a root imports it any more."""
        del self.app[f"scripts/{node}.py"]

    def break_unreadable_repo(self) -> None:
        self.app = {}
        self.unreadable = True

    def source(self) -> GraphBuildingVerdictSource:
        cfg = graph_overlay()
        repos: ProtocolRepoSource
        if self.backend == "memory":
            clones = {"example_core": {REF: ("c" * 40, CORE)}}
            if not self.unreadable:
                clones["example_app"] = {REF: ("a" * 40, self.app)}
            repos = InMemoryRepoSource(clones)
        else:
            make_clone(self.base, "example_core", CORE)
            if not self.unreadable:
                make_clone(self.base, "example_app", self.app)
            repos = GitRepoSource(self.base, max_file_bytes=2_000_000)
        return GraphBuildingVerdictSource(
            source=repos,
            rules=GraphRules(roots=cfg.graph_roots),
            repos=tuple((r.name, r.ref) for r in cfg.repos),
            nodes=settings().nodes,
        )


@pytest.fixture(params=BACKENDS)
def world(request: pytest.FixtureRequest, tmp_path: Path) -> JoinWorld:
    return JoinWorld(request.param, tmp_path)


def verdict(node: str, state: str = "used") -> ModelGraphNodeVerdict:
    return ModelGraphNodeVerdict(
        node=node,
        verdict=EnumGraphNodeVerdict(state),
        modules=0 if state == "missing" else 2,
        reached=2 if state == "used" else 0,
    )


def floor_fold(
    events: tuple[ModelNodeEmission, ...], now: datetime
) -> ModelFloorReceipts:
    """The floor's own receipt fold over the emissions, with the join the overlay declares."""
    cfg = settings()
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
            emissions=events,
            pieces=(
                ModelPieceDef(
                    piece=cfg.from_piece, name="graph", reading_max_age_s=7200
                ),
                ModelPieceDef(piece=cfg.to_piece, name="hosts", reading_max_age_s=7200),
            ),
            joins=(
                ModelJoinDef(
                    id=cfg.join,
                    from_piece=cfg.from_piece,
                    to_piece=cfg.to_piece,
                    label="code graph to hosts",
                ),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=600,
        )
    )


def run_join(
    world: JoinWorld, sink: InMemoryEmissionSink, correlation: str = CORRELATION
) -> tuple[ModelJoinJ1Result, ModelProbeReading]:
    return read_join_j1(
        settings(),
        world.source(),
        sink,
        correlation_id=correlation,
        clock=_clock(),
    )


# --- golden ------------------------------------------------------------------------------------------------------------


def test_golden_graph_run_with_every_verdict_gives_one_typed_result_under_its_correlation_id(
    world: JoinWorld,
) -> None:
    sink = InMemoryEmissionSink()
    result, reading = run_join(world, sink)
    assert isinstance(result, ModelJoinJ1Result)
    assert result.state == "passed"
    assert result.started is True
    assert result.correlation_id == CORRELATION
    assert result.join == "J1"
    assert {v.node: v.verdict for v in result.verdicts} == {
        DISCOVERY: EnumGraphNodeVerdict.USED,
        PLACEMENT: EnumGraphNodeVerdict.USED,
    }
    assert result.missing == ()
    assert result.failure_code == ""
    assert reading.verdict is EnumReadingVerdict.WORKING
    assert reading.outcome is EnumReadingOutcome.READ
    assert reading.cells["correlation"] == CORRELATION
    assert reading.cells["started"] == "yes"


def test_golden_an_unused_verdict_is_a_verdict_and_the_join_passes(
    world: JoinWorld,
) -> None:
    world.break_unlink_contract(PLACEMENT)
    sink = InMemoryEmissionSink()
    result, _ = run_join(world, sink)
    assert isinstance(result, ModelJoinJ1Result)
    assert result.state == "passed"
    assert {v.node: v.verdict for v in result.verdicts}[PLACEMENT] is (
        EnumGraphNodeVerdict.UNUSED
    )


def test_golden_both_pieces_emit_a_start_and_a_success_under_the_graph_runs_id(
    world: JoinWorld,
) -> None:
    sink = InMemoryEmissionSink()
    run_join(world, sink)
    cfg = settings()
    shape = [(e.piece, e.phase, e.correlation_id) for e in sink.events]
    assert shape == [
        (cfg.from_piece, "start", CORRELATION),
        (cfg.from_piece, "success", CORRELATION),
        (cfg.to_piece, "start", CORRELATION),
        (cfg.to_piece, "success", CORRELATION),
    ]
    assert {e.backend for e in sink.events} == {"real"}
    stamps = [e.emitted_at for e in sink.events]
    assert stamps == sorted(stamps)


def test_golden_the_floors_own_fold_reads_the_emissions_as_the_golden_side_of_j1(
    world: JoinWorld,
) -> None:
    sink = InMemoryEmissionSink()
    run_join(world, sink)
    receipts = floor_fold(sink.read(RUN_AT), RUN_AT + timedelta(minutes=5))
    (join,) = receipts.joins
    assert join.join == "J1"
    assert join.state == "golden_only"
    assert join.golden_at is not None
    assert join.error_at is None
    assert receipts.findings == ()


# --- error -------------------------------------------------------------------------------------------------------------


def test_error_graph_run_with_a_verdict_missing_gives_a_typed_failure_and_no_start(
    world: JoinWorld,
) -> None:
    world.break_remove_package(PLACEMENT)
    sink = InMemoryEmissionSink()
    result, reading = run_join(world, sink, "graph-run-error-1")
    assert isinstance(result, ModelJoinJ1Result)
    assert result.state == "failed"
    assert result.started is False
    assert result.correlation_id == "graph-run-error-1"
    assert result.failure_code == VERDICT_MISSING
    assert result.missing == (PLACEMENT,)
    assert PLACEMENT in result.failure_detail
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.reasons == (f"{UPSTREAM_PREFIX}{VERDICT_MISSING}",)
    assert reading.cells["started"] == "no"
    assert reading.cells["missing"] == PLACEMENT


def test_error_the_host_stage_emits_a_typed_upstream_failure_and_never_a_start(
    world: JoinWorld,
) -> None:
    world.break_remove_package(DISCOVERY)
    sink = InMemoryEmissionSink()
    run_join(world, sink, "graph-run-error-2")
    cfg = settings()
    host = [e for e in sink.events if e.piece == cfg.to_piece]
    assert [e.phase for e in host] == ["failure"]
    assert host[0].failure_code == f"{UPSTREAM_PREFIX}{VERDICT_MISSING}"
    assert host[0].correlation_id == "graph-run-error-2"
    graph = [e for e in sink.events if e.piece == cfg.from_piece]
    assert [e.phase for e in graph] == ["start", "failure"]
    assert graph[1].failure_code == VERDICT_MISSING


def test_error_the_floors_own_fold_reads_the_emissions_as_the_error_side_of_j1(
    world: JoinWorld,
) -> None:
    world.break_remove_package(PLACEMENT)
    sink = InMemoryEmissionSink()
    run_join(world, sink, "graph-run-error-3")
    receipts = floor_fold(sink.read(RUN_AT), RUN_AT + timedelta(minutes=5))
    (join,) = receipts.joins
    assert join.state == "error_only"
    assert join.error_at is not None
    assert join.golden_at is None
    assert receipts.findings == ()


def test_error_an_unreadable_graph_run_is_a_typed_failure_and_no_start(
    world: JoinWorld,
) -> None:
    world.break_unreadable_repo()
    sink = InMemoryEmissionSink()
    result, _ = run_join(world, sink, "graph-run-error-4")
    assert isinstance(result, ModelJoinJ1Result)
    assert (result.state, result.started) == ("failed", False)
    assert result.failure_code == GRAPH_RUN_FAILED
    assert result.failure_detail.startswith("repo-unreadable:example_app")
    assert not [
        e for e in sink.events if e.piece == settings().to_piece and e.phase == "start"
    ]


def test_error_a_golden_and_an_error_run_make_the_join_pass_on_the_floor(
    tmp_path: Path,
) -> None:
    good = JoinWorld("memory", tmp_path / "good")
    bad = JoinWorld("memory", tmp_path / "bad")
    bad.break_remove_package(PLACEMENT)
    sink = InMemoryEmissionSink()
    run_join(good, sink, "graph-run-g")
    run_join(bad, sink, "graph-run-e")
    (join,) = floor_fold(sink.read(RUN_AT), RUN_AT + timedelta(minutes=5)).joins
    assert join.state == "passed"
    assert join.golden_at is not None
    assert join.error_at is not None


def test_error_a_verdict_entry_that_says_missing_is_not_a_verdict() -> None:
    run = ModelGraphRunVerdicts(
        correlation_id="g",
        status="completed",
        verdicts=(verdict(DISCOVERY), verdict(PLACEMENT, "missing")),
    )
    result = judge_join_j1(settings(), run)
    assert (result.state, result.missing) == ("failed", (PLACEMENT,))


def test_error_a_failed_run_that_names_no_failure_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelGraphRunVerdicts(correlation_id="g", status="failed")
    with pytest.raises(ValidationError):
        ModelJoinJ1Result(
            join="J1",
            correlation_id="g",
            state="failed",
            started=True,
            failure_code="x",
        )


# --- the journal and the host reading row --------------------------------------------------------------------------------


def test_golden_and_error_chains_land_in_the_floors_journal_file(
    tmp_path: Path,
) -> None:
    journal = JsonlEmissionSink(tmp_path / "floor/emissions.jsonl")
    for correlation, state in (("graph-run-j-g", "used"), ("graph-run-j-e", "missing")):
        run = InMemoryGraphVerdictSource(
            [verdict(DISCOVERY), verdict(PLACEMENT, state)]
        )
        read_join_j1(
            settings(),
            run,
            journal,
            correlation_id=correlation,
            clock=_clock(),
        )
    events = journal.read(RUN_AT)
    assert journal.skipped_lines == 0
    (join,) = floor_fold(events, RUN_AT + timedelta(minutes=5)).joins
    assert join.state == "passed"


def _handler(
    source: ProtocolGraphVerdictSource | None,
    ledger: InMemoryLedger,
    sink: InMemoryEmissionSink | None,
) -> HandlerHostReading:
    return HandlerHostReading(
        overlay=overlay(),
        host=JOIN_HOST,
        files=InMemoryHostFiles(),
        appender=ledger,
        clock=_clock(),
        environ={},
        graph_source=source,
        sink=sink,
        nonce_for=lambda _host: "n1",
    )


def _command(write: bool = True) -> ModelHostReadingCommand:
    return ModelHostReadingCommand(
        correlation_id="run-1", probes=(EnumHostProbe.JOIN_J1,), write=write
    )


def test_golden_the_host_reading_run_writes_a_working_row_that_names_the_graph_run() -> (
    None
):
    ledger, sink = InMemoryLedger(), InMemoryEmissionSink()
    source = InMemoryGraphVerdictSource([verdict(DISCOVERY), verdict(PLACEMENT)])
    result = _handler(source, ledger, sink).handle(_command())
    (probe,) = result.probes
    assert probe.verdict is EnumReadingVerdict.WORKING
    assert not probe.failed
    (row,) = ledger.lines
    cells = {k: v for k, _, v in (c.strip().partition("=") for c in row.split(" | "))}
    assert cells["probe"] == "join-j1"
    assert cells["correlation"] == "graph-run-n1"
    assert cells["verdict"] == "WORKING"
    assert cells["started"] == "yes"
    assert {e.correlation_id for e in sink.events} == {"graph-run-n1"}


def test_error_the_host_reading_run_writes_a_not_working_row_with_the_typed_code() -> (
    None
):
    ledger, sink = InMemoryLedger(), InMemoryEmissionSink()
    source = InMemoryGraphVerdictSource([verdict(DISCOVERY)])
    result = _handler(source, ledger, sink).handle(_command())
    (probe,) = result.probes
    assert probe.verdict is EnumReadingVerdict.NOT_WORKING
    assert probe.reasons == (f"{UPSTREAM_PREFIX}{VERDICT_MISSING}",)
    (row,) = ledger.lines
    assert "verdict=NOT_WORKING" in row
    assert "started=no" in row
    assert not [e for e in sink.events if e.piece == 2 and e.phase == "start"]


def test_a_probe_run_appends_and_emits_nothing() -> None:
    ledger, sink = InMemoryLedger(), InMemoryEmissionSink()
    source = InMemoryGraphVerdictSource([verdict(DISCOVERY), verdict(PLACEMENT)])
    handler = _handler(source, ledger, sink)
    result = handler.handle(_command(write=False))
    assert result.probes[0].ledger == "not-written"
    assert ledger.lines == []
    assert sink.events == []


def test_a_host_with_no_graph_source_reads_unknown_never_working() -> None:
    result = _handler(None, InMemoryLedger(), None).handle(_command())
    (probe,) = result.probes
    assert probe.verdict is EnumReadingVerdict.UNKNOWN
    assert probe.reasons == ("join-j1-ports-not-wired",)


def test_the_overlay_refuses_a_host_that_declares_join_j1_with_no_settings() -> None:
    document = overlay().model_dump(mode="json")
    document["join_j1"] = None
    with pytest.raises(ValidationError, match="join-j1 with no join_j1 settings"):
        ModelHostReadingOverlay.model_validate(document)


def test_the_overlay_refuses_a_join_that_joins_a_piece_to_itself() -> None:
    document = overlay().model_dump(mode="json")
    document["join_j1"]["to_piece"] = document["join_j1"]["from_piece"]
    with pytest.raises(ValidationError, match="joins two pieces"):
        ModelHostReadingOverlay.model_validate(document)


# --- the failing control -------------------------------------------------------------------------------------------------


def _host_stage_that_starts_on_a_missing_verdict(
    sink: InMemoryEmissionSink, correlation: str, clock: Callable[[], datetime]
) -> None:
    """The named break: the graph run fails, and the host stage starts and succeeds anyway."""
    cfg = settings()
    graph = ChainEmitter(
        sink,
        node=cfg.graph_chain_node,
        piece=cfg.from_piece,
        clock=clock,
        correlation_id=correlation,
    )
    graph.start()
    graph.failure(VERDICT_MISSING, "no verdict for node_lab_placement_compute")
    host = ChainEmitter(
        sink,
        node=cfg.host_chain_node,
        piece=cfg.to_piece,
        clock=clock,
        correlation_id=correlation,
    )
    host.start()
    host.success()


def test_control_the_floor_names_a_host_stage_that_starts_on_a_missing_verdict() -> (
    None
):
    sink = InMemoryEmissionSink()
    _host_stage_that_starts_on_a_missing_verdict(sink, "graph-run-broken", _clock())
    receipts = floor_fold(sink.read(RUN_AT), RUN_AT + timedelta(minutes=5))
    (join,) = receipts.joins
    assert join.state == "broken"
    assert [f.code for f in receipts.findings] == [
        "downstream_started_after_upstream_failed"
    ]


def test_control_the_error_assertions_turn_red_on_that_break() -> None:
    """The error case's own check, run on the named break: it must find a start, so the error test would fail."""
    sink = InMemoryEmissionSink()
    _host_stage_that_starts_on_a_missing_verdict(sink, "graph-run-broken", _clock())
    started = [
        e for e in sink.events if e.piece == settings().to_piece and e.phase == "start"
    ]
    assert started, (
        "the break starts the host stage; test_error_* asserts it never does"
    )


def test_control_a_run_that_gives_no_correlation_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelJoinJ1Result(join="J1", correlation_id="", state="passed", started=True)
