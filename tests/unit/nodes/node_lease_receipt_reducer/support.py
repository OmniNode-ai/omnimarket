# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the lease reducer tests: the overlay with a second, expiry kind, and a command builder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReleaseRule,
)
from omnimarket.models.lease_receipt.model_lease_overlay import (
    ModelLeaseKindConfig,
    ModelLeaseOverlay,
)
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseCaller,
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)

NOW: Final = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
MINUTE: Final = timedelta(minutes=1)
SLOT_A: Final = "slot-alpha"
SLOT_B: Final = "slot-beta"
SLOT_KIND: Final = "registry-slot"
SLOT_CALLER: Final = "registry"


def slot_overlay(*, retain_receipts: bool = True, bound: int = 3) -> ModelLeaseOverlay:
    """One expiry kind with all six operations, the shape rung 1 of the plan will use."""
    return ModelLeaseOverlay(
        lease_kinds={
            SLOT_KIND: ModelLeaseKindConfig(
                key_pattern=r"^slot-[a-z0-9-]+$",
                release_rule=EnumLeaseReleaseRule.EXPIRY,
                operations_enabled=tuple(EnumLeaseOperation),
                max_ttl_seconds=3600,
                max_now_skew_seconds=1_000_000_000,
                admitted_callers=(SLOT_CALLER,),
                retain_receipts=retain_receipts,
                max_receipts_per_subject=bound,
                outcome_ref_pattern=r"^[A-Za-z0-9_.:-]{0,64}$",
            )
        }
    )


def command(
    operation: EnumLeaseOperation,
    table: ModelLeaseTable,
    subject: str,
    now: datetime,
    **fields: Any,
) -> ModelLeaseReduceInput:
    return ModelLeaseReduceInput(
        correlation_id=f"t-{operation.value}-{subject}",
        caller=ModelLeaseCaller(principal=SLOT_CALLER, channel="in_process"),
        operation=operation,
        table=table,
        subject=subject,
        now=now,
        **fields,
    )


def reduce(
    handler: HandlerLeaseReceiptReducer,
    operation: EnumLeaseOperation,
    table: ModelLeaseTable,
    subject: str,
    now: datetime,
    **fields: Any,
) -> ModelLeaseReduceOutput:
    return handler.handle(command(operation, table, subject, now, **fields))
