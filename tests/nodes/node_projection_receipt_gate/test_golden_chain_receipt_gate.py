# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_projection_receipt_gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_receipt_gate.handlers.handler_projection_receipt_gate import (
    HandlerProjectionReceiptGate,
)
from omnimarket.nodes.node_projection_receipt_gate.reducers.reducer_receipt_gate import (
    reduce_receipt_gate,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = Path("src/omnimarket/nodes/node_projection_receipt_gate/contract.yaml")


def test_golden_chain_receipt_gate_projects_verification_then_occ_evidence() -> None:
    state = reduce_receipt_gate(
        (),
        {
            "_event_type": "onex.evt.omnimarket.verification-receipt-completed.v1",
            "task_id": "OMN-13081",
            "pr_number": 1500,
            "verifier": "node_verification_receipt_generator",
            "overall_pass": True,
            "verified_at": "2026-06-29T07:40:00Z",
            "checks": [
                {
                    "dimension": "focused-tests",
                    "passed": True,
                    "summary": "receipt-gate focused tests passed",
                }
            ],
        },
    )
    state = reduce_receipt_gate(
        state,
        {
            "_event_type": "onex.evt.omnimarket.evidence-validated.v1",
            "ticket_id": "OMN-13081",
            "pr_number": 1500,
            "validation_state": "PASSED",
            "draft_hash": "sha256:" + "1" * 64,
            "validated_at": "2026-06-29T07:41:00Z",
        },
    )

    assert [row.name for row in state] == ["occ-evidence", "focused-tests"]
    assert all(row.pass_ for row in state)
    assert state[0].pr_ref == "OMN-13081 / #1500"
    assert state[0].evidence_hash == "sha256:" + "1" * 64
    assert state[1].verifier == "node_verification_receipt_generator"


def test_golden_chain_receipt_gate_handler_round_trip_preserves_aliases() -> None:
    handler = HandlerProjectionReceiptGate()

    result = handler.handle(
        {
            "rows": [],
            "event": {
                "task_id": "OMN-13081",
                "pr_number": 1500,
                "overall_pass": False,
                "verified_at": "2026-06-29T07:42:00Z",
            },
        }
    )

    assert result["rows"] == [
        {
            "name": "overall",
            "pass": False,
            "detail": "OMN-13081",
            "pr_ref": "OMN-13081 / #1500",
            "worker": None,
            "verifier": None,
            "evidence_count": None,
            "evidence_hash": None,
            "signed_at": "2026-06-29T07:42:00+00:00",
            "observed_at": "2026-06-29T07:42:00Z",
        }
    ]


def test_golden_chain_receipt_gate_contract_binds_snapshot_and_migration() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["node_type"] == "reducer"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["projection_api"]["expose"] is True
    assert contract["projection_api"]["exposures"] == [
        {
            "topic": "onex.snapshot.projection.receipt-gate.v1",
            "table": "receipt_gate_rows",
            "schema": "public",
            "columns": [
                "id",
                "name",
                "pass",
                "detail",
                "pr_ref",
                "worker",
                "verifier",
                "evidence_count",
                "evidence_hash",
                "signed_at",
                "observed_at",
            ],
            "order_by": "observed_at DESC",
            "freshness_column": "observed_at",
            "limit": 100,
        }
    ]
    assert contract["db_io"]["db_tables"] == [
        {
            "name": "receipt_gate_rows",
            "migration": "0000_create_receipt_gate_projection_table.sql",
            "access": "write",
            "database": "omnidash_analytics",
            "role": "receipt_gate_projection",
        }
    ]
