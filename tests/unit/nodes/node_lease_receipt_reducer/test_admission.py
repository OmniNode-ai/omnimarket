# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The admission rule is internal-only: the overlay names who may reduce (OMN-24484).

A caller whose principal the kind's ``admitted_callers`` does not name is
refused ``caller-not-admitted`` — on either channel — before anything else of
the command is read, and the refused reply carries the input table unchanged
and never the caller's principal. The contract states the rule as
``admission.internal_only: true``.

The failing control shows the rule lives in the overlay's list, not in code:
an overlay that names the outside caller admits it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseReleaseRule,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_overlay import (
    ModelLeaseKindConfig,
    ModelLeaseOverlay,
)
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseCaller,
    ModelLeaseReduceInput,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)

pytestmark = pytest.mark.unit

NOW: Final = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
ADMITTED: Final = "landing-controller"
OUTSIDE: Final = "outside-principal"
PR: Final = "OmniNode-ai/omnimarket#5003"
CONTRACT_PATH: Final = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lease_receipt_reducer"
    / "contract.yaml"
)


def _overlay(admitted: tuple[str, ...] = (ADMITTED,)) -> ModelLeaseOverlay:
    """The packaged default ``landing`` kind, with the named admitted callers."""
    return ModelLeaseOverlay(
        lease_kinds={
            "landing": ModelLeaseKindConfig(
                key_pattern=(
                    r"^(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*"
                    r"|cause:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:[0-9a-f]{12})$"
                ),
                release_rule=EnumLeaseReleaseRule.EVIDENCE,
                operations_enabled=(
                    EnumLeaseOperation.ACQUIRE,
                    EnumLeaseOperation.RELEASE,
                    EnumLeaseOperation.REVOKE,
                    EnumLeaseOperation.RECORD_RECEIPT,
                ),
                max_ttl_seconds=604800,
                admitted_callers=admitted,
                retain_receipts=False,
                max_receipts_per_subject=1,
                outcome_ref_pattern=r"^[A-Za-z0-9_.:-]{0,64}$",
            )
        }
    )


def _acquire(caller: ModelLeaseCaller) -> ModelLeaseReduceInput:
    return ModelLeaseReduceInput(
        correlation_id="corr-admission-1",
        caller=caller,
        operation=EnumLeaseOperation.ACQUIRE,
        table=ModelLeaseTable(kind="landing"),
        subject=PR,
        now=NOW,
        holder_run="run-abc",
        deadline_at=NOW + timedelta(hours=1),
    )


def _contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_an_admitted_in_process_caller_is_recorded() -> None:
    caller = ModelLeaseCaller(principal=ADMITTED, channel="in_process")
    out = HandlerLeaseReceiptReducer(_overlay()).handle(_acquire(caller))
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.reason is EnumLeaseReason.NONE
    assert out.epoch == 1


def test_an_admitted_bus_caller_is_recorded() -> None:
    caller = ModelLeaseCaller(principal=ADMITTED, channel="bus")
    out = HandlerLeaseReceiptReducer(_overlay()).handle(_acquire(caller))
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.reason is EnumLeaseReason.NONE
    assert out.epoch == 1


def test_an_unlisted_caller_is_refused_in_process() -> None:
    caller = ModelLeaseCaller(principal=OUTSIDE, channel="in_process")
    request = _acquire(caller)
    out = HandlerLeaseReceiptReducer(_overlay()).handle(request)
    assert out.status is EnumLeaseStatus.REFUSED
    assert out.reason is EnumLeaseReason.CALLER_NOT_ADMITTED
    assert out.epoch is None
    assert out.table == request.table  # no table change


def test_an_unlisted_caller_is_refused_on_the_bus() -> None:
    caller = ModelLeaseCaller(principal=OUTSIDE, channel="bus")
    request = _acquire(caller)
    out = HandlerLeaseReceiptReducer(_overlay()).handle(request)
    assert out.status is EnumLeaseStatus.REFUSED
    assert out.reason is EnumLeaseReason.CALLER_NOT_ADMITTED
    assert out.table == request.table


def test_the_reply_never_carries_the_caller_principal() -> None:
    handler = HandlerLeaseReceiptReducer(_overlay())
    recorded = handler.handle(
        _acquire(ModelLeaseCaller(principal=ADMITTED, channel="bus"))
    )
    refused = handler.handle(
        _acquire(ModelLeaseCaller(principal=OUTSIDE, channel="bus"))
    )
    for out in (recorded, refused):
        dump = json.dumps(out.model_dump(mode="json"))
        assert OUTSIDE not in dump, "the reply must never carry the caller principal"
        assert ADMITTED not in dump


def test_the_contract_carries_the_internal_only_admission() -> None:
    admission = _contract()["admission"]
    assert admission["internal_only"] is True
    assert admission["admitted_channels"] == ["in_process", "bus"]
    assert "admitted_callers" in admission["admission_rule"]
    assert "caller-not-admitted" in admission["admission_rule"]
    assert admission["reply_scope"].strip() != ""


def test_control_an_overlay_that_names_the_outside_caller_admits_it() -> None:
    request = _acquire(ModelLeaseCaller(principal=OUTSIDE, channel="in_process"))
    assert HandlerLeaseReceiptReducer(_overlay()).handle(request).status is (
        EnumLeaseStatus.REFUSED
    )

    widened = _overlay(admitted=(ADMITTED, OUTSIDE))
    out = HandlerLeaseReceiptReducer(widened).handle(request)
    assert out.status is EnumLeaseStatus.RECORDED, (
        "the admission rule must come from the overlay's list, not from code"
    )
