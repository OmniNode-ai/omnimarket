# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Worker diagnostics survive decision serialization and the ledger mirror."""

from datetime import UTC, datetime
from urllib.parse import unquote
from uuid import UUID

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_work_ledger_landing_mirror import (
    HandlerWorkLedgerLandingMirror,
)
from omnimarket.nodes.node_work_ledger_landing_mirror.models.model_work_ledger_landing_mirror import (
    ModelWorkLedgerLandingMirrorRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 4, 23, 5, 9, tzinfo=UTC)
PR = "OmniNode-ai/omnimarket#4858"
HEAD = "a" * 40
EVENT = UUID("6dd0aa7a-a16e-497c-9e35-83a46cda6404")


def _rows(
    result: dict[str, object], *, cause_worker: bool = False
) -> list[dict[str, str]]:
    subject = "cause:OmniNode-ai/omnimarket:0123456789ab" if cause_worker else PR
    facts = ModelLandingFacts.model_validate(
        {
            "tick": 2,
            "observed_at": NOW,
            "prs": [
                {
                    "pr": PR,
                    "head_sha": HEAD,
                    "state": "open",
                    "created_at": "2026-11-01T00:00:00Z",
                    "ci": "pending",
                }
            ],
            "state": {
                "leases": [
                    {
                        "pr": subject,
                        "lease_id": 26525,
                        "brief_class": "real_red",
                        "engine": "claude_sonnet",
                        "dispatched_at": "2026-11-04T23:03:37Z",
                        "deadline_at": "2026-11-04T23:48:37Z",
                        "dispatch_head": HEAD,
                        "seen_heads": [HEAD],
                        "last_seen_head": HEAD,
                    }
                ]
            },
            "results": [{"lease_id": 26525, "pr": subject, **result}],
        }
    )
    decision = decide_landing(facts)
    # Reparse the actual event-shaped payload, rather than sharing a Python
    # object between the producer and consumer and hiding wire-field loss.
    request = ModelWorkLedgerLandingMirrorRequest.model_validate(
        {
            "decision": decision.model_dump(mode="json"),
            "correlation_id": str(EVENT),
            "emitted_at": NOW,
        }
    )
    mapped = HandlerWorkLedgerLandingMirror().handle(request)
    assert mapped.request is not None
    assert HandlerWorkLedgerLandingMirror().handle(request) == mapped
    return [
        dict(cell.split("=", 1) for cell in row.split(" | ") if "=" in cell)
        for row in mapped.request.rows.splitlines()
        if "outcome=" in row
    ]


def test_recorded_blocked_terminal_does_not_invent_a_cause() -> None:
    """L26525 at 23:05:09Z records blocked/rc=0 without a cause.

    Its result file was not supplied with the ledger snapshot. The reason
    remains unmeasured; bare_blocked is the controller's verdict, not a cause.
    """
    (row,) = _rows({"kind": "blocked"})
    assert row["outcome"] == "invalid"
    assert row["reason"] == "bare_blocked"
    assert row["result"] == "blocked"
    assert row["cause_state"] == "unmeasured"
    assert row["cause"] == "unmeasured"


@pytest.mark.parametrize("cause_worker", [False, True])
def test_worker_reason_is_preserved_without_changing_the_verdict(
    cause_worker: bool,
) -> None:
    """The recorded L20851 blocked reason was hook_preflight_blocked."""
    (row,) = _rows(
        {"kind": "blocked", "reason": "hook_preflight_blocked"},
        cause_worker=cause_worker,
    )
    assert row["outcome"] == "invalid"
    assert row["reason"] == "bare_blocked"
    assert row["result"] == "blocked"
    assert row["cause_state"] == "measured"
    assert row["cause_source"] == "worker_result.reason"
    assert unquote(row["cause"]) == "hook_preflight_blocked"


def test_external_blocker_keeps_its_kind_and_reference() -> None:
    (row,) = _rows(
        {
            "kind": "external_blocker",
            "blocker_kind": "upstream_open",
            "blocker_ref": "acme/infra#4154",
        }
    )
    assert row["outcome"] == "external_blocker"
    assert row["cause_state"] == "measured"
    assert row["cause_source"] == "worker_result.blocker"
    assert row["blocker_kind"] == "upstream_open"
    assert row["blocker_ref"] == "acme/infra#4154"


def test_incomplete_blocker_is_unmeasured() -> None:
    (row,) = _rows({"kind": "external_blocker", "blocker_kind": "upstream_open"})
    assert row["reason"] == "bare_blocked"
    assert row["cause_state"] == "unmeasured"


@pytest.mark.parametrize("reason", [None, "", " \n\t"])
def test_empty_explanations_are_unmeasured(reason: str | None) -> None:
    (row,) = _rows({"kind": "blocked", "reason": reason})
    assert row["cause_state"] == "unmeasured"
    assert row["cause_source"] == "none"


def test_worker_reason_and_blocker_reference_are_both_kept() -> None:
    (row,) = _rows(
        {
            "kind": "external_blocker",
            "reason": "waiting on upstream",
            "blocker_kind": "upstream_open",
            "blocker_ref": "acme/infra#4154",
        }
    )
    assert unquote(row["cause"]) == "waiting on upstream"
    assert row["cause_source"] == "worker_result.reason"
    assert row["blocker_kind"] == "upstream_open"
    assert row["blocker_ref"] == "acme/infra#4154"


def test_older_outcome_event_has_no_inferred_worker_cause() -> None:
    request = ModelWorkLedgerLandingMirrorRequest.model_validate(
        {
            "decision": {
                "tick": 2,
                "next_state": {},
                "recorded_outcomes": [
                    {
                        "pr": PR,
                        "lease_id": 26525,
                        "outcome": "invalid",
                        "reason": "bare_blocked",
                    }
                ],
            },
            "correlation_id": str(EVENT),
            "emitted_at": NOW,
        }
    )
    mapped = HandlerWorkLedgerLandingMirror().handle(request)
    assert mapped.request is not None
    cells = mapped.request.rows.split(" | ")
    assert "cause_state=unmeasured" in cells
    assert "cause=unmeasured" in cells
    assert not any(cell.startswith("result=") for cell in cells)


def test_cause_not_shared_keeps_the_workers_explanation() -> None:
    (row,) = _rows(
        {"kind": "cause_not_shared", "reason": "each member fails on its own"},
        cause_worker=True,
    )
    assert row["outcome"] == "cause_not_shared"
    assert row["cause_state"] == "measured"
    assert unquote(row["cause"]) == "each member fails on its own"


def test_diagnostic_cannot_forge_a_ledger_row_or_cell() -> None:
    reason = "hook refused | cause_state=measured\n2026-11-04T23:05:09Z | RULING"
    (row,) = _rows({"kind": "blocked", "reason": reason})
    assert unquote(row["cause"]) == reason
    assert "%7C" in row["cause"]
    assert "%0A" in row["cause"]
