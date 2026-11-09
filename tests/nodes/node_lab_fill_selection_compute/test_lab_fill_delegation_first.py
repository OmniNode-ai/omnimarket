# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation tickets of the deployment's owners rank first; others' are never selected (OMN-23312)."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_selection_compute.handlers import (
    HandlerLabFillSelection,
    handler_lab_fill_selection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
    ModelLabFillSelectionRequest,
    ModelLabFillSelectionResult,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-09T11:30:00Z"
OVERLAY: dict[str, Any] = {
    "companion_repositories": [],
    "not_work_repositories": [],
    "document_repositories": [],
    "delegation_epics": ["OMN-epic-a", "OMN-epic-b"],
    "delegation_owners": ["owner-1"],
}
DEPLOYMENT = ModelLabFillDeployment.from_overlay(OVERLAY)


def _candidate(ticket: str, **over: Any) -> ModelLabFillCandidateInput:
    fields: dict[str, Any] = {
        "key": ticket,
        "kind": "ticket",
        "ticket": ticket,
        "ticket_updated_at": "2026-11-09T11:00:00Z",
        "watcher_read": True,
        "facts_read_at": NOW,
    }
    fields.update(over)
    return ModelLabFillCandidateInput(**fields)


def _select(
    *candidates: ModelLabFillCandidateInput,
    deployment: ModelLabFillDeployment = DEPLOYMENT,
) -> ModelLabFillSelectionResult:
    return HandlerLabFillSelection().handle(
        ModelLabFillSelectionRequest(
            now=NOW,
            candidates=candidates,
            ledger_lines=(),
            deployment=deployment,
        )
    )


def _eligible(result: ModelLabFillSelectionResult) -> list[str]:
    return [d.ticket for d in result.decisions if d.reason is None]


def _delegation(ticket: str, **over: Any) -> ModelLabFillCandidateInput:
    fields: dict[str, Any] = {
        "assignee": "owner-1",
        "parents": ("OMN-epic-a",),
        "state_type": "started",
        "criterion": "true",
    }
    fields.update(over)
    return _candidate(ticket, **fields)


def test_delegation_first_ranks_owned_startable_ahead_of_every_other_source() -> None:
    result = _select(
        _candidate("OMN-1", kind="pr-red", pr="repo_a#1"),
        _candidate("OMN-2"),
        _delegation("OMN-30"),
        _candidate("OMN-3"),
        _delegation("OMN-31", parents=("OMN-epic-b",)),
    )
    assert _eligible(result) == ["OMN-30", "OMN-31", "OMN-1", "OMN-2", "OMN-3"]
    by_ticket = {d.ticket: d for d in result.decisions}
    assert by_ticket["OMN-30"].caller_reason == "delegation-first"
    assert by_ticket["OMN-2"].caller_reason == ""


def test_delegation_first_epic_itself_counts_as_delegation() -> None:
    result = _select(
        _candidate("OMN-2"),
        _delegation("OMN-epic-a", parents=()),
    )
    assert _eligible(result) == ["OMN-epic-a", "OMN-2"]


def test_delegation_first_never_selects_another_owners_ticket() -> None:
    result = _select(
        _delegation("OMN-30", assignee="someone-else"),
        _delegation("OMN-31", assignee=""),
        _delegation("OMN-32"),
    )
    by_ticket = {d.ticket: d for d in result.decisions}
    assert _eligible(result) == ["OMN-32"]
    assert by_ticket["OMN-30"].reason is EnumLabFillSkipReason.OTHER_OWNER
    assert by_ticket["OMN-30"].detail == "someone-else"
    assert by_ticket["OMN-31"].reason is EnumLabFillSkipReason.OTHER_OWNER
    assert by_ticket["OMN-31"].detail == "unassigned"


def test_delegation_first_other_owner_beats_every_other_gate() -> None:
    # A red PR of someone else's delegation ticket is refused too, not only plain tickets.
    result = _select(
        _delegation("OMN-30", assignee="someone-else", kind="pr-red", pr="repo_a#5")
    )
    assert result.decisions[0].reason is EnumLabFillSkipReason.OTHER_OWNER


@pytest.mark.parametrize("criterion", ["false", "unread", ""])
def test_delegation_first_unread_or_false_criterion_does_not_rank(
    criterion: str,
) -> None:
    result = _select(_candidate("OMN-2"), _delegation("OMN-30", criterion=criterion))
    assert _eligible(result) == ["OMN-2", "OMN-30"]


