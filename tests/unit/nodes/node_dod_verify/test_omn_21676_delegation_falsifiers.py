# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21676 -- the three falsifiers of the hosted criteria delegation, run from this directory.

The ticket's acceptance criteria each name a ``-k`` selector under
``tests/unit/nodes/node_dod_verify``. The delegation itself landed with its tests under other
names (and the trusted check's tests under ``node_dod_criteria_check_effect``), so those selectors
matched nothing and a falsifier that selects nothing proves nothing. These three tests carry the
selectors and each one runs the whole path: the hosted verifier first, then the trusted check.

* AC1: a contract that records a requirements list verifies hosted with no credential and the
  verdict says the criteria check is delegated, never ``ticket_unavailable``.
* AC2: the same contract, with a ticket whose criterion was edited, still refuses
  ``CRITERIA_DRIFT`` naming the criterion at the trusted check, which reads the ticket itself and
  takes nothing from the hosted verdict.
* AC3: an unreadable ticket at the trusted check still refuses ``ticket_unavailable``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_criteria_check_effect.models import (
    EnumDodCriteriaVerdict,
    ModelDodCriteriaCheckRequest,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
)
from tests.test_dod_verify_acceptance_basis import _run
from tests.unit.nodes.node_dod_criteria_check_effect.test_omn_21676_criteria_check import (
    _HEAD,
    _KEY,
    _REPO,
    _TICKET,
    _check,
    _contract_text,
    _Github,
    _Issue,
    _Tracker,
    _tracker_for,
)
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import criteria_body
from tests.unit.nodes.node_dod_verify.test_omn_20858_criteria_drift import (
    _pinned_contract,
    _statements,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_linear_credential(monkeypatch: pytest.MonkeyPatch) -> list[str]:
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


def _hosted_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    return _run(tmp_path, monkeypatch, _pinned_contract(), no_ticket_reader=True)


def test_hosted_criteria_delegated_without_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_linear_credential: list[str]
) -> None:
    state = _hosted_verdict(tmp_path, monkeypatch)
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.unresolved_cause is None
    assert state.unresolved_cause is not EnumDodVerifyUnresolvedCause.TICKET_UNAVAILABLE
    assert "ticket_unavailable" not in (state.error_message or "")
    # The verdict names the criteria check as delegated, on the serialized form the gate reads.
    assert state.criteria_delegated is True
    assert state.model_dump(mode="json")["criteria_delegated"] is True
    # Nothing was compared, so nothing was judged, and no tracker was reached for.
    assert state.criteria_drift == ()
    assert state.criteria_revision is None
    assert no_linear_credential == []


async def test_delegated_criteria_drift_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = _hosted_verdict(tmp_path, monkeypatch)
    assert hosted.status is EnumDodVerifyStatus.VERIFIED
    assert hosted.criteria_delegated is True

    edited = {**_statements(), "AC1": "AC1: does a different thing -- falsifier: x"}
    tracker = _tracker_for(criteria_body(edited))
    result = await _check(
        _Github(contracts=_contract_text(_pinned_contract())), tracker
    )

    # The delegated hosted verdict above was VERIFIED; the trusted point refuses regardless.
    assert result.verdict is EnumDodCriteriaVerdict.DRIFT
    assert result.merge_blocked is True
    assert result.reason.startswith(f"{_TICKET}: CRITERIA_DRIFT")
    assert "AC1 edited" in result.reason
    # It compared against the live ticket it read itself, before and after, not a hosted answer.
    assert tracker.reads == 2

    # And it cannot be handed the hosted verdict as proof: the request has no field for one.
    with pytest.raises(ValidationError):
        ModelDodCriteriaCheckRequest.model_validate(
            {
                "correlation_id": uuid4(),
                "repo": _REPO,
                "pr_number": 7,
                "head_sha": _HEAD,
                "hosted_verdict": "verified",
            }
        )


@pytest.mark.parametrize(
    "answer",
    [
        KeyError("Issue not found: OMN-20999"),
        ConnectionError(f"unreachable {_KEY}"),
        _Issue("OMN-1", "- AC1: x"),
    ],
)
async def test_delegated_ticket_unavailable_still_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: _Issue | BaseException,
) -> None:
    hosted = _hosted_verdict(tmp_path, monkeypatch)
    assert hosted.criteria_delegated is True
    assert hosted.status is EnumDodVerifyStatus.VERIFIED

    result = await _check(
        _Github(contracts=_contract_text(_pinned_contract())), _Tracker(answer)
    )

    assert result.verdict is EnumDodCriteriaVerdict.UNREADABLE
    assert result.merge_blocked is True
    assert result.reason.startswith("CRITERIA_UNREADABLE")
    assert EnumDodVerifyUnresolvedCause.TICKET_UNAVAILABLE.value in result.reason
    assert _KEY not in result.model_dump_json()


async def test_delegated_ticket_unavailable_still_refuses_with_no_credential() -> None:
    """The trusted host's key store holds no Linear credential: still a refusal, never a pass."""
    result = await _check(
        _Github(contracts=_contract_text(_pinned_contract())), no_credential=True
    )
    assert result.verdict is EnumDodCriteriaVerdict.UNREADABLE
    assert result.merge_blocked is True
    assert EnumDodVerifyUnresolvedCause.TICKET_UNAVAILABLE.value in result.reason
    assert "LINEAR_API_KEY" in result.reason
    assert _KEY not in result.model_dump_json()
