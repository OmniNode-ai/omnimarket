# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stop opens only from an emit of a registered class by the authorised role (OMN-21958, AC6).

A ledger HOLD row that carries ``class=`` and ``key=`` cells is not an emit: ``open_stops`` reads the exception
queue file and nothing else, and the queue opens a stop only from ``HandlerExceptionEmit`` called by the contract's
opening role. Process health counts an ANDON HOLD row as the ``andon.stops_started`` metric and opens nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from omnimarket.nodes.node_exception_queue_effect import handler, models
from omnimarket.nodes.node_process_health_compute.handlers.calc_ledger import _is_andon
from omnimarket.nodes.node_process_health_compute.handlers.ledger_view import (
    parse_row,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-02T02:31:20Z"
HOLD = (
    "lane=lane-fixture | principal=fixture | id=2026-11-02T02:31:20Z-lane-fixture | to=all | "
    "until=2026-11-02T14:00:00Z | scope=dispatch | class=seam-mismatch | key=key-fixture | "
    "swarm=swarm-fixture | ANDON stop, operator 2026-11-02: lines stop on a bug and the bug gets fixed."
)


def test_stop_opens_only_from_an_emit_a_hold_row_with_class_and_key_opens_none(
    tmp_path: Path,
) -> None:
    row = parse_row(ModelLandingLedgerRow(ts=NOW, rtype="HOLD", text=HOLD))
    assert row.cells["class"] == "seam-mismatch"
    assert row.cells["key"] == "key-fixture"
    opened = handler.HandlerOpenStops().handle(
        models.ModelOpenStopsRequest(state_dir=tmp_path)
    )
    assert opened.stops == ()


def test_stop_opens_only_from_an_emit_process_health_counts_the_hold_as_a_metric_only() -> (
    None
):
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        config=SimpleNamespace(
            andon_reason_prefixes=["andon"], andon_text_prefixes=["andon", "andon stop"]
        )
    )
    row = parse_row(ModelLandingLedgerRow(ts=NOW, rtype="HOLD", text=HOLD))
    assert _is_andon(cast(Any, ctx), row)


def test_stop_opens_only_from_an_emit_of_a_registered_class_by_the_authorised_role(
    tmp_path: Path,
) -> None:
    row = models.ModelExceptionRow(
        cls="seam-mismatch",
        key="key-fixture",
        source="detector-fixture",
        evidence="e",
        tried="t",
        proposed_fix="f",
        readback_command="true",
    )
    refused = handler.HandlerExceptionEmit().handle(
        models.ModelExceptionEmitRequest(tmp_path, row, NOW, role="operator")
    )
    assert refused.status == "refused"
    none_yet = handler.HandlerOpenStops().handle(
        models.ModelOpenStopsRequest(state_dir=tmp_path)
    )
    assert none_yet.stops == ()
    opened = handler.HandlerExceptionEmit().handle(
        models.ModelExceptionEmitRequest(tmp_path, row, NOW, role="detector")
    )
    assert opened.stop.startswith("opened:")
    [stop] = (
        handler.HandlerOpenStops()
        .handle(models.ModelOpenStopsRequest(state_dir=tmp_path))
        .stops
    )
    assert stop.scope.key == "key-fixture"
