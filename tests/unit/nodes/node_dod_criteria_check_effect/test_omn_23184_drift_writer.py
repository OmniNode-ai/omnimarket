# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A criteria-drift verdict names who wrote the difference (OMN-23184 AC1).

The lanes' contracts claim a slice of a shared ticket's criteria, so the check used to say
"AC2 added to the ticket" or "deleted from the ticket" about a ticket nobody edited. The
verdict now starts with ``writer=``: the Linear actor and time of a description edit made
after the contract's acceptance, the contract file at the PR head when the ticket history
shows no such edit, and ``unmeasured`` when the history could not be read.

The fake tracker serves a body and a recorded history; no request leaves the machine.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.model_criteria_drift import ModelTicketDescriptionEdit
from omnimarket.nodes.node_dod_criteria_check_effect.models import (
    EnumDodCriteriaVerdict,
)
from tests.unit.nodes.node_dod_criteria_check_effect.test_omn_21676_criteria_check import (
    _HEAD,
    _TICKET,
    _check,
    _contract_text,
    _Github,
    _Issue,
)
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import criteria_body
from tests.unit.nodes.node_dod_verify.test_omn_20858_criteria_drift import (
    _pinned_contract,
    _statements,
)

pytestmark = pytest.mark.unit

_ACTOR = "a-ticket-editor"
_EDIT_AT = datetime(2026, 11, 8, 20, 39, 6, tzinfo=UTC)


class _HistoryTracker:
    """A recorded Linear that serves a body and a description-edit history."""

    def __init__(
        self, body: str, history: list[ModelTicketDescriptionEdit] | BaseException
    ) -> None:
        self._issue = _Issue(_TICKET, body)
        self._history = history
        self.closed = False

    async def get_issue(self, issue_id: str) -> _Issue:
        return self._issue

    async def description_edits(
        self, issue_id: str
    ) -> list[ModelTicketDescriptionEdit]:
        if isinstance(self._history, BaseException):
            raise self._history
        return self._history

    async def close(self) -> None:
        self.closed = True


def _deleted_ac2() -> str:
    return criteria_body({"AC1": _statements()["AC1"]})


def _added_ac3() -> str:
    return criteria_body({**_statements(), "AC3": "AC3: a new thing -- falsifier: z"})


@pytest.mark.asyncio
async def test_no_description_edit_names_the_contract_as_writer_for_a_deleted_claim() -> (
    None
):
    github = _Github(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _HistoryTracker(_deleted_ac2(), []))
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    ticket = result.tickets[0]
    assert ticket.writer is not None
    assert ticket.detail.startswith(
        f"writer=contract contracts/{_TICKET}.yaml at {_HEAD}"
    )
    assert result.reason.startswith(
        f"{_TICKET}: writer=contract contracts/{_TICKET}.yaml at {_HEAD}"
    )
    assert "AC2 deleted" in result.reason


@pytest.mark.asyncio
async def test_no_description_edit_names_the_contract_as_writer_for_an_added_claim() -> (
    None
):
    github = _Github(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _HistoryTracker(_added_ac3(), []))
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    assert result.reason.startswith(f"{_TICKET}: writer=contract ")
    assert "AC3 added" in result.reason


@pytest.mark.asyncio
async def test_a_description_edit_after_acceptance_names_its_actor_and_time() -> None:
    github = _Github(contracts=_contract_text(_pinned_contract()))
    history = [ModelTicketDescriptionEdit(actor=_ACTOR, created_at=_EDIT_AT)]
    result = await _check(github, _HistoryTracker(_deleted_ac2(), history))
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    assert result.tickets[0].detail.startswith(
        f"writer=ticket-edit {_ACTOR} {_EDIT_AT.isoformat()}"
    )
    assert "writer=contract" not in result.reason


@pytest.mark.asyncio
async def test_an_unreadable_history_is_unmeasured_never_a_guess() -> None:
    github = _Github(contracts=_contract_text(_pinned_contract()))
    tracker = _HistoryTracker(_deleted_ac2(), ConnectionError("history unreachable"))
    result = await _check(github, tracker)
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    assert result.tickets[0].detail.startswith("writer=unmeasured")
    assert "writer=contract" not in result.reason
    assert "writer=ticket-edit" not in result.reason


@pytest.mark.asyncio
async def test_a_tracker_with_no_history_read_is_unmeasured() -> None:
    from tests.unit.nodes.node_dod_criteria_check_effect.test_omn_21676_criteria_check import (
        _Tracker,
    )

    github = _Github(contracts=_contract_text(_pinned_contract()))
    result = await _check(github, _Tracker(_Issue(_TICKET, _deleted_ac2())))
    assert result.tickets[0].detail.startswith("writer=unmeasured")


@pytest.mark.asyncio
async def test_a_passing_ticket_reads_no_history_and_carries_no_writer() -> None:
    github = _Github(contracts=_contract_text(_pinned_contract()))
    tracker = _HistoryTracker(criteria_body(_statements()), [])
    result = await _check(github, tracker)
    assert result.verdict is EnumDodCriteriaVerdict.PASS
    assert result.tickets[0].writer is None
    assert not result.tickets[0].detail.startswith("writer=")
