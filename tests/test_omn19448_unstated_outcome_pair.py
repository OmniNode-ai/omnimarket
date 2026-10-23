# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448: a terminal row that states no outcome pair gets the one its evidence decides.

On the dev lane every accepted ``delegation_events`` row written from a
delegate-skill terminal that declared no ``operational_outcome`` and
``content_verdict`` (the claude-glm and codex harness terminals, and the
in-process evidence terminal) stored both as NULL, and so did a first-written
failure whose cause was not the reaper's. Both writers now fill the pair from
the row's own terminal evidence when neither the incoming terminal nor the
stored row states one: an evidenced success is completed/usable, the pair the
canonical producer states for a gate-passed answer, and a typed failure gets
the pair its cause maps to. A stated pair is never replaced.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
    apply_terminal_precedence,
)
from tests.test_omn19559_terminal_precedence import (
    CORRELATION_ID,
    MODEL_NAME,
    _inner_completed,
    _outer_terminal,
    _replay,
)

HARNESS_MODEL = "glm-5.3-flash"


def _harness_terminal() -> dict[str, object]:
    """The harness-delegate completed terminal: no outcome pair, one accepted rung."""
    return {
        "tenant_id": "omninode",
        "_event_type": "onex.evt.omnimarket.delegate-skill-completed.v1",
        "status": "completed",
        "correlation_id": CORRELATION_ID,
        "task_type": "document",
        "provider": "harness:claude-glm",
        "model_name": HARNESS_MODEL,
        "delegated_to": HARNESS_MODEL,
        "response": "harness answer",
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "metrics": {"input_tokens": 120, "output_tokens": 40, "latency_ms": 6961},
        "attempts": [
            {
                "tier": "harness",
                "backend_id": f"harness:claude-glm:{HARNESS_MODEL}",
                "model_id": HARNESS_MODEL,
                "quality_gate_passed": True,
                "acceptance_decision": "accept",
                "acceptance_reason": "quality_bar_met",
                "failure_class": None,
                "error_message": "",
            }
        ],
        "attempts_count": 1,
        "escalation_count": 0,
    }


class _StoredRowDb:
    def __init__(self, stored: dict[str, object] | None) -> None:
        self._stored = stored

    async def execute(self, *_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        return [] if self._stored is None else [dict(self._stored)]


def _async_preserve(
    stored: dict[str, object] | None, row: dict[str, object]
) -> dict[str, object]:
    runner = object.__new__(DelegationProjectionRunner)
    runner.__dict__["_db"] = _StoredRowDb(stored)
    runner.__dict__["_table_delegation"] = "delegation_events"
    asyncio.run(runner._preserve_existing_evidence_async(row))
    return row


def _evidenced_success_row() -> dict[str, object]:
    return {
        "correlation_id": CORRELATION_ID,
        "terminal_ok": True,
        "terminal_failure_cause": None,
        "model_name": HARNESS_MODEL,
        "quality_gate_passed": True,
        "attempt_history": [
            {
                "model_id": HARNESS_MODEL,
                "quality_gate_passed": True,
                "failure_class": None,
            }
        ],
    }


@pytest.mark.unit
def test_sync_writer_stores_completed_usable_for_a_harness_terminal() -> None:
    row = _replay(_harness_terminal())
    assert row["terminal_ok"] is True
    assert row["operational_outcome"] == "completed"
    assert row["content_verdict"] == "usable"


@pytest.mark.unit
def test_sync_writer_stores_completed_usable_for_an_unpaired_local_success() -> None:
    row = _replay(_outer_terminal())
    assert row["model_name"] == MODEL_NAME
    assert row["operational_outcome"] == "completed"
    assert row["content_verdict"] == "usable"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cause", "outcome", "verdict"),
    [
        ("timeout", "timeout", "not_applicable"),
        ("provider_error", "inference_failed", "not_applicable"),
        ("quality_gate_refused", "quality_rejected", "unusable"),
    ],
)
def test_sync_writer_first_failure_states_the_pair_its_cause_maps_to(
    cause: str, outcome: str, verdict: str
) -> None:
    row = _replay(_outer_terminal(cause))
    assert row["terminal_ok"] is False
    assert row["terminal_failure_cause"] == cause
    assert row["operational_outcome"] == outcome
    assert row["content_verdict"] == verdict


@pytest.mark.unit
def test_sync_writer_keeps_the_pair_a_canonical_terminal_stated() -> None:
    stated = {**_inner_completed(), "content_verdict": "correct"}
    row = _replay(stated, _outer_terminal())
    assert row["operational_outcome"] == "completed"
    assert row["content_verdict"] == "correct"


@pytest.mark.unit
def test_async_writer_first_write_of_an_evidenced_success_states_the_pair() -> None:
    row = _async_preserve(None, _evidenced_success_row())
    assert row["operational_outcome"] == "completed"
    assert row["content_verdict"] == "usable"


@pytest.mark.unit
def test_async_writer_first_failure_states_the_pair_its_cause_maps_to() -> None:
    failed: dict[str, object] = {
        "correlation_id": CORRELATION_ID,
        "terminal_ok": False,
        "terminal_failure_cause": "timeout",
        "model_name": "",
        "attempt_history": [],
    }
    row = _async_preserve(None, failed)
    assert row["operational_outcome"] == "timeout"
    assert row["content_verdict"] == "not_applicable"


@pytest.mark.unit
def test_async_writer_leaves_a_stored_pair_to_the_upsert() -> None:
    stored = {
        **_evidenced_success_row(),
        "operational_outcome": "completed",
        "content_verdict": "correct",
    }
    row = _async_preserve(stored, _evidenced_success_row())
    assert "operational_outcome" not in row
    assert "content_verdict" not in row


@pytest.mark.unit
@pytest.mark.parametrize(
    "row",
    [
        # A success with no model is not evidenced; nothing is stated for it.
        {**_evidenced_success_row(), "model_name": ""},
        # A success whose only rung was refused is not evidenced either.
        {
            **_evidenced_success_row(),
            "attempt_history": [
                {
                    "model_id": HARNESS_MODEL,
                    "quality_gate_passed": False,
                    "failure_class": "quality_gate_failed",
                }
            ],
        },
        # A non-terminal event states no outcome at all.
        {"correlation_id": CORRELATION_ID, "terminal_ok": None},
        # A failure that names no cause is not classified here.
        {"correlation_id": CORRELATION_ID, "terminal_ok": False},
    ],
)
def test_no_pair_is_stated_without_terminal_evidence(row: dict[str, object]) -> None:
    expected = dict(row)
    apply_terminal_precedence({}, row)
    assert row == expected


@pytest.mark.unit
def test_a_half_stated_pair_is_not_completed() -> None:
    row: dict[str, object] = {**_evidenced_success_row(), "content_verdict": "correct"}
    expected = dict(row)
    apply_terminal_precedence({}, row)
    assert row == expected
