# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No value of the lease rules lives in code (OMN-24484): kinds, callers and bounds are the overlay's."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_table import (
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.overlay import load_lease_overlay
from tests.unit.nodes.node_lease_receipt_reducer.support import (
    NOW,
    SLOT_A,
    SLOT_KIND,
    reduce,
    slot_overlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lease_receipt_reducer"
)


def test_the_packaged_overlay_carries_the_landing_kind_only() -> None:
    overlay = load_lease_overlay({})

    assert tuple(overlay.lease_kinds) == ("landing",)
    landing = overlay.lease_kinds["landing"]
    assert landing.admitted_callers == ("landing-controller",)
    assert EnumLeaseOperation.RENEW not in landing.operations_enabled
    assert EnumLeaseOperation.TAKE_OVER not in landing.operations_enabled


def test_a_kind_the_overlay_does_not_name_is_refused_unknown_kind() -> None:
    packaged = HandlerLeaseReceiptReducer(load_lease_overlay({}))

    reply = reduce(
        packaged,
        EnumLeaseOperation.ACQUIRE,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
    )

    assert reply.reason is EnumLeaseReason.UNKNOWN_KIND


def test_the_handler_source_names_no_kind_caller_or_bound() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(NODE_DIR.rglob("*.py"))
        if path.name != "overlay.py"
    )

    for literal in ("landing", "registry-slot", "landing-controller", "604800"):
        assert literal not in sources, literal


def test_the_overlay_is_the_only_source_of_a_kind() -> None:
    renamed = HandlerLeaseReceiptReducer(slot_overlay())

    reply = reduce(
        renamed,
        EnumLeaseOperation.ACQUIRE,
        ModelLeaseTable(kind="landing"),
        SLOT_A,
        NOW,
    )

    assert reply.reason is EnumLeaseReason.UNKNOWN_KIND
