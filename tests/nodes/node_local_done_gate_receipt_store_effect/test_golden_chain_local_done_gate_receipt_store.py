# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the local Done gate receipt store node (OMN-26729).

A verdict that carries a receipt goes in, the receipt is in the store, and a
hosted collector then reads the skipped item as verified; a verdict without a
receipt stores nothing, so the same item stays skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.models.model_dod_verify_state import ModelDodVerifyState
from omnimarket.models.model_local_done_gate_receipt import (
    ModelLocalDoneGateItemVerdict,
    ModelLocalDoneGateReceipt,
)
from omnimarket.models.model_local_done_gate_receipt_store import (
    FilesystemLocalDoneGateReceiptStore,
    receipt_reads_verified,
)
from omnimarket.nodes.node_local_done_gate_receipt_store_effect.handlers.handler_local_done_gate_receipt_store import (
    HandlerLocalDoneGateReceiptStore,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)
_TICKET = "OMN-26729"
_SHA = "a" * 40
_ITEM = "dod-lab-only"


def _receipt() -> ModelLocalDoneGateReceipt:
    return ModelLocalDoneGateReceipt(
        ticket_id=_TICKET,
        contract_sha=_SHA,
        items=(ModelLocalDoneGateItemVerdict(item_id=_ITEM, verdict="VERIFIED"),),
        host="h053",
        run_id="run-1",
    )


def test_a_verdict_with_a_receipt_reaches_the_store_and_one_without_stores_nothing(
    tmp_path: Path,
) -> None:
    store = FilesystemLocalDoneGateReceiptStore(tmp_path)
    handler = HandlerLocalDoneGateReceiptStore(store=store)
    bare = ModelDodVerifyState(
        correlation_id=uuid4(), ticket_id=_TICKET, started_at=_NOW, completed_at=_NOW
    )
    assert handler.handle(bare) is False
    assert (
        receipt_reads_verified(
            store.receipts_for(_TICKET),
            ticket_id=_TICKET,
            contract_sha=_SHA,
            item_id=_ITEM,
        )
        is None
    )
    carrying = bare.model_copy(update={"local_done_gate_receipt": _receipt()})
    assert handler.handle(carrying) is True
    assert (
        receipt_reads_verified(
            store.receipts_for(_TICKET),
            ticket_id=_TICKET,
            contract_sha=_SHA,
            item_id=_ITEM,
        )
        == _receipt()
    )
