# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command and reply shapes other pieces build on are pinned (seam L7, OMN-24484).

The registry standby (plan B, rung 1) and the federation forwarder (rung 3) are owned by other lanes
and reach this node over the bus. Their code is not here, so this test pins the field names of the
command, the reply and the table: a change to any of them fails here and names what to tell those
owners, before either side is first to fail at runtime.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseCaller,
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseReceipt,
    ModelLeaseRetained,
    ModelLeaseTable,
)

pytestmark = pytest.mark.unit

PINNED: dict[type[BaseModel], set[str]] = {
    ModelLeaseCaller: {"principal", "channel"},
    ModelLeaseReduceInput: {
        "correlation_id",
        "caller",
        "operation",
        "table",
        "subject",
        "now",
        "holder_run",
        "epoch",
        "deadline_at",
        "evidence",
        "outcome_ref",
    },
    ModelLeaseReduceOutput: {
        "correlation_id",
        "operation",
        "subject",
        "status",
        "reason",
        "epoch",
        "held_by_run",
        "held_epoch",
        "table",
    },
    ModelLeaseTable: {
        "kind",
        "next_epoch",
        "leases",
        "revoked_epochs",
        "receipts",
        "retained",
    },
    ModelLeaseEntry: {
        "subject",
        "epoch",
        "holder_run",
        "granted_at",
        "deadline_at",
        "revoked",
    },
    ModelLeaseReceipt: {"subject", "epoch", "run", "recorded_at", "outcome_ref"},
    ModelLeaseRetained: {"subject", "epoch", "holder_run", "released_at"},
}


@pytest.mark.parametrize("model", list(PINNED), ids=lambda m: m.__name__)
def test_the_shape_is_pinned(model: type[BaseModel]) -> None:
    assert set(model.model_fields) == PINNED[model]


def test_control_an_added_field_is_red() -> None:
    assert set(ModelLeaseTable.model_fields) | {"extra"} != PINNED[ModelLeaseTable]
