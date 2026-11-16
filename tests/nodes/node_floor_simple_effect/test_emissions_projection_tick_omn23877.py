# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor cycle reads real work from the floor emissions projection, never from the journal file (OMN-23877)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.handlers.floor_projection import payload_from_emissions
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorSimpleTickRequest,
    ModelNodeEmission,
)
from omnimarket.models.floor_17a.model_floor_17a import ChainOrigin
from omnimarket.nodes.node_floor_emission_compute.handlers import (
    handler_floor_receipt as fold_module,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    build_home,
    environ,
    put_projection,
)

pytestmark = pytest.mark.unit

PROJECTION = "floor_emissions"


def chain_events(
    origin: str, cid: str, node: str = "node_x:x"
) -> list[ModelNodeEmission]:
    sink = InMemoryEmissionSink()
    emitter = ChainEmitter(
        sink,
        node=node,
        piece=3,
        clock=lambda: NOW - timedelta(minutes=5),
        correlation_id=cid,
        origin=cast(ChainOrigin, origin),
    )
    emitter.start()
    emitter.success()
    return sink.events


def seen_by_the_fold(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    *,
    journal: list[ModelNodeEmission],
) -> list[ModelFloorReceiptInput]:
    """Run one cycle with ``journal`` in the file sink; the inputs the receipt fold was given."""
    inputs: list[ModelFloorReceiptInput] = []
    real_handle = fold_module.HandlerFloorReceipt.handle

    def spy(
        self: fold_module.HandlerFloorReceipt, request: ModelFloorReceiptInput
    ) -> Any:
        inputs.append(request)
        return real_handle(self, request)

    monkeypatch.setattr(fold_module.HandlerFloorReceipt, "handle", spy)
    sink = InMemoryEmissionSink()
    sink.events.extend(journal)
    HandlerFloorSimpleTick(sink=sink, clock=lambda: NOW).run(
        ModelFloorSimpleTickRequest(home=str(home)), environ=environ(home)
    )
    return inputs


def origins(inputs: list[ModelFloorReceiptInput], cid: str) -> set[str]:
    return {e.origin for i in inputs for e in i.emissions if e.correlation_id == cid}


def test_golden_chain_a_chain_the_projection_holds_reads_as_the_publisher_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    put_projection(
        home, PROJECTION, payload_from_emissions(chain_events("real", "bus"), 0, 48)
    )
    inputs = seen_by_the_fold(monkeypatch, home, journal=[])
    assert inputs
    assert origins(inputs, "bus") == {"real"}


def test_named_break_a_chain_only_the_journal_file_holds_never_reads_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    inputs = seen_by_the_fold(
        monkeypatch, home, journal=chain_events("real", "file-only")
    )
    assert origins(inputs, "file-only") == {"unknown"}


def test_error_chain_a_projection_that_was_never_published_holds_no_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    inputs = seen_by_the_fold(monkeypatch, home, journal=[])
    assert inputs
    assert origins(inputs, "bus") == set()


def test_a_chain_in_the_journal_and_the_projection_is_read_once_as_the_projection_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    both = chain_events("real", "both")
    put_projection(home, PROJECTION, payload_from_emissions(both, 0, 48))
    inputs = seen_by_the_fold(monkeypatch, home, journal=both)
    assert origins(inputs, "both") == {"real"}
    assert len([e for e in inputs[-1].emissions if e.correlation_id == "both"]) == 2
