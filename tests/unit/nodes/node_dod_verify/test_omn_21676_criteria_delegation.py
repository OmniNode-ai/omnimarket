# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21676 -- the hosted verifier holds no Linear credential and delegates the criteria comparison.

The hosted ``repo-evidence / dod-verify`` job runs ``node_dod_verify`` with the hosted
audience, no secret and no ticket reader. Before this change the criteria-drift check read
live Linear anyway, found no credential and ended every contract that records criteria in
``VERIFICATION_UNRESOLVED ticket_unavailable`` (omnibase_infra, from 09:01Z on 2026-10-31).

* A contract with a ``requirements`` acceptance list verifies in the hosted run with no
  credential, builds no tracker, and says on its verdict that the comparison is delegated.
* A contract that records no criteria is not marked delegated: there was nothing to delegate.
* The local Done gate is not hosted: with no reader it reads through the project tracker, and
  an unbuildable tracker (no credential in the key store) is still ``ticket_unavailable``.
* An injected reader is always used, hosted or not, so a trusted caller keeps the drift check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
)
from tests.test_dod_verify_acceptance_basis import _contract, _run
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import (
    RecordedTicketReader,
    criteria_body,
)
from tests.unit.nodes.node_dod_verify.test_omn_20858_criteria_drift import (
    _pinned_contract,
    _statements,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def tracker_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No credential in the environment, and a tracker that records being built."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("LINEAR_TOKEN", raising=False)
    built: list[str] = []

    async def _tracker(contract_path: Path, **_kwargs: Any) -> Any:
        built.append(contract_path.parent.name)
        raise RuntimeError("api_key_ref 'LINEAR_API_KEY' resolved to None")

    monkeypatch.setattr(
        "omnimarket.nodes.node_dod_verify.services.linear_criteria_tracker.linear_project_tracker",
        _tracker,
    )
    return built


def test_a_requirements_contract_verifies_hosted_with_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracker_builds: list[str]
) -> None:
    state = _run(tmp_path, monkeypatch, _pinned_contract(), no_ticket_reader=True)
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.error_message is None
    assert state.unresolved_cause is None
    assert state.criteria_delegated is True
    # Nothing was compared, so nothing drifted and no revision was judged.
    assert state.criteria_drift == ()
    assert state.criteria_revision is None
    # The hosted run never reached for a tracker, with a credential or without.
    assert tracker_builds == []


def test_the_delegation_is_on_the_serialized_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(tmp_path, monkeypatch, _pinned_contract(), no_ticket_reader=True)
    assert state.model_dump(mode="json")["criteria_delegated"] is True


def test_a_contract_recording_no_criteria_is_not_marked_delegated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(tmp_path, monkeypatch, _contract(falsifiers={}), no_ticket_reader=True)
    assert state.criteria_delegated is False
    assert "criteria_delegated" not in state.model_dump(mode="json", exclude_none=True)


def test_the_local_done_gate_still_refuses_an_unreadable_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracker_builds: list[str]
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        _pinned_contract(),
        no_ticket_reader=True,
        execution_audience="local_done_gate",
    )
    assert state.status is EnumDodVerifyStatus.UNRESOLVED
    assert state.unresolved_cause is EnumDodVerifyUnresolvedCause.TICKET_UNAVAILABLE
    assert state.criteria_delegated is False
    # It asked the project tracker, through the verifier's own contract, and no credential
    # reached the message.
    assert set(tracker_builds) == {"node_dod_verify"}
    assert "resolved to None" not in (state.error_message or "")


def test_the_local_done_gate_still_refuses_real_drift_through_a_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited = {**_statements(), "AC1": "AC1: does a different thing -- falsifier: x"}
    state = _run(
        tmp_path,
        monkeypatch,
        _pinned_contract(),
        ticket_reader=RecordedTicketReader([criteria_body(edited)]),
        execution_audience="local_done_gate",
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
    assert state.criteria_delegated is False


def test_an_injected_reader_is_used_even_hosted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited = {**_statements(), "AC1": "AC1: does a different thing -- falsifier: x"}
    reader = RecordedTicketReader([criteria_body(edited)])
    state = _run(tmp_path, monkeypatch, _pinned_contract(), ticket_reader=reader)
    assert reader.reads == 2
    assert state.criteria_delegated is False
    assert state.error_message is not None
    assert state.error_message.startswith("CRITERIA_DRIFT")
