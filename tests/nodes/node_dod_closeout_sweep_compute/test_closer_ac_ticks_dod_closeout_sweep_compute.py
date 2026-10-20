# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the closer ticks an acceptance box only on a verified bound check.

The done-flip guard refuses Done while an acceptance box is unticked, and lanes
may not edit descriptions. decide_ticket, given the ticket's description, names
the description with exactly the boxes whose criterion dod_verify verified
ticked, and the evidence comment that goes with it. An untested criterion never
ticks: a box with no verified check, or with no criterion label at all, holds
the ticket open and nothing is ticked.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumTicketDecision,
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
)

pytestmark = pytest.mark.unit

STATE_MODEL = (
    "omnimarket.nodes.node_dod_verify.models.model_dod_verify_state.ModelDodVerifyState"
)
HEAD = "9978f18ad53d32e07d122e32d246b6e8c001d1be"
DESCRIPTION = (
    "Intro text.\n\n"
    "## Acceptance\n\n"
    "- [ ] AC1: the read returns under a running loop.\n"
    "- [ ] **AC2** the closer ticks verified boxes.\n"
    "- [x] AC3: already met.\n\n"
    "Trailing prose with a [ ] token that is not a box.\n"
)


def _ac(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "proposed": True,
        "accepted": True,
        "proposed_by": "lane-a",
        "accepted_by": "lane-b",
    }


def _receipt(statuses: dict[str, str]) -> str:
    checks = [
        {"evidence_id": f"dod-{label.lower()}", "status": status, "binds_ac": [label]}
        for label, status in statuses.items()
    ]
    all_verified = all(s == "verified" for s in statuses.values())
    return json.dumps(
        {
            "result_model": STATE_MODEL,
            "result": {
                "status": "verified" if all_verified else "failed",
                "total_checks": len(checks),
                "checks": checks,
            },
        }
    )


def decide(
    description: str, statuses: dict[str, str]
) -> ModelDodCloseoutDecisionResult:
    return HandlerDodCloseoutSweep().handle(
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "decide_ticket",
                "date": "2026-10-19",
                "ticket_id": "OMN-17427",
                "run_key": "run-1",
                "acs": [_ac(label) for label in statuses],
                "dod_verify_receipt": _receipt(statuses),
                "description": description,
                "verified_head": HEAD,
            }
        )
    )


def test_every_verified_criterion_ticks_and_only_its_box_changes() -> None:
    result = decide(
        DESCRIPTION, {"AC1": "verified", "AC2": "verified", "AC3": "verified"}
    )

    assert result.decision is EnumTicketDecision.DONE
    assert result.ticked_description == DESCRIPTION.replace(
        "- [ ] AC1:", "- [x] AC1:"
    ).replace("- [ ] **AC2**", "- [x] **AC2**")
    assert result.box_ticks is not None
    assert [(t.label, t.evidence_ids) for t in result.box_ticks] == [
        ("AC1", ["dod-ac1"]),
        ("AC2", ["dod-ac2"]),
    ]
    assert result.tick_comment is not None
    lines = result.tick_comment.splitlines()
    assert lines[0].startswith("actor: ")
    assert "- AC1: dod-ac1 verified" in lines
    assert "- AC2: dod-ac2 verified" in lines
    assert any(HEAD in line for line in lines)


def test_an_unverified_criterion_ticks_nothing_and_holds_the_ticket() -> None:
    result = decide(
        DESCRIPTION, {"AC1": "verified", "AC2": "failed", "AC3": "verified"}
    )

    assert result.decision is EnumTicketDecision.OPEN
    assert result.ticked_description is None
    assert result.box_ticks is None
    assert result.tick_comment is None


def test_a_box_naming_no_criterion_label_holds_the_ticket_and_ticks_nothing() -> None:
    description = DESCRIPTION + "- [ ] Plan committed at the path above.\n"

    result = decide(
        description, {"AC1": "verified", "AC2": "verified", "AC3": "verified"}
    )

    assert result.decision is EnumTicketDecision.OPEN
    assert result.ticked_description is None
    assert result.ticket_unmet is not None
    box = [u for u in result.ticket_unmet if u.label == "box"]
    assert len(box) == 1
    assert "Plan committed at the path above." in box[0].reason


def test_a_box_whose_criterion_has_no_verified_check_holds_the_ticket() -> None:
    description = DESCRIPTION + "- [ ] AC4: never bound.\n"

    result = decide(
        description, {"AC1": "verified", "AC2": "verified", "AC3": "verified"}
    )

    assert result.decision is EnumTicketDecision.OPEN
    assert result.ticked_description is None
    assert result.ticket_unmet is not None
    assert any(u.label == "AC4" for u in result.ticket_unmet)


def test_without_a_description_the_decision_is_unchanged_and_nothing_ticks() -> None:
    result = HandlerDodCloseoutSweep().handle(
        ModelDodCloseoutDecisionRequest.model_validate(
            {
                "kind": "decide_ticket",
                "date": "2026-10-19",
                "ticket_id": "OMN-17427",
                "acs": [_ac("AC1")],
                "dod_verify_receipt": _receipt({"AC1": "verified"}),
            }
        )
    )

    assert result.decision is EnumTicketDecision.DONE
    assert result.ticked_description is None
    assert result.box_ticks is None
