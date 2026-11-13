# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A start whose process was killed gets its typed failure from the next cycle (OMN-22864).

Measured on the served floor record of 2026-11-06 18:38Z, and again on the board of 2026-11-07 10:28Z: the chains row names
``piece 17 node_floor_simple_effect:run_floor_simple_cycle: start_without_terminal``. The cycle that left it open ran at
11:09:30Z on 2026-11-06, before every way out of a cycle ended its start (test_cycle_exits_floor_simple.py): the journal is
append-only and the receipts read 48 hours of it, so one open start stayed red for two days with nothing able to close it.

Golden chain: the open start is closed once, with a typed failure under its own correlation id, and the real receipt reader
then finds no open chain. Error chain (the failing controls): a start that is still young is left alone, a node outside the
cycle's pipeline is left alone, a drilled start is closed as a drill, and with the closure dropped (the named injected break)
the receipt reader still reads the open start.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_17a import (
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
    ModelNodeEmission,
)
from omnimarket.nodes.node_floor_simple_effect.handlers import (
    handler_floor_simple_tick as tick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.abandoned_starts import (
    ABANDONED_CODE,
    abandoned_starts,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    EMIT_CYCLE,
    load_runtime,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
    overlay_text,
)
from tests.nodes.node_floor_simple_effect.test_cycle_exits_floor_simple import (
    handler,
    open_chains,
)

pytestmark = pytest.mark.unit

PIECE = ModelFloorSimpleOverlay.model_validate(
    overlay_text("node_floor_simple_compute")
).rebuild.lab_state_piece
ORPHAN = "812794f4f9e646d4a3f9ec3f81aadfdf"
CYCLE_SUBJECT = f"piece {PIECE} {EMIT_CYCLE}"


def start(
    node: str, cid: str, age: timedelta, *, drill: bool = False
) -> ModelNodeEmission:
    return ModelNodeEmission.model_validate(
        {
            "node": node,
            "piece": PIECE,
            "correlation_id": cid,
            "phase": "start",
            "emitted_at": NOW - age,
            "backend": "real",
            "drill": drill,
        }
    )


def run_cycle(tmp_path: Path, sink: InMemoryEmissionSink) -> tuple[Path, bool]:
    home = build_home(tmp_path)
    result = handler(sink).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    return home, result.ok


def test_golden_the_next_cycle_closes_the_start_a_killed_process_left_open(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    sink.emit(start(EMIT_CYCLE, ORPHAN, timedelta(hours=20)))
    home = build_home(tmp_path)
    assert open_chains(sink, home) == [CYCLE_SUBJECT]
    result = handler(sink).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    assert result.ok
    closure = [
        e for e in sink.events if e.correlation_id == ORPHAN and e.phase == "failure"
    ]
    assert [(e.node, e.failure_code, e.piece) for e in closure] == [
        (EMIT_CYCLE, ABANDONED_CODE, PIECE)
    ]
    assert closure[0].emitted_at == NOW - timedelta(hours=20) + timedelta(
        seconds=load_runtime().abandon_after_seconds
    )
    assert closure[0].failure_detail.startswith("the process that emitted this start")
    assert open_chains(sink, home) == []
    success = next(
        e for e in sink.events if e.node == EMIT_CYCLE and e.phase == "success"
    )
    assert success.counters["abandoned_closed"] == 1


def test_golden_a_start_is_closed_once_and_a_clean_journal_closes_nothing(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    sink.emit(start(EMIT_CYCLE, ORPHAN, timedelta(hours=20)))
    home = build_home(tmp_path)
    for _ in range(2):
        handler(sink).run(
            ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
        )
    closures = [e for e in sink.events if e.failure_code == ABANDONED_CODE]
    assert len(closures) == 1
    last_success = [
        e for e in sink.events if e.node == EMIT_CYCLE and e.phase == "success"
    ][-1]
    assert last_success.counters["abandoned_closed"] == 0


def test_error_a_start_younger_than_the_bound_is_a_cycle_still_running(
    tmp_path: Path,
) -> None:
    bound = load_runtime().abandon_after_seconds
    sink = InMemoryEmissionSink()
    sink.emit(start(EMIT_CYCLE, "young", timedelta(seconds=bound - 60)))
    run_cycle(tmp_path, sink)
    assert [e for e in sink.events if e.failure_code == ABANDONED_CODE] == []


def test_error_a_node_outside_the_cycle_pipeline_is_not_closed(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    sink.emit(
        start("node_code_graph_effect:run_code_graph", "other", timedelta(hours=9))
    )
    run_cycle(tmp_path, sink)
    assert [e for e in sink.events if e.failure_code == ABANDONED_CODE] == []


def test_error_a_drilled_start_is_closed_as_a_drill(tmp_path: Path) -> None:
    sink = InMemoryEmissionSink()
    sink.emit(start(EMIT_CYCLE, "drilled", timedelta(hours=9), drill=True))
    run_cycle(tmp_path, sink)
    closure = [e for e in sink.events if e.failure_code == ABANDONED_CODE]
    assert [e.drill for e in closure] == [True]


def test_error_the_start_stays_open_when_the_closure_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected break: the cycle never looks for abandoned starts, and the real receipt reader reads the open start."""
    sink = InMemoryEmissionSink()
    sink.emit(start(EMIT_CYCLE, ORPHAN, timedelta(hours=20)))
    monkeypatch.setattr(tick, "abandoned_starts", lambda *_a, **_k: ())
    home, ok = run_cycle(tmp_path, sink)
    assert ok
    assert open_chains(sink, home) == [CYCLE_SUBJECT]


def test_error_a_chain_that_has_a_terminal_is_not_closed() -> None:
    chain = [
        start(EMIT_CYCLE, "done", timedelta(hours=9)),
        start(EMIT_CYCLE, "done", timedelta(hours=9)).model_copy(
            update={"phase": "success"}
        ),
    ]
    assert (
        abandoned_starts(
            chain,
            now=NOW,
            abandon_after_s=300,
            piece=PIECE,
            nodes=frozenset({EMIT_CYCLE}),
        )
        == ()
    )


def test_the_bound_is_above_the_publish_limit_and_below_the_chain_deadline() -> None:
    """A running cycle is never closed from under itself, and the closure comes before the receipts flag the open start."""
    publish = yaml.safe_load(
        (
            Path(tick.__file__).resolve().parents[2]
            / "node_factory_floor_publish_effect"
            / "contract.yaml"
        ).read_text(encoding="utf-8")
    )
    steps = [
        s
        for s in publish["runtime"]["steps"] + publish["runtime"]["after_publish_steps"]
        if s.get("module") == "omnimarket.nodes.node_floor_simple_effect"
    ]
    assert steps, "the publish node runs the floor cycle"
    bound = load_runtime().abandon_after_seconds
    limits = TEST_LIMITS.step_timeout_seconds
    assert bound > max(limits[s["name"]] for s in steps)
    overlay = yaml.safe_load(
        (OVERLAYS / "node_floor_simple_compute" / "overlay.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert bound < overlay["chain_deadline_s"]


def test_error_a_failure_is_never_read_without_its_start_at_the_window_edge(
    tmp_path: Path,
) -> None:
    """The closing failure is stamped start plus the bound, so near the window edge it outlives its start (Tessl, OMN-22864)."""
    runtime = load_runtime()
    window = timedelta(hours=runtime.state.emission_window_hours)
    bound = timedelta(seconds=runtime.abandon_after_seconds)
    beyond_edge = timedelta(seconds=bound.total_seconds() / 3)
    sink = InMemoryEmissionSink()
    first = start(EMIT_CYCLE, ORPHAN, window + beyond_edge)
    sink.emit(first)
    sink.emit(
        ModelNodeEmission.model_validate(
            {
                "node": EMIT_CYCLE,
                "piece": PIECE,
                "correlation_id": ORPHAN,
                "phase": "failure",
                "emitted_at": first.emitted_at + bound,
                "backend": "real",
                "drill": False,
                "failure_code": ABANDONED_CODE,
                "failure_detail": "closed by a later cycle",
            }
        )
    )
    # the start is older than the window and the failure is inside it
    assert first.emitted_at < NOW - window <= first.emitted_at + bound
    home = build_home(tmp_path)
    floor_ov = load_structured_overlay(
        "node_floor_simple_compute",
        ModelFloorSimpleOverlay,
        environ(home),
        file_env="X",
    )
    receipts = tick.HandlerFloorSimpleTick._receipts(
        sink, floor_ov, NOW, runtime, None, ()
    )
    assert [
        f.subject for f in receipts.findings if f.code == "failure_without_start"
    ] == []


COMPARE = "node_floor_compare_effect:run_floor_compare_cycle"
COMPARE_SUBJECT = f"piece {PIECE} {COMPARE}"


def test_golden_the_next_cycle_closes_a_start_the_comparison_cycle_left_open(
    tmp_path: Path,
) -> None:
    """The board's row of 2026-11-08 08:09Z: the comparison cycle was killed past the publish step's limit (OMN-22864)."""
    sink = InMemoryEmissionSink()
    sink.emit(start(COMPARE, ORPHAN, timedelta(minutes=30)))
    home = build_home(tmp_path)
    assert open_chains(sink, home) == [COMPARE_SUBJECT]
    result = handler(sink).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    assert result.ok
    closure = [
        e for e in sink.events if e.correlation_id == ORPHAN and e.phase == "failure"
    ]
    assert [(e.node, e.failure_code, e.piece) for e in closure] == [
        (COMPARE, ABANDONED_CODE, PIECE)
    ]
    assert open_chains(sink, home) == []


def test_error_the_comparison_start_stays_open_when_it_is_not_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected break: the contract does not name the comparison chain, and the receipt reader reads the open start."""
    sink = InMemoryEmissionSink()
    sink.emit(start(COMPARE, ORPHAN, timedelta(minutes=30)))
    undeclared = load_runtime().model_copy(update={"abandon_chains": ()})
    monkeypatch.setattr(tick, "load_runtime", lambda *_a, **_k: undeclared)
    home, ok = run_cycle(tmp_path, sink)
    assert ok
    assert [e for e in sink.events if e.failure_code == ABANDONED_CODE] == []
    assert open_chains(sink, home) == [COMPARE_SUBJECT]


def test_the_comparison_chain_is_closed_but_is_not_a_pipeline_chain() -> None:
    """No join reads it: declaring it for closure changes no join."""
    rt = load_runtime()
    assert COMPARE in rt.abandon_chains
    assert COMPARE not in rt.pipeline_chains


J2_SEAM = "node_lab_placement_compute:join_j2"
J2_CANARY = "node_host_reading_effect:join_j2"
J2_SEAM_PIECE = 3
J2_CANARY_PIECE = 2


def start_on(piece: int, node: str, cid: str, age: timedelta) -> ModelNodeEmission:
    return ModelNodeEmission.model_validate(
        {
            "node": node,
            "piece": piece,
            "correlation_id": cid,
            "phase": "start",
            "emitted_at": NOW - age,
            "backend": "real",
            "drill": False,
        }
    )


def test_golden_the_next_cycle_closes_the_j2_seam_start_a_killed_join_left_open(
    tmp_path: Path,
) -> None:
    """The floor row of 2026-11-08 21:00Z: piece 3 node_lab_placement_compute:join_j2, start_without_terminal (OMN-23230)."""
    sink = InMemoryEmissionSink()
    sink.emit(start_on(J2_CANARY_PIECE, J2_CANARY, ORPHAN, timedelta(hours=2)))
    sink.emit(start_on(J2_SEAM_PIECE, J2_SEAM, ORPHAN, timedelta(hours=2)))
    home = build_home(tmp_path)
    assert open_chains(sink, home) == [
        f"piece {J2_CANARY_PIECE} {J2_CANARY}",
        f"piece {J2_SEAM_PIECE} {J2_SEAM}",
    ]
    result = handler(sink).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    assert result.ok
    closure = sorted(
        (e.node, e.piece, e.failure_code)
        for e in sink.events
        if e.correlation_id == ORPHAN and e.phase == "failure"
    )
    assert closure == [
        (J2_CANARY, J2_CANARY_PIECE, ABANDONED_CODE),
        (J2_SEAM, J2_SEAM_PIECE, ABANDONED_CODE),
    ]
    assert open_chains(sink, home) == []


def test_error_the_j2_seam_start_stays_open_when_the_contract_does_not_name_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected break: the contract names no other piece's chains, and the receipt reader reads the open seam start."""
    sink = InMemoryEmissionSink()
    sink.emit(start_on(J2_SEAM_PIECE, J2_SEAM, ORPHAN, timedelta(hours=2)))
    undeclared = load_runtime().model_copy(update={"abandon_other_pieces": ()})
    monkeypatch.setattr(tick, "load_runtime", lambda *_a, **_k: undeclared)
    home, ok = run_cycle(tmp_path, sink)
    assert ok
    assert [e for e in sink.events if e.failure_code == ABANDONED_CODE] == []
    assert open_chains(sink, home) == [f"piece {J2_SEAM_PIECE} {J2_SEAM}"]


def test_error_a_young_j2_seam_start_is_left_to_the_join_that_is_still_running(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    sink.emit(start_on(J2_SEAM_PIECE, J2_SEAM, ORPHAN, timedelta(seconds=30)))
    _home, ok = run_cycle(tmp_path, sink)
    assert ok
    assert [e for e in sink.events if e.failure_code == ABANDONED_CODE] == []
