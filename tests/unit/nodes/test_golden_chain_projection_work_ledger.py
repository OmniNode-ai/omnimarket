# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_projection_work_ledger (OMN-19513): the fold from rows to state.

The full row-to-bus-to-SQL chain is asserted in
``test_golden_chain_work_ledger_emit_effect``; this one pins the projection's own
contract: its terminal event and what it writes on a claim's whole lifecycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_fold import (
    apply_ops,
    fold_row,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_work_ledger/contract.yaml"
)


def test_the_projection_publishes_only_its_applied_event() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.projection-work-ledger-applied.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.projection-work-ledger-applied.v1"
    )


def test_a_claim_lifecycle_folds_to_closed() -> None:
    rows = [
        "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work",
        "2026-09-28T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done",
    ]
    state: dict[str, dict[str, object]] = {}
    for row in rows:
        apply_ops(state, fold_row(ModelWorkLedgerFoldRequest(raw_row=row)).ops)
    assert state["claim:alpha"]["is_open"] is False
