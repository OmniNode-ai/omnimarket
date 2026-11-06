# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One suite on both backends: the in-memory home with the in-process validator, and a home on
disk judged by a runtime interpreter run as a separate process (OMN-22660)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    EnumOverlayVerdict,
    ModelOverlayDriftCommand,
    ModelOverlayDriftResult,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_overlay_drift_effect.overlay_drift_support import PLANTED, World

pytestmark = pytest.mark.unit

BACKENDS = ("memory", "local")


def _run(
    world: World, write: bool = True
) -> tuple[ModelOverlayDriftResult, InMemoryLedger]:
    ledger = InMemoryLedger()
    result = world.handler(ledger if write else None).handle(
        ModelOverlayDriftCommand(correlation_id="c-1", write=write)
    )
    return result, ledger


@pytest.mark.parametrize("backend", BACKENDS)
def test_clean_home_reads_working(backend: str, tmp_path: Path) -> None:
    result, ledger = _run(World(backend, tmp_path))
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.counts.checked == 2
    assert result.counts.accepted == 2
    assert result.runtime is not None
    assert result.runtime.version != "not-installed"
    assert len(ledger.lines) == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_planted_rejected_key_reads_not_working_naming_overlay_and_field(
    backend: str, tmp_path: Path
) -> None:
    world = World(backend, tmp_path)
    world.break_plant_rejected_key()
    result, ledger = _run(world)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert result.counts.rejected == 1
    assert result.counts.accepted == 1
    assert [(c.overlay_id, c.verdict, c.fields) for c in result.failing] == [
        (PLANTED, EnumOverlayVerdict.REJECTED, ("rung_order",))
    ]
    assert "claude" not in ledger.lines[0]
    assert "opus" not in ledger.lines[0]


@pytest.mark.parametrize("backend", BACKENDS)
def test_planted_model_the_runtime_lacks_is_counted_and_reads_working(
    backend: str, tmp_path: Path
) -> None:
    world = World(backend, tmp_path)
    world.break_pin_a_model_the_runtime_lacks()
    result, _ = _run(world)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.counts.model_missing == 1
    assert result.counts.accepted == 2
    assert "model_missing-1" in result.reasons


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_model_missing_reads_unknown(backend: str, tmp_path: Path) -> None:
    world = World(backend, tmp_path)
    world.break_pin_every_model_the_runtime_lacks()
    result, _ = _run(world)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.counts.model_missing == 2
    assert result.counts.accepted == 0
    assert "no-overlay-judged" in result.reasons


@pytest.mark.parametrize("backend", BACKENDS)
def test_rejected_key_with_a_missing_model_reads_not_working(
    backend: str, tmp_path: Path
) -> None:
    # The planted key lands on node_reading_writer_effect, the missing model on node_gone_effect.
    world = World(backend, tmp_path)
    world.break_plant_rejected_key()
    world.break_pin_a_model_the_runtime_lacks()
    result, _ = _run(world)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "rejected-1" in result.reasons
    assert "model_missing-1" in result.reasons


@pytest.mark.parametrize("backend", BACKENDS)
def test_planted_pin_for_an_absent_overlay_reads_not_working(
    backend: str, tmp_path: Path
) -> None:
    world = World(backend, tmp_path)
    world.break_pin_an_absent_overlay()
    result, _ = _run(world)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.counts.overlay_missing == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_unpinned_overlay_is_counted_and_does_not_fail(
    backend: str, tmp_path: Path
) -> None:
    world = World(backend, tmp_path)
    world.add_unpinned_overlay()
    result, ledger = _run(world)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.unpinned == ("node_unpinned_effect",)
    assert "unpinned=1" in ledger.lines[0]


@pytest.mark.parametrize("backend", BACKENDS)
def test_absent_runtime_reads_unknown(backend: str, tmp_path: Path) -> None:
    world = World(backend, tmp_path)
    world.break_runtime_absent()
    result, _ = _run(world)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert result.failed


@pytest.mark.parametrize("backend", BACKENDS)
def test_probe_appends_nothing(backend: str, tmp_path: Path) -> None:
    result, ledger = _run(World(backend, tmp_path), write=False)
    assert result.ledger == "not-written"
    assert ledger.lines == []
    assert result.row_key
