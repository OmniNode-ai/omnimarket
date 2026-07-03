# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_projection_receipt_gate reducer and contract."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_receipt_gate.reducers.reducer_receipt_gate import (
    reduce_receipt_gate,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = Path("src/omnimarket/nodes/node_projection_receipt_gate/contract.yaml")


# ── Contract structure ───────────────────────────────────────────────────────


def test_contract_is_reducer_node() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["node_type"] == "reducer"


def test_contract_owns_receipt_gate_snapshot_topic() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    topics = {snap["topic"] for snap in contract["projection_api"]["snapshots"]}
    assert topics == {"onex.snapshot.projection.receipt-gate.v1"}
    assert set(contract["externally_consumed_topics"]) == topics


def test_contract_subscribes_to_verification_and_evidence_topics() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    subscribe = set(contract["event_bus"]["subscribe_topics"])
    assert "onex.evt.omnimarket.verification-receipt-completed.v1" in subscribe
    assert "onex.evt.omnimarket.evidence-validated.v1" in subscribe


# ── Reducer — verification-receipt events ────────────────────────────────────


def test_reducer_handles_verification_receipt_with_checks() -> None:
    rows = reduce_receipt_gate(
        (),
        {
            "task_id": "OMN-12345",
            "pr_number": 99,
            "repo": "OmniNode-ai/omnimarket",
            "verifier": "node_verification_receipt_generator",
            "overall_pass": True,
            "verified_at": "2026-06-28T10:00:00Z",
            "checks": [
                {
                    "dimension": "ci_checks",
                    "passed": True,
                    "summary": "All CI checks passed",
                },
                {"dimension": "pytest", "passed": True, "summary": "108 tests passed"},
            ],
        },
    )

    assert len(rows) == 2
    assert rows[0].name == "ci_checks"
    assert rows[0].pass_ is True
    assert rows[0].detail == "All CI checks passed"
    assert rows[0].pr_ref == "OMN-12345 / #99"
    assert rows[0].verifier == "node_verification_receipt_generator"
    assert rows[1].name == "pytest"
    assert rows[1].pass_ is True


def test_reducer_handles_verification_receipt_no_checks() -> None:
    """When checks list is absent, a single summary row is emitted."""
    rows = reduce_receipt_gate(
        (),
        {
            "task_id": "OMN-11111",
            "overall_pass": False,
            "verified_at": "2026-06-28T11:00:00Z",
        },
    )

    assert len(rows) == 1
    assert rows[0].name == "overall"
    assert rows[0].pass_ is False
    assert rows[0].pr_ref == "OMN-11111"


def test_reducer_handles_verification_receipt_pr_number_only() -> None:
    rows = reduce_receipt_gate(
        (),
        {
            "pr_number": 42,
            "overall_pass": True,
            "claim": "all tests pass",
            "verified_at": "2026-06-28T12:00:00Z",
        },
    )

    assert len(rows) == 1
    assert rows[0].pr_ref == "#42"
    assert rows[0].detail == "all tests pass"


# ── Reducer — evidence-validated events ──────────────────────────────────────


def test_reducer_handles_evidence_validated_pass() -> None:
    rows = reduce_receipt_gate(
        (),
        {
            "_event_type": "onex.evt.omnimarket.evidence-validated.v1",
            "ticket_id": "OMN-99999",
            "pr_number": 500,
            "validation_state": "PASSED",
            "draft_hash": "sha256:" + "a" * 64,
            "validated_at": "2026-06-28T09:00:00Z",
        },
    )

    assert len(rows) == 1
    assert rows[0].name == "occ-evidence"
    assert rows[0].pass_ is True
    assert rows[0].pr_ref == "OMN-99999 / #500"
    assert rows[0].evidence_hash == "sha256:" + "a" * 64


def test_reducer_handles_evidence_validated_fail() -> None:
    rows = reduce_receipt_gate(
        (),
        {
            "_event_type": "onex.evt.omnimarket.evidence-validated.v1",
            "ticket_id": "OMN-88888",
            "validation_state": "FAILED",
            "validated_at": "2026-06-28T08:00:00Z",
        },
    )

    assert len(rows) == 1
    assert rows[0].pass_ is False
    assert rows[0].pr_ref == "OMN-88888"


# ── Reducer — state accumulation ─────────────────────────────────────────────


def test_reducer_prepends_new_rows() -> None:
    """Each call prepends the new rows; ordering is newest-first."""
    first_event = {
        "task_id": "OMN-1",
        "overall_pass": True,
        "verified_at": "2026-06-28T10:00:00Z",
    }
    second_event = {
        "task_id": "OMN-2",
        "overall_pass": False,
        "verified_at": "2026-06-28T11:00:00Z",
    }

    after_first = reduce_receipt_gate((), first_event)
    after_second = reduce_receipt_gate(after_first, second_event)

    assert len(after_second) == 2
    # Second (newer) event should be first in the tuple.
    assert after_second[0].pr_ref == "OMN-2"
    assert after_second[1].pr_ref == "OMN-1"


def test_reducer_caps_at_100_rows() -> None:
    """The snapshot is capped at 100 rows to avoid unbounded growth."""
    # Seed with 99 rows.
    state: tuple = ()
    for i in range(99):
        state = reduce_receipt_gate(
            state,
            {
                "task_id": f"OMN-{i}",
                "overall_pass": True,
                "verified_at": "2026-06-28T10:00:00Z",
            },
        )

    assert len(state) == 99

    # A receipt that emits 2 checks should push total past 100.
    state = reduce_receipt_gate(
        state,
        {
            "task_id": "OMN-999",
            "verified_at": "2026-06-28T12:00:00Z",
            "overall_pass": True,
            "checks": [
                {"dimension": "ci_checks", "passed": True, "summary": "ok"},
                {"dimension": "pytest", "passed": True, "summary": "ok"},
            ],
        },
    )

    assert len(state) == 100


# ── Handler round-trip ───────────────────────────────────────────────────────


def test_handler_round_trip() -> None:
    from omnimarket.nodes.node_projection_receipt_gate.handlers.handler_projection_receipt_gate import (
        HandlerProjectionReceiptGate,
    )

    handler = HandlerProjectionReceiptGate()
    result = handler.handle_dict(
        {
            "rows": [],
            "event": {
                "task_id": "OMN-13081",
                "overall_pass": True,
                "verified_at": "2026-06-28T14:00:00Z",
                "checks": [
                    {"dimension": "ci_checks", "passed": True, "summary": "green"},
                ],
            },
        }
    )

    assert "rows" in result
    rows = result["rows"]
    assert len(rows) == 1
    assert rows[0]["name"] == "ci_checks"
    assert rows[0]["pass"] is True
