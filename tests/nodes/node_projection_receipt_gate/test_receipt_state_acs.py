# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt state ACs at the projection handler boundary.

This node projects receipt outcomes and their original timestamps. Expiry
decisions belong to the verifier; the projection must retain age evidence.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_receipt_gate.handlers.handler_projection_receipt_gate import (
    HandlerProjectionReceiptGate,
)

pytestmark = pytest.mark.unit


def test_ac_gate_missing_receipt_never_projects_a_pass() -> None:
    result = HandlerProjectionReceiptGate().handle({})

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["pass"] is False
    assert row["name"] == "unknown"
    assert row["signed_at"] is None
    assert row["pr_ref"] is None


def test_ac_gate_stale_receipt_preserves_original_timestamp_and_status() -> None:
    result = HandlerProjectionReceiptGate().handle(
        {
            "event": {
                "task_id": "OMN-17427",
                "overall_pass": True,
                "verified_at": "2020-01-01T00:00:00Z",
            }
        }
    )

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["pass"] is True
    assert row["name"] == "overall"
    assert row["signed_at"] == "2020-01-01T00:00:00+00:00"
    assert row["observed_at"] == "2020-01-01T00:00:00Z"


def test_ac_gate_fail_status_receipt_projects_failure_despite_previous_pass() -> None:
    handler = HandlerProjectionReceiptGate()
    previous = handler.handle(
        {
            "event": {
                "task_id": "OMN-17427",
                "overall_pass": True,
                "verified_at": "2026-10-03T11:00:00Z",
            }
        }
    )

    result = handler.handle(
        {
            "rows": previous["rows"],
            "event": {
                "task_id": "OMN-17427",
                "overall_pass": False,
                "claim": "focused tests failed",
                "verified_at": "2026-10-03T12:00:00Z",
            },
        }
    )

    assert len(result["rows"]) == 2
    latest, historical = result["rows"]
    assert latest["pass"] is False
    assert latest["name"] == "overall"
    assert latest["detail"] == "focused tests failed"
    assert latest["pr_ref"] == "OMN-17427"
    assert historical == previous["rows"][0]
    assert historical["pass"] is True