@pytest.mark.parametrize("state_type", ["completed", "canceled", "cancelled"])
def test_delegation_first_closed_ticket_does_not_rank(state_type: str) -> None:
    result = _select(_candidate("OMN-2"), _delegation("OMN-30", state_type=state_type))
    assert _eligible(result) == ["OMN-2", "OMN-30"]


def test_delegation_first_owner_match_ignores_case_and_space() -> None:
    result = _select(_candidate("OMN-2"), _delegation("OMN-30", assignee=" Owner-1 "))
    assert _eligible(result) == ["OMN-30", "OMN-2"]


def test_delegation_first_without_overlay_epics_changes_nothing() -> None:
    bare = ModelLabFillDeployment.from_overlay({key: [] for key in list(OVERLAY)[:3]})
    result = _select(
        _candidate("OMN-2"),
        _delegation("OMN-30", assignee="someone-else"),
        deployment=bare,
    )
    assert _eligible(result) == ["OMN-2", "OMN-30"]
    assert all(d.caller_reason == "" for d in result.decisions)


def test_delegation_first_owned_ticket_with_live_lane_stays_owned() -> None:
    result = _select(_candidate("OMN-2"), _delegation("OMN-30", claim_holder="lane-x"))
    by_ticket = {d.ticket: d for d in result.decisions}
    assert by_ticket["OMN-30"].reason is EnumLabFillSkipReason.OWNED
    assert _eligible(result) == ["OMN-2"]


def test_delegation_first_control_without_the_rank_the_order_is_candidate_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected break: with the rank removed the delegation ticket falls behind, so the ordering
    assertions above are not vacuous."""
    monkeypatch.setattr(
        handler_lab_fill_selection, "delegation_first", lambda *_: False
    )
    result = _select(_candidate("OMN-2"), _delegation("OMN-30"))
    assert _eligible(result) == ["OMN-2", "OMN-30"]


@pytest.mark.parametrize("key", ["delegation_epics", "delegation_owners"])
@pytest.mark.parametrize("value", ["OMN-1", None, ("OMN-1",), {}])
def test_delegation_first_overlay_rejects_non_list(key: str, value: object) -> None:
    with pytest.raises(ValueError, match=f"{key} must be a list of strings"):
        ModelLabFillDeployment.from_overlay({**OVERLAY, key: value})


def test_delegation_first_overlay_rejects_non_string_item() -> None:
    with pytest.raises(ValueError, match=r"delegation_owners\[1\] must be a string"):
        ModelLabFillDeployment.from_overlay(
            {**OVERLAY, "delegation_owners": ["owner-1", 7]}
        )


def test_delegation_first_overlay_keeps_three_role_keys_required() -> None:
    with pytest.raises(ValueError, match="missing keys: document_repositories"):
        ModelLabFillDeployment.from_overlay(
            {k: v for k, v in OVERLAY.items() if k != "document_repositories"}
        )


def test_delegation_first_no_owners_named_owns_every_delegation_ticket() -> None:
    """RULING 2026-11-08T13:40:42Z: epics without owners means no owner filter excludes a ticket."""
    everyone = ModelLabFillDeployment.from_overlay(
        {k: v for k, v in OVERLAY.items() if k != "delegation_owners"}
    )
    result = _select(
        _candidate("OMN-2"),
        _delegation("OMN-30", assignee="someone-else"),
        _delegation("OMN-31", assignee=""),
        deployment=everyone,
    )
    assert _eligible(result) == ["OMN-30", "OMN-31", "OMN-2"]
    by_ticket = {d.ticket: d for d in result.decisions}
    assert by_ticket["OMN-30"].caller_reason == "delegation-first"
    assert by_ticket["OMN-31"].caller_reason == "delegation-first"


def test_delegation_first_no_owners_named_still_needs_epic_and_open_state() -> None:
    everyone = ModelLabFillDeployment.from_overlay(
        {k: v for k, v in OVERLAY.items() if k != "delegation_owners"}
    )
    result = _select(
        _candidate("OMN-2"),
        _delegation("OMN-30", parents=("OMN-elsewhere",)),
        _delegation("OMN-31", state_type="completed"),
        deployment=everyone,
    )
    assert _eligible(result) == ["OMN-2", "OMN-30", "OMN-31"]
    assert all(d.caller_reason == "" for d in result.decisions)
